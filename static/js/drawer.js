// drawer.js - Right panel: Project File Tree, Terminal Runner (+ Paste into Prompt), and Project Backups/Undo

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
const postTestsBtn = document.getElementById("btn-run-post-tests");
const backupList = document.getElementById("backup-list");

const expandedDirs = new Set();
const dirCache = new Map();

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
    emit("composer:paste-text", `[Terminal: ${state.lastTerminalLabel}]\n${state.lastTerminalOutput}`);
  });

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
  on("drawer:switch-tab", (tabName) => {
    if (tabName && panes[tabName]) switchTab(tabName);
  });
  on("terminal:run-post-tests", triggerPostPatchTests);
  on("terminal:show-result", (res) => {
    // Update terminal contents silently so split-screen collapsed drawer isn't forced open unnecessarily
    showTerminalResult(res, { suppressMdBar: true });
  });
  on("terminal:suggest-post-tests", (cmd) => {
    customCmdInput.value = cmd;
    terminalMeta.textContent = `Patch applied · Click Run Tests (${cmd})`;
  });
}

function triggerPostPatchTests() {
  const proj = getActiveProject();
  if (!proj || !proj.post_patch_command) {
    emit("chat:error", "No Post-Patch Test Command configured. Click Edit next to Active Project to set one.");
    return;
  }
  runTerminalCommand(proj.post_patch_command, { reportInChat: true, label: "tests" });
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

async function runTerminalCommand(command, opts = {}) {
  const proj = getActiveProject();
  if (!proj) return;
  const sid = state.activeSessionId;
  const reportInChat = Boolean(opts.reportInChat && sid);
  const actId = reportInChat ? `act-${Date.now()}` : null;

  terminalMeta.textContent = `Running: ${command}...`;
  terminalOutput.textContent = "";
  pasteTermBtn.disabled = true;

  if (reportInChat) {
    upsertActivity(sid, {
      id: actId,
      status: "running",
      title: `Running tests: ${command}`,
      detail: "Waiting for process exit code...",
      preview: "",
      timestamp: new Date().toISOString(),
    });
    emit("chat:loaded");
  }

  try {
    const res = await api.runCommand(proj.id, command);
    showTerminalResult(res);
    if (reportInChat) {
      const rawOut = res.output || "";
      const hasTraceback = /Traceback \(most recent call last\):|\nSyntaxError:/i.test(rawOut);
      const ok = res.exit_code === 0 && !hasTraceback;
      const tailLines = rawOut.split("\n").filter((l) => l.trim()).slice(-6).join("\n").slice(0, 500);
      upsertActivity(sid, {
        id: actId,
        status: ok ? "ok" : "err",
        title: ok ? `Validated & tested cleanly (exit 0)` : `Possible errors detected (exit ${res.exit_code})`,
        detail: `${command} · ${res.duration_ms}ms · View full output in Console`,
        preview: tailLines,
        showConsoleBtn: true,
        timestamp: new Date().toISOString(),
      });
      emit("chat:loaded");
    }
  } catch (err) {
    terminalMeta.textContent = "Command failed";
    terminalOutput.textContent = err.message;
    if (reportInChat) {
      upsertActivity(sid, {
        id: actId,
        status: "err",
        title: "Test execution failed",
        detail: err.message,
        preview: "",
        showConsoleBtn: true,
        timestamp: new Date().toISOString(),
      });
      emit("chat:loaded");
    }
  }
}

function upsertActivity(sessionId, entry) {
  if (!sessionId) return;
  const list = state.sessionActivities[sessionId] || [];
  const idx = list.findIndex((a) => a.id === entry.id);
  if (idx >= 0) {
    const anchored = {
      ...entry,
      afterMsgIndex: list[idx].afterMsgIndex ?? (state.messages || []).length,
    };
    list[idx] = anchored;
  } else {
    list.push({
      ...entry,
      afterMsgIndex: (state.messages || []).length,
    });
  }
  state.sessionActivities[sessionId] = list.slice(-10);
}

function showTerminalResult(res, opts = {}) {
  state.lastTerminalLabel = `${res.command} (exit ${res.exit_code})`;
  state.lastTerminalOutput = res.output || "";
  terminalMeta.textContent = `Exit ${res.exit_code} · ${res.duration_ms}ms · ~${(res.estimated_tokens || 0).toLocaleString()} tok`;
  terminalOutput.textContent = res.output || "(No output)";
  pasteTermBtn.disabled = !state.lastTerminalOutput;
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
