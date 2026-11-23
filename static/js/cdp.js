// cdp.js - Chrome DevTools Protocol (:9222) tab picker, Screenshot, and Console capture

import { api } from "./api.js";
import { emit, getActiveProject, on } from "./state.js";

const tabSelect = document.getElementById("cdp-tab-select");
const refreshBtn = document.getElementById("btn-cdp-refresh");
const shotBtn = document.getElementById("btn-cdp-shot");
const consoleBtn = document.getElementById("btn-cdp-console");

export function initCdp() {
  refreshBtn.addEventListener("click", refreshTabs);
  shotBtn.addEventListener("click", captureScreenshot);
  consoleBtn.addEventListener("click", captureConsole);
  on("project:changed", refreshTabs);
  refreshTabs();
}

function getCdpPort() {
  const proj = getActiveProject();
  return (proj && proj.cdp_port) || 9222;
}

export async function refreshTabs() {
  const port = getCdpPort();
  try {
    const res = await api.cdpTabs(port);
    const hasTabs = Boolean(res.connected && res.tabs && res.tabs.length > 0);
    tabSelect.classList.toggle("hidden", !hasTabs);
    shotBtn.classList.toggle("hidden", !hasTabs);
    consoleBtn.classList.toggle("hidden", !hasTabs);
    refreshBtn.innerHTML = hasTabs ? "&#8635;" : "Chrome &#8635;";
    refreshBtn.title = hasTabs
      ? `Refresh Chrome :${port} tabs`
      : `Chrome :${port} offline — click to check debug tabs`;
    if (!hasTabs) {
      tabSelect.innerHTML = "";
      return;
    }
    const prev = tabSelect.value;
    tabSelect.innerHTML = res.tabs
      .map((t) => `<option value="${escapeAttr(t.ws_url)}">${escapeHtml(t.title || t.url)}</option>`)
      .join("");
    if (prev && res.tabs.some((t) => t.ws_url === prev)) {
      tabSelect.value = prev;
    }
  } catch {
    tabSelect.classList.add("hidden");
    shotBtn.classList.add("hidden");
    consoleBtn.classList.add("hidden");
    tabSelect.innerHTML = "";
    refreshBtn.innerHTML = "Chrome &#8635;";
    refreshBtn.title = `Chrome :${port} offline — click to check debug tabs`;
  }
}

async function captureScreenshot() {
  const wsUrl = tabSelect.value;
  if (!wsUrl) {
    emit("chat:error", "No Chrome :9222 tab selected. Click Chrome refresh once Chrome is open in debug mode.");
    return;
  }
  shotBtn.disabled = true;
  shotBtn.textContent = "Capturing...";
  try {
    const res = await api.cdpScreenshot(wsUrl, false);
    emit("composer:attach", {
      kind: "image",
      label: `cdp-screenshot-${new Date().toLocaleTimeString()}.png`,
      content: `data:${res.mime_type};base64,${res.base64_data}`,
      mime_type: res.mime_type,
      estimated_tokens: res.estimated_tokens,
    });
  } catch (err) {
    emit("chat:error", err.message);
  } finally {
    shotBtn.disabled = false;
    shotBtn.textContent = "Screenshot";
  }
}

async function captureConsole() {
  const wsUrl = tabSelect.value;
  if (!wsUrl) {
    emit("chat:error", "No Chrome :9222 tab selected.");
    return;
  }
  consoleBtn.disabled = true;
  consoleBtn.textContent = "Reading...";
  try {
    const res = await api.cdpConsole(wsUrl);
    emit("composer:paste-text", `[Chrome Console (${res.count} entries · ~${res.estimated_tokens} tok)]\n${res.text}`);
  } catch (err) {
    emit("chat:error", err.message);
  } finally {
    consoleBtn.disabled = false;
    consoleBtn.textContent = "Console";
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(str) {
  return escapeHtml(str);
}
