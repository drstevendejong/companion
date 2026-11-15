// lifecycle.js - Tab heartbeat and unload beacon so main.py exits when all tabs close

const HEARTBEAT_INTERVAL_MS = 3000;

function makeClientId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function initLifecycle() {
  const clientId = makeClientId();

  const sendHeartbeat = () => {
    fetch("/api/lifecycle/heartbeat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId }),
      keepalive: true,
    }).catch(() => {});
  };

  let byeSent = false;
  const sendBye = () => {
    if (byeSent) return;
    byeSent = true;
    const payload = JSON.stringify({ client_id: clientId });
    if (typeof navigator.sendBeacon === "function") {
      const blob = new Blob([payload], { type: "application/json" });
      navigator.sendBeacon("/api/lifecycle/bye", blob);
    } else {
      fetch("/api/lifecycle/bye", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true,
      }).catch(() => {});
    }
  };

  sendHeartbeat();
  setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS);
  window.addEventListener("pagehide", sendBye);
  window.addEventListener("beforeunload", sendBye);
}
