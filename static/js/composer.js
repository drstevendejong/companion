// composer.js - Prompt box, Ctrl+V image paste, attachment chips, and Paste-into-Prompt

import { api } from "./api.js";
import { emit, estimateTokensLocal, on, state } from "./state.js";

const promptInput = document.getElementById("prompt-input");
const attachmentStrip = document.getElementById("attachment-strip");
const tokenEstEl = document.getElementById("composer-token-est");
const sendBtn = document.getElementById("btn-send");

export function initComposer() {
  promptInput.addEventListener("input", updateTokenEstimate);
  promptInput.addEventListener("paste", handleClipboardPaste);
  promptInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      sendCurrentPrompt();
    }
  });

  sendBtn.addEventListener("click", sendCurrentPrompt);
  on("composer:attach", (att) => addAttachment(att));
  on("composer:paste-text", (text) => pasteTextIntoPrompt(text));
  on("gates:changed", updateSendButtonState);

  updateTokenEstimate();
}

export function addAttachment(attachment) {
  state.attachments.push(attachment);
  renderAttachments();
  updateTokenEstimate();
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
    });
    attachmentStrip.appendChild(chip);
  });
}

function updateTokenEstimate() {
  const promptTokens = estimateTokensLocal(promptInput.value);
  const attTokens = state.attachments.reduce((acc, a) => acc + (a.estimated_tokens || 0), 0);
  tokenEstEl.textContent = `Prompt ~${(promptTokens + attTokens).toLocaleString()} tok`;
}

function updateSendButtonState() {
  const hasGates = state.pendingGates && state.pendingGates.length > 0;
  sendBtn.disabled = state.busy || hasGates || !state.activeSessionId;
  sendBtn.title = hasGates ? "Resolve pending tool approval gates first" : "Send prompt (Ctrl+Enter)";
}

async function sendCurrentPrompt() {
  if (!state.activeSessionId || state.busy) return;
  if (state.pendingGates.length > 0) return;

  const prompt = promptInput.value.trim();
  if (!prompt && state.attachments.length === 0) return;

  state.busy = true;
  updateSendButtonState();
  sendBtn.textContent = "Thinking...";

  try {
    const res = await api.sendChat({
      session_id: state.activeSessionId,
      prompt,
      attachments: state.attachments,
    });
    promptInput.value = "";
    state.attachments = [];
    renderAttachments();
    state.activeSession = res.session;
    state.messages = res.messages;
    state.pendingGates = res.pending_gates;
    emit("session:updated");
    emit("chat:loaded");
  } catch (err) {
    emit("chat:error", err.message);
    if (/api key|API_KEY_INVALID|PERMISSION_DENIED|not configured/i.test(err.message)) {
      emit("settings:open-key-modal", err.message);
    }
  } finally {
    state.busy = false;
    sendBtn.textContent = "Send";
    updateTokenEstimate();
    updateSendButtonState();
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
