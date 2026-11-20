// lifecycle.js - Sleep-proof WebSocket keepalive, mutual teardown, and live command streaming

import { emit } from "./state.js";

function makeClientId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function initLifecycle() {
  const clientId = makeClientId();
  const closedOverlay = document.getElementById("server-closed-overlay");
  const reconnectBtn = document.getElementById("btn-reconnect-server");

  let ws = null;
  let unloading = false;
  let serverStopped = false;
  let fallbackTimer = null;
  let reconnectTimer = null;
  let consecutivePingFailures = 0;

  const stopTimers = () => {
    if (fallbackTimer !== null) {
      clearInterval(fallbackTimer);
      fallbackTimer = null;
    }
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const handleServerShutdown = () => {
    if (serverStopped) return;
    serverStopped = true;
    unloading = true;
    stopTimers();
    if (ws && ws.readyState <= WebSocket.OPEN) {
      try {
        ws.close(1000, "server_shutdown");
      } catch {}
    }
    ws = null;
    document.title = "[Stopped] Gemini Companion";

    try {
      window.close();
    } catch {}
    try {
      if (!window.closed) {
        window.open("", "_self");
        window.close();
      }
    } catch {}

    if (closedOverlay) {
      closedOverlay.classList.remove("hidden");
    }
  };

  const probeServerAlive = async () => {
    if (unloading || serverStopped) return;
    try {
      const res = await fetch("/api/lifecycle/ping", { cache: "no-store" });
      if (res.ok) {
        consecutivePingFailures = 0;
        return;
      }
    } catch {
      // network error if server terminated or still waking
    }
    if (document.visibilityState === "visible" && navigator.onLine !== false) {
      consecutivePingFailures += 1;
      if (consecutivePingFailures >= 4) {
        handleServerShutdown();
      }
    }
  };

  const sendHttpHeartbeat = async () => {
    if (unloading || serverStopped) return;
    try {
      const res = await fetch("/api/lifecycle/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: clientId }),
        keepalive: true,
      });
      if (res.ok) {
        consecutivePingFailures = 0;
        return;
      }
    } catch {
      // fall through to awake liveness probe
    }
    await probeServerAlive();
  };

  const startFallbackPolling = () => {
    if (fallbackTimer !== null || unloading || serverStopped) return;
    void sendHttpHeartbeat();
    fallbackTimer = setInterval(() => void sendHttpHeartbeat(), 5000);
  };

  const scheduleReconnect = (delayMs = 1500) => {
    if (unloading || serverStopped || reconnectTimer !== null) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectWs();
    }, delayMs);
  };

  const connectWs = () => {
    if (unloading || serverStopped) return;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/api/lifecycle/ws?client_id=${encodeURIComponent(clientId)}`;
    try {
      const sock = new WebSocket(url);
      ws = sock;
      sock.addEventListener("open", () => {
        if (ws !== sock) return;
        consecutivePingFailures = 0;
        if (fallbackTimer !== null) {
          clearInterval(fallbackTimer);
          fallbackTimer = null;
        }
      });
      sock.addEventListener("message", (ev) => {
        try {
          const msg = JSON.parse(String(ev.data || "{}"));
          if (msg.type === "ping" && sock.readyState === WebSocket.OPEN) {
            sock.send(JSON.stringify({ type: "pong" }));
          } else if (msg.type === "server_shutdown") {
            handleServerShutdown();
          } else if (
            msg.type === "exec_start" ||
            msg.type === "exec_chunk" ||
            msg.type === "exec_done"
          ) {
            emit("terminal:stream-event", msg);
          }
        } catch {
          // ignore malformed keepalive payload
        }
      });
      sock.addEventListener("close", (ev) => {
        if (ws === sock) {
          ws = null;
        }
        if (unloading || serverStopped) return;
        if (ev.code === 4001 || ev.reason === "server_shutdown") {
          handleServerShutdown();
          return;
        }
        startFallbackPolling();
        scheduleReconnect(1500);
      });
      sock.addEventListener("error", () => {
        if (!unloading && !serverStopped) {
          startFallbackPolling();
        }
      });
    } catch {
      startFallbackPolling();
      scheduleReconnect(2000);
    }
  };

  const wakeAndSync = () => {
    if (unloading || serverStopped) return;
    consecutivePingFailures = 0;
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (!ws || ws.readyState > WebSocket.OPEN) {
      void sendHttpHeartbeat();
      connectWs();
    } else {
      try {
        ws.send(JSON.stringify({ type: "pong" }));
      } catch {
        connectWs();
      }
    }
  };

  const handleUnload = () => {
    if (unloading || serverStopped) return;
    unloading = true;
    stopTimers();
    if (ws && ws.readyState <= WebSocket.OPEN) {
      try {
        ws.close(1000, "tab-closed");
      } catch {}
    }
    const payload = JSON.stringify({ client_id: clientId });
    if (typeof navigator.sendBeacon === "function") {
      navigator.sendBeacon("/api/lifecycle/bye", new Blob([payload], { type: "application/json" }));
    }
  };

  if (reconnectBtn) {
    reconnectBtn.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/lifecycle/ping", { cache: "no-store" });
        if (res.ok) {
          window.location.reload();
        }
      } catch {
        // still offline
      }
    });
  }

  connectWs();
  window.addEventListener("pagehide", handleUnload);
  window.addEventListener("focus", wakeAndSync);
  window.addEventListener("pageshow", wakeAndSync);
  window.addEventListener("online", wakeAndSync);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      wakeAndSync();
    }
  });
}
