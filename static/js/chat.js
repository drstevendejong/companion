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

const chatStream = document.getElementById("chat-stream");
const RECENT_COPY_BALLOON_LIMIT = 2;

export function initChat() {
  on("chat:loaded", () => renderMessages({ preserveScroll: false }));
  on("chat:refresh-badges", () => renderMessages({ preserveScroll: true }));
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
      state.sessionActivities[state.activeSessionId].push({
        id: errKey,
        batchKey: errKey,
        kind: "error",
        status: "err",
        title: "Companion encountered an error",
        errorText: text,
        afterMsgIndex: (state.messages || []).length,
        timestamp: new Date().toISOString(),
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
    chatStream.scrollTop = chatStream.scrollHeight;
  });
}

function renderMessages({ preserveScroll = false } = {}) {
  const prevTop = chatStream.scrollTop;
  const wasNearBottom = chatStream.scrollHeight - chatStream.scrollTop - chatStream.clientHeight < 36;

  chatStream.innerHTML = "";
  const activities = (state.activeSessionId && state.sessionActivities[state.activeSessionId]) || [];
  const hasAnything = state.messages.length > 0 || activities.length > 0 || Boolean(state.pendingStatusText);

  if (!hasAnything) {
    chatStream.innerHTML = `
      <div class="empty-state">
        <h3>New Session Ready</h3>
        <p>Write a prompt below, attach a file from the right panel, or click Paste &amp; Run Clipboard.</p>
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

  const sortedActivities = [...activities].sort(
    (a, b) => Number(a.afterMsgIndex ?? Infinity) - Number(b.afterMsgIndex ?? Infinity)
  );
  let actCursor = 0;
  const flushActivitiesUpTo = (count) => {
    while (actCursor < sortedActivities.length) {
      const target = sortedActivities[actCursor].afterMsgIndex;
      if (target !== undefined && Number(target) <= count) {
        renderActivityCard(sortedActivities[actCursor], actCursor === sortedActivities.length - 1);
        actCursor += 1;
      } else {
        break;
      }
    }
  };

  flushActivitiesUpTo(0);
  let renderedMsgCount = 0;

  for (const item of items) {
    if (item.kind === "batch") {
      const keepCopyControls = item.batchIndex >= totalBatches - RECENT_COPY_BALLOON_LIMIT;
      const isLatestBatch = item.batchIndex === totalBatches - 1;
      renderToolBatchCard(item.batch, keepCopyControls, isLatestBatch);
      renderedMsgCount += item.batch.length;
      flushActivitiesUpTo(renderedMsgCount);
      continue;
    }

    const msg = item.msg;
    const card = document.createElement("article");
    card.className = `msg-card ${msg.role === "user" ? "msg-user" : "msg-model"}`;

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
    flushActivitiesUpTo(renderedMsgCount);
  }

  while (actCursor < sortedActivities.length) {
    renderActivityCard(sortedActivities[actCursor], actCursor === sortedActivities.length - 1);
    actCursor += 1;
  }

  if (state.pendingStatusText) {
    const pendingCard = document.createElement("article");
    pendingCard.className = "msg-card msg-exec";
    pendingCard.innerHTML = `
      <div class="msg-header">
        <span class="msg-role">Companion</span>
        <span class="exec-badge is-running">Running</span>
      </div>
      <div class="companion-summary-line">${escapeHtml(state.pendingStatusText)}</div>
    `;
    chatStream.appendChild(pendingCard);
  }

  if (preserveScroll && !wasNearBottom) {
    chatStream.scrollTop = prevTop;
  } else {
    chatStream.scrollTop = chatStream.scrollHeight;
  }
  if (!preserveScroll) {
    emit("gates:changed");
  }
}

function renderToolBatchCard(rawMessages, keepCopyControls, isLatestBatch = false) {
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
    if (resp.written && resp.executed) {
      executedScripts.push(resp);
      if (Array.isArray(resp.generated_md_files)) {
        for (const f of resp.generated_md_files) {
          if (f && f.path && !generatedMdFiles.some((x) => x.path === f.path)) {
            generatedMdFiles.push(f);
          }
        }
      }
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

  let badgeHtml = `<span class="mono tabular">${escapeHtml(sizeStr)} · ~${totalEstTokens.toLocaleString()} tok</span>`;
  let headline = "";

  if (executedScripts.length > 0 && events.length === executedScripts.length) {
    const lastExec = executedScripts[executedScripts.length - 1];
    const ok = lastExec.exit_code === 0 && !lastExec.has_traceback;
    card.classList.add("msg-exec", ok ? "is-ok" : "is-err");
    badgeHtml = `<span class="exec-badge ${ok ? "is-ok" : "is-err"}">${
      ok ? "Executed cleanly (exit 0)" : `Possible errors detected (exit ${lastExec.exit_code})`
    }</span>`;
    headline = `Proposed script executed: ${lastExec.path} (${lastExec.duration_ms ?? 0}ms) · View full output in Console`;
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
    if (execResp.output_preview) {
      const pre = document.createElement("pre");
      pre.className = "exec-preview";
      pre.textContent = execResp.output_preview;
      card.appendChild(pre);
    }
  }

  const proj = getActiveProject();
  if (executedScripts.length > 0 || (hasAppliedPatch && proj && proj.post_patch_command)) {
    const actRow = document.createElement("div");
    actRow.className = "exec-actions";
    if (executedScripts.length > 0) {
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
    if (hasAppliedPatch && proj && proj.post_patch_command) {
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

function renderActivityCard(act, isLatestActivity = false) {
  if (act.kind === "error") {
    const errText = act.errorText || act.title || "Error";
    const card = document.createElement("article");
    card.className = "msg-card msg-companion msg-exec is-err";
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
  const badgeText = act.status === "ok" ? "Validated (exit 0)" : act.status === "err" ? "Possible errors" : "Running";
  const badgeCls = act.status === "ok" ? "is-ok" : act.status === "err" ? "is-err" : "is-running";
  card.innerHTML = `
    <div class="msg-header">
      <span class="msg-role">Companion</span>
      <span class="exec-badge ${badgeCls}">${escapeHtml(badgeText)}</span>
    </div>
    <div class="companion-summary-line">${escapeHtml(act.title)}</div>
    ${act.detail ? `<div class="meta-line mono mt-2">${escapeHtml(act.detail)}</div>` : ""}
  `;
  if (act.preview) {
    const pre = document.createElement("pre");
    pre.className = "exec-preview";
    pre.textContent = act.preview;
    card.appendChild(pre);
  }

  const timeStr = formatTimeHHMMSS(act.timestamp);
  const copyableErr = act.status === "err" ? act.preview || act.detail || act.title || "" : "";
  if (act.showConsoleBtn || copyableErr || timeStr) {
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
      const errBtn = document.createElement("button");
      errBtn.type = "button";
      errBtn.className = "btn btn-danger btn-xs";
      errBtn.textContent = `Copy Error (~${estimateTokensLocal(copyableErr).toLocaleString()} tok)`;
      errBtn.addEventListener("click", async () => {
        try {
          await copyTextToClipboard(copyableErr);
          errBtn.textContent = "Copied Error ✓";
          setTimeout(() => {
            errBtn.textContent = `Copy Error (~${estimateTokensLocal(copyableErr).toLocaleString()} tok)`;
          }, 1600);
        } catch {
          errBtn.textContent = "Clipboard blocked";
        }
      });
      row.appendChild(errBtn);
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
  if (name === "search_code" && resp.matches) return resp.matches.length;
  if ((name === "read_lines" || name === "read_file") && resp.content) return resp.content.length;
  if (resp.output_bytes) return Number(resp.output_bytes);
  return 0;
}

function estimateToolPayloadTokens(name, resp) {
  if (name === "list_tree" && resp.tree) return estimateTokensLocal(resp.tree);
  if (name === "search_code" && resp.matches) return estimateTokensLocal(resp.matches);
  if ((name === "read_lines" || name === "read_file") && resp.content) return estimateTokensLocal(resp.content);
  return 0;
}

function formatSingleToolDetail(name, resp, bytes) {
  const sz = bytes > 0 ? ` (${formatBytes(bytes)})` : "";
  if (resp.denied_by_user) return `${name} — Denied`;
  if (resp.applied) return `${name}: Patched ${resp.path}`;
  if (resp.written) {
    return `${name}: Wrote ${resp.path}${resp.executed ? ` (exit ${resp.exit_code})` : ""}`;
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
  if (name === "search_code") {
    const scope = a.path_glob ? `, ${a.path_glob}` : "";
    return `search_code("${a.pattern || ""}"${scope})`;
  }
  if (name === "read_lines") return `read_lines(${a.path || ""}:${a.start_line ?? 1}-${a.end_line ?? "?"})`;
  if (name === "read_file") return `read_file(${a.path || ""})`;
  if (name === "patch_literal") return `patch_literal(${a.path || ""})`;
  if (name === "patch_lines") return `patch_lines(${a.path || ""}:${a.start_line ?? "?"}-${a.end_line ?? "?"})`;
  if (name === "write_file") return `write_file(${a.path || ""})`;
  if (name === "propose_script") return `propose_script(${a.path || ""})`;
  return name;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
