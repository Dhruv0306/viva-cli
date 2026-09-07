# System Design Reference — Part 16: Phase 11 Voice I/O Design

## 16.1 Scope

Lets a person speak their answers instead of typing them, and has the
session speak questions aloud instead of (or alongside) printing them.
CLI only for this phase — the web UI ("viva room") gets its own follow-up
phase once the CLI path has real-world mileage, since browser audio
capture and playback are a different integration surface (`MediaRecorder`,
`<audio>`) from a terminal's stdin/stdout.

Everything runs locally, consistent with the project's no-external-API-
calls guarantee. This rules out the browser's native `SpeechRecognition`
API as a model for anything — in Chrome that call round-trips through
Google's servers rather than running on-device, which would quietly break
the guarantee this whole tool is built on. Both the speech-to-text and
text-to-speech engines chosen here run entirely offline once their models
are pulled, the same trust boundary as Ollama.

Explicitly out of scope: web UI voice support; true voice-activity
detection (VAD) beyond simple energy thresholding; multi-language
transcription (English-only for this phase, matching the questions and
evaluation prompts, which are English-only today).

## 16.2 Engine choices

**Speech-to-text: faster-whisper.** Chosen over Vosk. Vosk is lighter and
faster to set up, but weaker on technical vocabulary, code identifiers,
and library names — exactly the terms an answer to a code question is
made of. A misheard identifier turns into a wrong-looking answer through
no fault of the person answering, which is a worse failure mode here than
a slower or larger model. faster-whisper (Whisper via CTranslate2) costs
more disk space and a longer first-run pull, but that's a one-time cost
against a per-answer accuracy risk.

**Text-to-speech: Piper.** Chosen over `pyttsx3`. `pyttsx3` wraps
whatever the OS provides (SAPI5/NSSpeech/espeak) with zero setup, but
quality varies a lot by platform and espeak in particular is rough. Piper
is a small local neural TTS engine (ONNX-based) with noticeably better
output, fully offline, at the cost of a per-voice model pull — the same
tradeoff shape as the STT choice, and the same one-time-cost-for-
consistent-quality reasoning.

Both engines need a model/voice file before first use. Rather than
downloading on the critical path of a person's first `viva start` with
voice enabled, model management follows the same shape Ollama's `pull`
already establishes: a `viva voice setup` command (§16.5) does the
download up front, so `viva start` itself never stalls on it.

## 16.3 New component: `voice_io.py`

One new module, mirroring the `LLMClient`/`EmbeddingClient` thin-interface
pattern (NFR5, `embedding_client.py`'s docstring) — an ABC so nothing
downstream imports faster-whisper, Piper, or `sounddevice` directly:

```python
class VoiceIO(abc.ABC):
    def speak(self, text: str) -> None: ...
    def record(self, max_seconds: float, silence_timeout: float) -> bytes | None: ...
    def transcribe(self, audio: bytes) -> str | None: ...
```

`record()` and `transcribe()` are deliberately two calls, not one
`listen()` — see §16.4 for why the timer needs that seam.

`record()`'s stop condition: recording starts immediately, and stops on
whichever comes first — `max_seconds` elapses (the hard safety-net cap,
`Config.voice_max_answer_seconds`), or the captured audio's RMS amplitude
stays below a fixed threshold for `silence_timeout` seconds
(`Config.voice_silence_timeout_seconds`) after speech has been detected
at least once. This is a simple energy-based cutoff, not real VAD (no
`webrtcvad`/model-based speech detection) — sufficient to stop recording
shortly after a person finishes talking without adding another model
dependency, but it can be fooled by a noisy room. Logged as a known
limitation (§16.8) rather than solved now.

`LocalVoiceIO` is the one real implementation, wrapping `sounddevice`
(capture/playback), faster-whisper (`transcribe`), and Piper (`speak`).
All three third-party imports are lazy (inside the methods that need
them, via module-level helper functions `_load_whisper_model()`,
`_load_piper_voice()`, `_record_pcm()`, `_play_pcm()`), for two reasons:
first, so importing `viva.voice_io` doesn't require the `voice` extra to
be installed at all (`viva.cli` can still import the module to type-hint
against `VoiceIO` even when nobody's opted in); second, so the CI suite
never needs real audio hardware or multi-hundred-MB model downloads —
tests monkeypatch these module-level helpers directly (mirroring how
`test_orchestrator.py` monkeypatches `orchestrator_module.ingest_repo`
and friends), never triggering a real import of `sounddevice`/
`faster_whisper`/`piper`.

`VoiceDependencyError(RuntimeError)` is raised by the lazy loaders if the
`voice` extra isn't installed, with a message pointing at
`pip install -e ".[voice]"` — caught at the call site in `session_ui.py`
and treated as a fallback trigger (§16.6), not a crash.

## 16.4 The timer boundary (FR17)

The existing rule — "the session clock must reflect answering time only"
(`timer.py`'s module docstring) — extends to voice cleanly once the
question is asked correctly: **recording the person's spoken answer
counts as answering time, the same as the seconds spent typing would.**
It should *not* be excluded. What must be excluded is any computation the
person isn't actively producing the answer during:

- **Transcribing the recording** (faster-whisper inference) — the same
  category as `generate_question()`'s or `classify()`'s LLM latency.
- **Synthesizing and playing the question aloud** (Piper + playback,
  before the person starts answering at all) — this isn't the person's
  time in any sense, any more than the orchestrator printing the question
  text is.

This is why `record()` and `transcribe()` are separate `VoiceIO` calls
rather than one `listen()`: `RichSessionUI.read_answer(timer)` already
receives the timer, so it can wrap only the second call:

```python
audio = self._voice.record(timer.remaining(), self._config.voice_silence_timeout_seconds)
with timer.excluding():
    answer_text = self._voice.transcribe(audio)
```

**Correction from the pre-implementation plan:** the original proposal
for this phase suggested pausing the visible countdown while recording.
That's inconsistent with the rest of the system — typed answering time
isn't paused or excluded either, and treating spoken answering time
differently would mean the clock rewards speaking over typing for no
principled reason. Recording ticks normally; only the STT/TTS compute
around it is excluded.

For speaking the question, `Orchestrator._run_live_session()`'s existing
call site gets the same treatment every other latency source there
already has:

```python
with timer.excluding():
    self.ui.ask_question(question_text, selected_item.category, question_number)
```

This is a no-op change for `RichSessionUI` in text mode (printing is
effectively instant) but is required once `ask_question()` can also
trigger several seconds of TTS synthesis and playback — without it, this
phase reproduces the exact class of bug §12.10 (below) documents: system-
side latency silently eating into the person's answering budget.

## 16.5 `viva voice setup`

New CLI command, `src/viva/cli.py`:

```
viva voice setup [--stt-model base] [--tts-voice en_US-lessac-medium]
```

Calls `_load_whisper_model()`/`_load_piper_voice()` eagerly against
`Config.voice_cache_dir`, reporting progress the same way `ollama pull`
does, and exits 0 once both are cached. `viva start`/`viva resume` don't
call this automatically — if `voice_enabled=true` and the models aren't
cached yet, `LocalVoiceIO` raises `VoiceDependencyError` pointing at this
command up front (at Orchestrator construction, before any ingest work
starts), rather than failing awkwardly mid-session on the first question.

## 16.6 Fallback behavior

Three failure points, all handled the same way — degrade the current
question to text input, log a visible one-line notice, keep the session
running:

- **`VoiceDependencyError`** at construction (extra not installed, or
  models not pulled) — checked once, before `_run_live_session` starts,
  so the whole session falls back to text mode rather than failing
  per-question.
- **Silence/empty recording** — `record()` returns `None` if nothing
  crossed the amplitude threshold before `max_seconds`; `read_answer()`
  treats this exactly like today's empty-string submission.
- **Mic/output-device failure at record or playback time** (device
  unplugged mid-session, permission revoked) — caught per-call, falls
  back to text input for that one question, voice mode stays enabled for
  the next one (a transient failure shouldn't permanently downgrade the
  whole session, mirroring the "prefer, never exclude" principle applied
  to hardware failures rather than duplicate questions).

## 16.7 `Config` additions (FR28)

```
# --- Voice I/O (Phase 11, docs/system-design/16-phase-11-voice-io-design.md) ---
VOICE_ENABLED=false
STT_MODEL_SIZE=base
TTS_VOICE=en_US-lessac-medium
VOICE_CACHE_DIR=./data/voice_models
VOICE_MAX_ANSWER_SECONDS=120
VOICE_SILENCE_TIMEOUT_SECONDS=2.5
```

`STT_MODEL_SIZE` is validated against faster-whisper's known model sizes
(`tiny`/`base`/`small`/`medium`/`large`/`large-v3`) rather than left as an
arbitrary string, so a typo fails at `Config.load()` instead of a
confusing error from inside the STT call. `TTS_VOICE` is not validated
against Piper's voice catalog — that list is fetched from Piper's model
repository and changes over time, the same reasoning `.env.example`
already gives for not format-validating `GITHUB_TOKEN`.

Per the existing rule, every test file constructing `Config(...)`
directly needed updating in the same patch as the field additions —
`test_analyzer_integration.py`, `test_analyzer_reduce.py`,
`test_indexer_chunking.py`, `test_indexer_integration.py`,
`test_ingest_integration.py`, `test_orchestrator.py`,
`test_questiongen_integration.py`, `test_questiongen_planner.py`,
`test_web_app.py`, `test_web_registry.py`.

## 16.8 Known limitations

- Energy-threshold silence detection (§16.3) can cut a recording short in
  a noisy room, or run to the full `voice_max_answer_seconds` cap in a
  very quiet one if the threshold is miscalibrated for a given
  microphone. No per-device calibration step exists yet.
- English-only. `faster-whisper` and Piper both support other languages,
  but question generation and evaluation prompts are English-only today,
  so multilingual voice input would transcribe correctly and then likely
  evaluate poorly — out of scope until that's addressed independently.
- No mid-recording visual feedback beyond "recording" — no live waveform
  or amplitude meter. `RichSessionUI` prints a static indicator while
  `record()` blocks.
- Web UI ("viva room") does not support voice yet (§16.1).

## 16.9 Real-world bugs found during testing

(Populated as issues surface during Windows testing, per the project's
established workflow — none logged yet as of this patch series.)
