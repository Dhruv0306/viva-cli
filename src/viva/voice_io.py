"""Local speech I/O for spoken answers and spoken questions (FR-Voice,
docs/system-design/16-phase-11-voice-io-design.md).

Mirrors the `LLMClient`/`EmbeddingClient` thin-interface pattern (NFR5):
`VoiceIO` is the seam so nothing outside this module ever imports
`sounddevice`, `faster_whisper`, or `piper` directly. `LocalVoiceIO` is
the one real implementation, wrapping all three.

All three third-party imports are lazy, done inside the module-level
`_load_whisper_model()` / `_load_piper_voice()` / `_record_pcm()` /
`_play_pcm()` helpers rather than at module import time. Two reasons:

- The `voice` extra (`pip install -e ".[voice]"`) is optional -- most
  installs never enable voice mode, and importing `viva.voice_io` (e.g.
  for type hints elsewhere) must not require it.
- The test suite must run without real audio hardware or multi-hundred-
  MB model downloads (CONTRIBUTING.md: "the test suite must run
  without" real dependencies it can't assume). Tests monkeypatch these
  four helper functions directly, the same way `test_orchestrator.py`
  monkeypatches `orchestrator_module.ingest_repo` and friends -- no real
  import of the underlying libraries ever happens in CI.

See design doc §16.4 for why `record()` and `transcribe()` are separate
calls rather than one `listen()` -- it's the seam that lets the caller
exclude only the transcription's compute time from the answer clock
while recording time itself still counts as answering time.
"""
from __future__ import annotations

import abc
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class VoiceDependencyError(RuntimeError):
    """The `voice` extra isn't installed, or a model/voice isn't cached
    yet. Callers (session_ui.py) catch this and fall back to text mode
    rather than crashing the session -- see design doc §16.6."""


class VoiceIOError(RuntimeError):
    """A microphone or speaker call failed at runtime (device unplugged,
    permission revoked, etc). Distinct from VoiceDependencyError so
    callers can tell "never going to work this session" (missing deps)
    apart from "this one call failed, try again next question"."""


@dataclass(frozen=True)
class _Recording:
    pcm: bytes
    sample_rate: int


class VoiceIO(abc.ABC):
    @abc.abstractmethod
    def speak(self, text: str) -> None:
        """Synthesize and play `text` aloud. Blocks until playback
        finishes. Callers wrap this in `timer.excluding()` -- see design
        doc §16.4."""
        raise NotImplementedError

    @abc.abstractmethod
    def record(self, max_seconds: float, silence_timeout: float) -> bytes | None:
        """Block, capturing microphone audio, until either `max_seconds`
        elapses or `silence_timeout` seconds of below-threshold amplitude
        follow detected speech (design doc §16.3's energy-based cutoff).

        Returns raw audio bytes, or None if nothing was captured before
        the cutoff. Callers must NOT wrap this in `timer.excluding()` --
        recording time is answering time (§16.4).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def transcribe(self, audio: bytes) -> str | None:
        """Transcribe previously recorded audio. Returns None if
        transcription produced no text. Callers wrap this in
        `timer.excluding()` -- it's compute, not answering time (§16.4).
        """
        raise NotImplementedError


def _load_whisper_model(model_size: str, cache_dir: str):
    """Lazy-imports faster-whisper and loads (downloading into
    `cache_dir` on first use) the given model size. Raises
    VoiceDependencyError if the `voice` extra isn't installed.

    Forces `device="cpu"` explicitly rather than leaving CTranslate2 to
    auto-select the best available hardware (design doc §16.9's
    real-world bug writeup): on newer NVIDIA GPUs, `compute_type="int8"`
    on an auto-selected CUDA device raises `RuntimeError: cuBLAS failed
    with status CUBLAS_STATUS_NOT_SUPPORTED` -- a known CTranslate2/
    int8-tensor-core incompatibility, not something this project can
    detect or work around per-GPU. CPU is the one target guaranteed to
    work on every machine regardless of GPU vendor, driver, or cuBLAS
    version, consistent with the rest of the stack already assuming no
    GPU (Ollama, ChromaDB). GPU acceleration as an opt-in is a
    reasonable future enhancement, not the safe default.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceDependencyError(
            "faster-whisper is not installed. Run "
            'pip install -e ".[voice]" and `viva voice setup` first.'
        ) from exc
    return WhisperModel(model_size, download_root=cache_dir, device="cpu", compute_type="int8")


def _load_piper_voice(voice_id: str, cache_dir: str):
    """Lazy-imports Piper and loads the given voice, downloading it into
    `cache_dir` first if it isn't already cached there. Raises
    VoiceDependencyError if the `voice` extra isn't installed or the
    download fails.

    Downloads via the documented `python -m piper.download_voices` CLI
    utility (design doc §16.9's real-world bug writeup) rather than
    importing anything from inside `piper.download_voices` -- current
    piper-tts (OHF-Voice's `piper1-gpl` rewrite, v1.3.0+) removed the
    old `piper.download` module's `ensure_voice_exists()`/`get_voices()`
    functions this code originally called against; that module doesn't
    exist any more; only the CLI entrypoint is documented as stable.
    Shelling out to it avoids depending on undocumented internals that
    have already changed once.
    """
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise VoiceDependencyError(
            "piper-tts is not installed. Run "
            'pip install -e ".[voice]" and `viva voice setup` first.'
        ) from exc

    model_path = Path(cache_dir) / f"{voice_id}.onnx"
    if not model_path.exists():
        _download_piper_voice(voice_id, cache_dir)
    return PiperVoice.load(str(model_path))


def _download_piper_voice(voice_id: str, cache_dir: str) -> None:
    """Downloads `voice_id` into `cache_dir` via `python -m
    piper.download_voices`. Stdout/stderr are left connected to the
    parent process (not captured) so the download's own progress output
    reaches the terminal during `viva voice setup`, the same way `ollama
    pull`'s progress is visible."""
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "piper.download_voices", voice_id, "--data-dir", cache_dir]
    )
    if result.returncode != 0:
        raise VoiceDependencyError(
            f"Failed to download Piper voice {voice_id!r} (exit code "
            f"{result.returncode}). Check your network connection and the "
            "voice ID, then retry `viva voice setup`."
        )


def _record_pcm(max_seconds: float, silence_timeout: float, sample_rate: int = 16000) -> _Recording | None:
    """Captures microphone audio via sounddevice until `max_seconds`
    elapses or `silence_timeout` seconds of below-threshold amplitude
    follow detected speech. Returns None if nothing crossed the
    threshold before the cutoff."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise VoiceDependencyError(
            "sounddevice/numpy are not installed. Run "
            'pip install -e ".[voice]" first.'
        ) from exc

    _SILENCE_RMS_THRESHOLD = 500  # int16 PCM units; empirically quiet-room noise floor
    _BLOCK_SECONDS = 0.1

    blocks: list = []
    speech_detected = False
    silence_block_count = 0
    block_frames = int(sample_rate * _BLOCK_SECONDS)
    # Block counts, not accumulated float seconds -- summing _BLOCK_SECONDS
    # (0.1) repeatedly drifts under float64 rounding (10 additions of 0.1
    # land just under 1.0, not at it), which pushed the cutoff a block
    # late. Counting blocks and comparing against a precomputed integer
    # threshold sidesteps that entirely.
    max_blocks = round(max_seconds / _BLOCK_SECONDS)
    silence_block_threshold = round(silence_timeout / _BLOCK_SECONDS)

    try:
        with sd.InputStream(
            samplerate=sample_rate, channels=1, dtype="int16", blocksize=block_frames
        ) as stream:
            for _ in range(max_blocks):
                block, _overflowed = stream.read(block_frames)
                blocks.append(block.copy())
                rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
                if rms >= _SILENCE_RMS_THRESHOLD:
                    speech_detected = True
                    silence_block_count = 0
                elif speech_detected:
                    silence_block_count += 1
                    if silence_block_count >= silence_block_threshold:
                        break
    except Exception as exc:  # pragma: no cover -- real hardware failure path
        raise VoiceIOError(f"Recording failed: {exc}") from exc

    if not speech_detected or not blocks:
        return None

    pcm = np.concatenate(blocks, axis=0).tobytes()
    return _Recording(pcm=pcm, sample_rate=sample_rate)


def _play_pcm(pcm: bytes, sample_rate: int) -> None:
    """Plays raw PCM audio via sounddevice, blocking until finished."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise VoiceDependencyError(
            "sounddevice/numpy are not installed. Run "
            'pip install -e ".[voice]" first.'
        ) from exc

    try:
        audio = np.frombuffer(pcm, dtype="int16")
        sd.play(audio, samplerate=sample_rate, blocking=True)
    except Exception as exc:  # pragma: no cover -- real hardware failure path
        raise VoiceIOError(f"Playback failed: {exc}") from exc


def setup_models(stt_model_size: str, tts_voice: str, cache_dir: str) -> None:
    """Pulls (or confirms already-cached) the STT model and TTS voice
    into `cache_dir`. The public entrypoint behind `viva voice setup`
    (design doc §16.5) -- callers outside this module use this instead
    of the private `_load_whisper_model`/`_load_piper_voice` loaders
    directly, so `LocalVoiceIO` stays the only thing that touches those.

    Raises VoiceDependencyError if the `voice` extra isn't installed.
    """
    _load_whisper_model(stt_model_size, cache_dir)
    _load_piper_voice(tts_voice, cache_dir)


class LocalVoiceIO(VoiceIO):
    """Real `VoiceIO`: faster-whisper for transcription, Piper for
    synthesis, `sounddevice` for capture/playback. Models are loaded
    once and cached on the instance -- reloading per question would add
    seconds of latency to every single answer."""

    def __init__(self, stt_model_size: str, tts_voice: str, cache_dir: str) -> None:
        self._stt_model_size = stt_model_size
        self._tts_voice = tts_voice
        self._cache_dir = cache_dir
        self._whisper_model = None
        self._piper_voice = None

    def _whisper(self):
        if self._whisper_model is None:
            self._whisper_model = _load_whisper_model(self._stt_model_size, self._cache_dir)
        return self._whisper_model

    def _piper(self):
        if self._piper_voice is None:
            self._piper_voice = _load_piper_voice(self._tts_voice, self._cache_dir)
        return self._piper_voice

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        voice = self._piper()
        sample_rate = voice.config.sample_rate
        chunks = [chunk.audio_int16_bytes for chunk in voice.synthesize(text)]
        _play_pcm(b"".join(chunks), sample_rate)

    def record(self, max_seconds: float, silence_timeout: float) -> bytes | None:
        recording = _record_pcm(max_seconds, silence_timeout)
        if recording is None:
            return None
        return recording.pcm

    def transcribe(self, audio: bytes) -> str | None:
        import numpy as np

        model = self._whisper()
        # faster-whisper expects a float32 waveform in [-1, 1], not raw
        # int16 PCM bytes -- record()/_record_pcm() capture int16 for
        # compact in-memory storage, converted here at the one call site
        # that needs the float representation.
        samples = np.frombuffer(audio, dtype="int16").astype("float32") / 32768.0
        # vad_filter=True (design doc §16.9): faster-whisper's built-in
        # voice-activity filtering trims leading/trailing silence and
        # background noise before decoding, which reduces the repeated-
        # phrase hallucination pattern Whisper is known for on quiet or
        # noisy segments -- found from a real transcript that repeated
        # phrases like "top language cut" several times.
        segments, _info = model.transcribe(samples, language="en", vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text or None

    def ensure_ready(self) -> None:
        """Loads and caches both the STT model and TTS voice right now,
        rather than lazily on the first speak()/transcribe() call.

        Used at session construction (cli.py's `_build_session_ui`) so a
        missing `voice` extra or an un-pulled model surfaces once, before
        any ingest work starts, and the whole session can fall back to
        text mode up front rather than failing awkwardly on the first
        question (design doc §16.6).
        """
        self._whisper()
        self._piper()
