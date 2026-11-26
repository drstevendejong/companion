// chat.js - Renders chat messages, grouped Companion tool summaries, HH:MM:SS timestamps,
// and persistent clipboard recovery controls on the most recent Companion balloons.

import { api } from "./api.js";
import {
  copyTextToClipboard,
  formatBridgeToolResults,
  getCachedGeneratedMd,
  setCachedGeneratedMd,
} from "./bridge.js";
import { renderMarkdown } from "./markdown.js";
import { emit, estimateTokensLocal, getActiveProject, on, state } from "./state.js";
import { applyGateResponse } from "./tool_gates.js";

const chatStream = document.getElementById("chat-stream");
const RECENT_COPY_BALLOON_LIMIT = 2;
const ARRIVAL_FLASH_MS = 3200;
let pendingScrollRaf = null;
let chatPinnedToBottom = true;
let isRenderingChat = false;
let scoutElapsedTimer = null;
let execElapsedTimer = null;
const seenCardKeysBySession = new Map();
const arrivalStartedAtByKey = new Map();
let lastRenderedSessionId = null;

function applyArrivalFlash(card, cardKey, renderCtx) {
  if (!card || !cardKey || !renderCtx) return;
  const { sid, seen, isBaseline, now } = renderCtx;
  const fullKey = `${sid}:${cardKey}`;
  if (isBaseline) {
    seen.add(cardKey);
    return;
  }
  if (!seen.has(cardKey)) {
    seen.add(cardKey);
    arrivalStartedAtByKey.set(fullKey, now);
  }
  const startedAt = arrivalStartedAtByKey.get(fullKey);
  if (!startedAt) return;
  const elapsed = now - startedAt;
  if (elapsed >= ARRIVAL_FLASH_MS) {
    arrivalStartedAtByKey.delete(fullKey);
    return;
  }
  card.classList.add("is-new-arrival");
  if (elapsed > 0) {
    card.style.animationDelay = `-${elapsed}ms`;
  }
  card.addEventListener(
    "animationend",
    () => {
      card.classList.remove("is-new-arrival");
      card.style.animationDelay = "";
      arrivalStartedAtByKey.delete(fullKey);
    },
    { once: true }
  );
}

function ensureScoutElapsedTimer() {
  if (scoutElapsedTimer !== null) return;
  scoutElapsedTimer = window.setInterval(() => {
    const run = state.liveScoutRun;
    if (!run) {
      clearScoutElapsedTimer();
      return;
    }
    const el = chatStream.querySelector("[data-scout-elapsed]");
    if (el) {
      const secs = Math.max(0, Math.floor((Date.now() - Number(run.started_at_ms || Date.now())) / 1000));
      el.textContent = `${secs}s`;
    }
  }, 1000);
}

function clearScoutElapsedTimer() {
  if (scoutElapsedTimer !== null) {
    window.clearInterval(scoutElapsedTimer);
    scoutElapsedTimer = null;
  }
}

function formatChatExecIdleNote() {
  const exec = state.liveExec;
  if (!exec) return "waiting for output";
  if (exec.phaseText) {
    const phaseStartedAt = Number(exec.phaseStartedAtMs || Date.now());
    const phaseSec = Math.max(0, Math.floor((Date.now() - phaseStartedAt) / 1000));
    return exec.phase === "scout_triage" ? `triaging (${phaseSec}s)` : `summarizing (${phaseSec}s)`;
  }
  const bytes = Number(exec.bytesReceived || (exec.output ? exec.output.length : 0) || 0);
  if (bytes <= 0) return "waiting for output";
  const lastAt = Number(exec.lastOutputAtMs || Date.now());
  const idleSec = Math.max(0, Math.floor((Date.now() - lastAt) / 1000));
  return idleSec >= 2 ? `${bytes.toLocaleString()} B · silent ${idleSec}s` : `${bytes.toLocaleString()} B streaming`;
}

function updateChatExecElapsedDom() {
  const startedAt = Number(
    state.pendingStartedAtMs || (state.liveExec && state.liveExec.startedAtMs) || Date.now()
  );
  const elapsedSec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  for (const el of chatStream.querySelectorAll("[data-exec-elapsed]")) {
    el.textContent = `${elapsedSec}s`;
  }
  const idleNote = ` · ${formatChatExecIdleNote()}`;
  for (const el of chatStream.querySelectorAll("[data-exec-idle]")) {
    el.textContent = idleNote;
  }
  const phaseText = state.liveExec && state.liveExec.phaseText;
  for (const el of chatStream.querySelectorAll("[data-exec-headline]")) {
    el.textContent = phaseText || el.getAttribute("data-default-headline") || el.textContent;
  }
  const badgeText = phaseText
    ? state.liveExec.phase === "scout_triage"
      ? "Triaging"
      : "Summarizing"
    : "Running";
  for (const el of chatStream.querySelectorAll("[data-exec-badge]")) {
    el.textContent = badgeText;
  }
  const hideStop = Boolean(state.liveExec && state.liveExec.done && phaseText);
  for (const btn of chatStream.querySelectorAll("[data-stop-chat-exec]")) {
    btn.classList.toggle("hidden", hideStop);
    if (!hideStop && btn.textContent === "Stop Execution") {
      btn.disabled = false;
    }
  }
}

function ensureExecElapsedTimer() {
  if (execElapsedTimer !== null) return;
  execElapsedTimer = window.setInterval(() => {
    const active = Boolean(
      state.pendingStatusText || (state.liveExec && (!state.liveExec.done || state.liveExec.phaseText))
    );
    if (!active) {
      clearExecElapsedTimer();
      return;
    }
    updateChatExecElapsedDom();
  }, 1000);
}

function clearExecElapsedTimer() {
  if (execElapsedTimer !== null) {
    window.clearInterval(execElapsedTimer);
    execElapsedTimer = null;
  }
}

function scrollChatToBottom({ deferred = true } = {}) {
  chatPinnedToBottom = true;
  chatStream.scrollTop = chatStream.scrollHeight;
  if (!deferred || typeof requestAnimationFrame !== "function") return;
  if (pendingScrollRaf !== null) {
    cancelAnimationFrame(pendingScrollRaf);
  }
  pendingScrollRaf = requestAnimationFrame(() => {
    pendingScrollRaf = null;
    if (chatPinnedToBottom) {
      chatStream.scrollTop = chatStream.scrollHeight;
    }
  });
}

function resolveActivityAfterCount(act, messages) {
  if (act.afterMsgId) {
    const idIdx = messages.findIndex((m) => m && m.id === act.afterMsgId);
    if (idIdx >= 0) return idIdx + 1;
  }
  const tsStr = act.timestamp || act.anchorTimestamp;
  const actMs = tsStr ? Date.parse(tsStr) : NaN;
  if (Number.isFinite(actMs)) {
    for (let i = 0; i < messages.length; i += 1) {
      const msgMs = messages[i] && messages[i].created_at ? Date.parse(messages[i].created_at) : NaN;
      if (Number.isFinite(msgMs) && msgMs > actMs) {
        return i;
      }
    }
    return messages.length;
  }
  if (act.afterMsgId === null) return 0;
  return act.afterMsgIndex !== undefined ? Number(act.afterMsgIndex) : messages.length;
}

export function initChat() {
  chatStream.addEventListener(
    "scroll",
    () => {
      if (isRenderingChat) return;
      const dist = chatStream.scrollHeight - chatStream.scrollTop - chatStream.clientHeight;
      chatPinnedToBottom = dist < 48;
    },
    { passive: true }
  );
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      if (!isRenderingChat && chatPinnedToBottom) {
        chatStream.scrollTop = chatStream.scrollHeight;
      }
    });
    ro.observe(chatStream);
  }

  on("chat:loaded", () => renderMessages({ preserveScroll: false }));
  on("chat:refresh-badges", () => renderMessages({ preserveScroll: true }));
  on("chat:live-exec", updateLiveExecPreview);
  on("scout:stream-event", handleScoutStreamEvent);
  on("chat:error", async (msg) => {
    const text = String(msg || "Unknown error");
    const errKey = `err:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`;
    if (/PAID API KEY REQUIRED/i.test(text)) {
      emit("models:refresh");
    }

    if (state.activeSessionId) {
      if (!state.sessionActivities[state.activeSessionId]) {
        state.sessionActivities[state.activeSessionId] = [];
      }
      const msgs = state.messages || [];
      const errNow = new Date().toISOString();
      state.sessionActivities[state.activeSessionId].push({
        id: errKey,
        batchKey: errKey,
        kind: "error",
        status: "err",
        title: "Companion encountered an error",
        errorText: text,
        afterMsgIndex: msgs.length,
        afterMsgId: msgs.length > 0 ? msgs[msgs.length - 1].id : null,
        anchorTimestamp: errNow,
        timestamp: errNow,
      });
      state.lastErrorContext = {
        sessionId: state.activeSessionId,
        reasonText: text.replace(/^Bridge parse warnings:\s*/i, "").trim(),
      };
      if ((state.pendingGates || []).length > 0) {
        state.gatesHiddenDueToError = true;
      }
      renderMessages({ preserveScroll: false });

      let clipStatus = "copied";
      try {
        await copyTextToClipboard(text);
      } catch {
        clipStatus = "blocked";
      }
      state.lastClipboardInfo = { key: errKey, status: clipStatus };
      emit("chat:refresh-badges");
      return;
    }

    const card = document.createElement("div");
    card.className = "msg-card msg-companion msg-exec is-err";
    card.innerHTML = `
      <div class="msg-header">
        <span class="msg-role">You · Companion</span>
        <span class="exec-badge is-err">Error</span>
      </div>
      <pre class="exec-preview">${escapeHtml(text)}</pre>
    `;
    appendTimestampFooter(card, new Date().toISOString());
    chatStream.appendChild(card);
    scrollChatToBottom({ deferred: true });
  });
}

function renderMessages({ preserveScroll = false } = {}) {
  const prevTop = chatStream.scrollTop;
  const distFromBottom = chatStream.scrollHeight - chatStream.scrollTop - chatStream.clientHeight;
  const wasNearBottom = distFromBottom < 72 || chatPinnedToBottom;
  const shouldStickToBottom = !preserveScroll || wasNearBottom;
  isRenderingChat = true;
  try {
    chatStream.innerHTML = "";
  const sid = state.activeSessionId || "__none__";
  const isBaseline = sid !== lastRenderedSessionId || !seenCardKeysBySession.has(sid);
  let seen = seenCardKeysBySession.get(sid);
  if (!seen) {
    seen = new Set();
    seenCardKeysBySession.set(sid, seen);
  }
  lastRenderedSessionId = state.activeSessionId || null;
  const renderCtx = { sid, seen, isBaseline, now: Date.now() };

  const activities = (state.activeSessionId && state.sessionActivities[state.activeSessionId]) || [];
  const checkpoints = (state.checkpoints || []).filter(
    (cp) => !state.activeSessionId || cp.session_id === state.activeSessionId
  );
  const gateTrayShowingStatus =
    Boolean(state.resolvingGateId) &&
    (state.pendingGates || []).length > 0 &&
    !state.gatesHiddenDueToError;
  const liveExecRunning = Boolean(state.liveExec && (!state.liveExec.done || state.liveExec.phaseText));
  const showChatRunningCard = Boolean((state.pendingStatusText || liveExecRunning) && !gateTrayShowingStatus);
  const hasAnything =
    state.messages.length > 0 ||
    activities.length > 0 ||
    checkpoints.length > 0 ||
    Boolean(state.liveScoutRun) ||
    showChatRunningCard;

  if (!hasAnything) {
    chatStream.innerHTML = `
      <div class="empty-state">
        <h3>New Session Ready</h3>
        <p>Write a prompt below, attach a file from the right panel, or click Paste &amp; Run AI Reply.</p>
      </div>
    `;
    emit("gates:changed");
    return;
  }

  // Group contiguous tool_event blocks first so we know total batch count
  const items = [];
  let totalBatches = 0;
  for (let i = 0; i < state.messages.length; i += 1) {
    const msg = state.messages[i];
    if (msg.role === "tool_event") {
      const batch = [msg];
      while (i + 1 < state.messages.length && state.messages[i + 1].role === "tool_event") {
        i += 1;
        batch.push(state.messages[i]);
      }
      items.push({ kind: "batch", batch, batchIndex: totalBatches });
      totalBatches += 1;
    } else {
      items.push({ kind: "msg", msg });
    }
  }

  const sortedCheckpoints = [...checkpoints].sort(
    (a, b) => Number(a.after_msg_index ?? 0) - Number(b.after_msg_index ?? 0)
  );
  const sortedActivities = activities
    .map((act) => ({
      act,
      resolvedIndex: resolveActivityAfterCount(act, state.messages),
    }))
    .sort((a, b) => {
      if (a.resolvedIndex !== b.resolvedIndex) return a.resolvedIndex - b.resolvedIndex;
      return String(a.act.timestamp || "").localeCompare(String(b.act.timestamp || ""));
    });
  let cpCursor = 0;
  let actCursor = 0;
  const flushInterleavedUpTo = (count) => {
    while (cpCursor < sortedCheckpoints.length) {
      const target = Number(sortedCheckpoints[cpCursor].after_msg_index ?? 0);
      if (target <= count) {
        renderCheckpointCard(sortedCheckpoints[cpCursor], renderCtx);
        cpCursor += 1;
      } else {
        break;
      }
    }
    while (actCursor < sortedActivities.length) {
      const entry = sortedActivities[actCursor];
      if (entry.resolvedIndex <= count) {
        renderActivityCard(entry.act, actCursor === sortedActivities.length - 1, renderCtx);
        actCursor += 1;
      } else {
        break;
      }
    }
  };

  flushInterleavedUpTo(0);
  let renderedMsgCount = 0;

  const latestScriptOutcomeByPath = new Map();
  for (const msg of state.messages) {
    if (msg.role !== "tool_event") continue;
    try {
      const ev = JSON.parse(msg.text);
      const resp = ev && ev.response ? ev.response : {};
      if (
        resp.path &&
        ((resp.written && resp.executed) ||
          ev.tool_name === "run_script" ||
          ev.tool_name === "run_and_delete_script")
      ) {
        const passed = resp.exit_code === 0 && !resp.has_traceback;
        latestScriptOutcomeByPath.set(resp.path, {
          passed,
          deleted: Boolean(resp.deleted),
        });
      }
    } catch {
      // skip malformed
    }
  }

  for (const item of items) {
    if (item.kind === "batch") {
      const keepCopyControls = item.batchIndex >= totalBatches - RECENT_COPY_BALLOON_LIMIT;
      const isLatestBatch = item.batchIndex === totalBatches - 1;
      renderToolBatchCard(
        item.batch,
        keepCopyControls,
        isLatestBatch,
        latestScriptOutcomeByPath,
        renderCtx
      );
      renderedMsgCount += item.batch.length;
      flushInterleavedUpTo(renderedMsgCount);
      continue;
    }

    const msg = item.msg;
    const card = document.createElement("article");
    card.className = `msg-card ${msg.role === "user" ? "msg-user" : "msg-model"}`;
    applyArrivalFlash(card, msg.id ? `msg:${msg.id}` : null, renderCtx);

    const usageStr = msg.token_usage
      ? `In ${msg.token_usage.input_tokens} · Out ${msg.token_usage.output_tokens} · Think ${msg.token_usage.thinking_tokens}`
      : "";

    const header = document.createElement("div");
    header.className = "msg-header";
    header.innerHTML = `
      <span class="msg-role">${msg.role === "user" ? "You" : "Gemini"}</span>
      <span class="mono tabular">${escapeHtml(usageStr)}</span>
    `;
    card.appendChild(header);

    if (msg.attachments && msg.attachments.length > 0) {
      const attWrap = document.createElement("div");
      attWrap.className = "msg-attachments";
      for (const att of msg.attachments) {
        if (att.kind === "image") {
          const img = document.createElement("img");
          img.className = "thumb-img";
          img.src = att.content;
          img.alt = att.label;
          if (shouldStickToBottom) {
            img.addEventListener("load", () => scrollChatToBottom({ deferred: false }), { once: true });
          }
          attWrap.appendChild(img);
        } else {
          const span = document.createElement("span");
          span.className = "meta-line mono";
          span.textContent = `[${att.label} · ~${att.estimated_tokens} tok]`;
          attWrap.appendChild(span);
        }
      }
      card.appendChild(attWrap);
    }

    if (msg.text) {
      const body = document.createElement("div");
      body.className = "msg-body";
      body.innerHTML = msg.role === "model" ? renderMarkdown(msg.text) : escapeHtml(msg.text);
      card.appendChild(body);
    }

    if (msg.tool_calls && msg.tool_calls.length > 0) {
      const tcMeta = document.createElement("div");
      tcMeta.className = "meta-line mono mt-2";
      const summaries = msg.tool_calls.map(formatToolCallSummary);
      tcMeta.textContent = `Requested tools (${summaries.length}): ${summaries.join(" · ")}`;
      tcMeta.title = summaries.join("\n");
      card.appendChild(tcMeta);
    }

    appendTimestampFooter(card, msg.created_at);
    chatStream.appendChild(card);
    renderedMsgCount += 1;
    flushInterleavedUpTo(renderedMsgCount);
  }

  while (cpCursor < sortedCheckpoints.length) {
    renderCheckpointCard(sortedCheckpoints[cpCursor], renderCtx);
    cpCursor += 1;
  }
  while (actCursor < sortedActivities.length) {
    renderActivityCard(sortedActivities[actCursor].act, actCursor === sortedActivities.length - 1, renderCtx);
    actCursor += 1;
  }

  if (state.liveScoutRun) {
    renderLiveScoutCard(state.liveScoutRun);
  } else if (showChatRunningCard) {
    ensureExecElapsedTimer();
    const liveSlice = formatLivePreviewSlice(state.liveExec && state.liveExec.output);
    const defaultHeadline =
      state.pendingStatusText ||
      (state.liveExec && state.liveExec.command
        ? `Running: ${state.liveExec.command}...`
        : "Executing action locally...");
    const phaseText = state.liveExec && state.liveExec.phaseText;
    const runHeadline = phaseText || defaultHeadline;
    const badgeText = phaseText
      ? state.liveExec.phase === "scout_triage"
        ? "Triaging"
        : "Summarizing"
      : "Running";
    const startedAt = Number(
      state.pendingStartedAtMs || (state.liveExec && state.liveExec.startedAtMs) || Date.now()
    );
    const elapsedSec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));

    const pendingCard = document.createElement("article");
    pendingCard.className = "msg-card msg-exec";
    pendingCard.dataset.chatRunningCard = "true";
    pendingCard.innerHTML = `
      <div class="msg-header">
        <span class="msg-role">Companion</span>
        <span class="exec-badge is-running" data-exec-badge="true">${escapeHtml(badgeText)}</span>
      </div>
      <div class="companion-summary-line" data-exec-headline="true" data-default-headline="${escapeHtml(defaultHeadline)}">${escapeHtml(runHeadline)}</div>
      <div class="live-status-row" data-live-status="chat">
        <span class="live-pulse-dot" aria-hidden="true"></span>
        <span class="live-status-meta mono tabular">
          <span data-exec-elapsed>${elapsedSec}s</span><span data-exec-idle> · ${escapeHtml(formatChatExecIdleNote())}</span>
        </span>
      </div>
      <pre data-live-exec="gate" class="exec-preview${liveSlice ? "" : " is-empty"}">${escapeHtml(liveSlice || "(Waiting for stdout / stderr output...)")}</pre>
    `;

    const stopBtn = document.createElement("button");
    stopBtn.type = "button";
    stopBtn.className = "btn btn-danger btn-xs btn-stop-exec";
    stopBtn.setAttribute("data-stop-chat-exec", "true");
    stopBtn.textContent = "Stop Execution";
    stopBtn.title = "Terminate the active subprocess immediately";
    if (state.liveExec && state.liveExec.done && phaseText) {
      stopBtn.classList.add("hidden");
      stopBtn.disabled = true;
    }
    stopBtn.addEventListener("click", async () => {
      stopBtn.disabled = true;
      stopBtn.textContent = "Stopping...";
      const abortCtrl = state.activeAbortController;
      const fallbackTimer = window.setTimeout(() => {
        if (abortCtrl) abortCtrl.abort();
      }, 2500);
      try {
        if (state.liveExec && state.liveExec.source === "console") {
          await api.cancelWorkspaceRun(state.activeSessionId || null, state.liveExec.runId || null);
        } else if (state.activeSessionId) {
          await api.cancelExec(
            state.activeSessionId,
            (state.liveExec && state.liveExec.gateId) || null,
            (state.liveExec && state.liveExec.runId) || null
          );
        }
      } catch {
        if (abortCtrl) abortCtrl.abort();
      } finally {
        window.clearTimeout(fallbackTimer);
      }
    });
    const statusRowEl = pendingCard.querySelector('[data-live-status="chat"]');
    if (statusRowEl) {
      statusRowEl.appendChild(stopBtn);
    }
    chatStream.appendChild(pendingCard);
  } else {
    clearExecElapsedTimer();
  }

  if (!preserveScroll) {
    emit("gates:changed");
  }
  chatPinnedToBottom = shouldStickToBottom;
  if (!shouldStickToBottom) {
    chatStream.scrollTop = prevTop;
  } else {
    scrollChatToBottom({ deferred: true });
  }
  } finally {
    isRenderingChat = false;
  }
}

function isStaticScriptFailureText(text) {
  return /^(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError|NameError|UnboundLocalError)\s*:/m.test(
    String(text || "")
  );
}

function renderToolBatchCard(
  rawMessages,
  keepCopyControls,
  isLatestBatch = false,
  latestScriptOutcomeByPath = null,
  renderCtx = null
) {
  const events = [];
  for (const m of rawMessages) {
    try {
      events.push(JSON.parse(m.text));
    } catch {
      // skip malformed
    }
  }
  if (!events.length) return;

  const lastRaw = rawMessages[rawMessages.length - 1];
  const batchKey = `batch:${lastRaw.id}`;
  let totalBytes = 0;
  let totalEstTokens = 0;
  const toolCounts = new Map();
  const detailLines = [];
  const executedScripts = [];
  const testRuns = [];
  const generatedMdFiles = [];
  const errorEvents = [];
  let hasAppliedPatch = false;

  for (const ev of events) {
    const name = ev.tool_name || "tool";
    const resp = ev.response || {};
    toolCounts.set(name, (toolCounts.get(name) || 0) + 1);

    if (resp.error) {
      errorEvents.push(ev);
    }

    const payloadBytes = estimateToolPayloadBytes(name, resp);
    totalBytes += payloadBytes;
    totalEstTokens += estimateToolPayloadTokens(name, resp);

    if (resp.applied) hasAppliedPatch = true;
    if (
      (resp.written && resp.executed) ||
      ((name === "run_script" || name === "run_and_delete_script") && resp.executed)
    ) {
      executedScripts.push({ ...resp, _toolName: name });
      if (Array.isArray(resp.generated_md_files)) {
        for (const f of resp.generated_md_files) {
          if (f && f.path && !generatedMdFiles.some((x) => x.path === f.path)) {
            generatedMdFiles.push(f);
          }
        }
      }
    }
    if (name === "run_tests" && !resp.denied_by_user && !resp.error) {
      testRuns.push(resp);
    }
    detailLines.push(formatSingleToolDetail(name, resp, payloadBytes));
  }

  const hasErrors = errorEvents.length > 0;
  const nonErrorCount = events.length - errorEvents.length;
  const sizeStr = formatBytes(totalBytes);
  const countStr = Array.from(toolCounts.entries())
    .map(([k, n]) => (n > 1 ? `${k} ×${n}` : k))
    .join(", ");

  const card = document.createElement("article");
  card.className = "msg-card msg-companion";
  applyArrivalFlash(card, batchKey, renderCtx);

  let badgeHtml = `<span class="mono tabular">${escapeHtml(sizeStr)} · ~${totalEstTokens.toLocaleString()} tok</span>`;
  let headline = "";

  if (executedScripts.length > 0 && events.length === executedScripts.length) {
    const lastExec = executedScripts[executedScripts.length - 1];
    const cancelled = Boolean(lastExec.cancelled);
    const ok = !cancelled && lastExec.exit_code === 0 && !lastExec.has_traceback;
    card.classList.add("msg-exec", ok ? "is-ok" : "is-err");
    const badgeLabel = cancelled
      ? `Stopped by user (exit ${lastExec.exit_code ?? -1})`
      : ok
        ? lastExec.deleted
          ? "Executed & auto-deleted (exit 0)"
          : "Executed cleanly (exit 0)"
        : lastExec.kept_for_patching
          ? `Failed — script kept for patching (exit ${lastExec.exit_code})`
          : `Possible errors detected (exit ${lastExec.exit_code})`;
    badgeHtml = `<span class="exec-badge ${ok ? "is-ok" : "is-err"}">${escapeHtml(badgeLabel)}</span>`;
    const autoNote = lastExec.auto_rerun_after_patch
      ? "Auto-reran "
      : lastExec.auto_approved_inspection_script
        ? "Auto-approved "
        : "";
    const prefix = cancelled
      ? "Stopped script"
      : lastExec._toolName === "run_and_delete_script"
        ? lastExec.deleted
          ? `${autoNote}Executed & deleted script`
          : `${autoNote}Script run (kept for patching)`
        : lastExec._toolName === "run_script"
          ? `${autoNote}Executed script`
          : lastExec._toolName === "propose_inspection_script"
            ? lastExec.deleted
              ? `${autoNote}Inspection script executed & deleted`
              : `${autoNote}Inspection script executed`
            : lastExec.deleted
              ? `${autoNote}Proposed script executed & deleted`
              : `${autoNote}Proposed script executed`;
    headline = `${prefix}: ${lastExec.path} (${lastExec.duration_ms ?? 0}ms) · View full output in Console`;
  } else if (testRuns.length > 0 && !hasErrors) {
    const lastTest = testRuns[testRuns.length - 1];
    const cancelled = Boolean(lastTest.cancelled);
    const ok = !cancelled && Boolean(lastTest.passed) && lastTest.exit_code === 0 && !lastTest.has_traceback;
    card.classList.add("msg-exec", ok ? "is-ok" : "is-err");
    badgeHtml = `<span class="exec-badge ${ok ? "is-ok" : "is-err"}">${
      cancelled
        ? "Tests stopped by user"
        : ok
          ? "Tests passed (exit 0)"
          : `Tests failed (exit ${lastTest.exit_code ?? "?"})`
    }</span>`;
    const commitNote = lastTest.checkpoint_committed ? " · Savepoint committed ✓" : "";
    headline =
      events.length === testRuns.length
        ? `${cancelled ? "Stopped tests" : "Executed tests"}: ${lastTest.command || "tests"} (${lastTest.duration_ms ?? 0}ms)${commitNote}`
        : `Executed ${events.length} actions (${countStr})${commitNote}`;
  } else if (hasErrors) {
    card.classList.add("msg-exec", "is-err");
    badgeHtml = `<span class="exec-badge is-err">${
      errorEvents.length === 1 ? "Error" : `Errors (${errorEvents.length})`
    }</span>`;
    if (nonErrorCount === 0) {
      headline = `${errorEvents.length === 1 ? "Tool error" : `${errorEvents.length} tool errors`} (${countStr})`;
    } else {
      headline = `${errorEvents.length} ${errorEvents.length === 1 ? "error" : "errors"} · ${nonErrorCount} completed (${countStr})`;
    }
  } else if (hasAppliedPatch && totalBytes === 0) {
    headline = `Executed ${events.length} local ${events.length === 1 ? "action" : "actions"} (${countStr})`;
  } else {
    headline = `Requested content generated: ${events.length} ${events.length === 1 ? "item" : "items"} (${countStr}) — ${sizeStr}`;
  }

  card.innerHTML = `
    <div class="msg-header">
      <span class="msg-role">You · Companion</span>
      ${badgeHtml}
    </div>
    <div class="companion-summary-line">${escapeHtml(headline)}</div>
  `;

  if (hasErrors) {
    const errPre = document.createElement("pre");
    errPre.className = "exec-preview";
    errPre.textContent = errorEvents
      .map((ev) => {
        const resp = ev.response || {};
        const target = resp.target ? ` (${resp.target})` : "";
        return `${ev.tool_name || "tool"}${target}: ${resp.error}`;
      })
      .join("\n");
    card.appendChild(errPre);
  }

  for (const execResp of executedScripts) {
    if (execResp.ai_summary) {
      const sumDiv = document.createElement("div");
      sumDiv.className = "scout-summary-banner";
      sumDiv.textContent = `Scout Summary${execResp.ai_summary_model ? ` (${execResp.ai_summary_model})` : ""}: ${execResp.ai_summary}`;
      card.appendChild(sumDiv);
    }
    if (execResp.output_preview) {
      const pre = document.createElement("pre");
      pre.className = "exec-preview";
      pre.textContent = execResp.output_preview;
      card.appendChild(pre);
    }
  }
  for (const testResp of testRuns) {
    if (testResp.ai_summary) {
      const sumDiv = document.createElement("div");
      sumDiv.className = "scout-summary-banner";
      sumDiv.textContent = `Scout Summary${testResp.ai_summary_model ? ` (${testResp.ai_summary_model})` : ""}: ${testResp.ai_summary}`;
      card.appendChild(sumDiv);
    }
    if (testResp.output_preview) {
      const pre = document.createElement("pre");
      pre.className = "exec-preview";
      pre.textContent = testResp.output_preview;
      card.appendChild(pre);
    }
  }
  for (const ev of events) {
    const resp = ev.response || {};
    if (ev.tool_name === "scout_workspace" && resp.answer && !resp.error) {
      const sumDiv = document.createElement("div");
      sumDiv.className = "scout-summary-banner";
      const mLabel = resp.model_used ? `${resp.model_used} · ` : "";
      sumDiv.textContent = `Scout Findings (${mLabel}${resp.steps_taken ?? 0} steps):\n${resp.answer}`;
      card.appendChild(sumDiv);
    }
  }

  const proj = getActiveProject();
  const retryableScripts = executedScripts.filter((execResp) => {
    const ok = execResp.exit_code === 0 && !execResp.has_traceback;
    if (ok || execResp.deleted || !execResp.path) return false;
    if (execResp.retryable === false || execResp.failure_kind === "static") return false;
    if (execResp.retryable !== true && isStaticScriptFailureText(execResp.output_preview)) return false;
    if (latestScriptOutcomeByPath && latestScriptOutcomeByPath.has(execResp.path)) {
      const latest = latestScriptOutcomeByPath.get(execResp.path);
      if (latest.passed || latest.deleted) return false;
    }
    return true;
  });

  if (
    executedScripts.length > 0 ||
    testRuns.length > 0 ||
    retryableScripts.length > 0 ||
    (hasAppliedPatch && proj && proj.post_patch_command)
  ) {
    const actRow = document.createElement("div");
    actRow.className = "exec-actions";
    if (executedScripts.length > 0 || testRuns.length > 0) {
      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "btn btn-secondary btn-xs";
      openBtn.textContent = "Open Console Output";
      openBtn.addEventListener("click", () => {
        emit("drawer:ensure-open");
        emit("drawer:switch-tab", "terminal");
      });
      actRow.appendChild(openBtn);
    }
    for (const failExec of retryableScripts) {
      const baseName = String(failExec.path).split(/[\\/]/).pop() || failExec.path;
      const retryBtn = document.createElement("button");
      retryBtn.type = "button";
      retryBtn.className = "btn btn-primary btn-xs";
      retryBtn.disabled = Boolean(state.busy);
      retryBtn.textContent =
        retryableScripts.length > 1 ? `Retry ${baseName}` : "Retry Script";
      retryBtn.title = `Re-run ${failExec.path} after resolving the runtime/environment issue`;
      retryBtn.addEventListener("click", async () => {
        if (!state.activeSessionId || state.busy) return;
        const autoDelete =
          typeof failExec.auto_delete_on_success === "boolean"
            ? failExec.auto_delete_on_success
            : failExec._toolName === "run_and_delete_script" || Boolean(failExec.kept_for_patching);
        state.busy = true;
        state.liveExec = null;
        state.pendingStartedAtMs = Date.now();
        state.pendingStatusText = `Retrying ${failExec.path}...`;
        emit("gates:changed");
        emit("chat:loaded");
        try {
          const res = await api.retryScript({
            session_id: state.activeSessionId,
            path: failExec.path,
            args: failExec.args || null,
            auto_delete: autoDelete,
            tool_name: failExec._toolName || "run_and_delete_script",
          });
          applyGateResponse(res, "retry_script");
        } catch (err) {
          state.busy = false;
          state.pendingStatusText = null;
          state.pendingStartedAtMs = null;
          state.liveExec = null;
          emit("gates:changed");
          emit("chat:loaded");
          emit("chat:error", err.message);
        }
      });
      actRow.appendChild(retryBtn);
    }
    if (hasAppliedPatch && testRuns.length === 0 && proj && proj.post_patch_command) {
      const testBtn = document.createElement("button");
      testBtn.type = "button";
      testBtn.className = "btn btn-primary btn-xs";
      testBtn.textContent = `Run Tests (${proj.post_patch_command})`;
      testBtn.addEventListener("click", () => emit("terminal:run-post-tests"));
      actRow.appendChild(testBtn);
    }
    card.appendChild(actRow);
  }

  if (detailLines.length > 0 && (!hasErrors || nonErrorCount > 0)) {
    const details = document.createElement("details");
    details.className = "tool-batch-details";
    const summary = document.createElement("summary");
    summary.textContent = `View ${detailLines.length} ${detailLines.length === 1 ? "item" : "items"}`;
    const ul = document.createElement("ul");
    ul.className = "tool-batch-list";
    for (const line of detailLines) {
      const li = document.createElement("li");
      li.textContent = line;
      ul.appendChild(li);
    }
    details.append(summary, ul);
    card.appendChild(details);
  }

  const isActiveClip = Boolean(state.lastClipboardInfo && state.lastClipboardInfo.key === batchKey);
  const showCopyButtons = !state.apiEnabled && (keepCopyControls || isActiveClip);
  const payloadText = showCopyButtons ? formatBridgeToolResults(events) : "";
  const pendingCount = (state.pendingGates || []).length;
  const showGateToggle = isLatestBatch && hasErrors && pendingCount > 0;
  const timeStr = formatTimeHHMMSS(lastRaw.created_at);

  if (showGateToggle || (showCopyButtons && (payloadText || generatedMdFiles.length > 0)) || timeStr) {
    const footer = document.createElement("div");
    footer.className =
      showGateToggle || (showCopyButtons && (payloadText || generatedMdFiles.length > 0))
        ? "companion-footer"
        : "msg-footer";

    if (showGateToggle) {
      footer.appendChild(createGateVisibilityButton(pendingCount));
    }

    if (showCopyButtons) {
      for (const mdFile of generatedMdFiles.slice(0, 2)) {
        const mdTok = Number(mdFile.estimated_tokens || 0).toLocaleString();
        const mdBtn = document.createElement("button");
        mdBtn.type = "button";
        mdBtn.className = "btn btn-primary btn-xs";
        mdBtn.textContent = `Copy ${mdFile.path} (~${mdTok} tok)`;
        mdBtn.addEventListener("click", async () => {
          if (!proj) return;
          mdBtn.disabled = true;
          try {
            let content = getCachedGeneratedMd(proj.id, mdFile.path);
            if (!content) {
              const res = await api.bridgeReadMd(proj.id, mdFile.path, state.activeSessionId, false);
              content = res.content;
              setCachedGeneratedMd(proj.id, mdFile.path, content);
            }
            await copyTextToClipboard(content);
            state.lastClipboardInfo = { key: batchKey, status: "copied" };
            emit("chat:refresh-badges");
          } catch (err) {
            emit("chat:error", err.message);
          } finally {
            mdBtn.disabled = false;
          }
        });
        footer.appendChild(mdBtn);
      }

      if (payloadText) {
        const payloadTok = estimateTokensLocal(payloadText).toLocaleString();
        const copyResBtn = document.createElement("button");
        copyResBtn.type = "button";
        copyResBtn.className =
          hasErrors
            ? "btn btn-danger btn-xs"
            : generatedMdFiles.length > 0
              ? "btn btn-secondary btn-xs"
              : "btn btn-primary btn-xs";
        if (hasErrors) {
          copyResBtn.textContent =
            nonErrorCount > 0
              ? `Copy Error & Results (~${payloadTok} tok)`
              : `Copy Error (~${payloadTok} tok)`;
        } else {
          copyResBtn.textContent = `Copy Result (~${payloadTok} tok)`;
        }
        copyResBtn.addEventListener("click", async () => {
          try {
            await copyTextToClipboard(payloadText);
            state.lastClipboardInfo = { key: batchKey, status: "copied" };
            emit("chat:refresh-badges");
          } catch (err) {
            emit("chat:error", err.message);
          }
        });
        footer.appendChild(copyResBtn);
      }

      if (isActiveClip) {
        const badge = document.createElement("span");
        if (state.lastClipboardInfo.status === "copied") {
          badge.className = "exec-badge is-ok";
          badge.textContent = "On Clipboard ✓";
        } else {
          badge.className = "exec-badge is-err";
          badge.textContent = hasErrors
            ? "Click Copy Error for Clipboard"
            : "Click Copy Result for Clipboard";
        }
        footer.appendChild(badge);
      }
    }

    if (timeStr) {
      const tsEl = document.createElement("span");
      tsEl.className = "msg-timestamp mono tabular";
      tsEl.textContent = timeStr;
      footer.appendChild(tsEl);
    }

    card.appendChild(footer);
  }

  chatStream.appendChild(card);
}

function renderActivityCard(act, isLatestActivity = false, renderCtx = null) {
  if (act.kind === "error") {
    const errText = act.errorText || act.title || "Error";
    const card = document.createElement("article");
    card.className = "msg-card msg-companion msg-exec is-err";
    applyArrivalFlash(card, act.id ? `act:${act.id}` : null, renderCtx);
    card.innerHTML = `
      <div class="msg-header">
        <span class="msg-role">You · Companion</span>
        <span class="exec-badge is-err">Error</span>
      </div>
      <div class="companion-summary-line">${escapeHtml(act.title || "Error")}</div>
    `;
    const pre = document.createElement("pre");
    pre.className = "exec-preview";
    pre.textContent = errText;
    card.appendChild(pre);

    const footer = document.createElement("div");
    footer.className = "companion-footer";
    const pendingCount = (state.pendingGates || []).length;
    if (isLatestActivity && pendingCount > 0) {
      footer.appendChild(createGateVisibilityButton(pendingCount));
    }

    const tokEst = estimateTokensLocal(errText).toLocaleString();
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "btn btn-danger btn-xs";
    copyBtn.textContent = `Copy Error (~${tokEst} tok)`;
    copyBtn.addEventListener("click", async () => {
      try {
        await copyTextToClipboard(errText);
        state.lastClipboardInfo = { key: act.batchKey, status: "copied" };
        emit("chat:refresh-badges");
      } catch (err) {
        copyBtn.textContent = "Clipboard blocked";
      }
    });
    footer.appendChild(copyBtn);

    if (state.lastClipboardInfo && state.lastClipboardInfo.key === act.batchKey) {
      const badge = document.createElement("span");
      badge.className = `exec-badge ${state.lastClipboardInfo.status === "copied" ? "is-ok" : "is-err"}`;
      badge.textContent =
        state.lastClipboardInfo.status === "copied"
          ? "On Clipboard ✓"
          : "Click Copy Error for Clipboard";
      footer.appendChild(badge);
    }

    const timeStr = formatTimeHHMMSS(act.timestamp);
    if (timeStr) {
      const tsEl = document.createElement("span");
      tsEl.className = "msg-timestamp mono tabular";
      tsEl.textContent = timeStr;
      footer.appendChild(tsEl);
    }
    card.appendChild(footer);
    chatStream.appendChild(card);
    return;
  }

  const card = document.createElement("article");
  const statusCls = act.status === "ok" ? "is-ok" : act.status === "err" ? "is-err" : "";
  card.className = `msg-card msg-exec ${statusCls}`.trim();
  applyArrivalFlash(card, act.id ? `act:${act.id}:${act.status || "info"}` : null, renderCtx);
  const badgeText = act.status === "ok" ? "Validated (exit 0)" : act.status === "err" ? "Possible errors" : "Running";
  const badgeCls = act.status === "ok" ? "is-ok" : act.status === "err" ? "is-err" : "is-running";
  card.innerHTML = `
    <div class="msg-header">
      <span class="msg-role">Companion</span>
      <span class="exec-badge ${badgeCls}">${escapeHtml(badgeText)}</span>
    </div>
    <div class="companion-summary-line">${escapeHtml(act.title)}</div>
    ${act.detail ? `<div class="meta-line mono mt-2">${escapeHtml(act.detail)}</div>` : ""}
    ${act.hint ? `<div class="meta-line mt-2">${escapeHtml(act.hint)}</div>` : ""}
  `;
  if (act.preview) {
    const pre = document.createElement("pre");
    pre.className = "exec-preview";
    pre.textContent = act.preview;
    card.appendChild(pre);
  } else if (act.status === "running") {
    const liveSlice = formatLivePreviewSlice(state.liveExec && state.liveExec.output);
    const pre = document.createElement("pre");
    pre.className = `exec-preview${liveSlice ? "" : " is-empty"}`;
    pre.dataset.liveActivity = act.id;
    pre.textContent = liveSlice || "(Waiting for stdout / stderr output...)";
    card.appendChild(pre);
  }

  const timeStr = formatTimeHHMMSS(act.timestamp);
  const copyableErr =
    act.status === "err" ? act.copyErrorText || act.preview || act.detail || act.title || "" : "";
  const copyFullText = act.status === "err" ? act.copyFullText || "" : "";
  const actKey = act.batchKey || act.id || null;
  const isActiveClip = Boolean(actKey && state.lastClipboardInfo && state.lastClipboardInfo.key === actKey);

  if (act.showConsoleBtn || copyableErr || copyFullText || isActiveClip || timeStr) {
    const row = document.createElement("div");
    row.className = "exec-actions";
    if (act.showConsoleBtn) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-secondary btn-xs";
      btn.textContent = "Open Console Output";
      btn.addEventListener("click", () => {
        emit("drawer:ensure-open");
        emit("drawer:switch-tab", "terminal");
      });
      row.appendChild(btn);
    }
    if (copyableErr) {
      const errLabel = `Copy Error${copyFullText ? "s" : ""} (~${estimateTokensLocal(copyableErr).toLocaleString()} tok)`;
      const errBtn = document.createElement("button");
      errBtn.type = "button";
      errBtn.className = "btn btn-danger btn-xs";
      errBtn.textContent = errLabel;
      errBtn.addEventListener("click", async () => {
        try {
          await copyTextToClipboard(copyableErr);
          if (actKey) {
            state.lastClipboardInfo = { key: actKey, status: "copied" };
            emit("chat:refresh-badges");
          } else {
            errBtn.textContent = "Copied Error ✓";
            setTimeout(() => {
              errBtn.textContent = errLabel;
            }, 1600);
          }
        } catch {
          errBtn.textContent = "Clipboard blocked";
        }
      });
      row.appendChild(errBtn);
    }
    if (copyFullText) {
      const fullLabel = `Copy Full Output (~${estimateTokensLocal(copyFullText).toLocaleString()} tok)`;
      const fullBtn = document.createElement("button");
      fullBtn.type = "button";
      fullBtn.className = "btn btn-secondary btn-xs";
      fullBtn.textContent = fullLabel;
      fullBtn.addEventListener("click", async () => {
        try {
          await copyTextToClipboard(copyFullText);
          if (actKey) {
            state.lastClipboardInfo = { key: actKey, status: "copied" };
            emit("chat:refresh-badges");
          } else {
            fullBtn.textContent = "Copied Full Output ✓";
            setTimeout(() => {
              fullBtn.textContent = fullLabel;
            }, 1600);
          }
        } catch {
          fullBtn.textContent = "Clipboard blocked";
        }
      });
      row.appendChild(fullBtn);
    }
    if (isActiveClip) {
      const clipBadge = document.createElement("span");
      clipBadge.className = `exec-badge ${state.lastClipboardInfo.status === "copied" ? "is-ok" : "is-err"}`;
      clipBadge.textContent =
        state.lastClipboardInfo.status === "copied" ? "On Clipboard ✓" : "Click Copy Error for Clipboard";
      row.appendChild(clipBadge);
    }
    if (timeStr) {
      const tsEl = document.createElement("span");
      tsEl.className = "msg-timestamp mono tabular";
      tsEl.textContent = timeStr;
      row.appendChild(tsEl);
    }
    card.appendChild(row);
  }
  chatStream.appendChild(card);
}

function renderCheckpointCard(cp, renderCtx = null) {
  const status = cp.status || "active";
  const statusCls = status === "active" ? "is-active" : status === "committed" ? "is-committed" : "is-restored";
  const badgeCls = status === "active" ? "is-ok" : status === "committed" ? "is-committed" : "is-restored";
  const cpCount = Number(cp.files_count || 0);
  const badgeText =
    status === "active"
      ? cpCount > 0
        ? `Saved (${cpCount} ${cpCount === 1 ? "file" : "files"})`
        : "Saved"
      : status === "committed"
        ? "Committed"
        : "Restored";
  const headline =
    status === "active"
      ? cp.summary || "Current state of the project is saved."
      : cp.resolution_note || cp.summary || "Savepoint resolved.";

  const modFiles = Array.isArray(cp.modified_files) ? cp.modified_files : [];
  const createdFiles = Array.isArray(cp.created_files) ? cp.created_files : [];
  const detailBits = [];
  if (modFiles.length > 0) {
    detailBits.push(`Modified (${modFiles.length}): ${modFiles.slice(0, 4).join(", ")}${modFiles.length > 4 ? ", …" : ""}`);
  }
  if (createdFiles.length > 0) {
    detailBits.push(`Created (${createdFiles.length}): ${createdFiles.slice(0, 3).join(", ")}${createdFiles.length > 3 ? ", …" : ""}`);
  }
  const detailText = detailBits.join(" · ");
  const fullTip = [...modFiles.map((f) => `M ${f}`), ...createdFiles.map((f) => `A ${f}`)].join("\n");

  const card = document.createElement("article");
  card.className = `msg-card msg-checkpoint ${statusCls}`;
  applyArrivalFlash(card, cp.id ? `cp:${cp.id}` : null, renderCtx);
  card.innerHTML = `
    <div class="msg-header">
      <span class="msg-role">Companion · Savepoint</span>
      <span class="exec-badge ${badgeCls}">${escapeHtml(badgeText)}</span>
    </div>
    <div class="companion-summary-line">${escapeHtml(headline)}</div>
    ${detailText ? `<div class="meta-line mono mt-2" title="${escapeHtml(fullTip)}">${escapeHtml(detailText)}</div>` : ""}
  `;

  const footer = document.createElement("div");
  footer.className = status === "active" ? "companion-footer" : "msg-footer";

  if (status === "active") {
    const restoreBtn = document.createElement("button");
    restoreBtn.type = "button";
    restoreBtn.className = "btn btn-danger btn-xs";
    restoreBtn.textContent = "Restore";
    restoreBtn.title = "Revert all files edited/created since this savepoint back to their pre-batch state";

    const commitBtn = document.createElement("button");
    commitBtn.type = "button";
    commitBtn.className = "btn btn-secondary btn-xs";
    commitBtn.textContent = "Commit Anyway";
    commitBtn.title = "Keep current project edits and replace this saved checkpoint";

    restoreBtn.addEventListener("click", async () => {
      restoreBtn.disabled = true;
      commitBtn.disabled = true;
      restoreBtn.textContent = "Restoring...";
      try {
        const res = await api.restoreCheckpoint(cp.id);
        if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
        if (Array.isArray(res.pending_gates)) state.pendingGates = res.pending_gates;
        state.gatesHiddenDueToError = false;
        emit("backups:refresh");
        emit("git:refresh");
        emit("tree:refresh");
        emit("session:updated");
        emit("chat:loaded");
      } catch (err) {
        restoreBtn.disabled = false;
        commitBtn.disabled = false;
        restoreBtn.textContent = "Restore";
        emit("chat:error", err.message);
      }
    });

    commitBtn.addEventListener("click", async () => {
      restoreBtn.disabled = true;
      commitBtn.disabled = true;
      commitBtn.textContent = "Committing...";
      try {
        const res = await api.commitCheckpoint(cp.id);
        if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
        emit("chat:loaded");
      } catch (err) {
        restoreBtn.disabled = false;
        commitBtn.disabled = false;
        commitBtn.textContent = "Commit Anyway";
        emit("chat:error", err.message);
      }
    });

    footer.append(restoreBtn, commitBtn);
  }

  const timeStr = formatTimeHHMMSS(cp.resolved_at || cp.created_at);
  if (timeStr) {
    const tsEl = document.createElement("span");
    tsEl.className = "msg-timestamp mono tabular";
    tsEl.textContent = timeStr;
    footer.appendChild(tsEl);
  }
  card.appendChild(footer);
  chatStream.appendChild(card);
}

function createGateVisibilityButton(pendingCount) {
  const gateLabel = pendingCount === 1 ? "Pending Gate" : `Pending Gates (${pendingCount})`;
  const btn = document.createElement("button");
  btn.type = "button";
  if (state.gatesHiddenDueToError) {
    btn.className = "btn btn-secondary btn-xs";
    btn.textContent = `Inspect ${gateLabel}`;
    btn.title = "Open the hidden pending gate(s) to inspect, Approve, or Deny anyway";
    btn.addEventListener("click", () => {
      state.gatesHiddenDueToError = false;
      emit("gates:changed");
      emit("chat:refresh-badges");
    });
  } else {
    btn.className = "btn btn-ghost btn-xs";
    btn.textContent = `Hide ${gateLabel}`;
    btn.addEventListener("click", () => {
      state.gatesHiddenDueToError = true;
      emit("gates:changed");
      emit("chat:refresh-badges");
    });
  }
  return btn;
}

function appendTimestampFooter(card, isoStr) {
  const timeStr = formatTimeHHMMSS(isoStr);
  if (!timeStr) return;
  const footer = document.createElement("div");
  footer.className = "msg-footer";
  const tsEl = document.createElement("span");
  tsEl.className = "msg-timestamp mono tabular";
  tsEl.textContent = timeStr;
  footer.appendChild(tsEl);
  card.appendChild(footer);
}

function formatTimeHHMMSS(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return "";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function estimateToolPayloadBytes(name, resp) {
  if (name === "list_tree" && resp.tree) return resp.tree.length;
  if (name === "extract_headers" && resp.formatted) return resp.formatted.length;
  if (name === "search_code" && resp.matches) return resp.matches.length;
  if ((name === "read_lines" || name === "read_file" || name === "read_command_output") && resp.content) {
    return resp.content.length;
  }
  if (name === "scout_workspace" && resp.answer) return resp.answer.length;
  if (resp.output_bytes) return Number(resp.output_bytes);
  return 0;
}

function estimateToolPayloadTokens(name, resp) {
  if (name === "list_tree" && resp.tree) return estimateTokensLocal(resp.tree);
  if (name === "extract_headers" && resp.formatted) return estimateTokensLocal(resp.formatted);
  if (name === "search_code" && resp.matches) return estimateTokensLocal(resp.matches);
  if ((name === "read_lines" || name === "read_file" || name === "read_command_output") && resp.content) {
    return estimateTokensLocal(resp.content);
  }
  if (name === "scout_workspace" && resp.answer) return estimateTokensLocal(resp.answer);
  return 0;
}

function formatSingleToolDetail(name, resp, bytes) {
  const sz = bytes > 0 ? ` (${formatBytes(bytes)})` : "";
  if (resp.denied_by_user) return `${name} — Denied`;
  if (resp.applied) return `${name}: Patched ${resp.path}`;
  if (resp.renamed) return `${name}: Renamed ${resp.old_path} -> ${resp.new_path}`;
  if (name === "extract_headers" && !resp.error) {
    return `extract_headers(${resp.pattern || "."}) — ${resp.file_count ?? 0} files, ${resp.header_count ?? 0} headers${sz}`;
  }
  if (name === "read_command_output" && !resp.error) {
    if (!resp.found) return `read_command_output(${resp.exec_id || "latest"}) — Not found`;
    return `read_command_output(${resp.exec_id || "latest"}) — ${resp.shown_lines ?? "?"}/${resp.total_lines ?? "?"} lines${sz}`;
  }
  if (name === "scout_workspace" && !resp.error) {
    const hopNote = Array.isArray(resp.models_hopped) && resp.models_hopped.length ? ` · hopped ${resp.models_hopped.join("→")}` : "";
    const stopNote = resp.stopped_early ? " · stopped early" : "";
    return `scout_workspace(${resp.model_used || "scout"}) — ${resp.steps_taken ?? 0} steps, ~${Number(resp.tokens_used || 0).toLocaleString()} tok${hopNote}${stopNote}`;
  }
  if (name === "update_header" && !resp.error) {
    const n = Array.isArray(resp.staged_paths) ? resp.staged_paths.length : (resp.staged_count ?? 0);
    return `update_header — Drafted ${n} human-gated header patch(es)`;
  }
  if (resp.written) {
    const delSuffix = resp.deleted ? ", auto-deleted" : "";
    const statusSuffix = resp.cancelled ? "stopped by user" : `exit ${resp.exit_code}${delSuffix}`;
    return `${name}: Wrote ${resp.path}${resp.executed ? ` (${statusSuffix})` : ""}`;
  }
  if ((name === "run_script" || name === "run_and_delete_script") && !resp.error) {
    const statusText = resp.cancelled
      ? "STOPPED BY USER"
      : resp.passed
        ? resp.deleted
          ? "OK & auto-deleted"
          : "OK"
        : resp.kept_for_patching
          ? "FAILED (kept on disk)"
          : "FAILED";
    return `${name}(${resp.path || resp.command || "script"}) — ${statusText} (exit ${resp.exit_code ?? "?"})`;
  }
  if (resp.deleted) return `${name}: Deleted ${resp.path}`;
  if (name === "run_tests" && !resp.error) {
    const statusText = resp.cancelled ? "STOPPED BY USER" : resp.passed ? "PASSED" : "FAILED";
    return `${name}(${resp.command || "tests"}) — ${statusText} (exit ${resp.exit_code ?? "?"})`;
  }
  if (resp.path) return `${name}(${resp.path})${sz}`;
  if (resp.pattern !== undefined) return `${name}("${resp.pattern}") — ${resp.total_hits ?? 0} hits${sz}`;
  if (resp.subpath !== undefined) return `${name}(${resp.subpath})${sz}`;
  if (resp.error) return `${name} — Error: ${resp.error}`;
  return `${name} — Completed${sz}`;
}

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

function formatToolCallSummary(tc) {
  const name = tc.name || "tool";
  const a = tc.args || {};
  if (name === "list_tree") return `list_tree(${a.subpath || "."}, depth=${a.max_depth ?? 3})`;
  if (name === "extract_headers") return `extract_headers(${a.pattern || "."})`;
  if (name === "read_command_output") {
    return `read_command_output(${a.exec_id || "latest"}${a.pattern ? `, "${a.pattern}"` : ""})`;
  }
  if (name === "scout_workspace") return `scout_workspace(${a.subpath || "."}, "${String(a.goal || "").slice(0, 48)}")`;
  if (name === "update_header") {
    const pList = Array.isArray(a.paths) ? a.paths.join(", ") : (a.paths || a.path || "");
    return `update_header(${pList})`;
  }
  if (name === "search_code") {
    const scope = [a.subpath, a.path_glob, a.start_line || a.end_line ? `L${a.start_line || 1}-${a.end_line || ""}` : ""]
      .filter(Boolean)
      .join(", ");
    return `search_code("${a.pattern || ""}"${scope ? `, ${scope}` : ""})`;
  }
  if (name === "read_lines") return `read_lines(${a.path || ""}:${a.start_line ?? 1}-${a.end_line ?? "?"})`;
  if (name === "read_file") return `read_file(${a.path || ""})`;
  if (name === "patch_literal") return `patch_literal(${a.path || ""})`;
  if (name === "patch_lines") return `patch_lines(${a.path || ""}:${a.start_line ?? "?"}-${a.end_line ?? "?"})`;
  if (name === "write_file") return `write_file(${a.path || ""})`;
  if (name === "delete_file") return `delete_file(${a.path || ""})`;
  if (name === "rename_file") return `rename_file(${a.old_path || ""} -> ${a.new_path || ""})`;
  if (name === "propose_inspection_script" || name === "propose_script") return `${name}(${a.path || ""})`;
  if (name === "run_script") return `run_script(${a.path || ""}${a.args ? `, ${a.args}` : ""})`;
  if (name === "run_and_delete_script") {
    return `run_and_delete_script(${a.path || ""}${a.args ? `, ${a.args}` : ""})`;
  }
  if (name === "run_tests") return a.summary ? `run_tests(${a.summary})` : "run_tests";
  return name;
}

function handleScoutStreamEvent(msg) {
  if (!msg) return;
  if (state.activeSessionId && msg.session_id && msg.session_id !== state.activeSessionId) {
    return;
  }
  if (msg.type === "scout_start") {
    state.liveScoutRun = {
      run_id: msg.run_id,
      session_id: msg.session_id,
      goal: msg.goal || "",
      subpath: msg.subpath || ".",
      model: msg.model || "lite",
      max_steps: Number(msg.max_steps || 8),
      max_tokens: Number(msg.max_tokens || 30000),
      active_step: 0,
      phase: "Starting Local AI Scout...",
      started_at_ms: Date.now(),
      steps: [],
      hops: [],
      paused: false,
      stopped: false,
      tokens_used: 0,
    };
    ensureScoutElapsedTimer();
    emit("chat:refresh-badges");
    return;
  }
  if (!state.liveScoutRun || (msg.run_id && state.liveScoutRun.run_id !== msg.run_id)) {
    return;
  }
  if (msg.type === "scout_phase") {
    if (msg.model) state.liveScoutRun.model = msg.model;
    if (msg.tokens_used !== undefined) state.liveScoutRun.tokens_used = Number(msg.tokens_used || 0);
    if (msg.active_step !== undefined) state.liveScoutRun.active_step = Number(msg.active_step || 0);
    if (msg.max_steps) state.liveScoutRun.max_steps = Number(msg.max_steps);
    if (msg.phase) state.liveScoutRun.phase = String(msg.phase);
    emit("chat:refresh-badges");
  } else if (msg.type === "scout_step") {
    if (msg.model) state.liveScoutRun.model = msg.model;
    if (msg.tokens_used !== undefined) state.liveScoutRun.tokens_used = Number(msg.tokens_used || 0);
    if (msg.step !== undefined) state.liveScoutRun.active_step = Number(msg.step || 0);
    state.liveScoutRun.phase = `${msg.tool || "read"}: ${msg.summary || "done"}`;
    state.liveScoutRun.steps.push({
      step: msg.step,
      max_steps: msg.max_steps || state.liveScoutRun.max_steps,
      tool: msg.tool || "read",
      summary: msg.summary || "",
      status: msg.status || "ok",
    });
    emit("chat:refresh-badges");
  } else if (msg.type === "scout_hop") {
    state.liveScoutRun.model = msg.to_model || state.liveScoutRun.model;
    state.liveScoutRun.phase = `Switched model → ${state.liveScoutRun.model}`;
    state.liveScoutRun.hops.push({
      from_model: msg.from_model,
      to_model: msg.to_model,
      reason: msg.reason || "",
    });
    emit("chat:refresh-badges");
  } else if (msg.type === "scout_paused") {
    state.liveScoutRun.paused = Boolean(msg.paused);
    state.liveScoutRun.phase = state.liveScoutRun.paused ? "Paused by user" : "Resumed";
    emit("chat:refresh-badges");
  } else if (msg.type === "scout_done") {
    state.liveScoutRun = null;
    clearScoutElapsedTimer();
    emit("chat:refresh-badges");
    emit("telemetry:refresh");
  }
}

function renderLiveScoutCard(run) {
  ensureScoutElapsedTimer();
  const card = document.createElement("article");
  card.className = "msg-card scout-live-card";
  const highestRecordedStep = run.steps.reduce((acc, s) => Math.max(acc, Number(s.step || 0)), 0);
  const displayStep = Math.max(highestRecordedStep, Number(run.active_step || 0));
  const statusLabel = run.stopped
    ? "Stopping..."
    : run.paused
      ? "Paused"
      : `Step ${displayStep}/${run.max_steps}`;
  const badgeCls = run.paused ? "is-restored" : "is-running";
  const elapsedSec = Math.max(0, Math.floor((Date.now() - Number(run.started_at_ms || Date.now())) / 1000));

  card.innerHTML = `
    <div class="msg-header">
      <span class="msg-role">Local AI Scout · ${escapeHtml(run.model)}</span>
      <span class="exec-badge ${badgeCls}">${escapeHtml(statusLabel)}</span>
    </div>
    <div class="companion-summary-line">${escapeHtml(run.goal || "Investigating workspace...")}</div>
    <div class="meta-line mono tabular mt-2">Scope: ${escapeHtml(run.subpath || ".")} · ~${Number(run.tokens_used || 0).toLocaleString()} / ${Number(run.max_tokens || 30000).toLocaleString()} tok · <span data-scout-elapsed>${elapsedSec}s</span></div>
    <div class="meta-line mono mt-1" data-scout-phase>${escapeHtml(run.phase || "Working...")}</div>
  `;

  if (run.hops && run.hops.length > 0) {
    const lastHop = run.hops[run.hops.length - 1];
    const hopEl = document.createElement("div");
    hopEl.className = "scout-hop-note";
    hopEl.textContent = `Model hop: ${lastHop.from_model} → ${lastHop.to_model}${lastHop.reason ? ` (${lastHop.reason})` : ""}`;
    card.appendChild(hopEl);
  }

  if (run.steps && run.steps.length > 0) {
    const list = document.createElement("div");
    list.className = "scout-steps-list";
    for (const s of run.steps) {
      const row = document.createElement("div");
      row.className = "scout-step-item";
      row.innerHTML = `<span class="scout-step-tool">[${s.step}/${s.max_steps}] ${escapeHtml(s.tool)}</span><span>${escapeHtml(s.summary)}</span>`;
      list.appendChild(row);
    }
    card.appendChild(list);
    list.scrollTop = list.scrollHeight;
  }

  const actions = document.createElement("div");
  actions.className = "exec-actions";

  const pauseBtn = document.createElement("button");
  pauseBtn.type = "button";
  pauseBtn.className = "btn btn-secondary btn-xs";
  pauseBtn.textContent = run.paused ? "Resume Scout" : "Pause";
  pauseBtn.disabled = Boolean(run.stopped);
  pauseBtn.addEventListener("click", async () => {
    pauseBtn.disabled = true;
    try {
      if (run.paused) {
        await api.resumeScoutRun(run.run_id);
        run.paused = false;
      } else {
        await api.pauseScoutRun(run.run_id);
        run.paused = true;
      }
      emit("chat:refresh-badges");
    } catch (err) {
      emit("chat:error", err.message);
    } finally {
      pauseBtn.disabled = false;
    }
  });

  const stopBtn = document.createElement("button");
  stopBtn.type = "button";
  stopBtn.className = "btn btn-danger btn-xs";
  stopBtn.textContent = "Stop (Keep Findings)";
  stopBtn.disabled = Boolean(run.stopped);
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    run.stopped = true;
    try {
      await api.stopScoutRun(run.run_id);
      emit("chat:refresh-badges");
    } catch (err) {
      run.stopped = false;
      emit("chat:error", err.message);
    }
  });

  actions.append(pauseBtn, stopBtn);
  card.appendChild(actions);
  chatStream.appendChild(card);
}

function formatLivePreviewSlice(rawOutput) {
  const text = String(rawOutput || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!text.trim()) return "";
  return text.length > 2500 ? `...\n${text.slice(-2500)}` : text;
}

function updateLiveExecPreview(payload) {
  const gateTrayShowingStatus =
    Boolean(state.resolvingGateId) &&
    (state.pendingGates || []).length > 0 &&
    !state.gatesHiddenDueToError;
  if (
    payload &&
    (payload.type === "start" || (payload.type === "phase" && payload.phaseText)) &&
    !gateTrayShowingStatus &&
    !chatStream.querySelector("[data-chat-running-card], [data-live-activity]")
  ) {
    renderMessages({ preserveScroll: true });
    return;
  }
  if (
    payload &&
    payload.type === "phase" &&
    !payload.phaseText &&
    !state.pendingStatusText &&
    (!state.liveExec || state.liveExec.done) &&
    chatStream.querySelector("[data-chat-running-card]")
  ) {
    renderMessages({ preserveScroll: true });
    return;
  }

  updateChatExecElapsedDom();
  const slice = formatLivePreviewSlice(payload && payload.output);
  const targets = Array.from(document.querySelectorAll("[data-live-exec], [data-live-activity]"));
  const gatesEl = document.getElementById("pending-gates");

  const wasNearBottom = chatStream.scrollHeight - chatStream.scrollTop - chatStream.clientHeight < 72;
  for (const el of targets) {
    if (slice) {
      const wasHidden = el.classList.contains("hidden");
      el.classList.remove("hidden", "is-empty");
      el.textContent = slice;
      el.scrollTop = el.scrollHeight;
      if (wasHidden && gatesEl && gatesEl.contains(el) && typeof el.scrollIntoView === "function") {
        el.scrollIntoView({ block: "nearest" });
      }
    } else if (payload && payload.type === "start") {
      el.classList.remove("hidden");
      el.classList.add("is-empty");
      el.textContent = "(Waiting for stdout / stderr output...)";
    }
  }
  if (wasNearBottom) {
    scrollChatToBottom({ deferred: false });
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
