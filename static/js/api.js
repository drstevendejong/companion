// api.js - HTTP client wrapper for companion backend

export async function requestJson(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

export const api = {
  // Projects & Filesystem Picker
  listProjects: () => requestJson("/api/projects"),
  createProject: (body) => requestJson("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  updateProject: (id, body) => requestJson(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  getDirectives: (id) => requestJson(`/api/projects/${id}/directives`),
  browseFs: (path = "", includeFiles = true) =>
    requestJson(`/api/projects/browse-fs?path=${encodeURIComponent(path)}&include_files=${includeFiles ? "true" : "false"}`),

  // Sessions
  getModelCatalog: () => requestJson("/api/sessions/models"),
  listSessions: (projectId) => requestJson(`/api/sessions?project_id=${encodeURIComponent(projectId)}`),
  createSession: (body) => requestJson("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  getSessionDetail: (id) => requestJson(`/api/sessions/${id}`),
  updateSession: (id, body) => requestJson(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSession: (id) => requestJson(`/api/sessions/${id}`, { method: "DELETE" }),

  // Chat & Tool Gates
  sendChat: (body) => requestJson("/api/chat/send", { method: "POST", body: JSON.stringify(body) }),
  resolveGate: (gateId, body) => requestJson(`/api/chat/gate/${gateId}`, { method: "POST", body: JSON.stringify(body) }),

  // Workspace
  getTree: (projectId, subpath = ".", maxDepth = 0) =>
    requestJson(
      `/api/workspace/tree?project_id=${encodeURIComponent(projectId)}&subpath=${encodeURIComponent(subpath)}&max_depth=${maxDepth}`
    ),
  previewFile: (projectId, path) =>
    requestJson(`/api/workspace/file-preview?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`),
  runCommand: (projectId, command) =>
    requestJson("/api/workspace/run", { method: "POST", body: JSON.stringify({ project_id: projectId, command }) }),
  listBackups: (projectId) => requestJson(`/api/workspace/backups?project_id=${encodeURIComponent(projectId)}`),
  undoPatch: (patchId) => requestJson(`/api/workspace/undo/${patchId}`, { method: "POST" }),

  // Git
  gitStatus: (projectId) => requestJson(`/api/git/status?project_id=${encodeURIComponent(projectId)}`),
  gitDraftMessage: (projectId, model) =>
    requestJson("/api/git/draft-message", { method: "POST", body: JSON.stringify({ project_id: projectId, model }) }),
  gitCommit: (body) => requestJson("/api/git/commit", { method: "POST", body: JSON.stringify(body) }),

  // CDP
  cdpTabs: (port) => requestJson(`/api/cdp/tabs?cdp_port=${encodeURIComponent(port)}`),
  cdpScreenshot: (wsUrl, fullPage = false) =>
    requestJson("/api/cdp/screenshot", { method: "POST", body: JSON.stringify({ ws_url: wsUrl, full_page: fullPage }) }),
  cdpConsole: (wsUrl) =>
    requestJson("/api/cdp/console", { method: "POST", body: JSON.stringify({ ws_url: wsUrl }) }),

  // Settings (API Key)
  getApiKeyStatus: (verify = false) => requestJson(`/api/settings/api-key?verify=${verify ? "true" : "false"}`),
  saveApiKey: (apiKey, validateKey = true) =>
    requestJson("/api/settings/api-key", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey, validate_key: validateKey }),
    }),
  clearApiKey: () => requestJson("/api/settings/api-key", { method: "DELETE" }),
};
