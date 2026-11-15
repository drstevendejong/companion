// sessions.js - Project profile modal, sessions sidebar, model controls, and token meter

import { api } from "./api.js";
import { openFsBrowser } from "./fs_browser.js";
import { emit, getActiveProject, on, state } from "./state.js";

const projectSelect = document.getElementById("project-select");
const projectMeta = document.getElementById("project-meta");
const sessionList = document.getElementById("session-list");
const modelSelect = document.getElementById("model-select");
const thinkingSelect = document.getElementById("thinking-select");
const autoReadInput = document.getElementById("auto-read-input");
const tokenTotalLabel = document.getElementById("token-total-label");
const tokenMeterFill = document.getElementById("token-meter-fill");
const tokenBreakdown = document.getElementById("token-breakdown");
const sessionTitleInput = document.getElementById("session-title-input");
const directivesMeta = document.getElementById("directives-meta");

const projectModal = document.getElementById("project-modal");
const projectForm = document.getElementById("project-form");
let editingProjectId = null;

export async function initSessions() {
  state.catalog = await api.getModelCatalog();
  modelSelect.innerHTML = state.catalog.models
    .map((m) => `<option value="${m.id}">${m.label}</option>`)
    .join("");
  thinkingSelect.innerHTML = state.catalog.thinking_levels
    .map((lvl) => `<option value="${lvl}">${lvl}</option>`)
    .join("");

  document.getElementById("btn-new-project").addEventListener("click", () => openProjectModal(null));
  document.getElementById("btn-edit-project").addEventListener("click", () => openProjectModal(getActiveProject()));
  document.getElementById("btn-cancel-project").addEventListener("click", () => projectModal.close());
  document.getElementById("btn-browse-root").addEventListener("click", () => {
    const cur = document.getElementById("proj-root").value.trim();
    openFsBrowser({
      title: "Select Project Root Directory",
      mode: "dir",
      initialPath: cur,
      onSelect: (chosen) => {
        document.getElementById("proj-root").value = chosen;
      },
    });
  });
  document.getElementById("btn-browse-python").addEventListener("click", () => {
    const cur = document.getElementById("proj-python").value.trim();
    const root = document.getElementById("proj-root").value.trim();
    openFsBrowser({
      title: "Select Python Executable",
      mode: "file",
      initialPath: cur ? cur.replace(/[\\/][^\\/]+$/, "") : root,
      onSelect: (chosen) => {
        document.getElementById("proj-python").value = chosen;
      },
    });
  });
  document.getElementById("btn-browse-directives").addEventListener("click", () => {
    const root = document.getElementById("proj-root").value.trim();
    openFsBrowser({
      title: "Select Directives Markdown File",
      mode: "file",
      initialPath: root,
      baseRoot: root || null,
      onSelect: (chosen) => {
        document.getElementById("proj-directives").value = chosen;
      },
    });
  });
  projectForm.addEventListener("submit", handleProjectSave);

  projectSelect.addEventListener("change", () => selectProject(projectSelect.value));
  document.getElementById("btn-new-session").addEventListener("click", createNewSession);

  sessionTitleInput.addEventListener("change", async () => {
    if (!state.activeSessionId) return;
    const title = sessionTitleInput.value.trim();
    if (!title) return;
    const updated = await api.updateSession(state.activeSessionId, { title });
    state.activeSession = updated;
    await refreshSessions();
  });

  modelSelect.addEventListener("change", () => updateActiveSessionSettings({ model: modelSelect.value }));
  thinkingSelect.addEventListener("change", () => updateActiveSessionSettings({ thinking_level: thinkingSelect.value }));
  autoReadInput.addEventListener("change", () =>
    updateActiveSessionSettings({ auto_approve_read_tokens: Math.max(0, Number(autoReadInput.value) || 0) })
  );

  on("session:updated", renderTokenMeter);

  await loadProjects();
}

async function loadProjects() {
  state.projects = await api.listProjects();
  projectSelect.innerHTML = state.projects.length
    ? state.projects.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("")
    : `<option value="">No projects configured</option>`;

  if (state.projects.length > 0) {
    const targetId = state.activeProjectId && state.projects.some((p) => p.id === state.activeProjectId)
      ? state.activeProjectId
      : state.projects[0].id;
    await selectProject(targetId);
  } else {
    state.activeProjectId = null;
    projectMeta.textContent = "Click + Project to point at a codebase";
    emit("project:changed", null);
  }
}

async function selectProject(projectId) {
  state.activeProjectId = projectId || null;
  projectSelect.value = projectId || "";
  const proj = getActiveProject();
  if (!proj) return;

  projectMeta.textContent = proj.root_path;
  await refreshDirectivesBanner(proj.id);
  emit("project:changed", proj);

  await refreshSessions();
  if (state.sessions.length > 0) {
    await selectSession(state.sessions[0].id);
  } else {
    await createNewSession();
  }
}

async function refreshDirectivesBanner(projectId) {
  try {
    const info = await api.getDirectives(projectId);
    if (info.configured && info.exists) {
      directivesMeta.textContent = `Directives: ${info.path} (~${info.estimated_tokens} tok)`;
    } else if (info.configured) {
      directivesMeta.textContent = `Directives missing: ${info.path}`;
    } else {
      directivesMeta.textContent = "No Directives file linked";
    }
  } catch {
    directivesMeta.textContent = "";
  }
}

async function refreshSessions() {
  if (!state.activeProjectId) return;
  state.sessions = await api.listSessions(state.activeProjectId);
  sessionList.innerHTML = "";
  for (const s of state.sessions) {
    const row = document.createElement("div");
    row.className = `session-item ${s.id === state.activeSessionId ? "active" : ""}`;
    row.innerHTML = `
      <span class="session-item-title">${escapeHtml(s.title)}</span>
      <button class="btn btn-ghost btn-sm" title="Delete session">&times;</button>
    `;
    row.querySelector(".session-item-title").addEventListener("click", () => selectSession(s.id));
    row.querySelector("button").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await api.deleteSession(s.id);
      if (state.activeSessionId === s.id) {
        state.activeSessionId = null;
      }
      await refreshSessions();
      if (state.sessions.length > 0 && !state.activeSessionId) {
        await selectSession(state.sessions[0].id);
      }
    });
    sessionList.appendChild(row);
  }
}

async function createNewSession() {
  if (!state.activeProjectId) {
    openProjectModal(null);
    return;
  }
  const created = await api.createSession({
    project_id: state.activeProjectId,
    title: `Session ${state.sessions.length + 1}`,
    model: modelSelect.value || "gemini-4-hydrogen",
    thinking_level: thinkingSelect.value || "HIGH",
  });
  await refreshSessions();
  await selectSession(created.id);
}

export async function selectSession(sessionId) {
  const detail = await api.getSessionDetail(sessionId);
  state.activeSessionId = sessionId;
  state.activeSession = detail.session;
  state.messages = detail.messages || [];
  state.pendingGates = detail.pending_gates || [];

  sessionTitleInput.disabled = false;
  sessionTitleInput.value = detail.session.title;
  modelSelect.value = detail.session.model;
  thinkingSelect.value = detail.session.thinking_level;
  autoReadInput.value = detail.session.auto_approve_read_tokens || 0;

  renderTokenMeter();
  await refreshSessions();
  emit("chat:loaded");
}

async function updateActiveSessionSettings(patch) {
  if (!state.activeSessionId) return;
  const updated = await api.updateSession(state.activeSessionId, patch);
  state.activeSession = updated;
  renderTokenMeter();
}

export function renderTokenMeter() {
  const s = state.activeSession;
  if (!s) return;
  const stats = s.token_stats || { input_tokens: 0, output_tokens: 0, thinking_tokens: 0, total_tokens: 0 };
  const modelMeta = state.catalog.models.find((m) => m.id === s.model);
  const limit = modelMeta ? modelMeta.context_limit : 1_048_576;
  const pct = Math.min(100, ((stats.total_tokens || 0) / limit) * 100);
  tokenTotalLabel.textContent = `${(stats.total_tokens || 0).toLocaleString()} / ${limit.toLocaleString()}`;
  tokenMeterFill.style.width = `${pct.toFixed(1)}%`;
  tokenBreakdown.textContent = `In ${stats.input_tokens.toLocaleString()} · Out ${stats.output_tokens.toLocaleString()} · Think ${stats.thinking_tokens.toLocaleString()}`;
}

function openProjectModal(proj) {
  editingProjectId = proj ? proj.id : null;
  document.getElementById("project-modal-title").textContent = proj ? "Edit Project Profile" : "Add Project Profile";
  document.getElementById("proj-name").value = proj ? proj.name : "";
  document.getElementById("proj-root").value = proj ? proj.root_path : "";
  document.getElementById("proj-python").value = (proj && proj.python_path) || "";
  document.getElementById("proj-backup").value = (proj && proj.backup_dir) || "backup";
  document.getElementById("proj-directives").value = (proj && proj.directives_path) || "";
  document.getElementById("proj-cdp-port").value = (proj && proj.cdp_port) || 9222;
  document.getElementById("proj-post-cmd").value = (proj && proj.post_patch_command) || "";
  document.getElementById("proj-ignores").value = proj ? (proj.ignore_patterns || []).join(", ") : "";
  projectModal.showModal();
}

async function handleProjectSave(ev) {
  ev.preventDefault();
  const payload = {
    name: document.getElementById("proj-name").value.trim(),
    root_path: document.getElementById("proj-root").value.trim(),
    python_path: document.getElementById("proj-python").value.trim() || null,
    backup_dir: document.getElementById("proj-backup").value.trim() || "backup",
    directives_path: document.getElementById("proj-directives").value.trim() || null,
    cdp_port: Number(document.getElementById("proj-cdp-port").value) || 9222,
    post_patch_command: document.getElementById("proj-post-cmd").value.trim() || null,
    ignore_patterns: document
      .getElementById("proj-ignores")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  };
  try {
    if (editingProjectId) {
      await api.updateProject(editingProjectId, payload);
      state.activeProjectId = editingProjectId;
    } else {
      const created = await api.createProject(payload);
      state.activeProjectId = created.id;
    }
    projectModal.close();
    await loadProjects();
  } catch (err) {
    alert(err.message);
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
