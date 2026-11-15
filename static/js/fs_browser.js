// fs_browser.js - Cross-platform local directory and file picker modal

import { api } from "./api.js";

const modal = document.getElementById("fs-browser-modal");
const titleEl = document.getElementById("fs-browser-title");
const rootsEl = document.getElementById("fs-browser-roots");
const upBtn = document.getElementById("fs-btn-up");
const currentPathInput = document.getElementById("fs-current-path");
const listEl = document.getElementById("fs-browser-list");
const cancelBtn = document.getElementById("fs-btn-cancel");
const selectBtn = document.getElementById("fs-btn-select");

let pickerState = {
  mode: "dir",
  currentPath: "",
  parentPath: null,
  selectedFilePath: null,
  baseRoot: null,
  onSelect: null,
};

let initialized = false;

export function initFsBrowser() {
  if (initialized) return;
  initialized = true;

  cancelBtn.addEventListener("click", () => modal.close());
  upBtn.addEventListener("click", () => {
    if (pickerState.parentPath) {
      loadDirectory(pickerState.parentPath);
    }
  });
  selectBtn.addEventListener("click", () => {
    const chosen =
      pickerState.mode === "dir"
        ? pickerState.selectedFilePath || pickerState.currentPath
        : pickerState.selectedFilePath;
    if (!chosen) return;
    const output = pickerState.baseRoot ? makeRelativeIfInside(pickerState.baseRoot, chosen) : chosen;
    modal.close();
    if (typeof pickerState.onSelect === "function") {
      pickerState.onSelect(output);
    }
  });
}

export async function openFsBrowser({ title, mode = "dir", initialPath = "", baseRoot = null, onSelect }) {
  initFsBrowser();
  pickerState = {
    mode,
    currentPath: initialPath || "",
    parentPath: null,
    selectedFilePath: null,
    baseRoot,
    onSelect,
  };
  titleEl.textContent = title || (mode === "dir" ? "Select Directory" : "Select File");
  selectBtn.textContent = mode === "dir" ? "Use This Folder" : "Select File";
  selectBtn.disabled = mode === "file";
  modal.showModal();
  await loadDirectory(initialPath || "");
}

async function loadDirectory(path) {
  listEl.innerHTML = `<div class="meta-line" style="padding:8px;">Loading...</div>`;
  try {
    const data = await api.browseFs(path, pickerState.mode === "file");
    pickerState.currentPath = data.current_path;
    pickerState.parentPath = data.parent_path;
    pickerState.selectedFilePath = null;

    currentPathInput.value = data.current_path;
    upBtn.disabled = !data.parentPath && !data.parent_path;
    selectBtn.disabled = pickerState.mode === "file";
    selectBtn.textContent = pickerState.mode === "dir" ? "Use This Folder" : "Select File";

    rootsEl.innerHTML = "";
    for (const r of data.roots || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-ghost btn-xs mono";
      btn.textContent = r;
      btn.addEventListener("click", () => loadDirectory(r));
      rootsEl.appendChild(btn);
    }

    listEl.innerHTML = "";
    if (!data.entries.length) {
      listEl.innerHTML = `<div class="meta-line" style="padding:8px;">Empty directory</div>`;
      return;
    }

    for (const entry of data.entries) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `fs-entry ${entry.is_dir ? "is-dir" : ""}`;
      btn.innerHTML = `<span>${entry.is_dir ? "📁" : "📄"} ${escapeHtml(entry.name)}</span>`;

      if (entry.is_dir) {
        btn.addEventListener("click", () => loadDirectory(entry.path));
      } else {
        btn.addEventListener("click", () => {
          listEl.querySelectorAll(".fs-entry").forEach((el) => el.classList.remove("selected"));
          btn.classList.add("selected");
          pickerState.selectedFilePath = entry.path;
          selectBtn.disabled = false;
        });
        btn.addEventListener("dblclick", () => {
          pickerState.selectedFilePath = entry.path;
          selectBtn.click();
        });
      }
      listEl.appendChild(btn);
    }
  } catch (err) {
    listEl.innerHTML = `<div class="meta-line" style="padding:8px;color:#fca5a5;">${escapeHtml(err.message)}</div>`;
  }
}

function makeRelativeIfInside(baseRoot, fullPath) {
  const normBase = baseRoot.replace(/\\/g, "/").replace(/\/+$/, "");
  const normFull = fullPath.replace(/\\/g, "/");
  if (normFull.toLowerCase().startsWith(`${normBase.toLowerCase()}/`)) {
    return normFull.slice(normBase.length + 1);
  }
  return fullPath;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
