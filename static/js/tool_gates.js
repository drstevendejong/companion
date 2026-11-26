// tool_gates.js - Interactive approval gates for reads, diffs, and executable scripts

import { api } from "./api.js";
import { formatBridgeToolResults, getTrailingToolBatchKey } from "./bridge.js";
import { emit, getActiveProject, on, state } from "./state.js";

const gatesContainer = document.getElementById("pending-gates");
let gateElapsedTimer = null;

export function initToolGates() {
  on("chat:loaded", renderPendingGates);
  on("gates:changed", renderPendingGates);
  on("chat:live-exec", updateGateLiveStatus);
  document.addEventListener("click", (ev) => {
    if (!gatesContainer) return;
    for (const menu of gatesContainer.querySelectorAll(".split-btn-menu:not(.hidden)")) {
      const wrap = menu.closest(".split-btn");
      if (wrap && !wrap.contains(ev.target)) {
        menu.classList.add("hidden");
        const caret = wrap.querySelector(".split-btn-caret");
        if (caret) caret.setAttribute("aria-expanded", "false");
      }
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && gatesContainer) {
      for (const menu of gatesContainer.querySelectorAll(".split-btn-menu:not(.hidden)")) {
        menu.classList.add("hidden");
        const wrap = menu.closest(".split-btn");
        const caret = wrap.querySelector(".split-btn-caret");
        if (caret) caret.setAttribute("aria-expanded", "false");
      }
    }
  });
}

function formatGateIdleNote() {
  const exec = state.liveExec;
  if (!exec) return "waiting for output";
  if (exec.phaseText) {
    const phaseStartedAt = Number(exec.phaseStartedAtMs || Date.now());
    const phaseSec = Math.max(0, Math.floor((Date.now() - phaseStartedAt) / 1000));
    return exec.phase === "scout_triage" ? `triaging (${phaseSec}s)` : `summarizing (${phaseSec}s)`;
  }
  const bytes = Number(exec.bytesReceived || (exec.output ? exec.output.length : 0) || 0);
  if (bytes <= 0) return "waiting for output";
  const lastAt = Number(exec.lastOutputAtMs || Date.now());
  const idleSec = Math.max(0, Math.floor((Date.now() - lastAt) / 1000));
  return idleSec >= 2 ? `${bytes.toLocaleString()} B · silent ${idleSec}s` : `${bytes.toLocaleString()} B streaming`;
}

function updateGateLiveStatus() {
  if (!gatesContainer || !state.resolvingGateId) return;
  const startedAt = Number(
    state.pendingStartedAtMs || (state.liveExec && state.liveExec.startedAtMs) || Date.now()
  );
  const elapsedSec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  for (const el of gatesContainer.querySelectorAll("[data-gate-elapsed]")) {
    el.textContent = `${elapsedSec}s`;
  }
  const idleNote = ` · ${formatGateIdleNote()}`;
  for (const el of gatesContainer.querySelectorAll("[data-gate-idle]")) {
    el.textContent = idleNote;
  }
  const phaseText = state.liveExec && state.liveExec.phaseText;
  for (const el of gatesContainer.querySelectorAll("[data-gate-status]")) {
    el.textContent = phaseText || el.getAttribute("data-default-status") || el.textContent;
  }
  const hideStop = Boolean(state.liveExec && state.liveExec.done && state.liveExec.phaseText);
  for (const btn of gatesContainer.querySelectorAll("[data-stop-exec]")) {
    btn.classList.toggle("hidden", hideStop);
    if (!hideStop && btn.textContent === "Stop Execution") {
      btn.disabled = false;
    }
  }
}

function ensureGateElapsedTimer() {
  if (gateElapsedTimer !== null) return;
  gateElapsedTimer = window.setInterval(() => {
    if (!state.resolvingGateId) {
      clearGateElapsedTimer();
      return;
    }
    updateGateLiveStatus();
  }, 1000);
}

function clearGateElapsedTimer() {
  if (gateElapsedTimer !== null) {
    window.clearInterval(gateElapsedTimer);
    gateElapsedTimer = null;
  }
}

function renderPendingGates() {
  const gates = state.pendingGates || [];
  if (gates.length === 0 || state.gatesHiddenDueToError) {
    clearGateElapsedTimer();
    gatesContainer.classList.add("hidden");
    gatesContainer.innerHTML = "";
    return;
  }

  gatesContainer.classList.remove("hidden");
  gatesContainer.innerHTML = "";

  const totalEst = gates.reduce((sum, g) => sum + Number((g.preview || {}).estimated_tokens || 0), 0);
  const nonScript = gates.filter((g) => (g.preview || {}).category !== "script");
  const scriptOnly = gates.filter((g) => (g.preview || {}).category === "script");
  const unambiguousScripts = scriptOnly.filter((g) => !(g.preview || {}).intent_ambiguous);
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
  } else if (unambiguousScripts.length > 0 && unambiguousScripts.length === scriptOnly.length) {
    const allAutoDelete = unambiguousScripts.every((g) => Boolean((g.preview || {}).delete_on_success));
    batchBtns.appendChild(
      makeBtn(
        allAutoDelete
          ? `Write, Run & Delete All (${unambiguousScripts.length})`
          : `Write & Run All (${unambiguousScripts.length})`,
        "btn btn-primary btn-xs",
        () =>
          decideBatchGates(
            unambiguousScripts.map((g) => ({
              gate_id: g.id,
              decision: { action: (g.preview || {}).delete_on_success ? "write_run_and_delete" : "write_and_run" },
            })),
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
      const patchBtnLabel =
        gate.tool_name === "delete_file"
          ? "Delete File (Backup First)"
          : gate.tool_name === "rename_file"
            ? "Rename File (Backup First)"
            : "Apply Patch (Backup First)";
      actions.appendChild(
        makeBtn(patchBtnLabel, "btn btn-primary btn-sm", () =>
          decideGate(gate.id, { action: "approve" })
        )
      );
    } else if (preview.category === "script") {
      if (preview.intent_ambiguous) {
        const pathRow = document.createElement("div");
        pathRow.className = "ambiguous-path-row";
        const pathLbl = document.createElement("label");
        pathLbl.className = "meta-line";
        pathLbl.textContent = "Target Path:";
        const pathInput = document.createElement("input");
        pathInput.type = "text";
        pathInput.className = "text-input mono";
        pathInput.value = preview.script_path || (gate.args && gate.args.path) || "";
        pathInput.placeholder = "e.g. inspect_tmp.py or utils/helper.py";
        pathInput.disabled = Boolean(state.busy);
        pathRow.append(pathLbl, pathInput);
        card.appendChild(pathRow);

        const inspectBtn = makeBtn("Run Temp Inspection (Delete on Pass)", "btn btn-primary btn-sm", () => {
          const targetPath = pathInput.value.trim();
          decideGate(gate.id, {
            action: "write_run_and_delete",
            ...(targetPath ? { target_path: targetPath } : {}),
          });
        });
        inspectBtn.title = "Write temporary script, run in venv, and auto-delete if it passes cleanly";

        const saveBtn = makeBtn("Save as Code File", "btn btn-secondary btn-sm", () => {
          const targetPath = pathInput.value.trim();
          decideGate(gate.id, {
            action: "save_as_file",
            ...(targetPath ? { target_path: targetPath } : {}),
          });
        });
        saveBtn.title = "Write permanently to disk at Target Path (with savepoint & backup) without executing";

        actions.append(inspectBtn, saveBtn);
      } else {
        const splitWrap = document.createElement("div");
        splitWrap.className = "split-btn";

        const mainBtn = makeBtn(
          "Run & Delete on Pass",
          "btn btn-primary btn-sm split-btn-main",
          () => decideGate(gate.id, { action: "write_run_and_delete" })
        );
        mainBtn.title = "Write script, run in venv, delete on clean pass (keeps on disk if it fails)";

        const caretBtn = makeBtn("▾", "btn btn-primary btn-sm split-btn-caret", (ev) => {
          ev.stopPropagation();
          const open = menu.classList.contains("hidden");
          menu.classList.toggle("hidden", !open);
          caretBtn.setAttribute("aria-expanded", open ? "true" : "false");
        });
        caretBtn.title = "More script options";
        caretBtn.setAttribute("aria-haspopup", "menu");
        caretBtn.setAttribute("aria-expanded", "false");

        const menu = document.createElement("div");
        menu.className = "split-btn-menu is-left hidden";
        menu.setAttribute("role", "menu");

        const keepItem = document.createElement("button");
        keepItem.type = "button";
        keepItem.className = "split-btn-item";
        keepItem.setAttribute("role", "menuitem");
        keepItem.textContent = "Write & Run (Keep on Disk)";
        keepItem.disabled = Boolean(state.busy);
        keepItem.addEventListener("click", () => {
          menu.classList.add("hidden");
          void decideGate(gate.id, { action: "write_and_run" });
        });

        const writeItem = document.createElement("button");
        writeItem.type = "button";
        writeItem.className = "split-btn-item";
        writeItem.setAttribute("role", "menuitem");
        writeItem.textContent = "Write to Disk Only";
        writeItem.disabled = Boolean(state.busy);
        writeItem.addEventListener("click", () => {
          menu.classList.add("hidden");
          void decideGate(gate.id, { action: "write_only" });
        });

        menu.append(keepItem, writeItem);
        splitWrap.append(mainBtn, caretBtn, menu);
        actions.appendChild(splitWrap);
      }
    } else if (preview.category === "command") {
      const runBtnLabel =
        gate.tool_name === "run_and_delete_script"
          ? "Run & Delete on Pass"
          : gate.tool_name === "run_script"
            ? "Run Script in Venv"
            : preview.is_full_suite === false
              ? "Run Quick Check"
              : "Run Tests";
      actions.appendChild(
        makeBtn(runBtnLabel, "btn btn-primary btn-sm", () =>
          decideGate(gate.id, { action: "approve" })
        )
      );
    } else if (preview.category === "scout") {
      actions.appendChild(
        makeBtn("Approve Scout", "btn btn-primary btn-sm", () =>
          decideGate(gate.id, { action: "approve" })
        )
      );
    } else if (preview.category === "scout_headers") {
      actions.appendChild(
        makeBtn("Draft Header Diffs (Scout)", "btn btn-primary btn-sm", () =>
          decideGate(gate.id, { action: "approve" })
        )
      );
    }

    actions.appendChild(
      makeBtn("Deny & Discuss", "btn btn-danger btn-sm", () =>
        decideGate(gate.id, { action: "deny" })
      )
    );

    if (state.resolvingGateId === gate.id || state.resolvingGateId === "__batch__") {
      ensureGateElapsedTimer();
      const statusRow = document.createElement("div");
      statusRow.className = "live-status-row";
      statusRow.setAttribute("data-live-status", "gate");

      const pulseDot = document.createElement("span");
      pulseDot.className = "live-pulse-dot";
      pulseDot.setAttribute("aria-hidden", "true");

      const statusText = document.createElement("span");
      statusText.className = "meta-line mono";
      statusText.setAttribute("data-gate-status", "true");
      const remainingAfterThis = Math.max(0, gates.length - 1);
      let defaultStatusText = "";
      if (state.pendingStatusText) {
        defaultStatusText = state.pendingStatusText;
      } else if (!state.apiEnabled) {
        defaultStatusText = "Executing approved action locally (Bridge mode)...";
      } else {
        defaultStatusText =
          state.resolvingGateId === "__batch__" || remainingAfterThis === 0
            ? "Executing approved action & waiting for Gemini..."
            : `Executing action (${remainingAfterThis} gate(s) left before Gemini responds)...`;
      }
      statusText.setAttribute("data-default-status", defaultStatusText);
      statusText.textContent = (state.liveExec && state.liveExec.phaseText) || defaultStatusText;

      const startedAt = Number(
        state.pendingStartedAtMs || (state.liveExec && state.liveExec.startedAtMs) || Date.now()
      );
      const elapsedSec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      const timerSpan = document.createElement("span");
      timerSpan.className = "live-status-meta mono tabular";
      const elapsedEl = document.createElement("span");
      elapsedEl.setAttribute("data-gate-elapsed", "true");
      elapsedEl.textContent = `${elapsedSec}s`;
      const idleEl = document.createElement("span");
      idleEl.setAttribute("data-gate-idle", "true");
      idleEl.textContent = ` · ${formatGateIdleNote()}`;
      timerSpan.append(elapsedEl, idleEl);

      const stopBtn = document.createElement("button");
      stopBtn.type = "button";
      stopBtn.className = "btn btn-danger btn-xs btn-stop-exec";
      stopBtn.setAttribute("data-stop-exec", "true");
      stopBtn.textContent = "Stop Execution";
      stopBtn.title = "Terminate the active subprocess immediately";
      if (state.liveExec && state.liveExec.done && state.liveExec.phaseText) {
        stopBtn.classList.add("hidden");
        stopBtn.disabled = true;
      }
      stopBtn.addEventListener("click", async () => {
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping...";
        const abortCtrl = state.activeAbortController;
        const fallbackTimer = window.setTimeout(() => {
          if (abortCtrl) abortCtrl.abort();
        }, 2500);
        try {
          if (state.activeSessionId) {
            await api.cancelExec(
              state.activeSessionId,
              state.resolvingGateId === "__batch__" ? null : gate.id,
              (state.liveExec && state.liveExec.runId) || null
            );
          }
        } catch {
          if (abortCtrl) abortCtrl.abort();
        } finally {
          window.clearTimeout(fallbackTimer);
        }
      });

      statusRow.append(pulseDot, statusText, timerSpan, stopBtn);
      card.appendChild(statusRow);

      const cmdText = String((state.liveExec && state.liveExec.command) || "");
      const gateTarget = String((gate.args && (gate.args.path || gate.args.target)) || "");
      const isExecutableGate = preview.category === "script" || preview.category === "command";
      const matchesBatch =
        state.resolvingGateId === gate.id ||
        (isExecutableGate && (!cmdText || !gateTarget || cmdText.includes(gateTarget)));
      if ((isExecutableGate || Boolean(state.liveExec)) && matchesBatch) {
        const liveSlice = formatGateLiveSlice(state.liveExec && state.liveExec.output);
        const livePre = document.createElement("pre");
        livePre.className = `exec-preview${liveSlice ? "" : " is-empty"}`;
        livePre.dataset.liveExec = `gate-card-${gate.id}`;
        livePre.textContent = liveSlice || "(Waiting for stdout / stderr output...)";
        card.appendChild(livePre);
      }
    } else {
      card.appendChild(actions);
    }
    gatesContainer.appendChild(card);
  }

  if (state.resolvingGateId) {
    const activeTarget =
      gatesContainer.querySelector("[data-live-exec]:not(.hidden)") ||
      gatesContainer.querySelector("[data-live-status]");
    if (activeTarget && typeof activeTarget.scrollIntoView === "function") {
      activeTarget.scrollIntoView({ block: "nearest" });
    }
  } else {
    clearGateElapsedTimer();
  }
}

async function decideGate(gateId, body) {
  if (state.busy) return;
  const remainingCount = Math.max(0, (state.pendingGates || []).length - 1);
  const gateObj = (state.pendingGates || []).find((g) => g.id === gateId);
  const gatePath =
    String(body.target_path || "").trim() ||
    (gateObj && gateObj.args && (gateObj.args.path || gateObj.args.old_path || gateObj.args.target)) ||
    "";
  const controller = new AbortController();
  state.activeAbortController = controller;
  state.busy = true;
  state.liveExec = null;
  state.resolvingGateId = gateId;
  state.pendingStartedAtMs = Date.now();
  if (
    gateObj &&
    (gateObj.tool_name === "propose_inspection_script" || gateObj.tool_name === "propose_script") &&
    body.action === "save_as_file"
  ) {
    state.pendingStatusText = `Saving code file${gatePath ? `: ${gatePath}` : ""}...`;
  } else if (
    gateObj &&
    (gateObj.tool_name === "propose_inspection_script" || gateObj.tool_name === "propose_script") &&
    (body.action === "write_and_run" || body.action === "write_run_and_delete")
  ) {
    state.pendingStatusText =
      body.action === "write_run_and_delete"
        ? `Running & auto-deleting on pass${gatePath ? `: ${gatePath}` : ""}...`
        : `Running inspection script${gatePath ? `: ${gatePath}` : ""}...`;
  } else if (
    gateObj &&
    (gateObj.tool_name === "run_script" || gateObj.tool_name === "run_and_delete_script") &&
    body.action === "approve"
  ) {
    state.pendingStatusText =
      gateObj.tool_name === "run_and_delete_script"
        ? `Running & auto-deleting on pass${gatePath ? `: ${gatePath}` : ""}...`
        : `Running script${gatePath ? `: ${gatePath}` : ""}...`;
  } else if (gateObj && gateObj.tool_name === "run_tests" && body.action === "approve") {
    const cmd = (gateObj.preview && gateObj.preview.command) || "tests";
    state.pendingStatusText = `Running ${gateObj.preview && gateObj.preview.is_full_suite === false ? "quick check" : "tests"}: ${cmd}...`;
  } else if (gateObj && gateObj.tool_name === "delete_file" && body.action === "approve") {
    state.pendingStatusText = `Deleting${gatePath ? ` ${gatePath}` : " file"} (backing up first)...`;
  } else if (gateObj && gateObj.tool_name === "rename_file" && body.action === "approve") {
    state.pendingStatusText = `Renaming${gatePath ? ` ${gatePath}` : " file"} (backing up first)...`;
  } else if (gateObj && ["patch_literal", "patch_lines", "write_file"].includes(gateObj.tool_name) && body.action === "approve") {
    state.pendingStatusText = `Applying patch${gatePath ? ` to ${gatePath}` : ""}...`;
  } else if (gateObj && gateObj.tool_name === "scout_workspace" && body.action !== "deny") {
    state.pendingStatusText = "Running Local AI Scout investigation...";
  } else if (gateObj && gateObj.tool_name === "update_header" && body.action !== "deny") {
    state.pendingStatusText = "Drafting header comment diffs with Local AI Scout...";
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
    clearGateElapsedTimer();
    state.busy = false;
    state.resolvingGateId = null;
    state.pendingStatusText = null;
    state.pendingStartedAtMs = null;
    state.liveExec = null;
    emit("gates:changed");
  }
}

async function decideBatchGates(decisions, willCallGemini) {
  if (state.busy || !state.activeSessionId || !decisions.length) return;
  const controller = new AbortController();
  state.activeAbortController = controller;
  state.busy = true;
  state.liveExec = null;
  state.resolvingGateId = "__batch__";
  state.pendingStartedAtMs = Date.now();
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
    clearGateElapsedTimer();
    state.busy = false;
    state.resolvingGateId = null;
    state.pendingStatusText = null;
    state.pendingStartedAtMs = null;
    state.liveExec = null;
    emit("gates:changed");
  }
}

export function applyGateResponse(res, actionHint) {
  clearGateElapsedTimer();
  state.busy = false;
  state.pendingStatusText = null;
  state.pendingStartedAtMs = null;
  state.resolvingGateId = null;
  state.liveScoutRun = null;
  state.liveExec = null;
  state.activeSession = res.session;
  state.messages = res.messages;
  state.pendingGates = res.pending_gates || [];
  if (Array.isArray(res.checkpoints)) state.checkpoints = res.checkpoints;
  if (res.scout_auto_approve_enabled) {
    state.scout = { ...(state.scout || {}), auto_approve: true };
    emit("scout:refresh");
  }

  const cmdResults = Array.isArray(res.command_results)
    ? res.command_results
    : res.command_result
      ? [res.command_result]
      : [];
  if (res.patch_record) {
    emit("backups:refresh");
    emit("git:refresh");
    emit("tree:refresh");
    const proj = getActiveProject();
    if (proj && proj.post_patch_command && actionHint === "approve" && cmdResults.length === 0) {
      emit("terminal:suggest-post-tests", proj.post_patch_command);
    }
  }
  for (const cr of cmdResults) {
    emit("terminal:show-result", cr);
  }
  if (res.checkpoint_committed) {
    emit("git:refresh");
  }

  if (state.pendingGates.length === 0) {
    state.gatesHiddenDueToError = false;
  }

  const gateErrors = Array.isArray(res.gate_errors) ? res.gate_errors : [];
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
  } else if (!state.apiEnabled && gateErrors.length > 0 && state.pendingGates.length > 0) {
    const trailing = extractTrailingToolEvents(res.messages || []);
    const batchKey = getTrailingToolBatchKey(res.messages || []);
    const errorEvents = trailing.filter((ev) => ev.response && ev.response.error);
    state.gatesHiddenDueToError = true;
    state.lastErrorContext = {
      sessionId: state.activeSessionId,
      reasonText: gateErrors.join("; "),
    };
    const errPayload = res.error_reply_text || formatBridgeToolResults(errorEvents) || gateErrors.join("\n");
    if (errPayload && batchKey) {
      emit("bridge:auto-copy-result", {
        key: batchKey,
        text: errPayload,
      });
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
  clearGateElapsedTimer();
  state.busy = false;
  state.pendingStatusText = null;
  state.pendingStartedAtMs = null;
  state.resolvingGateId = null;
  state.liveScoutRun = null;
  state.liveExec = null;
  if (state.activeSessionId) {
    try {
      const detail = await api.getSessionDetail(state.activeSessionId);
      state.activeSession = detail.session;
      state.messages = detail.messages || [];
      state.pendingGates = detail.pending_gates || [];
      if (Array.isArray(detail.checkpoints)) state.checkpoints = detail.checkpoints;
      emit("backups:refresh");
      emit("git:refresh");
      emit("tree:refresh");
    } catch {
      // fallback if session reload also fails
    }
  }
  emit("session:updated");
  emit("chat:loaded");
  emit("chat:error", err.message);
}

function formatGateLiveSlice(rawOutput) {
  const text = String(rawOutput || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!text.trim()) return "";
  return text.length > 2500 ? `...\n${text.slice(-2500)}` : text;
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
