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
const mainAiToggle = document.getElementById("btn-main-ai-toggle");
const mainStatsLine = document.getElementById("main-stats-line");
const mainDetailsBtn = document.getElementById("btn-main-details");
const scoutStatsLine = document.getElementById("scout-stats-line");
const scoutDetailsBtn = document.getElementById("btn-scout-details");
const aiDetailsPopover = document.getElementById("ai-details-popover");
const aiDetailsTitle = document.getElementById("ai-details-title");
const aiDetailsBody = document.getElementById("ai-details-body");
const closeAiDetailsBtn = document.getElementById("btn-close-ai-details");
const sessionTitleInput = document.getElementById("session-title-input");
const activeModeBadge = document.getElementById("active-session-mode-badge");
const refreshModelsBtn = document.getElementById("btn-refresh-models");

const projectModal = document.getElementById("project-modal");
const projectForm = document.getElementById("project-form");

const setupModal = document.getElementById("session-setup-modal");
const setupForm = document.getElementById("session-setup-form");
const setupTitleEl = document.getElementById("session-setup-title");
const setupSubtitleEl = document.getElementById("session-setup-subtitle");
const setupNameInput = document.getElementById("new-session-name");
const setupApiToggle = document.getElementById("new-session-api-toggle");
const setupModeHint = document.getElementById("new-session-mode-hint");
const setupErrorEl = document.getElementById("session-setup-error");
const setupCancelBtn = document.getElementById("btn-cancel-session-setup");
const setupConfirmBtn = document.getElementById("btn-confirm-session-setup");

let editingProjectId = null;
let telemetryDecayTimer = null;
let openDetailsTarget = null; // "main" | "scout" | null

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

  if (mainAiToggle) {
    mainAiToggle.checked = apiEnabled;
    mainAiToggle.disabled = !state.activeProjectId;
    mainAiToggle.title = apiEnabled
      ? "Main AI ON (Direct Gemini API) · Click to start a new Bridge session without Main AI"
      : "Main AI OFF (Playground Bridge) · Click to start a new API session with Main AI";
  }

  if (activeModeBadge) {
    activeModeBadge.classList.toggle("hidden", !hasSession);
    activeModeBadge.classList.toggle("is-api", apiEnabled);
    activeModeBadge.classList.toggle("is-bridge", !apiEnabled);
    activeModeBadge.textContent = apiEnabled ? "API" : "BRIDGE";
    activeModeBadge.title = apiEnabled
      ? "Direct Gemini API session"
      : "Playground Bridge session (0 Main AI tokens)";
  }
}

export async function initSessions() {
  ensureThinkingOptions();
  applySessionModeUI(null);

  if (refreshModelsBtn) {
    // Explicit manual probe only — 0 RPD burned automatically on startup
    refreshModelsBtn.addEventListener("click", () => void refreshModelCatalog(true, true));
  }
  if (mainAiToggle) {
    mainAiToggle.addEventListener("change", () => {
      const targetApi = Boolean(mainAiToggle.checked);
      // Revert visual switch until the user confirms creating the new session
      mainAiToggle.checked = Boolean(state.apiEnabled);
      openSessionSetupModal({
        apiEnabled: targetApi,
        modeSwitchConfirm: true,
      });
    });
  }
  if (mainDetailsBtn) {
    mainDetailsBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleDetailsPopover("main", mainDetailsBtn);
    });
  }
  if (scoutDetailsBtn) {
    scoutDetailsBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleDetailsPopover("scout", scoutDetailsBtn);
    });
  }
  if (closeAiDetailsBtn) {
    closeAiDetailsBtn.addEventListener("click", closeDetailsPopover);
  }
  document.addEventListener("click", (ev) => {
    if (!openDetailsTarget || !aiDetailsPopover || aiDetailsPopover.classList.contains("hidden")) {
      return;
    }
    const target = ev.target;
    if (
      aiDetailsPopover.contains(target) ||
      (mainDetailsBtn && mainDetailsBtn.contains(target)) ||
      (scoutDetailsBtn && scoutDetailsBtn.contains(target))
    ) {
      return;
    }
    closeDetailsPopover();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && openDetailsTarget) {
      closeDetailsPopover();
    }
  });

  on("models:refresh", (opts) => {
    const probe = Boolean(opts && opts.probe);
    void refreshModelCatalog(probe, probe);
  });

  on("app:mode-changed", ({ apiEnabled }) => {
    if (openDetailsTarget === "main" && !apiEnabled) {
      closeDetailsPopover();
    }
    if (apiEnabled || (state.scout && state.scout.enabled)) {
      void refreshRateTelemetry();
    }
    if (apiEnabled && (!state.catalog.models || state.catalog.models.length === 0)) {
      void refreshModelCatalog(false, false);
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

  sessionTitleInput.addEventListener("input", syncSessionTitleWidth);
  sessionTitleInput.addEventListener("change", async () => {
    syncSessionTitleWidth();
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
  on("telemetry:refresh", () => void refreshRateTelemetry());
  on("scout:changed", (cfg) => {
    if (openDetailsTarget === "scout" && !(cfg && cfg.enabled)) {
      closeDetailsPopover();
    }
    renderRateTelemetry();
  });

  await loadProjects();
}

function updateSetupModeHint() {
  const onApi = Boolean(setupApiToggle.checked);
  setupModeHint.textContent = onApi
    ? "ON · Direct Gemini API (model calls & rate telemetry)"
    : "OFF · Playground Bridge (0 Main AI tokens, copy/paste)";
}

function openSessionSetupModal(options = {}) {
  if (!state.activeProjectId) {
    openProjectModal(null);
    return;
  }
  const wantApi = Boolean(options.apiEnabled);
  if (setupTitleEl) {
    if (options.modeSwitchConfirm) {
      setupTitleEl.textContent = wantApi
        ? "New Session with Main AI?"
        : "New Session without Main AI?";
    } else {
      setupTitleEl.textContent = "Session Setup";
    }
  }
  if (setupSubtitleEl) {
    if (options.modeSwitchConfirm) {
      setupSubtitleEl.textContent = wantApi
        ? "Start a new Direct Gemini API session so session history stays clean."
        : "Start a new Playground Bridge session (copy/paste with Playground).";
      setupSubtitleEl.classList.remove("hidden");
    } else {
      setupSubtitleEl.textContent = "";
      setupSubtitleEl.classList.add("hidden");
    }
  }
  const defaultName = options.name !== undefined
    ? options.name
    : `Session ${state.sessions.length + 1}`;
  setupNameInput.value = defaultName;
  setupApiToggle.checked = wantApi;
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
  state.checkpoints = [];
  state.gatesHiddenDueToError = false;
  state.lastErrorContext = null;
  sessionTitleInput.disabled = true;
  sessionTitleInput.value = "Create a session (+ New)";
  syncSessionTitleWidth();
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
      state.directivesInfoText = `${info.path} (~${info.estimated_tokens} tok)`;
    } else if (info.configured) {
      state.directivesTokensEst = 0;
      state.directivesInfoText = `Directives missing: ${info.path}`;
    } else {
      state.directivesTokensEst = 0;
      state.directivesInfoText = "No Directives file linked";
    }
  } catch {
    state.directivesTokensEst = 0;
    state.directivesInfoText = "";
  }
  emit("bridge:session-synced");
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
      : "Playground Bridge session (0 Main AI tokens)";

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
  state.checkpoints = detail.checkpoints || [];
  state.gatesHiddenDueToError = false;
  state.lastErrorContext = null;
  if (detail.telemetry) {
    state.telemetry = detail.telemetry;
  }

  applySessionModeUI(detail.session);

  sessionTitleInput.disabled = false;
  sessionTitleInput.value = detail.session.title;
  syncSessionTitleWidth();

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

function formatShortTelemetryModel(modelId) {
  if (!modelId) return "";
  return String(modelId).replace(/^gemini-/, "");
}

function renderModelTableHtml(rows) {
  if (!rows || rows.length === 0) {
    return `<div class="meta-line">No model usage recorded today.</div>`;
  }
  const head = `
    <div class="scout-model-row is-head">
      <span class="scout-model-name">Model</span>
      <span class="scout-model-num">Tok</span>
      <span class="scout-model-num">RPM</span>
      <span class="scout-model-num">TPM</span>
    </div>`;
  const body = rows
    .map((m) => {
      const shortName = formatShortTelemetryModel(m.model) || m.model;
      const mTok = formatCompactCount(m.total_tokens || 0);
      const mRpm = Number(m.rpm || 0);
      const mTpm = formatCompactCount(m.tpm || 0);
      const mTip = `${m.model} · RPD: ${m.rpd || 0} · In: ${Number(m.input_tokens || 0).toLocaleString()} · Out: ${Number(m.output_tokens || 0).toLocaleString()} · Think: ${Number(m.thinking_tokens || 0).toLocaleString()}`;
      return `
        <div class="scout-model-row" title="${escapeHtml(mTip)}">
          <span class="scout-model-name mono">${escapeHtml(shortName)}</span>
          <span class="scout-model-num mono tabular">${escapeHtml(mTok)}</span>
          <span class="scout-model-num mono tabular">${escapeHtml(String(mRpm))}</span>
          <span class="scout-model-num mono tabular">${escapeHtml(mTpm)}</span>
        </div>`;
    })
    .join("");
  return `<div class="scout-model-list">${head}${body}</div>`;
}

function renderScoutTelemetry(tel) {
  if (!scoutStatsLine) return;
  const scoutTel = (tel && tel.scout) || {
    rpm: 0,
    tpm: 0,
    total_tokens: 0,
  };
  const rpm = Number(scoutTel.rpm || 0);
  const tpm = formatCompactCount(scoutTel.tpm || 0);
  const tok = formatCompactCount(scoutTel.total_tokens || 0);
  scoutStatsLine.textContent = `${rpm} RPM · ${tpm} TPM · ${tok} tok`;
}

function renderAiDetailsPopoverContent() {
  if (!openDetailsTarget || !aiDetailsPopover || !aiDetailsBody || !aiDetailsTitle) return;
  const tel = state.telemetry || { rpm: 0, tpm: 0, rpd: 0, next_minute_reset_sec: 0, rpd_reset_label: "00:00 PT", models: [] };

  if (openDetailsTarget === "scout") {
    const scoutCfg = state.scout || {};
    const scoutTel = tel.scout || {
      active_model: scoutCfg.active_model || null,
      rpm: 0,
      tpm: 0,
      rpd: 0,
      input_tokens: 0,
      output_tokens: 0,
      thinking_tokens: 0,
      total_tokens: 0,
      next_minute_reset_sec: 0,
      models: [],
    };
    let perModel = Array.isArray(scoutTel.models) ? scoutTel.models.slice() : [];
    if (perModel.length === 0 && scoutCfg.enabled && scoutCfg.active_model) {
      perModel = [
        { model: scoutCfg.active_model, rpm: 0, tpm: 0, rpd: 0, input_tokens: 0, output_tokens: 0, thinking_tokens: 0, total_tokens: 0 },
      ];
    }
    aiDetailsTitle.textContent = "Scout AI Details";
    const sDecay = Number(scoutTel.next_minute_reset_sec || 0);
    const sMinNote = sDecay > 0 ? `60s (${sDecay}s)` : "60s idle";
    const activeLbl = formatShortTelemetryModel(scoutTel.active_model || scoutCfg.active_model) || "-lite";
    aiDetailsBody.innerHTML = `
      <div class="meta-line mono">Active: ${escapeHtml(activeLbl)} · RPD: ${scoutTel.rpd || 0}</div>
      <div class="rate-telemetry-grid">
        <div class="rate-cell"><span class="rate-lbl">RPM</span><span class="rate-val mono tabular">${Number(scoutTel.rpm || 0)}</span></div>
        <div class="rate-cell"><span class="rate-lbl">TPM</span><span class="rate-val mono tabular">${escapeHtml(formatCompactCount(scoutTel.tpm || 0))}</span></div>
        <div class="rate-cell"><span class="rate-lbl">Tokens</span><span class="rate-val mono tabular">${escapeHtml(formatCompactCount(scoutTel.total_tokens || 0))}</span></div>
      </div>
      <div class="meta-line mono tabular">In: ${Number(scoutTel.input_tokens || 0).toLocaleString()} · Out: ${Number(scoutTel.output_tokens || 0).toLocaleString()} · Think: ${Number(scoutTel.thinking_tokens || 0).toLocaleString()}</div>
      ${renderModelTableHtml(perModel)}
      <div class="meta-line mono tabular">Resets: ${escapeHtml(sMinNote)} · ${escapeHtml(tel.rpd_reset_label || "00:00 PT")}</div>
    `;
    return;
  }

  aiDetailsTitle.textContent = "Main AI Details";
  const activeModel = (state.activeSession && state.activeSession.model) || modelSelect.value || tel.model || "-";
  const allModels = Array.isArray(tel.models) ? tel.models : [];
  const activeRow = allModels.find((m) => m.model === activeModel) || {
    input_tokens: 0,
    output_tokens: 0,
    thinking_tokens: 0,
    total_tokens: 0,
  };
  const decaySec = Number(tel.next_minute_reset_sec || 0);
  const minNote = decaySec > 0 ? `60s (${decaySec}s)` : "60s idle";
  aiDetailsBody.innerHTML = `
    <div class="meta-line mono">Model: ${escapeHtml(formatShortTelemetryModel(activeModel) || activeModel)}</div>
    <div class="rate-telemetry-grid">
      <div class="rate-cell"><span class="rate-lbl">RPM</span><span class="rate-val mono tabular">${Number(tel.rpm || 0)}</span></div>
      <div class="rate-cell"><span class="rate-lbl">TPM</span><span class="rate-val mono tabular">${escapeHtml(formatCompactCount(tel.tpm || 0))}</span></div>
      <div class="rate-cell"><span class="rate-lbl">RPD</span><span class="rate-val mono tabular">${Number(tel.rpd || 0)}</span></div>
    </div>
    <div class="meta-line mono tabular">Today · In: ${Number(activeRow.input_tokens || 0).toLocaleString()} · Out: ${Number(activeRow.output_tokens || 0).toLocaleString()} · Think: ${Number(activeRow.thinking_tokens || 0).toLocaleString()}</div>
    ${renderModelTableHtml(allModels)}
    <div class="meta-line mono tabular">Resets: ${escapeHtml(minNote)} · ${escapeHtml(tel.rpd_reset_label || "00:00 PT")}</div>
  `;
}

function positionDetailsPopover(anchorBtn) {
  if (!aiDetailsPopover || !anchorBtn) return;
  const rect = anchorBtn.getBoundingClientRect();
  aiDetailsPopover.style.left = `${Math.min(window.innerWidth - 306, Math.max(12, rect.right + 12))}px`;
  const popH = aiDetailsPopover.offsetHeight || 240;
  const top = Math.max(12, Math.min(window.innerHeight - popH - 12, rect.top - 48));
  aiDetailsPopover.style.top = `${top}px`;
}

function closeDetailsPopover() {
  openDetailsTarget = null;
  if (aiDetailsPopover) aiDetailsPopover.classList.add("hidden");
  if (mainDetailsBtn) mainDetailsBtn.setAttribute("aria-expanded", "false");
  if (scoutDetailsBtn) scoutDetailsBtn.setAttribute("aria-expanded", "false");
}

function toggleDetailsPopover(target, anchorBtn) {
  if (!aiDetailsPopover) return;
  if (openDetailsTarget === target && !aiDetailsPopover.classList.contains("hidden")) {
    closeDetailsPopover();
    return;
  }
  openDetailsTarget = target;
  if (mainDetailsBtn) mainDetailsBtn.setAttribute("aria-expanded", String(target === "main"));
  if (scoutDetailsBtn) scoutDetailsBtn.setAttribute("aria-expanded", String(target === "scout"));
  renderAiDetailsPopoverContent();
  aiDetailsPopover.classList.remove("hidden");
  positionDetailsPopover(anchorBtn);
}

export function renderRateTelemetry(telemetry) {
  if (telemetry && Object.keys(telemetry).length > 0) {
    state.telemetry = telemetry;
  }
  const tel = state.telemetry || { rpm: 0, tpm: 0, rpd: 0, next_minute_reset_sec: 0, rpd_reset_label: "00:00 PT" };
  const decaySec = Number(tel.next_minute_reset_sec || 0);
  if (mainStatsLine) {
    mainStatsLine.textContent = `${Number(tel.rpm || 0)} RPM · ${formatCompactCount(tel.tpm || 0)} TPM · ${Number(tel.rpd || 0)} RPD`;
  }

  renderScoutTelemetry(tel);
  if (openDetailsTarget) {
    renderAiDetailsPopoverContent();
  }

  if (telemetryDecayTimer) {
    clearTimeout(telemetryDecayTimer);
    telemetryDecayTimer = null;
  }
  const scoutTel = tel.scout || {};
  const mainActive = state.apiEnabled && ((tel.rpm || 0) > 0 || (tel.tpm || 0) > 0);
  const scoutActive = (scoutTel.rpm || 0) > 0 || (scoutTel.tpm || 0) > 0;
  if (mainActive || scoutActive) {
    const activeWaits = [];
    if (mainActive) activeWaits.push(decaySec);
    if (scoutActive) activeWaits.push(Number(scoutTel.next_minute_reset_sec || 0));
    const soonest = Math.min(...activeWaits);
    const waitMs = Math.min(62000, Math.max(1500, (soonest + 1) * 1000));
    telemetryDecayTimer = setTimeout(() => void refreshRateTelemetry(), waitMs);
  }
}

async function refreshRateTelemetry() {
  const scoutTel = (state.telemetry && state.telemetry.scout) || {};
  const hasActiveScoutWindow = (scoutTel.rpm || 0) > 0 || (scoutTel.tpm || 0) > 0;
  if (!state.apiEnabled && !(state.scout && state.scout.enabled) && !hasActiveScoutWindow) {
    return;
  }
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
  document.getElementById("proj-post-cmd").value = (proj && proj.post_patch_command) || "";
  document.getElementById("proj-fast-cmd").value = (proj && proj.fast_test_command) || "";
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
    post_patch_command: document.getElementById("proj-post-cmd").value.trim() || null,
    fast_test_command: document.getElementById("proj-fast-cmd").value.trim() || null,
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

function syncSessionTitleWidth() {
  if (!sessionTitleInput) return;
  const len = (sessionTitleInput.value || "").length;
  sessionTitleInput.size = Math.min(54, Math.max(8, len + 1));
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
