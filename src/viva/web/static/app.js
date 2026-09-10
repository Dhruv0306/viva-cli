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
  // Only "awaiting_answer" is time the person is actually being timed on
  // (AnswerTimer.excluding(), timer.py -- the backend already excludes
  // everything else: LLM generation, classification, follow-up planning
  // between questions). Tracked here so tickTimer_() can freeze the
  // on-screen countdown during that gap instead of ticking straight
  // through it as if the person were still being timed.
  let currentStage = null;

  // -- Voice (Phase 12, docs/system-design/17-phase-12-web-voice-io-design.md)
  // Per-session toggle: purely client-side state (\u00a717.2) -- the backend
  // never learns whether a given browser session wants voice, it just
  // offers the endpoints below whenever the server has voice enabled at
  // all. voiceEnabledForSession is read from the start form's checkbox
  // once, when a session begins, and used for that browser tab only.
  let voiceServerAvailable = false;
  let voiceMaxAnswerSeconds = 120;
  let voiceSilenceTimeoutSeconds = 2.5;
  let voiceEnabledForSession = false;
  let lastSpokenQuestionNumber = null;
  let recordingState = null; // non-null exactly while a recording is in progress

  function voiceClientCapable() {
    // AudioWorklet requires a secure context (HTTPS or localhost) in
    // every modern browser (design doc \u00a717.7) -- binding `viva serve`
    // to a LAN address over plain HTTP will fail this even when the
    // server itself has voice enabled.
    return !!(
      window.isSecureContext &&
      window.AudioContext &&
      window.OfflineAudioContext &&
      navigator.mediaDevices &&
      navigator.mediaDevices.getUserMedia
    );
  }

  async function checkVoiceAvailable() {
    try {
      const result = await api("/api/voice/available");
      voiceServerAvailable = !!result.available;
      if (result.max_answer_seconds) voiceMaxAnswerSeconds = result.max_answer_seconds;
      if (result.silence_timeout_seconds) voiceSilenceTimeoutSeconds = result.silence_timeout_seconds;
    } catch {
      voiceServerAvailable = false;
    }
    document.getElementById("start-voice-enabled-row").hidden =
      !(voiceServerAvailable && voiceClientCapable());
  }

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
        const shortId = s.session_id.length > 12 ? `${s.session_id.slice(0, 10)}\u2026` : s.session_id;
        const repoLabel = s.repo_slug || s.repo_url;
        tr.innerHTML = `
          <td title="${s.session_id}">${shortId}</td>
          <td title="${repoLabel}">${repoLabel}</td>
          <td>${s.status}</td>
          <td>${s.updated_at}</td>
          <td></td>
        `;
        const actionCell = tr.lastElementChild;
        // s.resumable comes straight from Orchestrator.resume()'s own
        // validation (is_resumable() in orchestrator.py) -- not
        // re-derived here, so this can't drift out of sync with what
        // /resume will actually accept the way it used to (a session
        // interrupted mid-setup, e.g. status ANALYZING, used to still
        // show a "Resume" button that could only ever 409).
        if (s.resumable) {
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
        // Neither resumable nor COMPLETE (interrupted mid-setup, or
        // FAILED) -- nothing can be done with it from here besides
        // starting a new session, so no button at all rather than one
        // that's guaranteed to fail.
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
    voiceEnabledForSession =
      document.getElementById("start-voice-enabled").checked &&
      voiceServerAvailable && voiceClientCapable();

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
    currentStage = null;
    lastSpokenQuestionNumber = null;
    document.getElementById("live-voice-controls").hidden = !voiceEnabledForSession;
    document.getElementById("live-voice-status").textContent = "";
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
    if (currentStage !== "awaiting_answer") {
      // Not the person's turn to be timed right now (working/starting/
      // complete/error/time_expired) -- freeze the display at the last
      // known value instead of ticking through wall-clock time the
      // backend's own AnswerTimer.excluding() already isn't counting
      // against them. The next poll's fresh remaining_seconds
      // re-baselines lastRemaining/lastRemainingAt once the next
      // question actually starts the clock again.
      el.textContent = formatSeconds(lastRemaining);
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
    currentStage = state.stage;
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
      if (
        voiceEnabledForSession &&
        state.question_number != null &&
        state.question_number !== lastSpokenQuestionNumber
      ) {
        lastSpokenQuestionNumber = state.question_number;
        // Real bug found in testing (docs/system-design/
        // 17-phase-12-web-voice-io-design.md §17.8): a transcribed-
        // answer status message from a *previous* question was
        // staying on screen through the next question, looking like it
        // belonged to the new one. Cleared here as defense-in-depth on
        // top of the live-submit handler's own clear, since a new
        // question number showing up is itself proof the prior answer
        // is gone, regardless of which path got it submitted.
        document.getElementById("live-voice-status").textContent = "";
        playQuestionAudio();
      }
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

  // -- Voice: question playback (Phase 12) ------------------------------------

  async function playQuestionAudio() {
    if (!liveSessionId) return;
    try {
      const response = await fetch(
        `/api/sessions/${encodeURIComponent(liveSessionId)}/question-audio`,
      );
      if (!response.ok) return; // 409/503 -- question text is already on screen either way
      const blob = await response.blob();
      const audioEl = document.getElementById("live-question-audio");
      audioEl.src = URL.createObjectURL(blob);
      // Autoplay can be blocked by the browser until the person has
      // interacted with the page at least once -- not fatal, the
      // question text is already visible regardless.
      await audioEl.play().catch(() => {});
    } catch {
      /* best-effort; question text is already visible */
    }
  }

  // -- Voice: recording an answer (Phase 12) -----------------------------------
  // Mirrors the CLI's own energy-based silence cutoff (docs/system-design/
  // 16-phase-11-voice-io-design.md \u00a716.3) against ~0.1s PCM blocks the
  // AudioWorklet posts (static/voice-worklet.js), rather than inventing a
  // second cutoff behavior for the same feature.

  const SILENCE_RMS_THRESHOLD = 0.015; // float32 [-1,1] scale; ~500/32768, the CLI's int16 threshold

  function computeRms(float32Block) {
    let sumSquares = 0;
    for (let i = 0; i < float32Block.length; i += 1) {
      sumSquares += float32Block[i] * float32Block[i];
    }
    return Math.sqrt(sumSquares / float32Block.length);
  }

  function mergeFloat32Chunks(chunks) {
    const total = chunks.reduce((sum, c) => sum + c.length, 0);
    const merged = new Float32Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      merged.set(chunk, offset);
      offset += chunk.length;
    }
    return merged;
  }

  // Resamples to 16kHz (what faster-whisper expects) and converts to
  // int16 PCM via OfflineAudioContext -- the standard way to resample
  // client-side without a JS DSP library (design doc \u00a717.6).
  async function resampleTo16kInt16(float32Samples, nativeRate) {
    const targetRate = 16000;
    const targetLength = Math.ceil((float32Samples.length * targetRate) / nativeRate);
    const offlineCtx = new OfflineAudioContext(1, targetLength, targetRate);
    const buffer = offlineCtx.createBuffer(1, float32Samples.length, nativeRate);
    buffer.copyToChannel(float32Samples, 0);
    const source = offlineCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(offlineCtx.destination);
    source.start();
    const rendered = await offlineCtx.startRendering();
    const resampled = rendered.getChannelData(0);
    const int16 = new Int16Array(resampled.length);
    for (let i = 0; i < resampled.length; i += 1) {
      const s = Math.max(-1, Math.min(1, resampled[i]));
      int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return int16;
  }

  async function startRecording() {
    if (recordingState || !liveSessionId) return;
    clearError();
    const statusEl = document.getElementById("live-voice-status");
    const recordBtn = document.getElementById("live-record-btn");

    let stream;
    let audioContext;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // Explicit mono, matching the CLI's own capture format, rather
        // than relying on the worklet silently reading only channel 0
        // of what could otherwise be a stereo stream. echoCancellation/
        // noiseSuppression are free quality wins most browsers already
        // support for exactly this use case.
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      audioContext = new AudioContext();
      await audioContext.audioWorklet.addModule("/static/voice-worklet.js");
    } catch (err) {
      statusEl.textContent = `Couldn't access the microphone: ${err.message}`;
      return;
    }

    const source = audioContext.createMediaStreamSource(stream);
    const workletNode = new AudioWorkletNode(audioContext, "voice-capture-processor");
    const chunks = [];
    let speechDetected = false;
    let silenceBlocks = 0;
    let blockCount = 0;
    const blockSeconds = 0.1;
    const silenceBlockThreshold = Math.round(voiceSilenceTimeoutSeconds / blockSeconds);
    const maxBlocks = Math.round(voiceMaxAnswerSeconds / blockSeconds);

    recordingState = { stopped: false, finish: null };
    recordBtn.textContent = "Stop recording";
    statusEl.textContent = "\ud83c\udfa4 Recording... speak your answer.";

    const finishRecording = async () => {
      if (!recordingState || recordingState.stopped) return;
      recordingState.stopped = true;
      workletNode.port.onmessage = null;
      workletNode.disconnect();
      source.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      const nativeRate = audioContext.sampleRate;
      await audioContext.close();
      recordingState = null;
      recordBtn.textContent = "Record answer";

      if (!speechDetected || chunks.length === 0) {
        statusEl.textContent = "No speech detected -- try again, or type your answer below.";
        return;
      }

      statusEl.textContent = "Transcribing...";
      try {
        const merged = mergeFloat32Chunks(chunks);
        const pcm16 = await resampleTo16kInt16(merged, nativeRate);
        const result = await api(
          `/api/sessions/${encodeURIComponent(liveSessionId)}/answer-audio`,
          { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: pcm16.buffer },
        );
        // Real-world fix (docs/system-design/
        // 17-phase-12-web-voice-io-design.md §17.8): this used to
        // auto-submit immediately, so there was no way to see or
        // correct a misheard word before it was already recorded as
        // the answer. Puts the transcript in the textarea instead --
        // review it, edit it if needed, then submit the same way a
        // typed answer would be.
        const textarea = document.getElementById("live-answer");
        textarea.value = result.text;
        textarea.focus();
        statusEl.textContent = "Transcribed -- review below, then submit.";
      } catch (err) {
        // 422 (no speech) or 503 (voice unavailable) both land here --
        // either way, the typed textarea underneath is still right
        // there as a fallback, same as the CLI's own per-question
        // fallback (§16.6), just without a background thread to fall
        // through to it automatically.
        statusEl.textContent = `Couldn't transcribe (${err.message}) -- try again, or type your answer below.`;
      }
    };
    recordingState.finish = finishRecording;

    workletNode.port.onmessage = (event) => {
      const block = event.data;
      blockCount += 1;
      const rms = computeRms(block);
      if (rms >= SILENCE_RMS_THRESHOLD) {
        speechDetected = true;
        silenceBlocks = 0;
        chunks.push(block);
      } else if (speechDetected) {
        silenceBlocks += 1;
        chunks.push(block);
        if (silenceBlocks >= silenceBlockThreshold) {
          finishRecording();
          return;
        }
      }
      // Leading silence before speech is first detected isn't buffered
      // at all -- matches the CLI's own record() behavior.
      if (blockCount >= maxBlocks) {
        finishRecording();
      }
    };

    source.connect(workletNode);
  }

  document.getElementById("live-record-btn").addEventListener("click", () => {
    if (recordingState) {
      if (recordingState.finish) recordingState.finish();
    } else {
      startRecording();
    }
  });

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
      document.getElementById("live-voice-status").textContent = "";
      document.getElementById("live-question-block").hidden = true;
      pollState();
    } catch (err) {
      showError(`Couldn't submit answer: ${err.message}`);
    }
  });

  document.getElementById("live-back").addEventListener("click", () => {
    stopPolling();
    if (recordingState && recordingState.finish) recordingState.finish();
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
      const reportPath = `/api/sessions/${encodeURIComponent(sessionId)}/report`;
      const htmlFragment = await api(`${reportPath}?format=html&allow_partial=true`);
      document.getElementById("report-body").innerHTML = htmlFragment;
      document.getElementById("report-download-md").href =
        `${reportPath}?format=md&allow_partial=true&download=true`;
      document.getElementById("report-download-json").href =
        `${reportPath}?format=json&allow_partial=true&download=true`;
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
  checkVoiceAvailable();
})();
