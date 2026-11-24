// settings.js - Gemini API Key modal, verification, & Local AI Scout settings (0 startup API calls)

import { api } from "./api.js";
import { emit, on, state } from "./state.js";

const keyBtn = document.getElementById("btn-api-key");
const scoutToggleBtn = document.getElementById("btn-scout-toggle");
const scoutAutoBtn = document.getElementById("btn-scout-auto");
const scoutPanelBody = document.getElementById("scout-panel-body");
const scoutModelBadge = document.getElementById("scout-telemetry-model");
const modal = document.getElementById("api-key-modal");
const form = document.getElementById("api-key-form");
const keyInput = document.getElementById("api-key-input");
const toggleBtn = document.getElementById("btn-toggle-key-vis");
const statusMsg = document.getElementById("api-key-status-msg");
const errorEl = document.getElementById("api-key-error");
const clearBtn = document.getElementById("btn-clear-api-key");
const cancelBtn = document.getElementById("btn-cancel-api-key");
const saveBtn = document.getElementById("btn-save-api-key");

const companionSettingsBtn = document.getElementById("btn-companion-settings");
const companionModal = document.getElementById("companion-settings-modal");
const companionForm = document.getElementById("companion-settings-form");
const guardrailInput = document.getElementById("setting-bridge-guardrail");
const systemPromptInput = document.getElementById("setting-system-prompt");
const bridgeContractInput = document.getElementById("setting-bridge-contract");
const resetGuardrailBtn = document.getElementById("btn-reset-guardrail");
const resetSystemPromptBtn = document.getElementById("btn-reset-system-prompt");
const resetBridgeContractBtn = document.getElementById("btn-reset-bridge-contract");
const resetAllSettingsBtn = document.getElementById("btn-reset-all-settings");
const cancelCompanionSettingsBtn = document.getElementById("btn-cancel-companion-settings");
const saveCompanionSettingsBtn = document.getElementById("btn-save-companion-settings");
const companionSettingsError = document.getElementById("companion-settings-error");

let keyPromptResolver = null;
let companionDefaults = {
  bridge_guardrail: "",
  system_prompt: "",
  bridge_tool_contract: "",
};

function resolvePendingKeyPrompt(configured) {
  if (keyPromptResolver) {
    const resolve = keyPromptResolver;
    keyPromptResolver = null;
    resolve(Boolean(configured));
  }
}

export async function initSettings() {
  keyBtn.addEventListener("click", () => openKeyModal(false));
  if (scoutToggleBtn) {
    scoutToggleBtn.addEventListener("change", handleScoutToggleClick);
  }
  if (scoutAutoBtn) {
    scoutAutoBtn.addEventListener("change", handleScoutAutoClick);
  }

  cancelBtn.addEventListener("click", () => {
    modal.close();
    resolvePendingKeyPrompt(false);
  });
  modal.addEventListener("close", () => {
    resolvePendingKeyPrompt(false);
  });

  toggleBtn.addEventListener("click", () => {
    const hidden = keyInput.type === "password";
    keyInput.type = hidden ? "text" : "password";
    toggleBtn.textContent = hidden ? "Hide" : "Show";
  });
  clearBtn.addEventListener("click", handleClearKey);
  form.addEventListener("submit", handleSaveKey);

  on("settings:open-key-modal", (reason) => openKeyModal(true, reason));
  on("scout:refresh", refreshScoutSettings);

  if (companionSettingsBtn && companionModal) {
    companionSettingsBtn.addEventListener("click", () => openCompanionSettingsModal());
  }
  if (cancelCompanionSettingsBtn && companionModal) {
    cancelCompanionSettingsBtn.addEventListener("click", () => companionModal.close());
  }
  if (resetGuardrailBtn && guardrailInput) {
    resetGuardrailBtn.addEventListener("click", () => {
      guardrailInput.value = companionDefaults.bridge_guardrail || "";
    });
  }
  if (resetSystemPromptBtn && systemPromptInput) {
    resetSystemPromptBtn.addEventListener("click", () => {
      systemPromptInput.value = companionDefaults.system_prompt || "";
    });
  }
  if (resetBridgeContractBtn && bridgeContractInput) {
    resetBridgeContractBtn.addEventListener("click", () => {
      bridgeContractInput.value = companionDefaults.bridge_tool_contract || "";
    });
  }
  if (resetAllSettingsBtn) {
    resetAllSettingsBtn.addEventListener("click", handleResetAllCompanionSettings);
  }
  if (companionForm) {
    companionForm.addEventListener("submit", handleSaveCompanionSettings);
  }

  await refreshKeyStatus();
  await refreshScoutSettings();
}

export async function refreshKeyStatus() {
  try {
    const status = await api.getApiKeyStatus(false);
    state.hasApiKey = Boolean(status && status.configured);
    updateButtonBadge(status);
    emit("settings:key-changed");
    return status;
  } catch {
    state.hasApiKey = false;
    updateButtonBadge({ configured: false });
    emit("settings:key-changed");
    return { configured: false };
  }
}

export async function refreshScoutSettings() {
  let telemetry = null;
  try {
    const cfg = await api.getScoutSettings();
    state.scout = cfg || {
      enabled: false,
      configured_enabled: false,
      auto_approve: false,
      active_model: null,
      max_tokens: 30000,
      available_lite_models: [],
    };
    telemetry = cfg && cfg.telemetry ? cfg.telemetry : null;
  } catch {
    state.scout = {
      enabled: false,
      configured_enabled: false,
      auto_approve: false,
      active_model: null,
      max_tokens: 30000,
      available_lite_models: [],
    };
  }
  if (telemetry) {
    emit("telemetry:updated", telemetry);
  }
  renderScoutToggleButtons();
  emit("scout:changed", state.scout);
  return state.scout;
}

function formatShortModel(modelId) {
  if (!modelId) return "";
  return String(modelId).replace(/^gemini-/, "");
}

function renderScoutToggleButtons() {
  if (!scoutToggleBtn || !scoutAutoBtn) return;
  const s = state.scout || {};
  const enabled = Boolean(s.enabled);
  const autoApprove = Boolean(s.auto_approve);
  const shortModel = formatShortModel(s.active_model);

  scoutToggleBtn.checked = enabled;
  scoutToggleBtn.title = enabled
    ? `Scout AI ON · Model: ${s.active_model || "-lite"} (LOW/MINIMAL thinking) · Click to turn Off`
    : state.hasApiKey
      ? "Scout AI OFF · Click to enable lightweight -lite workspace search, output triage & header updates"
      : "Scout AI OFF · Click to save a Gemini API key and enable Scout (works in both Bridge & API modes)";

  if (scoutPanelBody) {
    scoutPanelBody.classList.toggle("hidden", !enabled);
  }
  if (scoutModelBadge) {
    scoutModelBadge.classList.toggle("hidden", !enabled);
    if (enabled) {
      scoutModelBadge.textContent = shortModel || "-lite";
      scoutModelBadge.title = `Active Scout model: ${s.active_model || "-lite"}`;
    }
  }

  scoutAutoBtn.checked = enabled && autoApprove;
  scoutAutoBtn.disabled = !enabled;
  scoutAutoBtn.title = autoApprove
    ? "Auto-run prompts ON: scout_workspace prompts run automatically without pausing at a gate"
    : "Auto-run prompts OFF: pause at a gate before running scout_workspace prompts";
}

async function handleScoutToggleClick() {
  const currentlyEnabled = Boolean(state.scout && state.scout.enabled);
  const targetEnabled = !currentlyEnabled;
  if (targetEnabled && !state.hasApiKey) {
    if (scoutToggleBtn) scoutToggleBtn.checked = false;
    const saved = await requestApiKeyForSessionSetup(
      "Save a Gemini API key to enable Local AI Scout (uses lightweight -lite models in both Bridge & API modes)."
    );
    if (!saved) {
      if (scoutToggleBtn) scoutToggleBtn.checked = false;
      return;
    }
  }

  if (scoutToggleBtn) scoutToggleBtn.disabled = true;
  try {
    const cfg = await api.updateScoutSettings({ enabled: targetEnabled });
    state.scout = cfg;
    if (cfg && cfg.telemetry) {
      emit("telemetry:updated", cfg.telemetry);
    }
    renderScoutToggleButtons();
    emit("scout:changed", state.scout);
  } catch (err) {
    if (scoutToggleBtn) scoutToggleBtn.checked = currentlyEnabled;
    emit("chat:error", err.message);
  } finally {
    if (scoutToggleBtn) scoutToggleBtn.disabled = false;
  }
}

async function handleScoutAutoClick() {
  if (!state.scout || !state.scout.enabled) {
    if (scoutAutoBtn) scoutAutoBtn.checked = false;
    return;
  }
  const prevAuto = Boolean(state.scout.auto_approve);
  const nextAuto = !prevAuto;
  if (scoutAutoBtn) scoutAutoBtn.disabled = true;
  try {
    const cfg = await api.updateScoutSettings({ auto_approve: nextAuto });
    state.scout = cfg;
    if (cfg && cfg.telemetry) {
      emit("telemetry:updated", cfg.telemetry);
    }
    renderScoutToggleButtons();
    emit("scout:changed", state.scout);
  } catch (err) {
    if (scoutAutoBtn) scoutAutoBtn.checked = prevAuto;
    emit("chat:error", err.message);
  } finally {
    if (scoutAutoBtn) scoutAutoBtn.disabled = false;
  }
}

export function requestApiKeyForSessionSetup(reason = "") {
  resolvePendingKeyPrompt(false);
  return new Promise((resolve) => {
    keyPromptResolver = resolve;
    void openKeyModal(true, reason);
  });
}

async function openKeyModal(forceNotice = false, customError = "") {
  errorEl.textContent = customError || "";
  keyInput.value = "";
  keyInput.type = "password";
  toggleBtn.textContent = "Show";

  try {
    const status = await api.getApiKeyStatus(false);
    state.hasApiKey = Boolean(status.configured);
    updateButtonBadge(status);
    emit("settings:key-changed");
    if (status.configured) {
      statusMsg.textContent = `Current key (${status.source}): ${status.masked_preview} · Stored at ${status.key_file}`;
    } else {
      statusMsg.textContent = `No key configured yet. Paste your Gemini API key below (saved locally to ${status.key_file}).`;
    }
  } catch {
    if (!forceNotice) {
      statusMsg.textContent = "Stored locally in data/gemini_key.json.";
    }
  }

  if (!modal.open) {
    modal.showModal();
  }
  keyInput.focus();
}

async function handleSaveKey(ev) {
  ev.preventDefault();
  const raw = keyInput.value.trim();
  if (!raw) return;

  errorEl.textContent = "";
  saveBtn.disabled = true;
  saveBtn.textContent = "Verifying...";

  try {
    const res = await api.saveApiKey(raw, true);
    state.hasApiKey = true;
    updateButtonBadge({
      configured: true,
      valid: true,
      masked_preview: res.masked_preview,
      source: res.source,
    });
    emit("settings:key-changed");
    await refreshScoutSettings();
    const resolver = keyPromptResolver;
    keyPromptResolver = null;
    modal.close();
    if (resolver) {
      resolver(true);
    }
  } catch (err) {
    errorEl.textContent = err.message;
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "Save & Verify";
  }
}

async function handleClearKey() {
  errorEl.textContent = "";
  try {
    await api.clearApiKey();
    state.hasApiKey = false;
    updateButtonBadge({ configured: false });
    emit("settings:key-changed");
    await refreshScoutSettings();
    statusMsg.textContent = "Saved key removed. Only Playground Bridge sessions can run until a key is saved.";
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

function updateButtonBadge(status) {
  if (status && status.configured && status.valid !== false) {
    keyBtn.textContent = "Key ✓";
    keyBtn.title = `Gemini API Key configured (${status.masked_preview || status.source})`;
  } else {
    keyBtn.textContent = "Key";
    keyBtn.title = "No Gemini API key configured (optional for Playground Bridge sessions)";
  }
}

async function openCompanionSettingsModal() {
  if (!companionModal) return;
  if (companionSettingsError) companionSettingsError.textContent = "";
  try {
    const res = await api.getCompanionSettings();
    companionDefaults = (res && res.defaults) || companionDefaults;
    if (guardrailInput) guardrailInput.value = res.bridge_guardrail ?? "";
    if (systemPromptInput) systemPromptInput.value = res.system_prompt ?? "";
    if (bridgeContractInput) bridgeContractInput.value = res.bridge_tool_contract ?? "";
  } catch (err) {
    if (companionSettingsError) {
      companionSettingsError.textContent = err.message || "Failed to load Companion settings.";
    }
  }
  if (!companionModal.open) {
    companionModal.showModal();
  }
}

async function handleSaveCompanionSettings(ev) {
  ev.preventDefault();
  if (!companionModal) return;
  if (companionSettingsError) companionSettingsError.textContent = "";
  if (saveCompanionSettingsBtn) {
    saveCompanionSettingsBtn.disabled = true;
    saveCompanionSettingsBtn.textContent = "Saving...";
  }
  try {
    const res = await api.updateCompanionSettings({
      bridge_guardrail: guardrailInput ? guardrailInput.value : undefined,
      system_prompt: systemPromptInput ? systemPromptInput.value : undefined,
      bridge_tool_contract: bridgeContractInput ? bridgeContractInput.value : undefined,
    });
    if (res && res.defaults) companionDefaults = res.defaults;
    companionModal.close();
  } catch (err) {
    if (companionSettingsError) {
      companionSettingsError.textContent = err.message || "Failed to save settings.";
    }
  } finally {
    if (saveCompanionSettingsBtn) {
      saveCompanionSettingsBtn.disabled = false;
      saveCompanionSettingsBtn.textContent = "Save Settings";
    }
  }
}

async function handleResetAllCompanionSettings() {
  if (companionSettingsError) companionSettingsError.textContent = "";
  if (resetAllSettingsBtn) resetAllSettingsBtn.disabled = true;
  try {
    const res = await api.updateCompanionSettings({
      reset_keys: ["bridge_guardrail", "system_prompt", "bridge_tool_contract"],
    });
    companionDefaults = (res && res.defaults) || companionDefaults;
    if (guardrailInput) guardrailInput.value = res.bridge_guardrail ?? "";
    if (systemPromptInput) systemPromptInput.value = res.system_prompt ?? "";
    if (bridgeContractInput) bridgeContractInput.value = res.bridge_tool_contract ?? "";
  } catch (err) {
    if (companionSettingsError) {
      companionSettingsError.textContent = err.message || "Failed to reset defaults.";
    }
  } finally {
    if (resetAllSettingsBtn) resetAllSettingsBtn.disabled = false;
  }
}

