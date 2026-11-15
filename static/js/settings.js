// settings.js - Gemini API key modal and startup validation check

import { api } from "./api.js";
import { on } from "./state.js";

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

export async function initSettings() {
  keyBtn.addEventListener("click", () => openKeyModal(false));
  cancelBtn.addEventListener("click", () => modal.close());
  toggleBtn.addEventListener("click", () => {
    const hidden = keyInput.type === "password";
    keyInput.type = hidden ? "text" : "password";
    toggleBtn.textContent = hidden ? "Hide" : "Show";
  });
  clearBtn.addEventListener("click", handleClearKey);
  form.addEventListener("submit", handleSaveKey);

  on("settings:open-key-modal", (reason) => openKeyModal(true, reason));

  await checkKeyOnStartup();
}

async function checkKeyOnStartup() {
  try {
    const status = await api.getApiKeyStatus(true);
    updateButtonBadge(status);
    if (!status.configured || status.valid === false) {
      openKeyModal(true, status.error || "Enter your Gemini API key to begin.");
    }
  } catch (err) {
    openKeyModal(true, err.message);
  }
}

async function openKeyModal(forceNotice = false, customError = "") {
  errorEl.textContent = customError || "";
  keyInput.value = "";
  keyInput.type = "password";
  toggleBtn.textContent = "Show";

  try {
    const status = await api.getApiKeyStatus(false);
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
    updateButtonBadge({ configured: true, valid: true, masked_preview: res.masked_preview });
    modal.close();
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
    updateButtonBadge({ configured: false, valid: false });
    statusMsg.textContent = "Saved key removed. Enter a new Gemini API key below.";
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

function updateButtonBadge(status) {
  if (status && status.configured && status.valid !== false) {
    keyBtn.textContent = "Key ✓";
    keyBtn.title = `Gemini API Key configured (${status.masked_preview || status.source})`;
  } else {
    keyBtn.textContent = "Key !";
    keyBtn.title = "Gemini API Key missing or invalid — click to configure";
  }
}
