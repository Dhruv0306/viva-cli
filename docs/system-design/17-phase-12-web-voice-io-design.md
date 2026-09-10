# System Design Reference — Part 17: Phase 12 Web Voice I/O Design

## 17.1 Scope

Brings Phase 11's voice mode (docs/system-design/
16-phase-11-voice-io-design.md) to "viva room" (`viva serve`) -- spoken
questions and spoken answers in the browser, using the same
`LocalVoiceIO` engine (faster-whisper + Piper) as the CLI. §16.1 of that
design doc explicitly deferred this; this is that follow-up phase.

The CLI and the web UI have fundamentally different audio I/O
capabilities: the CLI has a real microphone and speakers attached to
the process via `sounddevice`; a browser tab has neither, from the
server's point of view. Everything here is about bridging that gap
without compromising the no-external-API-calls guarantee the CLI's
voice mode already established -- browser-native `SpeechRecognition`
remains off the table for the same reason §16.1 rejected it (a
round-trip through Google's servers in Chrome).

## 17.2 What's actually different from the CLI, and the decisions made

**Speaking the question.** The CLI's `speak()` synthesizes *and plays*
audio locally through `sounddevice`. A browser can only play audio it's
been handed -- there's no "play through the server's speakers" that
means anything to the person using the browser. Decided: server-side
Piper synthesis (not browser-native `speechSynthesis`), served as a WAV
blob the browser plays through an `<audio>` element. `speechSynthesis`
would have meant zero backend work and is genuinely on-device (unlike
`SpeechRecognition`), but voice quality is OS/browser-dependent and
wouldn't match the CLI's Piper voice -- picking consistency and quality
over the zero-backend-work option.

**Capturing the answer.** The CLI records raw PCM directly via
`sounddevice` -- nothing to decode. A browser has two realistic capture
paths: `MediaRecorder` (simple, but only produces compressed audio --
WebM/Opus in Chrome/Firefox -- which would need a new server-side audio
decode dependency this project doesn't have today), or raw PCM capture
via the Web Audio API (`AudioContext` + `AudioWorklet`, more client-side
JS, but the backend gets the exact same clean 16-bit PCM signal
`transcribe()` already expects from the CLI, no lossy compression, no
new Python dependency). Decided: raw PCM via `AudioWorklet`, resampled
to 16kHz in the browser via `OfflineAudioContext` before it's sent --
best transcription quality, and it keeps `voice_io.py`'s dependency
surface exactly as-is.

**Per-session vs. server-wide.** Decided: per-session, meaning each
browser session decides independently whether to use voice for itself.
This turns out to need no backend session-state at all (§17.4) --
`VOICE_ENABLED` still gates whether the *server* is willing to offer
voice (same meaning as it already has for the CLI: are the engine and
models actually usable here), and a browser simply chooses, per tab,
whether to call the voice endpoints this phase adds. No new "is voice
on for session X" field anywhere in `SessionRegistry`/`WebSessionUI`
was needed to support this.

## 17.3 New `voice_io.py` capability: `synthesize()`

```python
def synthesize(self, text: str) -> bytes:
    """WAV-encoded audio, not played -- the web layer's entry point."""
```

Factored `LocalVoiceIO`'s Piper call out of `speak()` into a private
`_synthesize_pcm(text) -> tuple[bytes, int] | None` (raw PCM + sample
rate, or `None` for blank input), so `speak()` (plays the PCM locally,
CLI-only) and the new `synthesize()` (wraps the same PCM in a WAV
container for the web layer) share the actual Piper call without either
round-tripping through the other's output format. WAV wrapping uses the
stdlib `wave` module -- no new dependency, and WAV plays natively in
every browser's `<audio>` element with zero client-side decode work.

`transcribe()` is unchanged -- the web layer's uploaded PCM is already
in exactly the int16 format `transcribe()` expects from the CLI's own
`record()`, so there's nothing new to add there.

## 17.4 Backend: two new endpoints, no new session state

```
GET  /api/voice/available
GET  /api/sessions/{id}/question-audio
POST /api/sessions/{id}/answer-audio
```

`GET /api/voice/available` is a cheap capability check -- `{"available":
true}` or `{"available": false, "reason": "..."}` -- so the frontend
knows whether to show the mic toggle at all, without loading a model
just to answer that question. Checked once per page load, not polled.

`GET /api/sessions/{id}/question-audio` synthesizes the *session's
current* question (read from `WebSessionUI.snapshot()`, the same
`question_text` the text view already renders) and returns it as
`audio/wav`. 409 if the session isn't currently `awaiting_answer` (no
question to speak) -- mirrors the existing `/answer` route's 409 for
"not currently awaiting an answer."

`POST /api/sessions/{id}/answer-audio` takes a raw PCM body (16kHz mono
int16, exactly what `AudioWorklet`'s resampling step below produces)
and transcribes it -- it does *not* call `submit_answer()` itself
(revised from the original version of this design after real-world
testing, §17.8: transcribing and submitting in one step meant no
chance to see or correct a misheard word before it was already
recorded as the answer). Returns `{"text": "..."}`; the frontend puts
that in the same answer textarea a typed answer would use, and actual
submission goes through the existing `POST .../answer` route -- one
submission code path regardless of where the text came from, and from
the Orchestrator's point of view a voice answer and a typed answer
remain indistinguishable. Returns 422 (not 200 with empty text) if
nothing transcribable came through, matching the CLI's fallback
trigger for silence/empty transcription (§16.6) -- except here there's
no background thread to fall through to typed input on its own, so the
frontend has to handle "try again or type it" itself (§17.6).

**`SessionRegistry` gains one `LocalVoiceIO` instance, shared across
every live session in the process**, built lazily on first actual use
(not at server startup) and cached the same way `LocalVoiceIO` itself
already caches its loaded models. Two thin methods:

```python
def synthesize(self, text: str) -> bytes: ...
def transcribe(self, pcm: bytes, timer: AnswerTimer | None) -> str | None: ...
```

Both take a single `self._voice_lock` for the duration of the actual
Piper/faster-whisper call, not just construction. faster-whisper/Piper
model instances aren't documented as safe for concurrent inference from
multiple threads, and `viva serve` genuinely can have more than one
browser tab open against it -- serializing voice calls trades a small
amount of latency under real concurrent use (rare for what's
fundamentally a personal, single-operator tool) for not gambling on
undocumented thread-safety. `transcribe()` takes the *session's own*
`AnswerTimer` (via a new `WebSessionUI.timer` property, exposing what
`read_answer()` already stores in `self._timer` -- see that file's own
unlocked read of the same attribute inside `snapshot()`, same
justification applies here: assigned once per question on the
Orchestrator's thread, read from an HTTP thread, a plain object
reference under the GIL) and wraps the transcription call in
`timer.excluding()` -- transcription compute is excluded from the
answer clock exactly like it is for the CLI (§16.4); the WAV
synthesis for the question doesn't need an equivalent wrap on the web
side, since (unlike the CLI's `ask_question()`) nothing here blocks the
Orchestrator's own thread while it happens.

## 17.5 `AnswerTimer` gets a lock

Every prior caller of `timer.excluding()` ran on the same single thread
as everything else touching that `AnswerTimer`. `transcribe()` above is
the first caller from a *different* thread than the Orchestrator's own
loop, running concurrently with `WebSessionUI.snapshot()`'s own
`timer.remaining()` read from a polling request. `AnswerTimer` had no
locking at all. Added a `threading.Lock` around `start()`'s and
`excluding()`'s mutations of `_start`/`_excluded_seconds`, and around
`elapsed()`'s read of the same two fields -- makes the read/mutate pair
atomic with respect to each other rather than relying on a favorable
GIL scheduling of `+=`'s underlying load/add/store bytecodes. No
behavior change for the CLI's existing single-threaded usage; this is
purely a correctness fix for the newly-possible cross-thread case.
Honesty note on the accompanying test: an attempt to reproduce an
actual lost update against the pre-lock code (even with an aggressive
`sys.setswitchinterval` and a tight no-sleep loop) didn't succeed --
CPython's GIL makes a bare float `+=` surprisingly resistant to this
specific race in practice. The lock is still the correct fix for a
genuinely new cross-thread access pattern; the test is defensive
hardening, not a reproduced-failure regression test.

## 17.6 Frontend

**Mic toggle.** A checkbox on the start form (`index.html`), sent along
with nothing else -- it never reaches the backend at all (§17.2). Purely
client-side state (a JS variable) that decides which code path `app.js`
takes for the rest of that browser session: fetch `/question-audio` and
play it after each new question, and record via `AudioWorklet` instead
of showing the typed `<textarea>` for answers. Hidden entirely if `GET
/api/voice/available` reports unavailable.

**Playing the question.** On each new question (detected the same way
`app.js` already detects one -- `question_number` changing in a poll
response), fetch `/question-audio` and play it via a shared `<audio>`
element. No change to the polling loop itself.

**Recording the answer.** A new `AudioWorklet` processor
(`static/voice-worklet.js`, loaded via `audioContext.audioWorklet
.addModule()`) receives Float32 PCM at the `AudioContext`'s native
sample rate (44.1kHz/48kHz depending on the machine) and posts frames
back to the main thread. The main thread runs the same energy-based
silence cutoff `_record_pcm()` already established for the CLI (design
doc §16.3) -- RMS threshold, block-based silence counting against
`VOICE_SILENCE_TIMEOUT_SECONDS`, hard-capped at
`VOICE_MAX_ANSWER_SECONDS` -- reimplemented in JS against the same
constants rather than inventing a second cutoff behavior for the same
feature. Once recording stops, the captured buffer is resampled to
16kHz via an `OfflineAudioContext` (the standard way to resample
client-side without pulling in a JS DSP library) and posted as raw
int16 PCM bytes to `/answer-audio`. On success, the returned transcript
goes into the answer `<textarea>` for review -- not auto-submitted
(§17.8) -- so a misheard word can be corrected before the person
presses the same submit button a typed answer would use.

**On a 422 (nothing transcribable).** Shows an inline message and
re-offers the recording button rather than silently doing nothing --
there's no background thread to fall through to typed input on its own
the way the CLI's `read_answer()` does, so the person needs an explicit
way to just type the answer instead if voice keeps failing. The typed
`<textarea>` stays visible underneath the recording control the whole
time (not swapped out), so switching to typing is always just "type in
the box that's already there," never a mode switch.

## 17.7 Known limitations

- No live waveform/amplitude visual during recording, same limitation
  the CLI's own §16.8 already accepts.
- `AudioWorklet` requires a secure context (HTTPS or `localhost`) in
  every modern browser -- `viva serve`'s default plain-HTTP binding on
  a non-`localhost` address (e.g. binding to `0.0.0.0` for LAN access)
  will show the mic toggle as unavailable in that case; this is a
  browser platform restriction, not something this project can route
  around.
- Voice call serialization (§17.4) means two people using voice
  simultaneously against one `viva serve` process will queue behind
  each other for the duration of each synthesis/transcription call --
  acceptable for a personal tool, not something this phase optimizes.

## 17.8 Real-world bugs found during testing

- **A transcribed-answer status message persisted across questions,
  looking like it belonged to the wrong one.** Reported with a
  screenshot: question 2 was on screen, but the recording status area
  still showed question 1's "✓ Answer transcribed: ..." text.
  `renderLiveState()`'s new-question-detected branch reset
  `lastSpokenQuestionNumber` (for question-audio playback) but never
  cleared the voice status line itself -- nothing else in the polling
  loop touched it between questions. Fixed by clearing it both there
  (defensively, on any new question number) and in the submit
  handler (on any successful submission, typed or voice-originated).
- **The transcribed answer was submitted before the person could see
  or correct it.** Raised alongside the bug above: `answer-audio`
  used to transcribe *and* call `submit_answer()` in the same request,
  so a misheard technical term (a known STT accuracy risk, §16.2) was
  already recorded as the answer with no chance to review it -- the
  status line showed the transcript, but only as an after-the-fact
  receipt, not something editable. Changed `answer-audio` to only
  transcribe and return the text; the frontend now puts it in the
  answer textarea (the same box typed answers use) and waits for an
  explicit submit, giving a free proofreading step against exactly the
  accuracy risk that motivated picking faster-whisper's more accurate
  models in the first place. `ui.submit_answer()` is no longer called
  from this route at all -- actual submission goes through the
  existing `POST .../answer` route, so there's one submission code
  path regardless of whether the text came from typing or from voice.
