// api.js - HTTP client wrapper for companion backend

export async function requestJson(url, options = {}) {
  const { timeoutMs = 0, signal: externalSignal, headers: customHeaders, ...fetchOpts } = options;
  let controller = null;
  let timerId = null;
  let signal = externalSignal || undefined;

  if (timeoutMs > 0 || externalSignal) {
    controller = new AbortController();
    signal = controller.signal;
    if (externalSignal) {
      if (externalSignal.aborted) {
        controller.abort();
      } else {
        externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
      }
    }
    if (timeoutMs > 0) {
      timerId = setTimeout(() => controller.abort(new Error("Request timed out")), timeoutMs);
    }
  }

  let res;
  try {
    res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(customHeaders || {}) },
      ...fetchOpts,
      signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error("Request canceled or timed out. You can click Retry Turn or switch models.");
    }
    throw err;
  } finally {
    if (timerId) clearTimeout(timerId);
  }

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
  getModelCatalog: (refresh = false, probe = false) =>
    requestJson(`/api/sessions/models?refresh=${refresh ? "true" : "false"}&probe=${probe ? "true" : "false"}`),
  listSessions: (projectId) => requestJson(`/api/sessions?project_id=${encodeURIComponent(projectId)}`),
  createSession: (body) => requestJson("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  getSessionDetail: (id) => requestJson(`/api/sessions/${id}`),
  updateSession: (id, body) => requestJson(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSession: (id) => requestJson(`/api/sessions/${id}`, { method: "DELETE" }),

  // Chat & Tool Gates
  sendChat: (body, options = {}) =>
    requestJson("/api/chat/send", { method: "POST", body: JSON.stringify(body), timeoutMs: 120000, ...options }),
  retryChat: (sessionId, options = {}) =>
    requestJson("/api/chat/retry", { method: "POST", body: JSON.stringify({ session_id: sessionId }), timeoutMs: 120000, ...options }),
  resolveGate: (gateId, body, options = {}) =>
    requestJson(`/api/chat/gate/${gateId}`, { method: "POST", body: JSON.stringify(body), timeoutMs: 120000, ...options }),
  resolveGatesBatch: (sessionId, decisions, options = {}) =>
    requestJson("/api/chat/gates/batch", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, decisions }),
      timeoutMs: 120000,
      ...options,
    }),

  // Workspace
  getTree: (projectId, subpath = ".", maxDepth = 0) =>
    requestJson(
      `/api/workspace/tree?project_id=${encodeURIComponent(projectId)}&subpath=${encodeURIComponent(subpath)}&max_depth=${maxDepth}`
    ),
  previewFile: (projectId, path) =>
    requestJson(`/api/workspace/file-preview?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`),
  runCommand: (projectId, command, opts = {}) =>
    requestJson("/api/workspace/run", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        command,
        session_id: opts.sessionId || null,
        is_test_run: Boolean(opts.isTestRun),
      }),
    }),
  restoreCheckpoint: (checkpointId) =>
    requestJson(`/api/workspace/checkpoints/${checkpointId}/restore`, { method: "POST" }),
  commitCheckpoint: (checkpointId) =>
    requestJson(`/api/workspace/checkpoints/${checkpointId}/commit`, { method: "POST" }),
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

  // Settings (API Key, Telemetry)
  getTelemetry: (model = null) =>
    requestJson(`/api/settings/telemetry${model ? `?model=${encodeURIComponent(model)}` : ""}`),
  getApiKeyStatus: (verify = false) => requestJson(`/api/settings/api-key?verify=${verify ? "true" : "false"}`),
  saveApiKey: (apiKey, validateKey = true) =>
    requestJson("/api/settings/api-key", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey, validate_key: validateKey }),
    }),
  clearApiKey: () => requestJson("/api/settings/api-key", { method: "DELETE" }),

  // Playground Bridge (0 API tokens)
  bridgeBundle: (body) =>
    requestJson("/api/bridge/bundle", { method: "POST", body: JSON.stringify(body) }),
  bridgeParse: (sessionId, rawMarkdown, autoApproveReads = false) =>
    requestJson("/api/bridge/parse", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        raw_markdown: rawMarkdown,
        auto_approve_reads: Boolean(autoApproveReads),
      }),
    }),
  bridgeReadMd: (projectId, path, sessionId = null, recordSummary = false) => {
    const params = new URLSearchParams({ project_id: projectId, path });
    if (sessionId) params.set("session_id", sessionId);
    if (recordSummary) params.set("record_summary", "true");
    return requestJson(`/api/bridge/read-md?${params.toString()}`);
  },
  bridgePackFiles: (projectId, filterText = "", paths = []) =>
    requestJson("/api/bridge/pack-files", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, filter_text: filterText, paths }),
    }),
};
