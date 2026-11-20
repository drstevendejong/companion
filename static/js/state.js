// state.js - Lightweight shared state & pub/sub event bus

export const state = {
  projects: [],
  activeProjectId: null,
  sessions: [],
  activeSessionId: null,
  activeSession: null,
  messages: [],
  pendingGates: [],
  attachments: [],
  catalog: { models: [], thinking_levels: [] },
  lastTerminalOutput: "",
  lastTerminalLabel: "",
  busy: false,
  pendingStatusText: null,
  resolvingGateId: null,
  activeAbortController: null,
  sessionActivities: {},
  lastClipboardInfo: null,
  gatesHiddenDueToError: false,
  lastErrorContext: null,
  apiEnabled: false,
  hasApiKey: false,
  directivesTokensEst: 0,
  telemetry: { rpm: 0, tpm: 0, rpd: 0, next_minute_reset_sec: 0, rpd_reset_label: "00:00 PT" },
};

const listeners = new Map();

export function on(eventName, handler) {
  if (!listeners.has(eventName)) {
    listeners.set(eventName, new Set());
  }
  listeners.get(eventName).add(handler);
  return () => listeners.get(eventName).delete(handler);
}

export function emit(eventName, detail = null) {
  const set = listeners.get(eventName);
  if (!set) return;
  for (const fn of set) {
    fn(detail);
  }
}

export function getActiveProject() {
  return state.projects.find((p) => p.id === state.activeProjectId) || null;
}

export function estimateTokensLocal(text) {
  if (!text) return 0;
  return Math.max(1, Math.ceil(text.length / 3.6));
}
