// drawer.js - Right panel: Project File Tree, Console Runner (+ Paste into Prompt), Savepoint Commit, and Backups/Undo

import { api } from "./api.js";
import { copyTextToClipboard } from "./bridge.js";
import { emit, getActiveProject, on, state } from "./state.js";

const tabButtons = document.querySelectorAll(".drawer-tab");
const panes = {
  files: document.getElementById("tab-files"),
  terminal: document.getElementById("tab-terminal"),
  git: document.getElementById("tab-git"),
  backups: document.getElementById("tab-backups"),
};

const fileTreeList = document.getElementById("file-tree-list");
const fileTreeFilter = document.getElementById("file-tree-filter");
const customCmdInput = document.getElementById("custom-cmd-input");
const terminalMeta = document.getElementById("terminal-meta");
const terminalOutput = document.getElementById("terminal-output");
const pasteTermBtn = document.getElementById("btn-paste-terminal");
const terminalSavepointBar = document.getElementById("terminal-savepoint-bar");
const terminalSavepointLabel = document.getElementById("terminal-savepoint-label");
const commitCpBtn = document.getElementById("btn-terminal-commit-checkpoint");
const restoreCpBtn = document.getElementById("btn-terminal-restore-checkpoint");
const postTestsBtn = document.getElementById("btn-run-post-tests");
const backupList = document.getElementById("backup-list");

const expandedDirs = new Set();
const dirCache = new Map();
let lastManualCommandPassed = false;
let lastManualSessionId = null;

export function initDrawer() {
  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.getElementById("btn-refresh-tree").addEventListener("click", () => {
    dirCache.clear();
    refreshTree();
  });
  document.getElementById("btn-copy-tree").addEventListener("click", handleCopyTree);
  document.getElementById("btn-pack-filtered").addEventListener("click", handlePackFiltered);
  fileTreeFilter.addEventListener("input", renderTreeFromCache);
  document.getElementById("btn-run-custom").addEventListener("click", () => {
    const cmd = customCmdInput.value.trim();
    if (cmd) runTerminalCommand(cmd);
  });
  customCmdInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      const cmd = customCmdInput.value.trim();
      if (cmd) runTerminalCommand(cmd);
    }
  });

  postTestsBtn.addEventListener("click", triggerPostPatchTests);

  pasteTermBtn.addEventListener("click", () => {
    if (!state.lastTerminalOutput) return;
    emit("composer:paste-text", `[Console: ${state.lastTerminalLabel}]\n${state.lastTerminalOutput}`);
  });

  commitCpBtn.addEventListener("click", handleTerminalCommitCheckpoint);
  restoreCpBtn.addEventListener("click", handleTerminalRestoreCheckpoint);

  document.getElementById("btn-refresh-backups").addEventListener("click", refreshBackups);

  on("project:changed", () => {
    const proj = getActiveProject();
    if (proj && proj.post_patch_command) {
      customCmdInput.value = proj.post_patch_command;
    }
    expandedDirs.clear();
    dirCache.clear();
    refreshTree();
    refreshBackups();
  });

  on("backups:refresh", refreshBackups);
  on("tree:refresh", () => {
    dirCache.clear();
    refreshTree();
  });
  on("drawer:switch-tab", (tabName) => {
    if (tabName && panes[tabName]) switchTab(tabName);
  });
  on("chat:loaded", syncTerminalSavepointBar);
  on("session:updated", syncTerminalSavepointBar);
  on("terminal:run-post-tests", triggerPostPatchTests);
  on("terminal:show-result", (res) => {
    // Update Console contents silently so split-screen collapsed drawer isn't forced open unnecessarily
    showTerminalResult(res, { suppressMdBar: true });
  });
  on("terminal:stream-event", handleExecStreamEvent);
  on("terminal:suggest-post-tests", (cmd) => {
    customCmdInput.value = cmd;
    terminalMeta.textContent = `Patch applied · Click Run Tests (${cmd})`;
    syncTerminalSavepointBar();
  });

  syncTerminalSavepointBar();
}

function triggerPostPatchTests() {
  const proj = getActiveProject();
  if (!proj || !proj.post_patch_command) {
    emit("chat:error", "No Post-Patch Test Command configured. Click Edit next to Active Project to set one.");
    return;
  }
  runTerminalCommand(proj.post_patch_command, { reportInChat: true, label: "tests", isTestRun: true });
}

export function switchTab(tabName) {
  tabButtons.forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  Object.entries(panes).forEach(([k, el]) => el.classList.toggle("active", k === tabName));
}

async function refreshTree() {
  const proj = getActiveProject();
  if (!proj) {
    fileTreeList.innerHTML = `<div class="meta-line">No project selected</div>`;
    return;
  }
  try {
    const res = await api.getTree(proj.id, ".", 0);
    dirCache.set(".", res.entries || []);
    renderTreeFromCache();
  } catch (err) {
    fileTreeList.innerHTML = `<div class="meta-line">${escapeHtml(err.message)}</div>`;
  }
}

async function toggleDirectory(projId, dirPath) {
  if (expandedDirs.has(dirPath)) {
    expandedDirs.delete(dirPath);
    renderTreeFromCache();
    return;
  }
  expandedDirs.add(dirPath);
  if (!dirCache.has(dirPath)) {
    const res = await api.getTree(projId, dirPath, 0);
    dirCache.set(dirPath, res.entries || []);
  }
  renderTreeFromCache();
}

function renderTreeFromCache() {
  const proj = getActiveProject();
  if (!proj) return;
  const filter = (fileTreeFilter.value || "").trim().toLowerCase();
  fileTreeList.innerHTML = "";
  renderFolderChildren(proj.id, ".", 0, filter);
}

function renderFolderChildren(projId, folderPath, depth, filter) {
  const entries = dirCache.get(folderPath) || [];
  for (const entry of entries) {
    const matchesFilter = !filter || entry.path.toLowerCase().includes(filter);
    const isExpanded = expandedDirs.has(entry.path);

    if (entry.is_dir) {
      if (matchesFilter || isExpanded) {
        const row = document.createElement("div");
        row.className = "tree-row";
        row.style.paddingLeft = `${6 + depth * 14}px`;

        const dirBtn = document.createElement("button");
        dirBtn.type = "button";
        dirBtn.className = "tree-dir-btn";
        dirBtn.title = entry.path;
        dirBtn.innerHTML = `<span class="tree-caret">${isExpanded ? "▾" : "▸"}</span><span class="tree-name">${escapeHtml(entry.name)}/</span>`;
        dirBtn.addEventListener("click", () => toggleDirectory(projId, entry.path));
        row.appendChild(dirBtn);
        fileTreeList.appendChild(row);
      }
      if (isExpanded) {
        renderFolderChildren(projId, entry.path, depth + 1, filter);
      }
    } else if (matchesFilter) {
      const row = document.createElement("div");
      row.className = "tree-row";
      row.style.paddingLeft = `${20 + depth * 14}px`;

      const label = document.createElement("span");
      label.className = "mono tree-name";
      label.title = entry.path;
      label.textContent = entry.name;
      row.appendChild(label);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-secondary btn-xs tree-attach-btn";
      btn.textContent = "Attach";
      btn.addEventListener("click", async () => {
        try {
          const fileData = await api.previewFile(projId, entry.path);
          emit("composer:attach", {
            kind: "file",
            label: `${entry.path} (${fileData.total_lines}L)`,
            content: fileData.content,
            path: entry.path,
            estimated_tokens: fileData.estimated_tokens,
          });
        } catch (err) {
          emit("chat:error", err.message);
        }
      });
      row.appendChild(btn);
      fileTreeList.appendChild(row);
    }
  }
}

const FAILURE_LINE_RE =
  /(?:^\s*\[?\s*(?:ERROR|FAIL|FAILED|FATAL|CRITICAL|PANIC)\s*\]?(?:[:\s-]|$)|^\s*(?:={12,}|--- FAIL:|E\s{2,}\S|[✖✗✘×❌]\s)|\b(?:Traceback \(most recent call last\):|(?:Assertion|Syntax|Type|Reference|Value|Key|Attribute|Import|ModuleNotFound|Runtime|Name|Index|Indentation|ZeroDivision|OSError|FileNotFound|Permission)Error\b|UnhandledPromiseRejection)|(?::\d+(?::\d+)?|\(\d+(?:,\s*\d+)?\)):\s*(?:error|fatal)\b|\berror(?:\[E\d+\]|\s+TS\d+):|\b[1-9]\d*\s+(?:failed|errors?|failures?)\b)/i;

function extractFailureExcerpt(rawOutput, maxChars = 4500) {
  const normalized = String(rawOutput || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!normalized) {
    return { excerpt: "", matchedErrors: false, isFull: true };
  }
  if (normalized.length <= maxChars) {
    const hasMatch = normalized.split("\n").some((l) => FAILURE_LINE_RE.test(l));
    return { excerpt: normalized, matchedErrors: hasMatch, isFull: true };
  }

  const lines = normalized.split("\n");
  const n = lines.length;
  const ranges = [];
  for (let idx = 0; idx < n; idx += 1) {
    if (FAILURE_LINE_RE.test(lines[idx])) {
      const start = Math.max(0, idx - 1);
      const end = Math.min(n, idx + 11);
      if (ranges.length > 0 && start <= ranges[ranges.length - 1][1] + 1) {
        ranges[ranges.length - 1][1] = Math.max(ranges[ranges.length - 1][1], end);
      } else {
        ranges.push([start, end]);
      }
    }
  }

  if (ranges.length > 0) {
    const tailStart = Math.max(0, n - 8);
    if (tailStart > ranges[ranges.length - 1][1] + 1) {
      ranges.push([tailStart, n]);
    } else {
      ranges[ranges.length - 1][1] = n;
    }

    const chunks = [];
    let totalLen = 0;
    for (const [sIdx, eIdx] of ranges) {
      const block = lines.slice(sIdx, eIdx).join("\n");
      const sep = chunks.length > 0 ? "\n...\n" : "";
      if (totalLen + sep.length + block.length > maxChars) {
        const remaining = maxChars - totalLen - sep.length;
        if (remaining > 120) {
          chunks.push(`${sep}${block.slice(0, remaining)}\n...[truncated]`);
        }
        break;
      }
      chunks.push(`${sep}${block}`);
      totalLen += sep.length + block.length;
    }
    const combined = chunks.join("").trim();
    if (combined) {
      return { excerpt: combined, matchedErrors: true, isFull: false };
    }
  }

  let tail = lines.slice(-50).join("\n");
  if (tail.length > maxChars) {
    tail = `...[truncated]\n${tail.slice(-maxChars)}`;
  }
  return { excerpt: tail, matchedErrors: false, isFull: false };
}

function buildCommandFailureReport(command, exitCode, rawOutput) {
  const normalized = String(rawOutput || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  const header = `## Local Test / Command Failure (\`${command}\` · exit code ${exitCode})`;

  if (!normalized) {
    return {
      preview: "(No output produced)",
      copyErrorText: `${header}\n\nCommand exited with code ${exitCode} and produced no stdout/stderr output.`,
      copyFullText: "",
      matchedErrors: false,
      hint: "Command produced no output.",
    };
  }

  const uiSlice = extractFailureExcerpt(normalized, 1800);
  const fullCopyFormatted = `## Local Test / Command Output (\`${command}\` · exit code ${exitCode})\n\n\`\`\`text\n${normalized}\n\`\`\``;

  if (normalized.length <= 4000) {
    return {
      preview: uiSlice.excerpt,
      copyErrorText: `${header}\n\n\`\`\`text\n${normalized}\n\`\`\``,
      copyFullText: "",
      matchedErrors: uiSlice.matchedErrors,
      hint: "",
    };
  }

  const copySlice = extractFailureExcerpt(normalized, 5000);
  if (copySlice.matchedErrors) {
    return {
      preview: uiSlice.excerpt,
      copyErrorText: `${header}\n\n### Extracted Error Output\n\`\`\`text\n${copySlice.excerpt}\n\`\`\``,
      copyFullText: fullCopyFormatted,
      matchedErrors: true,
      hint: "",
    };
  }

  return {
    preview: uiSlice.excerpt,
    copyErrorText:
      `${header}\n\n` +
      `> Note: Output is ${normalized.length.toLocaleString()} chars and did not match standard ERROR/FAIL markers; showing the final lines. Check Console Output if earlier lines are needed.\n\n` +
      `\`\`\`text\n${copySlice.excerpt}\n\`\`\``,
    copyFullText: fullCopyFormatted,
    matchedErrors: false,
    hint: "No standard ERROR/FAIL markers matched automatically; showing tail lines — use Copy Full Output or Console if needed.",
  };
}

async function runTerminalCommand(command, opts = {}) {
  const proj = getActiveProject();
  if (!proj) return;
  const sid = state.activeSessionId;
  const reportInChat = Boolean(opts.reportInChat && sid);
  const actId = reportInChat ? `act-${Date.now()}` : null;
  const matchesConfiguredTests =
    Boolean(proj.post_patch_command) && command.trim() === proj.post_patch_command.trim();
  const isTestRun = opts.isTestRun !== undefined ? Boolean(opts.isTestRun) : matchesConfiguredTests;

  lastManualCommandPassed = false;
  lastManualSessionId = sid;
  syncTerminalSavepointBar();

  terminalMeta.textContent = `Running: ${command}...`;
  terminalOutput.textContent = "";
  pasteTermBtn.disabled = true;

  if (reportInChat) {
    upsertActivity(sid, {
      id: actId,
      batchKey: actId,
      status: "running",
      title: `Running tests: ${command}`,
      detail: "Streaming output live in Console...",
      preview: "",
      timestamp: new Date().toISOString(),
    });
    emit("chat:loaded");
  }

  try {
    const res = await api.runCommand(proj.id, command, {
      sessionId: sid,
      isTestRun,
    });
    if (Array.isArray(res.checkpoints)) {
      state.checkpoints = res.checkpoints;
    }
    const rawOut = res.output || "";
    const hasTraceback =
      /Traceback \(most recent call last\):|\n(?:SyntaxError|AssertionError|ImportError|ModuleNotFoundError):/i.test(
        rawOut
      );
    const ok = res.exit_code === 0 && !hasTraceback;
    lastManualCommandPassed = Boolean(ok && !res.checkpoint_committed);
    showTerminalResult(res);
    if (!reportInChat && res.checkpoint_committed) {
      emit("git:refresh");
      emit("chat:loaded");
    }
    if (reportInChat) {
      if (ok) {
        const tailLines = rawOut.split("\n").filter((l) => l.trim()).slice(-6).join("\n").slice(0, 500);
        const commitNote = res.checkpoint_committed ? " · Savepoint committed ✓" : "";
        upsertActivity(sid, {
          id: actId,
          batchKey: actId,
          status: "ok",
          title: "Validated & tested cleanly (exit 0)",
          detail: `${command} · ${res.duration_ms}ms${commitNote} · View full output in Console`,
          preview: tailLines,
          showConsoleBtn: true,
          timestamp: new Date().toISOString(),
        });
        emit("chat:loaded");
      } else {
        const report = buildCommandFailureReport(command, res.exit_code, rawOut);
        upsertActivity(sid, {
          id: actId,
          batchKey: actId,
          status: "err",
          title: `Possible errors detected (exit ${res.exit_code})`,
          detail: `${command} · ${res.duration_ms}ms · View full output in Console`,
          hint: report.hint,
          preview: report.preview,
          copyErrorText: report.copyErrorText,
          copyFullText: report.copyFullText,
          showConsoleBtn: true,
          timestamp: new Date().toISOString(),
        });
        state.lastErrorContext = {
          sessionId: sid,
          reasonText: `Test failure (${command}, exit ${res.exit_code}):\n${report.preview}`,
        };
        if (!state.apiEnabled && report.copyErrorText) {
          let clipStatus = "copied";
          try {
            await copyTextToClipboard(report.copyErrorText);
          } catch {
            clipStatus = "blocked";
          }
          state.lastClipboardInfo = { key: actId, status: clipStatus };
        }
        emit("chat:loaded");
      }
    }
  } catch (err) {
    terminalMeta.textContent = "Command failed";
    terminalOutput.textContent = err.message;
    if (reportInChat) {
      const errMsg = `## Local Test Execution Error (\`${command}\`)\n\n${err.message}`;
      upsertActivity(sid, {
        id: actId,
        batchKey: actId,
        status: "err",
        title: "Test execution failed",
        detail: err.message,
        preview: err.message,
        copyErrorText: errMsg,
        copyFullText: "",
        showConsoleBtn: true,
        timestamp: new Date().toISOString(),
      });
      if (!state.apiEnabled) {
        let clipStatus = "copied";
        try {
          await copyTextToClipboard(errMsg);
        } catch {
          clipStatus = "blocked";
        }
        state.lastClipboardInfo = { key: actId, status: clipStatus };
      }
      emit("chat:loaded");
    }
  }
}

function upsertActivity(sessionId, entry) {
  if (!sessionId) return;
  const list = state.sessionActivities[sessionId] || [];
  const msgs = state.messages || [];
  const latestMsgId = msgs.length > 0 ? msgs[msgs.length - 1].id : null;
  const idx = list.findIndex((a) => a.id === entry.id);
  if (idx >= 0) {
    const prev = list[idx];
    const anchored = {
      ...entry,
      afterMsgIndex: prev.afterMsgIndex ?? msgs.length,
      afterMsgId: prev.afterMsgId !== undefined ? prev.afterMsgId : latestMsgId,
      anchorTimestamp: prev.anchorTimestamp || entry.timestamp || new Date().toISOString(),
    };
    list[idx] = anchored;
  } else {
    list.push({
      ...entry,
      afterMsgIndex: msgs.length,
      afterMsgId: latestMsgId,
      anchorTimestamp: entry.timestamp || new Date().toISOString(),
    });
  }
  state.sessionActivities[sessionId] = list.slice(-10);
}

function handleExecStreamEvent(msg) {
  if (!msg || !msg.type) return;
  if (msg.type === "exec_start") {
    state.liveExec = {
      runId: msg.run_id || null,
      command: msg.command || "",
      source: msg.source || "console",
      output: "",
      done: false,
    };
    terminalMeta.textContent = `Running: ${state.liveExec.command}...`;
    terminalOutput.textContent = "";
    pasteTermBtn.disabled = true;
    emit("chat:live-exec", {
      type: "start",
      command: state.liveExec.command,
      source: state.liveExec.source,
      output: "",
    });
    return;
  }
  if (msg.type === "exec_chunk") {
    if (!state.liveExec || (msg.run_id && state.liveExec.runId && msg.run_id !== state.liveExec.runId)) {
      state.liveExec = {
        runId: msg.run_id || null,
        command: "",
        source: msg.source || "console",
        output: "",
        done: false,
      };
    }
    state.liveExec.output += String(msg.chunk || "");
    if (state.liveExec.output.length > 45000) {
      state.liveExec.output = state.liveExec.output.slice(-45000);
    }
    terminalOutput.textContent = state.liveExec.output || "(Running...)";
    terminalOutput.scrollTop = terminalOutput.scrollHeight;
    const lineCount = state.liveExec.output.split("\n").length;
    const cmdLabel = state.liveExec.command ? `: ${state.liveExec.command}` : "";
    terminalMeta.textContent = `Running${cmdLabel} · ${lineCount} ${lineCount === 1 ? "line" : "lines"} streamed...`;
    emit("chat:live-exec", {
      type: "chunk",
      command: state.liveExec.command,
      source: state.liveExec.source,
      output: state.liveExec.output,
    });
    return;
  }
  if (msg.type === "exec_done") {
    const finalOutput =
      typeof msg.output === "string" ? msg.output : state.liveExec ? state.liveExec.output : "";
    if (state.resolvingGateId || state.pendingStatusText) {
      state.liveExec = {
        runId: msg.run_id || (state.liveExec && state.liveExec.runId) || null,
        command: msg.command || (state.liveExec && state.liveExec.command) || "",
        source: msg.source || (state.liveExec && state.liveExec.source) || "gate",
        output: finalOutput,
        done: true,
      };
    } else {
      state.liveExec = null;
    }
    showTerminalResult(msg, { suppressMdBar: true });
    emit("chat:live-exec", {
      type: "done",
      command: msg.command || "",
      source: msg.source || "console",
      output: finalOutput,
    });
  }
}

function getActiveSessionCheckpoint() {
  const sid = state.activeSessionId;
  if (!sid) return null;
  const list = state.checkpoints || [];
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const cp = list[i];
    if (cp.session_id === sid && cp.status === "active") return cp;
  }
  return null;
}

function syncTerminalSavepointBar() {
  if (!terminalSavepointBar) return;
  if (state.activeSessionId !== lastManualSessionId) {
    lastManualSessionId = state.activeSessionId;
    lastManualCommandPassed = false;
  }
  const activeCp = getActiveSessionCheckpoint();
  if (!activeCp) {
    terminalSavepointBar.classList.add("hidden");
    terminalSavepointBar.classList.remove("is-ready");
    return;
  }
  terminalSavepointBar.classList.remove("hidden");
  terminalSavepointBar.classList.toggle("is-ready", lastManualCommandPassed);
  commitCpBtn.disabled = false;
  restoreCpBtn.disabled = false;
  commitCpBtn.textContent = "Commit Savepoint";
  restoreCpBtn.textContent = "Restore";
  if (lastManualCommandPassed) {
    terminalSavepointLabel.textContent = "Manual run exited 0 · Ready to commit savepoint";
    commitCpBtn.className = "btn btn-primary btn-xs";
  } else {
    const n = Number(activeCp.files_count || 0);
    terminalSavepointLabel.textContent = `Active savepoint (${n} ${n === 1 ? "file" : "files"}) · Commit after manual test pass`;
    commitCpBtn.className = "btn btn-secondary btn-xs";
  }
}

async function handleTerminalCommitCheckpoint() {
  const cp = getActiveSessionCheckpoint();
  if (!cp) return;
  commitCpBtn.disabled = true;
  restoreCpBtn.disabled = true;
  commitCpBtn.textContent = "Committing...";
  try {
    const res = await api.commitCheckpoint(cp.id);
    if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
    lastManualCommandPassed = false;
    syncTerminalSavepointBar();
    emit("git:refresh");
    emit("chat:loaded");
  } catch (err) {
    commitCpBtn.disabled = false;
    restoreCpBtn.disabled = false;
    commitCpBtn.textContent = "Commit Savepoint";
    emit("chat:error", err.message);
  }
}

async function handleTerminalRestoreCheckpoint() {
  const cp = getActiveSessionCheckpoint();
  if (!cp) return;
  commitCpBtn.disabled = true;
  restoreCpBtn.disabled = true;
  restoreCpBtn.textContent = "Restoring...";
  try {
    const res = await api.restoreCheckpoint(cp.id);
    if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
    if (Array.isArray(res.pending_gates)) state.pendingGates = res.pending_gates;
    state.gatesHiddenDueToError = false;
    lastManualCommandPassed = false;
    syncTerminalSavepointBar();
    emit("backups:refresh");
    emit("git:refresh");
    emit("tree:refresh");
    emit("session:updated");
    emit("chat:loaded");
  } catch (err) {
    commitCpBtn.disabled = false;
    restoreCpBtn.disabled = false;
    restoreCpBtn.textContent = "Restore";
    emit("chat:error", err.message);
  }
}

function showTerminalResult(res, opts = {}) {
  state.lastTerminalLabel = `${res.command} (exit ${res.exit_code})`;
  state.lastTerminalOutput = res.output || "";
  terminalMeta.textContent = `Exit ${res.exit_code} · ${res.duration_ms}ms · ~${(res.estimated_tokens || 0).toLocaleString()} tok`;
  terminalOutput.textContent = res.output || "(No output)";
  terminalOutput.scrollTop = terminalOutput.scrollHeight;
  pasteTermBtn.disabled = !state.lastTerminalOutput;
  syncTerminalSavepointBar();
  if (!opts.suppressMdBar && res.generated_md_files && res.generated_md_files.length > 0) {
    emit("bridge:generated-md", res.generated_md_files);
  }
}

async function handleCopyTree() {
  const proj = getActiveProject();
  if (!proj) return;
  const btn = document.getElementById("btn-copy-tree");
  const filter = (fileTreeFilter.value || "").trim().toLowerCase();
  try {
    const res = await api.getTree(proj.id, ".", 5);
    const lines = filter
      ? (res.entries || []).filter((e) => e.path.toLowerCase().includes(filter)).map((e) => `${e.path}${e.is_dir ? "/" : ""}`)
      : [res.tree];
    await copyTextToClipboard(lines.join("\n"));
    btn.textContent = "Tree Copied ✓";
    setTimeout(() => {
      btn.textContent = "Copy Tree";
    }, 1800);
  } catch (err) {
    emit("chat:error", err.message);
  }
}

async function handlePackFiltered() {
  const proj = getActiveProject();
  if (!proj) return;
  const btn = document.getElementById("btn-pack-filtered");
  const filter = (fileTreeFilter.value || "").trim();
  btn.disabled = true;
  btn.textContent = "Packing...";
  try {
    const res = await api.bridgePackFiles(proj.id, filter);
    if (!res.content) {
      throw new Error("No matching readable files found to pack.");
    }
    await copyTextToClipboard(res.content);
    btn.textContent = `Copied ${res.count} files (~${Number(res.estimated_tokens || 0).toLocaleString()} tok)`;
    setTimeout(() => {
      btn.textContent = "Pack Visible (.md)";
    }, 2200);
  } catch (err) {
    emit("chat:error", err.message);
    btn.textContent = "Pack Visible (.md)";
  } finally {
    btn.disabled = false;
  }
}

async function refreshBackups() {
  const proj = getActiveProject();
  if (!proj) {
    backupList.innerHTML = "";
    return;
  }
  try {
    const items = await api.listBackups(proj.id);
    backupList.innerHTML = "";
    if (!items.length) {
      backupList.innerHTML = `<div class="meta-line">No patches recorded yet.</div>`;
      return;
    }
    for (const rec of items) {
      const row = document.createElement("div");
      row.className = "backup-row";
      const isCleaned = !rec.reverted && rec.backup_exists === false;
      const statusSuffix = rec.reverted ? " · Reverted" : isCleaned ? " · Auto-cleaned" : "";
      row.innerHTML = `
        <div style="min-width:0; flex:1;">
          <div class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(rec.file_path)}</div>
          <div class="meta-line">${escapeHtml(rec.summary)}${statusSuffix}</div>
        </div>
      `;
      const btn = document.createElement("button");
      btn.className = "btn btn-secondary btn-sm";
      btn.textContent = rec.reverted ? "Reverted" : isCleaned ? "Cleaned" : "Undo";
      btn.disabled = Boolean(rec.reverted || isCleaned);
      if (isCleaned) {
        btn.title = "Snapshot exceeded backup retention limit and was auto-cleaned";
      }
      btn.addEventListener("click", async () => {
        try {
          await api.undoPatch(rec.id);
          await refreshBackups();
          emit("git:refresh");
        } catch (err) {
          emit("chat:error", err.message);
        }
      });
      row.appendChild(btn);
      backupList.appendChild(row);
    }
  } catch (err) {
    backupList.innerHTML = `<div class="meta-line">${escapeHtml(err.message)}</div>`;
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
