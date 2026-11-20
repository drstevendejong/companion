// sessions.js - Project profile modal, sessions sidebar, model controls, context meter, and RPM/TPM/RPD rate telemetry

import { api } from "./api.js";
import { openFsBrowser } from "./fs_browser.js";
import { requestApiKeyForSessionSetup } from "./settings.js";
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
const rpmValEl = document.getElementById("telemetry-rpm");
const tpmValEl = document.getElementById("telemetry-tpm");
const rpdValEl = document.getElementById("telemetry-rpd");
const sessionTitleInput = document.getElementById("session-title-input");
const activeModeBadge = document.getElementById("active-session-mode-badge");
const directivesMeta = document.getElementById("directives-meta");
const refreshModelsBtn = document.getElementById("btn-refresh-models");

const projectModal = document.getElementById("project-modal");
const projectForm = document.getElementById("project-form");

const setupModal = document.getElementById("session-setup-modal");
const setupForm = document.getElementById("session-setup-form");
const setupNameInput = document.getElementById("new-session-name");
const setupApiToggle = document.getElementById("new-session-api-toggle");
const setupModeHint = document.getElementById("new-session-mode-hint");
const setupErrorEl = document.getElementById("session-setup-error");
const setupCancelBtn = document.getElementById("btn-cancel-session-setup");
const setupConfirmBtn = document.getElementById("btn-confirm-session-setup");

let editingProjectId = null;
let telemetryDecayTimer = null;

function ensureThinkingOptions(levels = ["LOW", "MEDIUM", "HIGH", "XHIGH"]) {
  const prev = thinkingSelect.value;
  if (!thinkingSelect.options.length) {
    thinkingSelect.innerHTML = levels
      .map((lvl) => `<option value="${escapeHtml(lvl)}">${escapeHtml(lvl)}</option>`)
      .join("");
  }
  const desired =
    (state.activeSession && state.activeSession.thinking_level) ||
    (levels.includes(prev) ? prev : "HIGH");
  thinkingSelect.value = desired;
}

function renderModelCatalog(catalog) {
  const models = (catalog && catalog.models) || [];
  const optionsHtml = models
    .map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`)
    .join("");
  const noticeHtml =
    catalog && catalog.notice
      ? `<option disabled value="">${escapeHtml(catalog.notice)}</option>`
      : "";
  modelSelect.innerHTML = optionsHtml + noticeHtml;

  const levels = (catalog && catalog.thinking_levels) || ["LOW", "MEDIUM", "HIGH", "XHIGH"];
  ensureThinkingOptions(levels);
}

let modelProbePromise = null;

async function refreshModelCatalog(forceRefresh = false, allowProbe = false) {
  if (!state.apiEnabled && !allowProbe) {
    return;
  }
  if (allowProbe && modelProbePromise) {
    return modelProbePromise;
  }
  if (refreshModelsBtn) {
    refreshModelsBtn.disabled = true;
    refreshModelsBtn.textContent = "…";
    refreshModelsBtn.title = allowProbe ? "Checking free/paid model access..." : "Loading live models...";
  }
  const run = (async () => {
    try {
      state.catalog = await api.getModelCatalog(forceRefresh, allowProbe);
      renderModelCatalog(state.catalog);
      if (state.activeSession && state.apiEnabled) {
        await reconcileSessionModel(state.activeSession);
      }
      renderTokenMeter();
    } catch (err) {
      console.error("Failed to refresh model catalog:", err);
    } finally {
      if (refreshModelsBtn) {
        refreshModelsBtn.disabled = false;
        refreshModelsBtn.innerHTML = "&#8635;";
        refreshModelsBtn.title = "Refresh live models & probe free/paid access (uses 1 req/model)";
      }
      if (allowProbe) {
        modelProbePromise = null;
      }
    }
  })();

  if (allowProbe) {
    modelProbePromise = run;
  }
  return run;
}

async function reconcileSessionModel(session) {
  if (!session || !session.api_enabled) return;
  const selectable = Array.from(modelSelect.options).filter((o) => !o.disabled && o.value);
  if (!selectable.length) return;
  const match = selectable.find((o) => o.value === session.model);
  if (match) {
    modelSelect.value = session.model;
  } else {
    const preferred = selectable.find((o) => o.value === "gemini-4-hydrogen") || selectable[0];
    modelSelect.value = preferred.value;
    if (state.activeSessionId === session.id) {
      await updateActiveSessionSettings({ model: preferred.value });
    }
  }
}

function applySessionModeUI(session) {
  const hasSession = Boolean(session);
  const apiEnabled = Boolean(session && session.api_enabled);
  state.apiEnabled = apiEnabled;
  document.body.classList.toggle("mode-api", apiEnabled);
  document.body.classList.toggle("mode-bridge", !apiEnabled);

  if (activeModeBadge) {
    activeModeBadge.classList.toggle("hidden", !hasSession);
    activeModeBadge.classList.toggle("is-api", apiEnabled);
    activeModeBadge.classList.toggle("is-bridge", !apiEnabled);
    activeModeBadge.textContent = apiEnabled ? "API" : "BRIDGE";
    activeModeBadge.title = apiEnabled
      ? "Direct Gemini API session"
      : "Playground Bridge session (0 API tokens)";
  }
}

export async function initSessions() {
  ensureThinkingOptions();
  applySessionModeUI(null);

  if (refreshModelsBtn) {
    // Explicit manual probe only — 0 RPD burned automatically on startup
    refreshModelsBtn.addEventListener("click", () => void refreshModelCatalog(true, true));
  }
  on("models:refresh", (opts) => {
    const probe = Boolean(opts && opts.probe);
    void refreshModelCatalog(probe, probe);
  });

  on("app:mode-changed", ({ apiEnabled }) => {
    if (apiEnabled) {
      void refreshRateTelemetry();
      if (!state.catalog.models || state.catalog.models.length === 0) {
        void refreshModelCatalog(false, false);
      }
    } else if (telemetryDecayTimer) {
      clearTimeout(telemetryDecayTimer);
      telemetryDecayTimer = null;
    }
  });

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
  document.getElementById("btn-new-session").addEventListener("click", () => openSessionSetupModal());

  setupApiToggle.addEventListener("change", updateSetupModeHint);
  setupCancelBtn.addEventListener("click", () => setupModal.close());
  setupForm.addEventListener("submit", handleSessionSetupSubmit);

  sessionTitleInput.addEventListener("change", async () => {
    if (!state.activeSessionId) return;
    const title = sessionTitleInput.value.trim();
    if (!title) return;
    const updated = await api.updateSession(state.activeSessionId, { title });
    state.activeSession = updated;
    await refreshSessions();
  });

  modelSelect.addEventListener("change", async () => {
    await updateActiveSessionSettings({ model: modelSelect.value });
    await refreshRateTelemetry();
  });
  thinkingSelect.addEventListener("change", () => updateActiveSessionSettings({ thinking_level: thinkingSelect.value }));
  autoReadInput.addEventListener("change", () =>
    updateActiveSessionSettings({ auto_approve_read_tokens: Math.max(0, Number(autoReadInput.value) || 0) })
  );

  on("session:updated", renderTokenMeter);
  on("telemetry:updated", (tel) => renderRateTelemetry(tel));

  await loadProjects();
}

function updateSetupModeHint() {
  const onApi = Boolean(setupApiToggle.checked);
  setupModeHint.textContent = onApi
    ? "ON · Direct Gemini API (model calls & rate telemetry)"
    : "OFF · Playground Bridge (0 API tokens, copy/paste)";
}

function openSessionSetupModal(options = {}) {
  if (!state.activeProjectId) {
    openProjectModal(null);
    return;
  }
  const defaultName = options.name !== undefined
    ? options.name
    : `Session ${state.sessions.length + 1}`;
  setupNameInput.value = defaultName;
  setupApiToggle.checked = Boolean(options.apiEnabled);
  setupErrorEl.textContent = options.error || "";
  updateSetupModeHint();

  if (!setupModal.open) {
    setupModal.showModal();
  }
  setupNameInput.focus();
  setupNameInput.select();
}

async function handleSessionSetupSubmit(ev) {
  ev.preventDefault();
  if (!state.activeProjectId) return;

  const title = setupNameInput.value.trim() || `Session ${state.sessions.length + 1}`;
  const wantApi = Boolean(setupApiToggle.checked);
  setupErrorEl.textContent = "";
  setupConfirmBtn.disabled = true;

  try {
    if (wantApi) {
      const keyStatus = await api.getApiKeyStatus(false);
      state.hasApiKey = Boolean(keyStatus && keyStatus.configured);
      if (!state.hasApiKey) {
        setupModal.close();
        const keyConfigured = await requestApiKeyForSessionSetup(
          "Configure a Gemini API key to create an API session."
        );
        if (!keyConfigured) {
          openSessionSetupModal({
            name: title,
            apiEnabled: true,
            error: "A verified Gemini API key is required when Use Gemini API is ON.",
          });
          return;
        }
      }
    } else {
      setupModal.close();
    }

    if (setupModal.open) {
      setupModal.close();
    }

    const created = await api.createSession({
      project_id: state.activeProjectId,
      title,
      api_enabled: wantApi,
      model: modelSelect.value || "gemini-4-hydrogen",
      thinking_level: thinkingSelect.value || "HIGH",
    });
    await refreshSessions();
    await selectSession(created.id);
  } catch (err) {
    if (!setupModal.open) {
      openSessionSetupModal({ name: title, apiEnabled: wantApi, error: err.message });
    } else {
      setupErrorEl.textContent = err.message;
    }
  } finally {
    setupConfirmBtn.disabled = false;
  }
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
    clearActiveSession();
    emit("project:changed", null);
  }
}

function clearActiveSession() {
  state.activeSessionId = null;
  state.activeSession = null;
  state.messages = [];
  state.pendingGates = [];
  state.gatesHiddenDueToError = false;
  state.lastErrorContext = null;
  sessionTitleInput.disabled = true;
  sessionTitleInput.value = "Create a session (+ New)";
  applySessionModeUI(null);
  emit("app:mode-changed", { apiEnabled: false });
  emit("bridge:session-synced");
  emit("chat:loaded");
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
    clearActiveSession();
    openSessionSetupModal();
  }
}

async function refreshDirectivesBanner(projectId) {
  try {
    const info = await api.getDirectives(projectId);
    if (info.configured && info.exists) {
      state.directivesTokensEst = Number(info.estimated_tokens || 0);
      directivesMeta.textContent = `Directives: ${info.path} (~${info.estimated_tokens} tok)`;
    } else if (info.configured) {
      state.directivesTokensEst = 0;
      directivesMeta.textContent = `Directives missing: ${info.path}`;
    } else {
      state.directivesTokensEst = 0;
      directivesMeta.textContent = "No Directives file linked";
    }
  } catch {
    state.directivesTokensEst = 0;
    directivesMeta.textContent = "";
  }
  emit("composer:updated");
}

async function refreshSessions() {
  if (!state.activeProjectId) return;
  state.sessions = await api.listSessions(state.activeProjectId);
  sessionList.innerHTML = "";
  for (const s of state.sessions) {
    const isApi = Boolean(s.api_enabled);
    const badgeClass = isApi ? "is-api" : "is-bridge";
    const badgeLabel = isApi ? "API" : "BRIDGE";
    const badgeTitle = isApi
      ? "Direct Gemini API session"
      : "Playground Bridge session (0 API tokens)";

    const row = document.createElement("div");
    row.className = `session-item ${s.id === state.activeSessionId ? "active" : ""}`;
    row.innerHTML = `
      <span class="session-mode-tag ${badgeClass}" title="${badgeTitle}">${badgeLabel}</span>
      <span class="session-item-title">${escapeHtml(s.title)}</span>
      <button class="btn btn-ghost btn-sm" title="Delete session">&times;</button>
    `;
    row.addEventListener("click", () => selectSession(s.id));
    row.querySelector("button").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await api.deleteSession(s.id);
      if (state.activeSessionId === s.id) {
        state.activeSessionId = null;
      }
      await refreshSessions();
      if (state.sessions.length > 0 && !state.activeSessionId) {
        await selectSession(state.sessions[0].id);
      } else if (state.sessions.length === 0) {
        clearActiveSession();
      }
    });
    sessionList.appendChild(row);
  }
}

export async function selectSession(sessionId) {
  const detail = await api.getSessionDetail(sessionId);
  state.activeSessionId = sessionId;
  state.activeSession = detail.session;
  state.messages = detail.messages || [];
  state.pendingGates = detail.pending_gates || [];
  state.gatesHiddenDueToError = false;
  state.lastErrorContext = null;
  if (detail.telemetry) {
    state.telemetry = detail.telemetry;
  }

  applySessionModeUI(detail.session);

  sessionTitleInput.disabled = false;
  sessionTitleInput.value = detail.session.title;

  if (detail.session.api_enabled) {
    if (!state.catalog.models || state.catalog.models.length === 0) {
      await refreshModelCatalog(false, false);
    }
    await reconcileSessionModel(detail.session);
    thinkingSelect.value = detail.session.thinking_level;
    autoReadInput.value = detail.session.auto_approve_read_tokens || 0;
  }

  renderTokenMeter();
  renderRateTelemetry(state.telemetry);
  await refreshSessions();
  emit("app:mode-changed", { apiEnabled: state.apiEnabled });
  emit("bridge:session-synced");
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
  const stats = s.token_stats || {
    input_tokens: 0,
    output_tokens: 0,
    thinking_tokens: 0,
    total_tokens: 0,
    last_context_tokens: 0,
  };
  const modelMeta = (state.catalog.models || []).find((m) => m.id === s.model);
  const limit = modelMeta ? modelMeta.context_limit : 1_048_576;
  const ctxUsed = stats.last_context_tokens || 0;
  const pct = Math.min(100, (ctxUsed / limit) * 100);
  tokenTotalLabel.textContent = `${ctxUsed.toLocaleString()} / ${limit.toLocaleString()} (${pct.toFixed(0)}%)`;
  tokenMeterFill.style.width = `${pct.toFixed(1)}%`;
  renderRateTelemetry(state.telemetry);
}

function formatCompactCount(n) {
  const val = Number(n || 0);
  if (val >= 1_000_000) return `${(val / 1_000_000).toFixed(1)}M`;
  if (val >= 10_000) return `${Math.round(val / 1_000)}K`;
  if (val >= 1_000) return `${(val / 1_000).toFixed(1)}K`;
  return String(val);
}

export function renderRateTelemetry(telemetry) {
  if (telemetry) {
    state.telemetry = telemetry;
  }
  const tel = state.telemetry || { rpm: 0, tpm: 0, rpd: 0, next_minute_reset_sec: 0, rpd_reset_label: "00:00 PT" };
  rpmValEl.textContent = String(tel.rpm || 0);
  tpmValEl.textContent = formatCompactCount(tel.tpm || 0);
  rpdValEl.textContent = String(tel.rpd || 0);

  const decaySec = Number(tel.next_minute_reset_sec || 0);
  const minNote = decaySec > 0 ? `60s (${decaySec}s)` : "60s idle";
  tokenBreakdown.textContent = `Resets: ${minNote} · ${tel.rpd_reset_label || "00:00 PT"}`;

  if (telemetryDecayTimer) {
    clearTimeout(telemetryDecayTimer);
    telemetryDecayTimer = null;
  }
  if (state.apiEnabled && ((tel.rpm || 0) > 0 || (tel.tpm || 0) > 0)) {
    const waitMs = Math.min(62000, Math.max(1500, (decaySec + 1) * 1000));
    telemetryDecayTimer = setTimeout(() => void refreshRateTelemetry(), waitMs);
  }
}

async function refreshRateTelemetry() {
  if (!state.apiEnabled) return;
  try {
    const activeModel = (state.activeSession && state.activeSession.model) || modelSelect.value || null;
    const tel = await api.getTelemetry(activeModel);
    renderRateTelemetry(tel);
  } catch {
    // ignore transient poll failure
  }
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
