// composer.js - Prompt box, Ctrl+V image paste, attachment chips, and Paste-into-Prompt

import { api } from "./api.js";
import { isBridgeDirectivesChecked, isDetectedPromptAiPending } from "./bridge.js";
import { emit, estimateTokensLocal, on, state } from "./state.js";

const promptInput = document.getElementById("prompt-input");
const attachmentStrip = document.getElementById("attachment-strip");
const tokenEstEl = document.getElementById("composer-token-est");
const sendBtn = document.getElementById("btn-send");
const retryBtn = document.getElementById("btn-retry-turn");
const cancelBtn = document.getElementById("btn-cancel-req");

export function initComposer() {
  promptInput.addEventListener("input", () => {
    updateTokenEstimate();
    emit("composer:updated");
  });
  promptInput.addEventListener("paste", handleClipboardPaste);
  promptInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      if (state.apiEnabled) {
        sendCurrentPrompt();
      } else if (isDetectedPromptAiPending()) {
        emit("bridge:run-detected-prompt");
      } else {
        emit("bridge:copy-bundle");
      }
    }
  });

  sendBtn.addEventListener("click", sendCurrentPrompt);
  retryBtn.addEventListener("click", retryLastTurn);
  cancelBtn.addEventListener("click", cancelActiveRequest);
  on("composer:attach", (att) => addAttachment(att));
  on("composer:paste-text", (text) => pasteTextIntoPrompt(text));
  on("composer:attachments-changed", () => {
    renderAttachments();
    updateTokenEstimate();
  });
  on("composer:updated", updateTokenEstimate);
  on("app:mode-changed", () => {
    updateTokenEstimate();
    updateSendButtonState();
  });
  on("gates:changed", updateSendButtonState);

  updateTokenEstimate();
  updateSendButtonState();
}

export function addAttachment(attachment) {
  state.attachments.push(attachment);
  renderAttachments();
  updateTokenEstimate();
  emit("composer:attachments-changed");
}

export function pasteTextIntoPrompt(text) {
  const existing = promptInput.value;
  const prefix = existing && !existing.endsWith("\n") ? "\n\n" : "";
  promptInput.value = `${existing}${prefix}${text}`;
  promptInput.focus();
  updateTokenEstimate();
}

function handleClipboardPaste(ev) {
  const items = ev.clipboardData && ev.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type && item.type.startsWith("image/")) {
      ev.preventDefault();
      const file = item.getAsFile();
      if (!file) continue;
      const reader = new FileReader();
      reader.onload = () => {
        addAttachment({
          kind: "image",
          label: `pasted-${new Date().toLocaleTimeString()}.png`,
          content: String(reader.result),
          mime_type: item.type,
          estimated_tokens: 258,
        });
      };
      reader.readAsDataURL(file);
    }
  }
}

function renderAttachments() {
  if (state.attachments.length === 0) {
    attachmentStrip.classList.add("hidden");
    attachmentStrip.innerHTML = "";
    return;
  }
  attachmentStrip.classList.remove("hidden");
  attachmentStrip.innerHTML = "";
  state.attachments.forEach((att, idx) => {
    const chip = document.createElement("div");
    chip.className = "att-item";
    chip.innerHTML = `
      <span>${escapeHtml(att.label)}</span>
      <span class="meta-line mono tabular">~${att.estimated_tokens} tok</span>
      <button class="att-remove" title="Remove attachment">&times;</button>
    `;
    chip.querySelector(".att-remove").addEventListener("click", () => {
      state.attachments.splice(idx, 1);
      renderAttachments();
      updateTokenEstimate();
      emit("composer:attachments-changed");
    });
    attachmentStrip.appendChild(chip);
  });
}

function updateTokenEstimate() {
  const promptTokens = estimateTokensLocal(promptInput.value);
  const attTokens = state.attachments.reduce((acc, a) => acc + (a.estimated_tokens || 0), 0);
  const bridgeDirTokens =
    !state.apiEnabled && isBridgeDirectivesChecked()
      ? Number(state.directivesTokensEst || 0) + 450
      : 0;
  const total = promptTokens + attTokens + bridgeDirTokens;
  tokenEstEl.textContent = state.apiEnabled
    ? `Prompt ~${total.toLocaleString()} tok`
    : total > 0
      ? `~${total.toLocaleString()} tok`
      : "";
}

function updateSendButtonState() {
  const hasGates = state.pendingGates && state.pendingGates.length > 0;
  sendBtn.disabled = !state.apiEnabled || state.busy || hasGates || !state.activeSessionId;
  sendBtn.title = hasGates ? "Resolve pending tool approval gates first" : "Send prompt (Ctrl+Enter)";

  if (cancelBtn) {
    cancelBtn.classList.toggle("hidden", !state.apiEnabled || !state.busy);
  }
  if (retryBtn) {
    const msgs = state.messages || [];
    const lastMsg = msgs.length > 0 ? msgs[msgs.length - 1] : null;
    const canRetry = Boolean(
      state.apiEnabled && !state.busy && !hasGates && state.activeSessionId && lastMsg && lastMsg.role !== "model"
    );
    retryBtn.classList.toggle("hidden", !canRetry);
  }
}

function cancelActiveRequest() {
  if (state.activeAbortController) {
    state.activeAbortController.abort();
    state.activeAbortController = null;
  }
  state.busy = false;
  state.pendingStatusText = null;
  state.resolvingGateId = null;
  sendBtn.textContent = "Send";
  updateSendButtonState();
  emit("chat:loaded");
}

async function retryLastTurn() {
  if (!state.apiEnabled || !state.activeSessionId || state.busy) return;
  if (state.pendingGates.length > 0) return;

  const controller = new AbortController();
  state.activeAbortController = controller;
  state.busy = true;
  state.pendingStatusText = "Retrying Gemini turn...";
  updateSendButtonState();
  sendBtn.textContent = "Thinking...";
  emit("chat:loaded");

  try {
    const res = await api.retryChat(state.activeSessionId, { signal: controller.signal });
    state.busy = false;
    state.pendingStatusText = null;
    state.activeSession = res.session;
    state.messages = res.messages;
    state.pendingGates = res.pending_gates;
    if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
    if (res.telemetry) emit("telemetry:updated", res.telemetry);
    emit("session:updated");
    emit("chat:loaded");
  } catch (err) {
    state.busy = false;
    state.pendingStatusText = null;
    try {
      const detail = await api.getSessionDetail(state.activeSessionId);
      state.activeSession = detail.session;
      state.messages = detail.messages || [];
      state.pendingGates = detail.pending_gates || [];
      if (Array.isArray(detail.checkpoints)) state.checkpoints = detail.checkpoints;
    } catch {
      // keep existing view if refresh fails
    }
    emit("chat:loaded");
    emit("chat:error", err.message);
  } finally {
    if (state.activeAbortController === controller) {
      state.activeAbortController = null;
    }
    state.busy = false;
    state.pendingStatusText = null;
    sendBtn.textContent = "Send";
    updateTokenEstimate();
    updateSendButtonState();
    emit("gates:changed");
  }
}

async function sendCurrentPrompt() {
  if (!state.apiEnabled || !state.activeSessionId || state.busy) return;
  if (state.pendingGates.length > 0) return;

  const prompt = promptInput.value.trim();
  if (!prompt && state.attachments.length === 0) return;

  const sentAttachments = [...state.attachments];
  promptInput.value = "";
  state.attachments = [];
  renderAttachments();
  updateTokenEstimate();

  const controller = new AbortController();
  state.activeAbortController = controller;
  state.busy = true;
  state.pendingStatusText = "Gemini is thinking...";
  state.messages = [
    ...state.messages,
    {
      role: "user",
      text: prompt,
      attachments: sentAttachments,
      created_at: new Date().toISOString(),
    },
  ];
  updateSendButtonState();
  sendBtn.textContent = "Thinking...";
  emit("chat:loaded");

  try {
    const res = await api.sendChat(
      {
        session_id: state.activeSessionId,
        prompt,
        attachments: sentAttachments,
      },
      { signal: controller.signal }
    );
    state.busy = false;
    state.pendingStatusText = null;
    state.activeSession = res.session;
    state.messages = res.messages;
    state.pendingGates = res.pending_gates;
    if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
    if (res.telemetry) emit("telemetry:updated", res.telemetry);
    emit("session:updated");
    emit("chat:loaded");
  } catch (err) {
    state.busy = false;
    state.pendingStatusText = null;
    try {
      const detail = await api.getSessionDetail(state.activeSessionId);
      state.activeSession = detail.session;
      state.messages = detail.messages || [];
      state.pendingGates = detail.pending_gates || [];
      if (Array.isArray(detail.checkpoints)) state.checkpoints = detail.checkpoints;
    } catch {
      // keep optimistic user prompt visible if session refresh fails
    }
    emit("chat:loaded");
    emit("chat:error", err.message);
    if (/api key|API_KEY_INVALID|PERMISSION_DENIED|not configured/i.test(err.message)) {
      emit("settings:open-key-modal", err.message);
    }
  } finally {
    if (state.activeAbortController === controller) {
      state.activeAbortController = null;
    }
    state.busy = false;
    state.pendingStatusText = null;
    sendBtn.textContent = "Send";
    updateTokenEstimate();
    updateSendButtonState();
    emit("gates:changed");
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
