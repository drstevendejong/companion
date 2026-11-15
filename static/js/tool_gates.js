// tool_gates.js - Interactive approval gates for reads, diffs, and executable scripts

import { api } from "./api.js";
import { emit, getActiveProject, on, state } from "./state.js";

const gatesContainer = document.getElementById("pending-gates");

export function initToolGates() {
  on("chat:loaded", renderPendingGates);
  on("gates:changed", renderPendingGates);
}

function renderPendingGates() {
  const gates = state.pendingGates || [];
  if (gates.length === 0) {
    gatesContainer.classList.add("hidden");
    gatesContainer.innerHTML = "";
    return;
  }

  gatesContainer.classList.remove("hidden");
  gatesContainer.innerHTML = "";

  for (const gate of gates) {
    const preview = gate.preview || {};
    const card = document.createElement("div");
    card.className = "gate-card";

    const astLabel = preview.ast_valid === true ? " · AST Valid" : "";
    card.innerHTML = `
      <div class="gate-header">
        <span class="gate-title">${escapeHtml(preview.title || gate.tool_name)}</span>
        <span class="meta-line mono tabular">~${(preview.estimated_tokens || 0).toLocaleString()} tok${astLabel}</span>
      </div>
      ${preview.detail ? `<div class="meta-line">${escapeHtml(preview.detail)}</div>` : ""}
      ${preview.summary ? `<div class="meta-line">${escapeHtml(preview.summary)}</div>` : ""}
    `;

    if (preview.diff) {
      const pre = document.createElement("pre");
      pre.className = "diff-box";
      pre.innerHTML = formatUnifiedDiff(preview.diff);
      card.appendChild(pre);
    } else if (preview.script_content) {
      const pre = document.createElement("pre");
      pre.className = "diff-box";
      pre.textContent = preview.script_content;
      card.appendChild(pre);
    }

    const actions = document.createElement("div");
    actions.className = "gate-actions";

    if (preview.category === "read") {
      const approveBtn = makeBtn("Approve", "btn btn-primary btn-sm", () =>
        decideGate(gate.id, { action: "approve" })
      );
      actions.appendChild(approveBtn);

      if (gate.tool_name === "read_lines" || gate.tool_name === "read_file") {
        const rangeWrap = document.createElement("span");
        rangeWrap.className = "range-editor";
        const sInput = document.createElement("input");
        sInput.type = "number";
        sInput.min = "1";
        sInput.value = String(preview.start_line || 1);
        sInput.className = "text-input mono tabular";
        const eInput = document.createElement("input");
        eInput.type = "number";
        eInput.min = "1";
        eInput.value = String(preview.end_line || 150);
        eInput.className = "text-input mono tabular";
        const rangeBtn = makeBtn("Approve Range", "btn btn-secondary btn-sm", () =>
          decideGate(gate.id, {
            action: "edit_range",
            start_line: Number(sInput.value) || 1,
            end_line: Number(eInput.value) || 150,
          })
        );
        rangeWrap.append(sInput, document.createTextNode("–"), eInput, rangeBtn);
        actions.appendChild(rangeWrap);
      }
    } else if (preview.category === "patch") {
      actions.appendChild(
        makeBtn("Apply Patch (Backup First)", "btn btn-primary btn-sm", () =>
          decideGate(gate.id, { action: "approve" })
        )
      );
    } else if (preview.category === "script") {
      actions.appendChild(
        makeBtn("Write to Disk Only", "btn btn-secondary btn-sm", () =>
          decideGate(gate.id, { action: "write_only" })
        )
      );
      actions.appendChild(
        makeBtn("Write & Run in Venv", "btn btn-primary btn-sm", () =>
          decideGate(gate.id, { action: "write_and_run" })
        )
      );
    }

    actions.appendChild(
      makeBtn("Deny & Discuss", "btn btn-danger btn-sm", () =>
        decideGate(gate.id, { action: "deny" })
      )
    );

    card.appendChild(actions);
    gatesContainer.appendChild(card);
  }
}

async function decideGate(gateId, body) {
  if (state.busy) return;
  state.busy = true;
  emit("gates:changed");
  try {
    const res = await api.resolveGate(gateId, body);
    state.activeSession = res.session;
    state.messages = res.messages;
    state.pendingGates = res.pending_gates || [];

    if (res.patch_record) {
      emit("backups:refresh");
      emit("git:refresh");
      const proj = getActiveProject();
      if (proj && proj.post_patch_command && body.action === "approve") {
        emit("terminal:suggest-post-tests", proj.post_patch_command);
      }
    }
    if (res.command_result) {
      emit("terminal:show-result", res.command_result);
    }
    emit("session:updated");
    emit("chat:loaded");
  } catch (err) {
    emit("chat:error", err.message);
  } finally {
    state.busy = false;
    emit("gates:changed");
  }
}

function makeBtn(label, className, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
  btn.textContent = label;
  btn.addEventListener("click", onClick);
  return btn;
}

function formatUnifiedDiff(diffText) {
  return diffText
    .split("\n")
    .map((line) => {
      const esc = escapeHtml(line);
      if (line.startsWith("+") && !line.startsWith("+++")) return `<span class="diff-add">${esc}</span>`;
      if (line.startsWith("-") && !line.startsWith("---")) return `<span class="diff-del">${esc}</span>`;
      if (line.startsWith("@@")) return `<span class="diff-hunk">${esc}</span>`;
      return esc;
    })
    .join("\n");
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
