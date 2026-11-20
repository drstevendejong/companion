// bridge.js - Playground Bridge workflow (0 Gemini API tokens):
// 1-click Paste & Run Clipboard primary CTA (auto-reads, gated writes/scripts), collapsible manual paste fallback,
// persistent clipboard recovery, smart denial handoff, topbar Directives pill, and .md packer auto-copying.

import { api } from "./api.js";
import { emit, estimateTokensLocal, getActiveProject, on, state } from "./state.js";

const promptInput = document.getElementById("prompt-input");
const stageClipboardBtn = document.getElementById("btn-bridge-stage-clipboard");
const clipboardParseBtn = document.getElementById("btn-bridge-clipboard-parse");
const manualPasteBtn = document.getElementById("btn-bridge-manual-paste");

const directivesPill = document.getElementById("btn-bridge-directives-pill");
const copyImgBtn = document.getElementById("btn-bridge-copy-img");
const copyBundleBtn = document.getElementById("btn-bridge-copy-bundle");

const pasteWrap = document.getElementById("bridge-paste-wrap");
const pasteInput = document.getElementById("bridge-response-input");
const pasteHint = document.getElementById("bridge-paste-hint");
const parseStageBtn = document.getElementById("btn-bridge-parse-stage");
const parseBtn = document.getElementById("btn-bridge-parse");
const clearPasteBtn = document.getElementById("btn-bridge-clear-paste");
const closePasteBtn = document.getElementById("btn-bridge-close-paste");

const outputBar = document.getElementById("bridge-output-bar");
const outputLabel = document.getElementById("bridge-output-label");
const copyOutputBtn = document.getElementById("btn-bridge-copy-output");
const dismissOutputBtn = document.getElementById("btn-bridge-dismiss-output");

const DEFAULT_PASTE_HINT = "Manual Paste Fallback (used if clipboard read is blocked or empty)";

let pendingClipboardOutputText = "";
let pendingClipboardMdFile = null; // { path, estimated_tokens }
let queuedImages = [];
let lastSyncedSessionId = null;
let manualPasteOpen = false;
let outputAutoHideTimer = null;
let directivesOverride = null; // null = follow !session.directives_sent

const mdContentCache = new Map(); // `${projectId}:${path}` -> markdown text

export function initBridge() {
  copyBundleBtn.addEventListener("click", copyPromptBundle);
  copyImgBtn.addEventListener("click", copyNextCdpImage);
  clipboardParseBtn.addEventListener("click", () => handleClipboardQuickParse(true));
  stageClipboardBtn.addEventListener("click", () => handleClipboardQuickParse(false));
  manualPasteBtn.addEventListener("click", () => setManualPasteOpen(!manualPasteOpen, !manualPasteOpen));
  closePasteBtn.addEventListener("click", () => setManualPasteOpen(false, false));

  clearPasteBtn.addEventListener("click", () => {
    pasteInput.value = "";
    pasteHint.textContent = DEFAULT_PASTE_HINT;
    pasteInput.focus();
  });
  parseBtn.addEventListener("click", () => parseRawMarkdown(pasteInput.value, true));
  parseStageBtn.addEventListener("click", () => parseRawMarkdown(pasteInput.value, false));
  pasteInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      parseRawMarkdown(pasteInput.value, true);
    }
  });

  copyOutputBtn.addEventListener("click", handleCopyOutputClick);
  dismissOutputBtn.addEventListener("click", hideOutputBar);

  directivesPill.addEventListener("click", () => {
    directivesOverride = !isBridgeDirectivesChecked();
    syncDirectivesPillForSession();
    emit("composer:updated");
  });

  on("bridge:copy-bundle", copyPromptBundle);
  on("bridge:session-synced", syncDirectivesPillForSession);
  on("composer:attachments-changed", () => {
    syncImageCopyButton();
    syncCopyBundleHighlight();
  });
  on("composer:updated", syncCopyBundleHighlight);
  on("app:mode-changed", () => {
    syncManualPasteVisibility();
    syncDirectivesPillForSession();
    syncImageCopyButton();
    syncCopyBundleHighlight();
  });

  on("bridge:auto-copy-result", async ({ key, text, mdCacheKey, mdContent }) => {
    if (mdCacheKey && typeof mdContent === "string") {
      mdContentCache.set(mdCacheKey, mdContent);
    }
    if (!text) return;
    await tryAutoCopyResult(key, text);
  });

  on("bridge:handle-denials", ({ deniedEvents, approvedEvents, errorEvents, messages }) => {
    handleDeniedGateWorkflow(deniedEvents || [], approvedEvents || [], errorEvents || [], messages || []);
  });

  on("bridge:output-ready", ({ text, label }) => {
    if (!text) return;
    clearOutputAutoHide();
    pendingClipboardMdFile = null;
    pendingClipboardOutputText = text;
    outputLabel.textContent = label || "Tool results ready for Playground";
    copyOutputBtn.textContent = "Copy for Playground";
    outputBar.classList.remove("hidden");
  });

  on("bridge:generated-md", (files) => {
    if (!Array.isArray(files) || files.length === 0) return;
    clearOutputAutoHide();
    const first = files[0];
    pendingClipboardOutputText = "";
    pendingClipboardMdFile = first;
    outputLabel.textContent = `Generated .md: ${first.path} (~${Number(first.estimated_tokens || 0).toLocaleString()} tok)`;
    copyOutputBtn.textContent = `Copy ${first.path}`;
    outputBar.classList.remove("hidden");
  });

  syncManualPasteVisibility();
  syncDirectivesPillForSession();
  syncImageCopyButton();
  syncCopyBundleHighlight();
}

export function isBridgeDirectivesChecked() {
  if (directivesOverride !== null) return Boolean(directivesOverride);
  const s = state.activeSession;
  return !Boolean(s && s.directives_sent);
}

export function getCachedGeneratedMd(projectId, relPath) {
  return mdContentCache.get(`${projectId}:${relPath}`) || null;
}

export function setCachedGeneratedMd(projectId, relPath, content) {
  if (projectId && relPath && typeof content === "string") {
    mdContentCache.set(`${projectId}:${relPath}`, content);
  }
}

export function formatBridgeToolResults(toolEvents) {
  if (!Array.isArray(toolEvents) || toolEvents.length === 0) return "";
  const blocks = ["## Local Workspace Tool Results"];
  for (const ev of toolEvents) {
    const name = ev.tool_name || "tool";
    const resp = ev.response || {};
    if (resp.denied_by_user) {
      blocks.push(`- **${name}**: Denied by user (${resp.reason || "declined"})`);
    } else if (resp.error) {
      const targetPart = resp.target ? ` (\`${resp.target}\`)` : "";
      blocks.push(`- **${name}**${targetPart} **Error**: ${resp.error}`);
    } else if (name === "list_tree") {
      blocks.push(`### \`list_tree\` (${resp.subpath || "."})\n\`\`\`text\n${resp.tree || ""}\n\`\`\``);
    } else if (name === "search_code") {
      blocks.push(
        `### \`search_code\` (\`${resp.pattern || ""}\` · ${resp.total_hits ?? 0} hits)\n` +
          `\`\`\`text\n${resp.matches || ""}\n\`\`\``
      );
    } else if (name === "read_lines" || name === "read_file") {
      const p = resp.path || "";
      blocks.push(
        `### \`${name}\` (\`${p}\` · ${resp.total_lines ?? "?"} lines)\n` +
          `\`\`\`text\n${resp.content || ""}\n\`\`\``
      );
    } else if (resp.applied) {
      blocks.push(`- **${name}**: Patch applied to \`${resp.path}\` (backup: \`${resp.backup_path}\`)`);
    } else if (resp.written) {
      if (resp.executed) {
        blocks.push(
          `- **${name}**: Wrote \`${resp.path}\` and executed (exit code \`${resp.exit_code}\`).`
        );
      } else {
        blocks.push(`- **${name}**: Wrote \`${resp.path}\` to disk.`);
      }
    } else {
      blocks.push(`### \`${name}\`\n\`\`\`json\n${JSON.stringify(resp, null, 2)}\n\`\`\``);
    }
  }
  return blocks.join("\n\n").trim();
}

export function getTrailingToolBatchKey(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return null;
  let lastToolMsg = null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "tool_event") {
      if (!lastToolMsg) lastToolMsg = messages[i];
    } else {
      break;
    }
  }
  return lastToolMsg ? `batch:${lastToolMsg.id}` : null;
}

function setManualPasteOpen(open, focusInput = false) {
  manualPasteOpen = Boolean(open);
  syncManualPasteVisibility();
  if (focusInput && manualPasteOpen && !state.apiEnabled) {
    pasteInput.focus();
  }
}

function syncManualPasteVisibility() {
  if (state.apiEnabled) {
    pasteWrap.classList.add("hidden");
    return;
  }
  pasteWrap.classList.toggle("hidden", !manualPasteOpen);
}

function syncCopyBundleHighlight() {
  if (!copyBundleBtn) return;
  const hasDraft = Boolean((promptInput.value || "").trim()) || (state.attachments || []).length > 0;
  copyBundleBtn.classList.toggle("is-prompt-ready", isBridgeDirectivesChecked() || hasDraft);
}

function syncDirectivesPillForSession() {
  const s = state.activeSession;
  const sid = s ? s.id : null;
  if (sid !== lastSyncedSessionId) {
    lastSyncedSessionId = sid;
    directivesOverride = null;
    queuedImages = [];
    hideOutputBar();
    setManualPasteOpen(false, false);
    syncImageCopyButton();
  }
  const checked = isBridgeDirectivesChecked();
  const alreadySent = Boolean(s && s.directives_sent);
  directivesPill.classList.toggle("is-pending", checked);
  directivesPill.classList.toggle("is-sent", !checked);
  if (checked) {
    directivesPill.textContent = alreadySent ? "Directives: Re-include" : "Directives: On";
  } else {
    directivesPill.textContent = alreadySent ? "Directives ✓" : "Directives: Off";
  }
  if (!copyBundleBtn.disabled) {
    copyBundleBtn.textContent = checked ? "Copy Session Starter" : "Copy Prompt";
  }
  syncCopyBundleHighlight();
}

function syncImageCopyButton() {
  const imgs = (state.attachments || []).filter((a) => a.kind === "image");
  const allImgs = imgs.length ? imgs : queuedImages;
  if (allImgs.length > 0) {
    copyImgBtn.classList.remove("hidden");
    copyImgBtn.textContent = allImgs.length > 1 ? `Copy CDP Image (${allImgs.length})` : "Copy CDP Image";
  } else {
    copyImgBtn.classList.add("hidden");
  }
}

async function copyPromptBundle() {
  if (!state.activeSessionId) return;
  const prompt = promptInput.value.trim();
  const hasAtts = (state.attachments || []).length > 0;
  const includeDir = isBridgeDirectivesChecked();
  if (!prompt && !hasAtts && !includeDir) return;

  const sentAttachments = [...state.attachments];
  const imgAtts = sentAttachments.filter((a) => a.kind === "image");

  copyBundleBtn.disabled = true;
  copyBundleBtn.textContent = "Bundling...";

  try {
    const res = await api.bridgeBundle({
      session_id: state.activeSessionId,
      prompt,
      attachments: sentAttachments,
      include_directives: includeDir,
      record_turn: true,
    });

    await copyTextToClipboard(res.bundle_text);

    promptInput.value = "";
    state.attachments = [];
    if (imgAtts.length > 0) {
      queuedImages = imgAtts;
    }
    emit("composer:attachments-changed");

    if (res.session) state.activeSession = res.session;
    if (res.messages) state.messages = res.messages;
    directivesOverride = null;
    syncDirectivesPillForSession();
    setManualPasteOpen(false, false);
    emit("composer:updated");
    emit("chat:loaded");

    copyBundleBtn.textContent = `Copied (~${Number(res.estimated_tokens || 0).toLocaleString()} tok)`;
    setTimeout(() => {
      copyBundleBtn.textContent = isBridgeDirectivesChecked() ? "Copy Session Starter" : "Copy Prompt";
    }, 2000);
  } catch (err) {
    emit("chat:error", err.message);
    copyBundleBtn.textContent = isBridgeDirectivesChecked() ? "Copy Session Starter" : "Copy Prompt";
  } finally {
    copyBundleBtn.disabled = false;
  }
}

async function copyNextCdpImage() {
  const imgs = (state.attachments || []).filter((a) => a.kind === "image");
  const target = imgs[0] || queuedImages[0];
  if (!target) return;

  try {
    const resp = await fetch(target.content);
    const blob = await resp.blob();
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
      if (queuedImages.length > 0 && imgs.length === 0) {
        queuedImages.shift();
      }
      syncImageCopyButton();
      copyImgBtn.textContent = "Image Copied ✓";
      setTimeout(() => syncImageCopyButton(), 1800);
    } else {
      throw new Error("Clipboard image copy requires HTTPS or localhost in this browser.");
    }
  } catch (err) {
    emit("chat:error", `Could not copy image to clipboard: ${err.message}`);
  }
}

async function handleClipboardQuickParse(autoApproveReads) {
  if (!state.activeSessionId) return;
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      throw new Error("Clipboard read unavailable");
    }
    const raw = (await navigator.clipboard.readText()) || "";
    if (!raw.trim()) {
      setManualPasteOpen(true, true);
      pasteHint.textContent = "Clipboard is empty — paste Playground reply here and click Run";
      return;
    }
    await parseRawMarkdown(raw, autoApproveReads);
  } catch {
    setManualPasteOpen(true, true);
    pasteHint.textContent = "Clipboard read blocked by browser — press Ctrl+V here and click Run";
  }
}

async function parseRawMarkdown(rawInput, autoApproveReads = true) {
  if (!state.activeSessionId) return;
  const raw = String(rawInput || "").trim();
  if (!raw) return;

  if (
    raw.startsWith("## Local Workspace Tool Results") ||
    raw.startsWith("# Session Context — Project:")
  ) {
    emit(
      "chat:error",
      "Clipboard already holds Companion's own output (not a Playground reply). Paste it into AI Studio Playground first, then copy Playground's reply before running."
    );
    return;
  }

  parseBtn.disabled = true;
  parseStageBtn.disabled = true;
  clipboardParseBtn.disabled = true;
  stageClipboardBtn.disabled = true;
  parseBtn.textContent = "Running...";
  clipboardParseBtn.textContent = autoApproveReads ? "Running..." : "Staging...";
  let copyOutcome = null;
  let hasErrorOutcome = false;
  state.gatesHiddenDueToError = false;
  state.lastErrorContext = null;

  try {
    const res = await api.bridgeParse(state.activeSessionId, raw, autoApproveReads);
    pasteInput.value = "";
    pasteHint.textContent = DEFAULT_PASTE_HINT;
    hideOutputBar();
    setManualPasteOpen(false, false);
    state.activeSession = res.session;
    state.messages = res.messages;
    state.pendingGates = res.pending_gates || [];

    const errorCount = Number(res.error_count || 0);
    const warnings = Array.isArray(res.warnings) ? res.warnings : [];
    const extraWarnings = warnings.slice(errorCount);

    if (errorCount > 0) {
      hasErrorOutcome = true;
      state.gatesHiddenDueToError = state.pendingGates.length > 0;
      const errBatchKey = getTrailingToolBatchKey(res.messages) || `err:${Date.now()}`;
      const copyPayload =
        (state.pendingGates.length === 0 ? res.bridge_reply_text : "") ||
        res.error_reply_text ||
        warnings.join("\n");
      state.lastErrorContext = {
        sessionId: state.activeSessionId,
        reasonText: summarizeWarningList(warnings),
      };
      if (copyPayload) {
        copyOutcome = await tryAutoCopyResult(errBatchKey, copyPayload);
      }
    } else if (state.pendingGates.length === 0 && res.bridge_reply_text) {
      const batchKey = getTrailingToolBatchKey(res.messages);
      if (batchKey) {
        copyOutcome = await tryAutoCopyResult(batchKey, res.bridge_reply_text);
      }
    }

    emit("session:updated");
    emit("chat:loaded");
    if (extraWarnings.length > 0) {
      emit("chat:error", `Bridge parse warnings:\n${extraWarnings.join("\n")}`);
    }
  } catch (err) {
    emit("chat:error", err.message);
  } finally {
    parseBtn.disabled = false;
    parseStageBtn.disabled = false;
    clipboardParseBtn.disabled = false;
    stageClipboardBtn.disabled = false;
    parseBtn.textContent = "Run Reads & Stage Writes";
    if (copyOutcome === "copied") {
      clipboardParseBtn.textContent = hasErrorOutcome ? "Copied Error ✓" : "Copied Result ✓";
      setTimeout(() => {
        clipboardParseBtn.textContent = "Paste & Run Clipboard";
      }, 1800);
    } else if (copyOutcome === "blocked") {
      clipboardParseBtn.textContent = hasErrorOutcome ? "Click Copy Error" : "Click Copy in Balloon";
      setTimeout(() => {
        clipboardParseBtn.textContent = "Paste & Run Clipboard";
      }, 2400);
    } else {
      clipboardParseBtn.textContent = "Paste & Run Clipboard";
    }
  }
}

function summarizeWarningList(warnings) {
  const unique = [];
  const seen = new Set();
  for (const w of warnings || []) {
    const clean = String(w || "").trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    unique.push(clean);
  }
  return unique.join("; ");
}

async function tryAutoCopyResult(batchKey, text) {
  if (!batchKey || !text) return null;
  let status = "copied";
  try {
    await copyTextToClipboard(text);
  } catch {
    status = "blocked";
  }
  state.lastClipboardInfo = { key: batchKey, status };
  emit("chat:refresh-badges");
  return status;
}

function buildErrorReasonForDeniedAction(ev, errorEvents) {
  const deniedTool = ev.tool_name || "";
  const resp = ev.response || {};
  const deniedTarget = resp.target || "";

  const uniqueReasons = [];
  const seen = new Set();
  for (const errEv of errorEvents || []) {
    const errResp = errEv.response || {};
    const errText = String(errResp.error || "").trim();
    if (!errText) continue;
    const errTarget = String(errResp.target || "").trim();
    const sameContext =
      (!errTarget || errTarget === deniedTarget) && (!deniedTool || errEv.tool_name === deniedTool);
    const label = sameContext
      ? errText
      : `${errEv.tool_name || "tool"}${errTarget ? `(${errTarget})` : ""}: ${errText}`;
    if (!seen.has(label)) {
      seen.add(label);
      uniqueReasons.push(label);
    }
  }

  if (uniqueReasons.length > 0) {
    return uniqueReasons.join("; ");
  }

  if (
    state.lastErrorContext &&
    state.lastErrorContext.sessionId === state.activeSessionId &&
    state.lastErrorContext.reasonText
  ) {
    return String(state.lastErrorContext.reasonText).trim();
  }
  return "";
}

function handleDeniedGateWorkflow(deniedEvents, approvedEvents, errorEvents, messages) {
  setManualPasteOpen(false, false);

  const callTargets = extractToolTargetsFromMessages(messages);
  const denialLines = deniedEvents.map((ev) => {
    const resp = ev.response || {};
    const cid = ev.call_id;
    const target = resp.target || (cid && callTargets.get(cid)) || "";
    const sig = target ? `${ev.tool_name}(${target})` : ev.tool_name || "action";
    const evWithTarget = { ...ev, response: { ...resp, target } };
    const knownReason = buildErrorReasonForDeniedAction(evWithTarget, errorEvents);
    return knownReason ? `Action ${sig} was denied. Reason: ${knownReason}` : `Action ${sig} was denied. Reason: `;
  });

  if (state.pendingGates.length === 0) {
    state.lastErrorContext = null;
  }

  const blockText = denialLines.join("\n");
  const existing = promptInput.value;
  if (existing && !existing.endsWith("\n")) {
    promptInput.value = `${existing}\n\n${blockText}`;
  } else {
    promptInput.value = `${existing}${blockText}`;
  }

  if (approvedEvents.length > 0) {
    const approvedMd = formatBridgeToolResults(approvedEvents);
    if (approvedMd) {
      const alreadyAttached = (state.attachments || []).some(
        (a) => a.kind === "snippet" && a.content === approvedMd
      );
      if (!alreadyAttached) {
        emit("composer:attach", {
          kind: "snippet",
          label: `Approved tool results (${approvedEvents.length})`,
          content: approvedMd,
          estimated_tokens: estimateTokensLocal(approvedMd),
        });
      }
    }
  }

  promptInput.focus();
  const endPos = promptInput.value.length;
  promptInput.setSelectionRange(endPos, endPos);
  emit("composer:updated");
}

function extractToolTargetsFromMessages(messages) {
  const map = new Map();
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m.role === "model" && Array.isArray(m.tool_calls)) {
      m.tool_calls.forEach((tc, idx) => {
        const a = tc.args || {};
        const target = a.path || a.pattern || a.subpath || "";
        if (target) {
          map.set(`bridge-${idx}`, String(target));
        }
      });
      break;
    }
  }
  return map;
}

async function handleCopyOutputClick() {
  const proj = getActiveProject();
  try {
    let textToCopy = pendingClipboardOutputText;
    if (!textToCopy && pendingClipboardMdFile && proj) {
      const res = await api.bridgeReadMd(
        proj.id,
        pendingClipboardMdFile.path,
        state.activeSessionId,
        true
      );
      textToCopy = res.content;
      setCachedGeneratedMd(proj.id, pendingClipboardMdFile.path, textToCopy);
      if (res.messages) {
        state.messages = res.messages;
        emit("chat:loaded");
      }
    }
    if (!textToCopy) return;
    await copyTextToClipboard(textToCopy);
    copyOutputBtn.textContent = "Copied ✓";
    clearOutputAutoHide();
    outputAutoHideTimer = setTimeout(() => {
      hideOutputBar();
    }, 1100);
  } catch (err) {
    emit("chat:error", err.message);
  }
}

function clearOutputAutoHide() {
  if (outputAutoHideTimer) {
    clearTimeout(outputAutoHideTimer);
    outputAutoHideTimer = null;
  }
}

function hideOutputBar() {
  clearOutputAutoHide();
  pendingClipboardOutputText = "";
  pendingClipboardMdFile = null;
  outputBar.classList.add("hidden");
}

export async function copyTextToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}
