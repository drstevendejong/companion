// bridge.js - Playground Bridge workflow (0 Gemini API tokens):
// 2 clear composer CTAs (Copy Starter/Prompt -> Playground & Paste & Run AI Reply),
// automatic clipboard-blocked paste fallback, accidental prompt-box AI command detection,
// persistent clipboard recovery, smart denial handoff, topbar Directives pill, and .md packer auto-copying.

import { api } from "./api.js";
import { emit, estimateTokensLocal, getActiveProject, on, state } from "./state.js";

const promptInput = document.getElementById("prompt-input");
const clipboardParseBtn = document.getElementById("btn-bridge-clipboard-parse");

const copySplitWrap = document.getElementById("bridge-copy-split");
const copyImgBtn = document.getElementById("btn-bridge-copy-img");
const copyBundleBtn = document.getElementById("btn-bridge-copy-bundle");
const copyMenuToggleBtn = document.getElementById("btn-bridge-copy-menu-toggle");
const copyMenu = document.getElementById("bridge-copy-menu");
const copyWithDirectivesItem = document.getElementById("btn-bridge-copy-with-directives");
const skipDirectivesItem = document.getElementById("btn-bridge-skip-directives");
const copyMenuMeta = document.getElementById("bridge-copy-menu-meta");

const detectBar = document.getElementById("bridge-prompt-detect-bar");
const detectKeepBtn = document.getElementById("btn-prompt-keep-text");
const detectRunBtn = document.getElementById("btn-prompt-run-detected");

const pasteWrap = document.getElementById("bridge-paste-wrap");
const pasteInput = document.getElementById("bridge-response-input");
const pasteHint = document.getElementById("bridge-paste-hint");
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
let dismissedDetectForText = null;

const mdContentCache = new Map(); // `${projectId}:${path}` -> markdown text

export function initBridge() {
  copyBundleBtn.addEventListener("click", () => copyPromptBundle());
  if (copyMenuToggleBtn) {
    copyMenuToggleBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleCopyMenu();
    });
  }
  if (copyWithDirectivesItem) {
    copyWithDirectivesItem.addEventListener("click", () => {
      closeCopyMenu();
      void copyPromptBundle(true);
    });
  }
  if (skipDirectivesItem) {
    skipDirectivesItem.addEventListener("click", () => {
      directivesOverride = !isBridgeDirectivesChecked();
      closeCopyMenu();
      syncDirectivesPillForSession();
      emit("composer:updated");
    });
  }
  document.addEventListener("click", (ev) => {
    if (!copyMenu || copyMenu.classList.contains("hidden")) return;
    if (copySplitWrap && copySplitWrap.contains(ev.target)) return;
    closeCopyMenu();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && copyMenu && !copyMenu.classList.contains("hidden")) {
      closeCopyMenu();
    }
  });
  copyImgBtn.addEventListener("click", copyNextImage);
  clipboardParseBtn.addEventListener("click", () => handleClipboardQuickParse(true));
  closePasteBtn.addEventListener("click", () => setManualPasteOpen(false, false));

  detectKeepBtn.addEventListener("click", () => {
    dismissedDetectForText = promptInput.value.trim();
    syncPromptDetectBar();
    promptInput.focus();
  });
  detectRunBtn.addEventListener("click", runDetectedBridgeFromPrompt);

  clearPasteBtn.addEventListener("click", () => {
    pasteInput.value = "";
    pasteHint.textContent = DEFAULT_PASTE_HINT;
    pasteInput.focus();
  });
  parseBtn.addEventListener("click", () => parseRawMarkdown(pasteInput.value, true));
  pasteInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      parseRawMarkdown(pasteInput.value, true);
    }
  });

  copyOutputBtn.addEventListener("click", handleCopyOutputClick);
  dismissOutputBtn.addEventListener("click", hideOutputBar);

  on("bridge:copy-bundle", () => copyPromptBundle());
  on("bridge:run-detected-prompt", runDetectedBridgeFromPrompt);
  on("bridge:session-synced", syncDirectivesPillForSession);
  on("composer:attachments-changed", () => {
    syncImageCopyButton();
    syncDirectivesPillForSession();
  });
  on("composer:updated", () => {
    syncDirectivesPillForSession();
  });
  on("app:mode-changed", () => {
    closeCopyMenu();
    syncManualPasteVisibility();
    syncDirectivesPillForSession();
    syncImageCopyButton();
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
  syncPromptDetectBar();
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

export function formatBridgeToolResults(toolEvents, opts = {}) {
  if (!Array.isArray(toolEvents) || toolEvents.length === 0) return "";
  const fullMap = opts && opts.fullOutputByExecId ? opts.fullOutputByExecId : null;
  const blocks = ["## Local Workspace Tool Results"];
  for (const ev of toolEvents) {
    const name = ev.tool_name || "tool";
    const resp = ev.response || {};
    const fullRaw =
      fullMap && resp.exec_id && Object.prototype.hasOwnProperty.call(fullMap, resp.exec_id)
        ? String(fullMap[resp.exec_id] ?? "")
        : null;
    if (resp.denied_by_user) {
      blocks.push(`- **${name}**: Denied by user (${resp.reason || "declined"})`);
    } else if (resp.error) {
      const targetPart = resp.target ? ` (\`${resp.target}\`)` : "";
      blocks.push(`- **${name}**${targetPart} **Error**: ${resp.error}`);
    } else if (name === "list_tree") {
      blocks.push(`### \`list_tree\` (${resp.subpath || "."})\n\`\`\`text\n${resp.tree || ""}\n\`\`\``);
    } else if (name === "extract_headers") {
      blocks.push(
        `### \`extract_headers\` (\`${resp.pattern || "."}\` · ${resp.file_count ?? 0} files, ${resp.header_count ?? 0} headers)\n` +
          `\`\`\`text\n${resp.formatted || ""}\n\`\`\``
      );
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
    } else if (name === "read_command_output") {
      const eid = resp.exec_id || "latest";
      if (!resp.found) {
        blocks.push(`- **read_command_output** (\`${eid}\`): Not found (${resp.error || "buffer empty"})`);
      } else {
        blocks.push(
          `### \`read_command_output\` (\`${eid}\` · ${resp.shown_lines ?? "?"}/${resp.total_lines ?? "?"} lines)\n` +
            `\`\`\`text\n${resp.content || ""}\n\`\`\``
        );
      }
    } else if (name === "scout_workspace") {
      const stepsLbl = `${resp.steps_taken ?? resp.steps_count ?? 0} steps`;
      const modelLbl = resp.model_used || resp.model || "scout";
      const stopLbl = resp.stopped_early ? " · stopped early" : "";
      blocks.push(`### \`scout_workspace\` (${modelLbl} · ${stepsLbl}${stopLbl})\n${String(resp.answer || "").trim()}`);
    } else if (name === "update_header") {
      const staged = Array.isArray(resp.staged_paths) ? resp.staged_paths : [];
      const unchanged = Array.isArray(resp.unchanged_paths) ? resp.unchanged_paths : [];
      let msg = `- **update_header**: Drafted **${staged.length}** human-gated header patch(es)`;
      if (staged.length) msg += ` (\`${staged.join(", ")}\`)`;
      if (unchanged.length) msg += `; ${unchanged.length} unchanged`;
      blocks.push(`${msg}.`);
    } else if (resp.applied) {
      blocks.push(`- **${name}**: Patch applied to \`${resp.path}\` (backup: \`${resp.backup_path}\`)`);
    } else if (resp.renamed) {
      blocks.push(
        `- **${name}**: Renamed \`${resp.old_path}\` → \`${resp.new_path}\` (backup: \`${resp.backup_path}\`)`
      );
    } else if (resp.written) {
      if (resp.executed) {
        const delNote = resp.deleted ? " and deleted temporary script" : "";
        const eidNote = resp.exec_id ? ` · \`exec_id=${resp.exec_id}\`` : "";
        let line = `- **${name}**: Wrote \`${resp.path}\` and executed${delNote} (exit code \`${resp.exit_code}\`${eidNote}).`;
        if (fullRaw !== null) {
          line += `\n\`\`\`text\n${fullRaw.trim() || "(No output)"}\n\`\`\``;
        } else {
          const aiSum = String(resp.ai_summary || "").trim();
          if (aiSum) {
            const quoted = aiSum.split("\n").join("\n> ");
            line += `\n> **Scout Summary:** ${quoted}`;
          }
          const preview = String(resp.output_preview || "").trim();
          if (preview) {
            line += `\n\`\`\`text\n${preview}\n\`\`\``;
          } else if (aiSum && resp.exec_id) {
            line += `\n*(Raw output omitted to save tokens — call \`read_command_output\` with \`exec_id=${resp.exec_id}\` if exact lines are needed.)*`;
          }
        }
        blocks.push(line);
      } else {
        blocks.push(`- **${name}**: Wrote \`${resp.path}\` to disk.`);
      }
    } else if (name === "run_script" || name === "run_and_delete_script") {
      const cmd = resp.command || resp.path || "script";
      const code = resp.exit_code ?? "?";
      const ok = code === 0 && !resp.has_traceback;
      const statusLbl =
        name === "run_and_delete_script"
          ? ok && resp.deleted
            ? "OK — script auto-deleted"
            : ok
              ? "OK"
              : "FAILED (script kept on disk for patching)"
          : ok
            ? "OK"
            : "FAILED";
      const eidNote = resp.exec_id ? ` · \`exec_id=${resp.exec_id}\`` : "";
      let line = `- **${name}** (\`${cmd}\`): **${statusLbl}** (exit code \`${code}\`${eidNote}).`;
      if (fullRaw !== null) {
        line += `\n\`\`\`text\n${fullRaw.trim() || "(No output)"}\n\`\`\``;
      } else {
        const aiSum = String(resp.ai_summary || "").trim();
        if (aiSum) {
          const quoted = aiSum.split("\n").join("\n> ");
          line += `\n> **Scout Summary:** ${quoted}`;
        }
        const preview = String(resp.output_preview || "").trim();
        if (preview) {
          line += `\n\`\`\`text\n${preview}\n\`\`\``;
        } else if (aiSum && resp.exec_id) {
          line += `\n*(Raw output omitted to save tokens — call \`read_command_output\` with \`exec_id=${resp.exec_id}\` if exact lines are needed.)*`;
        }
      }
      blocks.push(line);
    } else if (resp.deleted) {
      blocks.push(`- **${name}**: Deleted \`${resp.path}\` (backup: \`${resp.backup_path}\`)`);
    } else if (name === "run_tests") {
      const cmd = resp.command || "tests";
      const code = resp.exit_code ?? "?";
      const passed = Boolean(resp.passed) && code === 0 && !resp.has_traceback;
      const statusLbl = passed ? "PASSED" : "FAILED";
      const scopeNote = resp.is_full_suite === false ? " (quick/targeted check — savepoint stays active)" : "";
      const eidNote = resp.exec_id ? ` · \`exec_id=${resp.exec_id}\`` : "";
      let line = `- **run_tests** (\`${cmd}\`): **${statusLbl}** (exit code \`${code}\`${eidNote})${scopeNote}.`;
      if (fullRaw !== null) {
        line += `\n\`\`\`text\n${fullRaw.trim() || "(No output)"}\n\`\`\``;
      } else {
        const aiSum = String(resp.ai_summary || "").trim();
        if (aiSum) {
          const quoted = aiSum.split("\n").join("\n> ");
          line += `\n> **Scout Summary:** ${quoted}`;
        }
        const preview = String(resp.output_preview || "").trim();
        if (preview) {
          line += `\n\`\`\`text\n${preview}\n\`\`\``;
        } else if (aiSum && resp.exec_id) {
          line += `\n*(Raw output omitted to save tokens — call \`read_command_output\` with \`exec_id=${resp.exec_id}\` if exact lines are needed.)*`;
        }
      }
      blocks.push(line);
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

let isCopyingBundle = false;

function hasComposerDraft() {
  return Boolean((promptInput.value || "").trim()) || (state.attachments || []).length > 0;
}

function closeCopyMenu() {
  if (!copyMenu) return;
  copyMenu.classList.add("hidden");
  if (copyMenuToggleBtn) copyMenuToggleBtn.setAttribute("aria-expanded", "false");
}

function toggleCopyMenu() {
  if (!copyMenu || !state.activeSessionId) return;
  const open = copyMenu.classList.contains("hidden");
  copyMenu.classList.toggle("hidden", !open);
  if (copyMenuToggleBtn) copyMenuToggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
}

function syncCopyBundleHighlight() {
  if (!copyBundleBtn) return;
  const hasSession = Boolean(state.activeSessionId);
  const hasDraft = hasComposerDraft();
  const includeDir = isBridgeDirectivesChecked();
  const canCopy = hasSession && (hasDraft || includeDir);
  if (!isCopyingBundle) {
    copyBundleBtn.disabled = !canCopy;
  }
  copyBundleBtn.classList.toggle("is-prompt-ready", canCopy);
  if (copyMenuToggleBtn) {
    copyMenuToggleBtn.disabled = !hasSession || isCopyingBundle;
  }
}

function syncDirectivesPillForSession() {
  const s = state.activeSession;
  const sid = s ? s.id : null;
  if (sid !== lastSyncedSessionId) {
    lastSyncedSessionId = sid;
    directivesOverride = null;
    queuedImages = [];
    closeCopyMenu();
    hideOutputBar();
    setManualPasteOpen(false, false);
    syncImageCopyButton();
  }
  const checked = isBridgeDirectivesChecked();
  const alreadySent = Boolean(s && s.directives_sent);
  const hasDraft = hasComposerDraft();

  if (!isCopyingBundle && copyBundleBtn) {
    copyBundleBtn.textContent = checked ? "Copy + Directives" : "Copy Prompt";
    copyBundleBtn.title = checked
      ? "Copy prompt, attachments & Project Directives for Playground (Ctrl+Enter)"
      : hasDraft
        ? "Copy prompt & attachments to clipboard for Playground (Ctrl+Enter)"
        : "Type a prompt or use the menu (▾) to copy Directives";
  }

  if (copyWithDirectivesItem) {
    if (!checked) {
      copyWithDirectivesItem.classList.remove("hidden");
      copyWithDirectivesItem.textContent = hasDraft
        ? "Copy Prompt + Directives"
        : "Copy Directives Only";
      copyWithDirectivesItem.title = hasDraft
        ? "Bundle current prompt & attachments with Project Directives for Playground"
        : "Copy Project Directives & Tool Contract alone (for a new Playground chat)";
    } else if (hasDraft) {
      copyWithDirectivesItem.classList.add("hidden");
    } else {
      copyWithDirectivesItem.classList.add("hidden");
    }
  }

  if (skipDirectivesItem) {
    if (!alreadySent) {
      skipDirectivesItem.classList.remove("hidden");
      skipDirectivesItem.textContent = checked ? "Skip Directives" : "Include Directives";
      skipDirectivesItem.title = checked
        ? "Do not include Project Directives in next copy"
        : "Include Project Directives in next copy";
    } else {
      skipDirectivesItem.classList.add("hidden");
    }
  }

  if (copyMenuMeta) {
    const baseDirTip = state.directivesInfoText || "Project Directives & Tool Contract";
    copyMenuMeta.textContent = alreadySent
      ? `Already sent · ${baseDirTip}`
      : `First turn · ${baseDirTip}`;
  }

  syncCopyBundleHighlight();
  syncPromptDetectBar();
}

export function looksLikeBridgeAiOutput(rawText) {
  const trimmed = String(rawText || "").trim();
  if (!trimmed || trimmed.length < 12) return false;
  if (
    trimmed.startsWith("## Local Workspace Tool Results") ||
    trimmed.startsWith("# Session Context — Project:") ||
    trimmed.startsWith("## Local Test / Command")
  ) {
    return false;
  }
  if (/^\s*```(?:json|jsonc|markdown|md)\b[\s\S]*"(?:tool|tool_name|name|action|actions|tool_calls)"\s*:/i.test(trimmed)) {
    return true;
  }
  if (/^(?:jsonc?|application\/json)?\s*\n?\s*[\[{][\s\S]*"(?:tool|tool_name|name|action|actions|tool_calls)"\s*:/i.test(trimmed)) {
    return true;
  }
  if (/```json[\s\S]*"(?:tool|tool_name|actions|tool_calls)"\s*:/i.test(trimmed)) {
    return true;
  }
  if (/(?:^|\n)json\s*\n\s*[\[{][\s\S]*"(?:tool|tool_name)"\s*:/i.test(trimmed)) {
    return true;
  }
  if (/```(?:patch|diff)\b[\s\S]*<<<<<<<\s*SEARCH[\s\S]*=======[\s\S]*>>>>>>>\s*REPLACE/i.test(trimmed)) {
    return true;
  }
  if (/<<<<<<<\s*SEARCH\b[\s\S]*\n=======\s*\n[\s\S]*>>>>>>>\s*REPLACE\b/.test(trimmed)) {
    return true;
  }
  if (/```(?:python|py)\b[\s\S]*\b(?:import|from|def|Path)\b/i.test(trimmed)) {
    return true;
  }
  if (
    /(?:^|\n)(?:python|from\s+[A-Za-z0-9_.]+(?:\s+import\b)|\s*import\s+[A-Za-z0-9_.]+|\s*def\s+[A-Za-z0-9_]+\s*\()/m.test(
      trimmed
    ) &&
    /(?:^|\n)\s*(?:import|from|def|class|print\(|with\s+|for\s+|if\s+|Path\()/m.test(trimmed)
  ) {
    return true;
  }
  return false;
}

export function isDetectedPromptAiPending() {
  if (state.apiEnabled || !state.activeSessionId) return false;
  const trimmed = String((promptInput && promptInput.value) || "").trim();
  if (!trimmed) return false;
  if (dismissedDetectForText && dismissedDetectForText === trimmed) return false;
  return looksLikeBridgeAiOutput(trimmed);
}

function syncPromptDetectBar() {
  if (!detectBar) return;
  const trimmed = String((promptInput && promptInput.value) || "").trim();
  if (!trimmed || (dismissedDetectForText && dismissedDetectForText !== trimmed)) {
    dismissedDetectForText = null;
  }
  detectBar.classList.toggle("hidden", !isDetectedPromptAiPending());
}

async function runDetectedBridgeFromPrompt() {
  if (state.apiEnabled || !state.activeSessionId) return;
  const raw = String(promptInput.value || "").trim();
  if (!raw) return;
  dismissedDetectForText = null;
  if (detectBar) detectBar.classList.add("hidden");
  await parseRawMarkdown(raw, true, { clearPromptOnRun: true });
}

function syncImageCopyButton() {
  const imgs = (state.attachments || []).filter((a) => a.kind === "image");
  const allImgs = imgs.length ? imgs : queuedImages;
  if (allImgs.length > 0) {
    copyImgBtn.classList.remove("hidden");
    copyImgBtn.textContent = allImgs.length > 1 ? `Copy Image (${allImgs.length})` : "Copy Image";
  } else {
    copyImgBtn.classList.add("hidden");
  }
}

async function copyPromptBundle(forceIncludeDirectives = null) {
  if (!state.activeSessionId) return;
  const prompt = promptInput.value.trim();
  const hasAtts = (state.attachments || []).length > 0;
  const includeDir =
    typeof forceIncludeDirectives === "boolean"
      ? forceIncludeDirectives
      : isBridgeDirectivesChecked();
  if (!prompt && !hasAtts && !includeDir) return;

  const sentAttachments = [...state.attachments];
  const imgAtts = sentAttachments.filter((a) => a.kind === "image");

  isCopyingBundle = true;
  copyBundleBtn.disabled = true;
  if (copyMenuToggleBtn) copyMenuToggleBtn.disabled = true;
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
    isCopyingBundle = false;
    syncDirectivesPillForSession();
    setManualPasteOpen(false, false);
    emit("composer:updated");
    emit("chat:loaded");

    copyBundleBtn.textContent = `Copied (~${Number(res.estimated_tokens || 0).toLocaleString()} tok)`;
    setTimeout(() => {
      syncDirectivesPillForSession();
    }, 2000);
  } catch (err) {
    isCopyingBundle = false;
    emit("chat:error", err.message);
    syncDirectivesPillForSession();
  } finally {
    isCopyingBundle = false;
    syncCopyBundleHighlight();
  }
}

async function copyNextImage() {
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

async function parseRawMarkdown(rawInput, autoApproveReads = true, opts = {}) {
  if (!state.activeSessionId) return;
  const raw = String(rawInput || "").trim();
  if (!raw) return;

  if (
    raw.startsWith("## Local Workspace Tool Results") ||
    raw.startsWith("# Session Context — Project:") ||
    raw.startsWith("## Local Test / Command")
  ) {
    emit(
      "chat:error",
      "Clipboard already holds Companion's own output (not a Playground reply). Paste it into AI Studio Playground first, then copy Playground's reply before running."
    );
    return;
  }

  parseBtn.disabled = true;
  clipboardParseBtn.disabled = true;
  if (detectRunBtn) detectRunBtn.disabled = true;
  parseBtn.textContent = "Running...";
  clipboardParseBtn.textContent = "Running...";
  let copyOutcome = null;
  let hasErrorOutcome = false;
  state.gatesHiddenDueToError = false;
  state.lastErrorContext = null;
  state.pendingStartedAtMs = Date.now();
  state.liveExec = null;

  try {
    const res = await api.bridgeParse(state.activeSessionId, raw, autoApproveReads);
    state.pendingStartedAtMs = null;
    state.liveExec = null;
    pasteInput.value = "";
    pasteHint.textContent = DEFAULT_PASTE_HINT;
    if (opts.clearPromptOnRun) {
      promptInput.value = "";
      dismissedDetectForText = null;
      emit("composer:updated");
    }
    hideOutputBar();
    setManualPasteOpen(false, false);
    state.activeSession = res.session;
    state.messages = res.messages;
    state.pendingGates = res.pending_gates || [];
    if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;

    if (res.patch_record) {
      emit("backups:refresh");
      emit("git:refresh");
      emit("tree:refresh");
    }
    const cmdResults = Array.isArray(res.command_results)
      ? res.command_results
      : res.command_result
        ? [res.command_result]
        : [];
    for (const cr of cmdResults) {
      emit("terminal:show-result", cr);
    }

    const errorCount = Number(res.error_count || 0);
    const warnings = Array.isArray(res.warnings) ? res.warnings : [];
    const extraWarnings = warnings.slice(errorCount);
    const proj = getActiveProject();

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
    } else if (state.pendingGates.length === 0 && res.generated_md_content) {
      const batchKey = getTrailingToolBatchKey(res.messages);
      if (proj && res.generated_md_path) {
        setCachedGeneratedMd(proj.id, res.generated_md_path, res.generated_md_content);
      }
      if (batchKey) {
        copyOutcome = await tryAutoCopyResult(batchKey, res.generated_md_content);
      }
    } else if (state.pendingGates.length === 0 && res.bridge_reply_text) {
      const batchKey = getTrailingToolBatchKey(res.messages);
      if (batchKey) {
        copyOutcome = await tryAutoCopyResult(batchKey, res.bridge_reply_text);
      }
    } else if (Array.isArray(res.generated_md_files) && res.generated_md_files.length > 0) {
      emit("bridge:generated-md", res.generated_md_files);
    }

    if (res.telemetry) {
      emit("telemetry:updated", res.telemetry);
    }
    emit("session:updated");
    emit("chat:loaded");
    if (extraWarnings.length > 0) {
      emit("chat:error", `Bridge parse warnings:\n${extraWarnings.join("\n")}`);
    }
  } catch (err) {
    state.pendingStartedAtMs = null;
    state.liveExec = null;
    emit("chat:loaded");
    emit("chat:error", err.message);
  } finally {
    state.pendingStartedAtMs = null;
    state.liveExec = null;
    parseBtn.disabled = false;
    clipboardParseBtn.disabled = false;
    if (detectRunBtn) detectRunBtn.disabled = false;
    parseBtn.textContent = "Run & Stage Tools";
    if (copyOutcome === "copied") {
      clipboardParseBtn.textContent = hasErrorOutcome ? "Copied Error \u2713" : "Copied Result \u2713";
      setTimeout(() => {
        clipboardParseBtn.textContent = "Paste & Run AI Reply";
      }, 1800);
    } else if (copyOutcome === "blocked") {
      clipboardParseBtn.textContent = hasErrorOutcome ? "Click Copy Error" : "Click Copy in Balloon";
      setTimeout(() => {
        clipboardParseBtn.textContent = "Paste & Run AI Reply";
      }, 2400);
    } else {
      clipboardParseBtn.textContent = "Paste & Run AI Reply";
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
        const target = a.path || a.old_path || a.target || a.pattern || a.subpath || "";
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
