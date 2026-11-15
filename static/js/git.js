// git.js - Git status, Gemini commit message drafting, Commit, and Commit & Push

import { api } from "./api.js";
import { getActiveProject, on, state } from "./state.js";

const branchLabel = document.getElementById("git-branch-label");
const filesList = document.getElementById("git-files-list");
const msgInput = document.getElementById("git-message-input");
const resultLabel = document.getElementById("git-result");
const draftBtn = document.getElementById("btn-git-draft");
const commitBtn = document.getElementById("btn-git-commit");
const pushBtn = document.getElementById("btn-git-commit-push");

export function initGit() {
  document.getElementById("btn-git-refresh").addEventListener("click", refreshGitStatus);
  draftBtn.addEventListener("click", draftMessage);
  commitBtn.addEventListener("click", () => commitChanges(false));
  pushBtn.addEventListener("click", () => commitChanges(true));

  on("project:changed", refreshGitStatus);
  on("git:refresh", refreshGitStatus);
}

export async function refreshGitStatus() {
  const proj = getActiveProject();
  if (!proj) {
    branchLabel.textContent = "Branch: -";
    filesList.innerHTML = "";
    return;
  }
  try {
    const status = await api.gitStatus(proj.id);
    if (!status.is_repo) {
      branchLabel.textContent = status.error || "Not a git repo";
      filesList.innerHTML = "";
      return;
    }
    branchLabel.textContent = `${status.branch} · Diff ~${(status.diff_tokens || 0).toLocaleString()} tok`;
    filesList.innerHTML = "";
    if (!status.changed_files.length) {
      filesList.innerHTML = `<div class="meta-line">Working tree clean</div>`;
      return;
    }
    for (const f of status.changed_files) {
      const row = document.createElement("div");
      row.className = "git-row mono";
      row.innerHTML = `<span>${escapeHtml(f.status)}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(f.path)}</span>`;
      filesList.appendChild(row);
    }
  } catch (err) {
    branchLabel.textContent = err.message;
  }
}

async function draftMessage() {
  const proj = getActiveProject();
  if (!proj) return;
  draftBtn.disabled = true;
  draftBtn.textContent = "Drafting...";
  resultLabel.textContent = "";
  try {
    const model = (state.activeSession && state.activeSession.model) || "gemini-4-hydrogen";
    const res = await api.gitDraftMessage(proj.id, model);
    msgInput.value = res.message;
    resultLabel.textContent = `Drafted from ~${res.diff_tokens} diff tokens`;
  } catch (err) {
    resultLabel.textContent = err.message;
  } finally {
    draftBtn.disabled = false;
    draftBtn.textContent = "Draft with Gemini";
  }
}

async function commitChanges(push) {
  const proj = getActiveProject();
  if (!proj) return;
  const message = msgInput.value.trim();
  if (!message) {
    resultLabel.textContent = "Enter or draft a commit message first.";
    return;
  }
  commitBtn.disabled = true;
  pushBtn.disabled = true;
  resultLabel.textContent = push ? "Committing & pushing..." : "Committing...";
  try {
    const res = await api.gitCommit({
      project_id: proj.id,
      message,
      push,
      stage_all: true,
    });
    msgInput.value = "";
    resultLabel.textContent = res.pushed ? "Committed and pushed." : "Committed locally.";
    await refreshGitStatus();
  } catch (err) {
    resultLabel.textContent = err.message;
  } finally {
    commitBtn.disabled = false;
    pushBtn.disabled = false;
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
