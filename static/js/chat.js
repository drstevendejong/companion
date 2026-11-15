// chat.js - Renders chat messages, attachments, tool call traces, and errors

import { renderMarkdown } from "./markdown.js";
import { emit, on, state } from "./state.js";

const chatStream = document.getElementById("chat-stream");

export function initChat() {
  on("chat:loaded", renderMessages);
  on("chat:error", (msg) => {
    const card = document.createElement("div");
    card.className = "msg-card";
    card.innerHTML = `<div class="msg-header"><span class="msg-role">Error</span></div><div class="msg-body">${escapeHtml(msg)}</div>`;
    chatStream.appendChild(card);
    chatStream.scrollTop = chatStream.scrollHeight;
  });
}

function renderMessages() {
  chatStream.innerHTML = "";
  if (!state.messages.length) {
    chatStream.innerHTML = `
      <div class="empty-state">
        <h3>New Session Ready</h3>
        <p>Write a prompt below, attach a file from the right panel, or capture a screenshot/console dump from Chrome :9222.</p>
      </div>
    `;
    emit("gates:changed");
    return;
  }

  for (const msg of state.messages) {
    if (msg.role === "tool_event") {
      renderToolEvent(msg);
      continue;
    }
    const card = document.createElement("article");
    card.className = `msg-card ${msg.role === "user" ? "msg-user" : "msg-model"}`;

    const usageStr = msg.token_usage
      ? `In ${msg.token_usage.input_tokens} · Out ${msg.token_usage.output_tokens} · Think ${msg.token_usage.thinking_tokens}`
      : "";

    const header = document.createElement("div");
    header.className = "msg-header";
    header.innerHTML = `
      <span class="msg-role">${msg.role === "user" ? "You" : "Gemini"}</span>
      <span class="mono tabular">${escapeHtml(usageStr)}</span>
    `;
    card.appendChild(header);

    if (msg.attachments && msg.attachments.length > 0) {
      const attWrap = document.createElement("div");
      attWrap.className = "msg-attachments";
      for (const att of msg.attachments) {
        if (att.kind === "image") {
          const img = document.createElement("img");
          img.className = "thumb-img";
          img.src = att.content;
          img.alt = att.label;
          attWrap.appendChild(img);
        } else {
          const span = document.createElement("span");
          span.className = "meta-line mono";
          span.textContent = `[${att.label} · ~${att.estimated_tokens} tok]`;
          attWrap.appendChild(span);
        }
      }
      card.appendChild(attWrap);
    }

    if (msg.text) {
      const body = document.createElement("div");
      body.className = "msg-body";
      body.innerHTML = msg.role === "model" ? renderMarkdown(msg.text) : escapeHtml(msg.text);
      card.appendChild(body);
    }

    if (msg.tool_calls && msg.tool_calls.length > 0) {
      const tcMeta = document.createElement("div");
      tcMeta.className = "meta-line mono mt-2";
      tcMeta.textContent = `Requested tools: ${msg.tool_calls.map((t) => t.name).join(", ")}`;
      card.appendChild(tcMeta);
    }

    chatStream.appendChild(card);
  }

  chatStream.scrollTop = chatStream.scrollHeight;
  emit("gates:changed");
}

function renderToolEvent(msg) {
  let parsed = {};
  try {
    parsed = JSON.parse(msg.text);
  } catch {
    return;
  }
  const card = document.createElement("div");
  card.className = "meta-line mono";
  const resp = parsed.response || {};
  let summary = `Tool [${parsed.tool_name}]`;
  if (resp.denied_by_user) summary += ` — Denied`;
  else if (resp.applied) summary += ` — Patch applied (${resp.path})`;
  else if (resp.written) summary += ` — Script written (${resp.path})${resp.executed ? `, exit ${resp.exit_code}` : ""}`;
  else if (resp.path) summary += ` — Delivered (${resp.path})`;
  else if (resp.error) summary += ` — Error: ${resp.error}`;
  else summary += ` — Completed`;

  card.textContent = summary;
  chatStream.appendChild(card);
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
