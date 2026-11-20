// tool_gates.js - Interactive approval gates for reads, diffs, and executable scripts

import { api } from "./api.js";
import { getTrailingToolBatchKey } from "./bridge.js";
import { emit, getActiveProject, on, state } from "./state.js";

const gatesContainer = document.getElementById("pending-gates");

export function initToolGates() {
  on("chat:loaded", renderPendingGates);
  on("gates:changed", renderPendingGates);
}

function renderPendingGates() {
  const gates = state.pendingGates || [];
  if (gates.length === 0 || state.gatesHiddenDueToError) {
    gatesContainer.classList.add("hidden");
    gatesContainer.innerHTML = "";
    return;
  }

  gatesContainer.classList.remove("hidden");
  gatesContainer.innerHTML = "";

  const totalEst = gates.reduce((sum, g) => sum + Number((g.preview || {}).estimated_tokens || 0), 0);
  const nonScript = gates.filter((g) => (g.preview || {}).category !== "script");
  const scriptOnly = gates.filter((g) => (g.preview || {}).category === "script");
  const batchBar = document.createElement("div");
  batchBar.className = "gates-batch-bar";

  const infoSpan = document.createElement("span");
  infoSpan.className = "meta-line mono tabular";
  infoSpan.textContent = `Pending ${gates.length === 1 ? "Gate" : "Gates"} (${gates.length}) · ~${totalEst.toLocaleString()} tok total`;

  const batchBtns = document.createElement("div");
  batchBtns.className = "row-gap";
  if (nonScript.length > 0) {
    const approveAllLabel =
      nonScript.length === gates.length ? `Approve All (${gates.length})` : `Approve Reads/Patches (${nonScript.length})`;
    batchBtns.appendChild(
      makeBtn(approveAllLabel, "btn btn-primary btn-xs", () =>
        decideBatchGates(
          nonScript.map((g) => ({ gate_id: g.id, decision: { action: "approve" } })),
          nonScript.length === gates.length
        )
      )
    );
  } else if (scriptOnly.length > 0) {
    batchBtns.appendChild(
      makeBtn(`Write & Run All (${scriptOnly.length})`, "btn btn-primary btn-xs", () =>
        decideBatchGates(
          scriptOnly.map((g) => ({ gate_id: g.id, decision: { action: "write_and_run" } })),
          true
        )
      )
    );
  }
  batchBtns.appendChild(
    makeBtn(`Deny All (${gates.length})`, "btn btn-danger btn-xs", () =>
      decideBatchGates(
        gates.map((g) => ({ gate_id: g.id, decision: { action: "deny" } })),
        true
      )
    )
  );
  batchBar.append(infoSpan, batchBtns);
  gatesContainer.appendChild(batchBar);

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
      if (gate.tool_name === "read_lines") {
        const origStart = Number(preview.start_line || 1);
        const origEnd = Number(preview.end_line || 150);
        const rangeWrap = document.createElement("span");
        rangeWrap.className = "range-editor";
        const sInput = document.createElement("input");
        sInput.type = "number";
        sInput.min = "1";
        sInput.value = String(origStart);
        sInput.className = "text-input mono tabular";
        const eInput = document.createElement("input");
        eInput.type = "number";
        eInput.min = "1";
        eInput.value = String(origEnd);
        eInput.className = "text-input mono tabular";

        const smartBtn = makeBtn(`Approve (${origStart}–${origEnd})`, "btn btn-primary btn-sm", () => {
          const curS = Number(sInput.value) || origStart;
          const curE = Number(eInput.value) || origEnd;
          if (curS === origStart && curE === origEnd) {
            decideGate(gate.id, { action: "approve" });
          } else {
            decideGate(gate.id, { action: "edit_range", start_line: curS, end_line: curE });
          }
        });
        const updateSmartLabel = () => {
          const curS = Number(sInput.value) || origStart;
          const curE = Number(eInput.value) || origEnd;
          smartBtn.textContent =
            curS === origStart && curE === origEnd
              ? `Approve (${origStart}–${origEnd})`
              : `Approve Edited (${curS}–${curE})`;
        };
        sInput.addEventListener("input", updateSmartLabel);
        eInput.addEventListener("input", updateSmartLabel);

        const lbl = document.createElement("span");
        lbl.className = "meta-line";
        lbl.textContent = "Lines:";
        rangeWrap.append(lbl, sInput, document.createTextNode("–"), eInput);
        actions.append(smartBtn, rangeWrap);
      } else {
        actions.appendChild(
          makeBtn(gate.tool_name === "read_file" ? "Approve Full" : "Approve", "btn btn-primary btn-sm", () =>
            decideGate(gate.id, { action: "approve" })
          )
        );
        if (gate.tool_name === "read_file") {
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
          const sliceBtn = makeBtn("Approve Slice", "btn btn-secondary btn-sm", () =>
            decideGate(gate.id, {
              action: "edit_range",
              start_line: Number(sInput.value) || 1,
              end_line: Number(eInput.value) || 150,
            })
          );
          rangeWrap.append(sInput, document.createTextNode("–"), eInput, sliceBtn);
          actions.appendChild(rangeWrap);
        }
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

    if (state.resolvingGateId === gate.id || state.resolvingGateId === "__batch__") {
      const statusRow = document.createElement("div");
      statusRow.className = "meta-line mono";
      const remainingAfterThis = Math.max(0, gates.length - 1);
      if (!state.apiEnabled) {
        statusRow.textContent = "Executing approved action locally (Bridge mode)...";
      } else {
        statusRow.textContent =
          state.resolvingGateId === "__batch__" || remainingAfterThis === 0
            ? "Executing approved action & waiting for Gemini..."
            : `Executing action (${remainingAfterThis} gate(s) left before Gemini responds)...`;
      }
      card.appendChild(statusRow);
    } else {
      card.appendChild(actions);
    }
    gatesContainer.appendChild(card);
  }
}

async function decideGate(gateId, body) {
  if (state.busy) return;
  const remainingCount = Math.max(0, (state.pendingGates || []).length - 1);
  const gateObj = (state.pendingGates || []).find((g) => g.id === gateId);
  const gatePath = (gateObj && gateObj.args && gateObj.args.path) || "";
  const controller = new AbortController();
  state.activeAbortController = controller;
  state.busy = true;
  state.resolvingGateId = gateId;
  if (gateObj && gateObj.tool_name === "propose_script" && body.action === "write_and_run") {
    state.pendingStatusText = `Running proposed script${gatePath ? `: ${gatePath}` : ""}...`;
  } else if (gateObj && ["patch_literal", "patch_lines", "write_file"].includes(gateObj.tool_name) && body.action === "approve") {
    state.pendingStatusText = `Applying patch${gatePath ? ` to ${gatePath}` : ""}...`;
  } else if (!state.apiEnabled) {
    state.pendingStatusText = "Executing approved gate locally...";
  } else {
    state.pendingStatusText =
      remainingCount > 0
        ? `Executing gate (${remainingCount} remaining before Gemini responds)...`
        : "Running approved tool & waiting for Gemini...";
  }
  emit("chat:loaded");
  try {
    const res = await api.resolveGate(gateId, body, { signal: controller.signal });
    applyGateResponse(res, body.action);
  } catch (err) {
    await recoverAfterGateError(err);
  } finally {
    if (state.activeAbortController === controller) {
      state.activeAbortController = null;
    }
    state.busy = false;
    state.resolvingGateId = null;
    state.pendingStatusText = null;
    emit("gates:changed");
  }
}

async function decideBatchGates(decisions, willCallGemini) {
  if (state.busy || !state.activeSessionId || !decisions.length) return;
  const controller = new AbortController();
  state.activeAbortController = controller;
  state.busy = true;
  state.resolvingGateId = "__batch__";
  state.pendingStatusText =
    !state.apiEnabled
      ? `Executing ${decisions.length} gates locally...`
      : willCallGemini
        ? `Executing ${decisions.length} gates & waiting for Gemini...`
        : `Executing ${decisions.length} gates...`;
  emit("chat:loaded");
  try {
    const res = await api.resolveGatesBatch(state.activeSessionId, decisions, {
      signal: controller.signal,
    });
    const hasApprove = decisions.some((d) => d.decision.action === "approve");
    applyGateResponse(res, hasApprove ? "approve" : "batch");
  } catch (err) {
    await recoverAfterGateError(err);
  } finally {
    if (state.activeAbortController === controller) {
      state.activeAbortController = null;
    }
    state.busy = false;
    state.resolvingGateId = null;
    state.pendingStatusText = null;
    emit("gates:changed");
  }
}

function applyGateResponse(res, actionHint) {
  state.busy = false;
  state.pendingStatusText = null;
  state.resolvingGateId = null;
  state.activeSession = res.session;
  state.messages = res.messages;
  state.pendingGates = res.pending_gates || [];

  if (res.patch_record) {
    emit("backups:refresh");
    emit("git:refresh");
    const proj = getActiveProject();
    if (proj && proj.post_patch_command && actionHint === "approve") {
      emit("terminal:suggest-post-tests", proj.post_patch_command);
    }
  }
  if (res.command_result) {
    emit("terminal:show-result", res.command_result);
  }

  if (state.pendingGates.length === 0) {
    state.gatesHiddenDueToError = false;
  }

  if (!state.apiEnabled && state.pendingGates.length === 0) {
    const trailing = extractTrailingToolEvents(res.messages || []);
    const batchKey = getTrailingToolBatchKey(res.messages || []);
    const deniedEvents = trailing.filter((ev) => ev.response && ev.response.denied_by_user);
    const errorEvents = trailing.filter((ev) => ev.response && ev.response.error);
    const approvedEvents = trailing.filter(
      (ev) => !(ev.response && (ev.response.denied_by_user || ev.response.error))
    );
    const proj = getActiveProject();

    if (deniedEvents.length > 0) {
      emit("bridge:handle-denials", {
        deniedEvents,
        approvedEvents,
        errorEvents,
        messages: res.messages || [],
      });
    } else {
      state.lastErrorContext = null;
      if (res.generated_md_content && batchKey) {
        emit("bridge:auto-copy-result", {
          key: batchKey,
          text: res.generated_md_content,
          mdCacheKey: proj && res.generated_md_path ? `${proj.id}:${res.generated_md_path}` : null,
          mdContent: res.generated_md_content,
        });
      } else if (res.bridge_reply_text && batchKey) {
        emit("bridge:auto-copy-result", {
          key: batchKey,
          text: res.bridge_reply_text,
        });
      }
    }
  } else {
    if (state.pendingGates.length === 0) {
      state.lastErrorContext = null;
    }
    if (res.generated_md_files && res.generated_md_files.length > 0) {
      emit("bridge:generated-md", res.generated_md_files);
    }
  }

  if (res.telemetry) {
    emit("telemetry:updated", res.telemetry);
  }
  emit("session:updated");
  emit("chat:loaded");
}

function extractTrailingToolEvents(messages) {
  const events = [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role !== "tool_event") break;
    try {
      events.push(JSON.parse(messages[i].text));
    } catch {
      // skip malformed
    }
  }
  events.reverse();
  return events;
}

async function recoverAfterGateError(err) {
  state.busy = false;
  state.pendingStatusText = null;
  state.resolvingGateId = null;
  if (state.activeSessionId) {
    try {
      const detail = await api.getSessionDetail(state.activeSessionId);
      state.activeSession = detail.session;
      state.messages = detail.messages || [];
      state.pendingGates = detail.pending_gates || [];
    } catch {
      // fallback if session reload also fails
    }
  }
  emit("session:updated");
  emit("chat:loaded");
  emit("chat:error", err.message);
}

function makeBtn(label, className, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
  btn.textContent = label;
  btn.disabled = Boolean(state.busy);
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
