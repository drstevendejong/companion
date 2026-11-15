// drawer.js - Right panel: Project File Tree, Terminal Runner (+ Paste into Prompt), and Project Backups/Undo

import { api } from "./api.js";
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

  postTestsBtn.addEventListener("click", () => {
    const proj = getActiveProject();
    if (!proj || !proj.post_patch_command) {
      emit("chat:error", "No Post-Patch Test Command configured. Click Edit next to Active Project to set one.");
      return;
    }
    switchTab("terminal");
    runTerminalCommand(proj.post_patch_command);
  });

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
  on("terminal:show-result", (res) => {
    switchTab("terminal");
    showTerminalResult(res);
  });
  on("terminal:suggest-post-tests", (cmd) => {
    customCmdInput.value = cmd;
    terminalMeta.textContent = `Patch applied · Click Run Tests (${cmd})`;
  });
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

async function runTerminalCommand(command) {
  const proj = getActiveProject();
  if (!proj) return;
  terminalMeta.textContent = `Running: ${command}...`;
  terminalOutput.textContent = "";
  pasteTermBtn.disabled = true;

  try {
    const res = await api.runCommand(proj.id, command);
    showTerminalResult(res);
  } catch (err) {
    terminalMeta.textContent = "Command failed";
    terminalOutput.textContent = err.message;
  }
}

function showTerminalResult(res) {
  state.lastTerminalLabel = `${res.command} (exit ${res.exit_code})`;
  state.lastTerminalOutput = res.output || "";
  terminalMeta.textContent = `Exit ${res.exit_code} · ${res.duration_ms}ms · ~${(res.estimated_tokens || 0).toLocaleString()} tok`;
  terminalOutput.textContent = res.output || "(No output)";
  pasteTermBtn.disabled = !state.lastTerminalOutput;
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
      row.innerHTML = `
        <div style="min-width:0; flex:1;">
          <div class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(rec.file_path)}</div>
          <div class="meta-line">${escapeHtml(rec.summary)}${rec.reverted ? " · Reverted" : ""}</div>
        </div>
      `;
      const btn = document.createElement("button");
      btn.className = "btn btn-secondary btn-sm";
      btn.textContent = rec.reverted ? "Reverted" : "Undo";
      btn.disabled = rec.reverted;
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
