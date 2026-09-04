// Vanilla JS, no build step, no framework (design doc \u00a715.2/\u00a715.6).
// Talks only to the plain JSON/text endpoints in app.py -- fetch() calls
// and three views toggled by `hidden`, nothing more.
(() => {
  "use strict";

  const POLL_INTERVAL_MS = 2000; // design doc \u00a715.12 item 2 -- a starting
  // value, not derived from a stated requirement; cheap to retune later
  // since it's purely client-side.

  const views = {
    start: document.getElementById("view-start"),
    live: document.getElementById("view-live"),
    report: document.getElementById("view-report"),
  };
  const errorBanner = document.getElementById("error-banner");

  let pollTimer = null;
  let tickTimer = null;
  let liveSessionId = null;
  let lastRemaining = null;
  let lastRemainingAt = null;

  function showView(name) {
    for (const [key, el] of Object.entries(views)) {
      el.hidden = key !== name;
    }
  }

  function showError(message) {
    errorBanner.textContent = message;
    errorBanner.hidden = false;
  }

  function clearError() {
    errorBanner.hidden = true;
    errorBanner.textContent = "";
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch {
        /* not JSON -- keep statusText */
      }
      throw new Error(`${response.status}: ${detail}`);
    }
    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json") ? response.json() : response.text();
  }

  // -- Session list / start / cleanup (view-start) --------------------------

  async function refreshSessions() {
    try {
      const sessions = await api("/api/sessions");
      const tbody = document.querySelector("#sessions-table tbody");
      tbody.innerHTML = "";
      for (const s of sessions) {
        const tr = document.createElement("tr");
        const resumable = s.status !== "COMPLETE" && s.status !== "FAILED";
        tr.innerHTML = `
          <td>${s.session_id}</td>
          <td>${s.repo_slug || s.repo_url}</td>
          <td>${s.status}</td>
          <td>${s.updated_at}</td>
          <td></td>
        `;
        const actionCell = tr.lastElementChild;
        if (resumable) {
          const btn = document.createElement("button");
          btn.textContent = "Resume";
          btn.className = "secondary";
          btn.onclick = () => resumeSession(s.session_id);
          actionCell.appendChild(btn);
        } else if (s.status === "COMPLETE") {
          const btn = document.createElement("button");
          btn.textContent = "Report";
          btn.className = "secondary";
          btn.onclick = () => viewReport(s.session_id);
          actionCell.appendChild(btn);
        }
        tbody.appendChild(tr);
      }
    } catch (err) {
      showError(`Couldn't load sessions: ${err.message}`);
    }
  }

  document.getElementById("refresh-sessions").addEventListener("click", refreshSessions);

  document.getElementById("start-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const repoUrl = document.getElementById("start-repo-url").value.trim();
    const branch = document.getElementById("start-branch").value.trim() || null;
    const durationRaw = document.getElementById("start-duration").value;
    const duration = durationRaw ? Number(durationRaw) : null;
    const sessionName = document.getElementById("start-session-name").value.trim() || null;

    try {
      const result = await api("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo_url: repoUrl, branch, duration_minutes: duration, session_name: sessionName,
        }),
      });
      enterLiveView(result.session_id);
    } catch (err) {
      showError(`Couldn't start session: ${err.message}`);
    }
  });

  document.getElementById("cleanup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const olderThanRaw = document.getElementById("cleanup-older-than").value;
    const olderThan = olderThanRaw ? Number(olderThanRaw) : null;
    const purgeAll = document.getElementById("cleanup-all").checked;

    try {
      const result = await api("/api/cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ older_than: olderThan, all: purgeAll }),
      });
      const out = document.getElementById("cleanup-result");
      out.hidden = false;
      out.textContent = JSON.stringify(result, null, 2);
      document.getElementById("cleanup-clear").hidden = false;
      refreshSessions();
    } catch (err) {
      showError(`Cleanup failed: ${err.message}`);
    }
  });

  document.getElementById("cleanup-clear").addEventListener("click", () => {
    const out = document.getElementById("cleanup-result");
    out.hidden = true;
    out.textContent = "";
    document.getElementById("cleanup-clear").hidden = true;
  });

  async function resumeSession(sessionId) {
    clearError();
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, { method: "POST" });
      enterLiveView(sessionId);
    } catch (err) {
      showError(`Couldn't resume session: ${err.message}`);
    }
  }

  // -- Live session (view-live) ----------------------------------------------

  function enterLiveView(sessionId) {
    liveSessionId = sessionId;
    document.getElementById("live-session-id").textContent = sessionId;
    document.getElementById("live-question-block").hidden = true;
    document.getElementById("live-complete-block").hidden = true;
    document.getElementById("live-answer").value = "";
    lastRemaining = null;
    showView("live");
    stopPolling();
    pollTimer = setInterval(pollState, POLL_INTERVAL_MS);
    tickTimer = setInterval(tickTimer_, 1000);
    pollState();
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    if (tickTimer) clearInterval(tickTimer);
    pollTimer = null;
    tickTimer = null;
  }

  function formatSeconds(totalSeconds) {
    const s = Math.max(0, Math.round(totalSeconds));
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    return `${mm}:${ss}`;
  }

  function tickTimer_() {
    const el = document.getElementById("live-timer");
    if (lastRemaining == null) {
      el.textContent = "";
      return;
    }
    const elapsed = (Date.now() - lastRemainingAt) / 1000;
    el.textContent = formatSeconds(lastRemaining - elapsed);
  }

  async function pollState() {
    if (!liveSessionId) return;
    try {
      const state = await api(`/api/sessions/${encodeURIComponent(liveSessionId)}/state`);
      clearError();
      renderLiveState(state);
    } catch (err) {
      stopPolling();
      showError(`Lost track of the live session: ${err.message}`);
    }
  }

  function renderLiveState(state) {
    document.getElementById("live-detail").textContent = state.detail || "";

    if (state.remaining_seconds != null) {
      lastRemaining = state.remaining_seconds;
      lastRemainingAt = Date.now();
      document.getElementById("live-timer").textContent = formatSeconds(state.remaining_seconds);
    }

    const questionBlock = document.getElementById("live-question-block");
    const completeBlock = document.getElementById("live-complete-block");

    if (state.stage === "awaiting_answer") {
      questionBlock.hidden = false;
      completeBlock.hidden = true;
      document.getElementById("live-category").textContent = state.category || "";
      document.getElementById("live-question-number").textContent = state.question_number ?? "";
      document.getElementById("live-question-text").textContent = state.question_text || "";
    } else {
      questionBlock.hidden = true;
    }

    if (state.stage === "complete") {
      completeBlock.hidden = false;
      stopPolling();
    }

    if (state.stage === "error") {
      showError(state.error_message || "The session hit an error.");
      stopPolling();
    }
  }

  document.getElementById("live-submit").addEventListener("click", async () => {
    const textarea = document.getElementById("live-answer");
    const text = textarea.value.trim();
    if (!text || !liveSessionId) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(liveSessionId)}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      textarea.value = "";
      document.getElementById("live-question-block").hidden = true;
      pollState();
    } catch (err) {
      showError(`Couldn't submit answer: ${err.message}`);
    }
  });

  document.getElementById("live-back").addEventListener("click", () => {
    stopPolling();
    liveSessionId = null;
    showView("start");
    refreshSessions();
  });

  document.getElementById("live-view-report").addEventListener("click", () => {
    if (liveSessionId) viewReport(liveSessionId);
  });

  // -- Report (view-report) --------------------------------------------------

  async function viewReport(sessionId) {
    clearError();
    stopPolling();
    try {
      const text = await api(`/api/sessions/${encodeURIComponent(sessionId)}/report?format=md&allow_partial=true`);
      document.getElementById("report-body").textContent = text;
      document.getElementById("report-json-link").href =
        `/api/sessions/${encodeURIComponent(sessionId)}/report?format=json&allow_partial=true`;
      showView("report");
    } catch (err) {
      showError(`Couldn't load report: ${err.message}`);
    }
  }

  document.getElementById("report-back").addEventListener("click", () => {
    showView("start");
    refreshSessions();
  });

  // -- init -------------------------------------------------------------------

  refreshSessions();
})();
