// settings.js - Gemini API Key modal & key verification (0 startup API calls)

import { api } from "./api.js";
import { on, state } from "./state.js";

const keyBtn = document.getElementById("btn-api-key");
const modal = document.getElementById("api-key-modal");
const form = document.getElementById("api-key-form");
const keyInput = document.getElementById("api-key-input");
const toggleBtn = document.getElementById("btn-toggle-key-vis");
const statusMsg = document.getElementById("api-key-status-msg");
const errorEl = document.getElementById("api-key-error");
const clearBtn = document.getElementById("btn-clear-api-key");
const cancelBtn = document.getElementById("btn-cancel-api-key");
const saveBtn = document.getElementById("btn-save-api-key");

let keyPromptResolver = null;

function resolvePendingKeyPrompt(configured) {
  if (keyPromptResolver) {
    const resolve = keyPromptResolver;
    keyPromptResolver = null;
    resolve(Boolean(configured));
  }
}

export async function initSettings() {
  keyBtn.addEventListener("click", () => openKeyModal(false));
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

  await refreshKeyStatus();
}

export async function refreshKeyStatus() {
  try {
    const status = await api.getApiKeyStatus(false);
    state.hasApiKey = Boolean(status && status.configured);
    updateButtonBadge(status);
    return status;
  } catch {
    state.hasApiKey = false;
    updateButtonBadge({ configured: false });
    return { configured: false };
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
