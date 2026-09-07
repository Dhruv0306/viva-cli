"""Tests for `viva.voice_io`.

No real audio hardware or model downloads are ever touched here --
per the module's docstring, `LocalVoiceIO` calls the lazy-import helper
functions (`_load_whisper_model`, `_load_piper_voice`, `_record_pcm`,
`_play_pcm`), and every test monkeypatches those directly (mirroring
`test_orchestrator.py`'s `monkeypatch.setattr(orchestrator_module, ...)`
pattern) rather than installing faster-whisper/Piper/sounddevice.

The `voice` extra genuinely isn't installed in this environment, which
is used deliberately in a couple of tests below to exercise the real
ImportError -> VoiceDependencyError translation without any mocking.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest

import viva.voice_io as voice_io_module
from viva.voice_io import LocalVoiceIO, VoiceDependencyError, VoiceIOError


# --- VoiceDependencyError when the `voice` extra isn't installed --------


def test_load_whisper_model_without_extra_raises_dependency_error():
    with pytest.raises(VoiceDependencyError, match="faster-whisper"):
        voice_io_module._load_whisper_model("base", "./cache")


def test_load_piper_voice_without_extra_raises_dependency_error():
    with pytest.raises(VoiceDependencyError, match="piper-tts"):
        voice_io_module._load_piper_voice("en_US-lessac-medium", "./cache")


def test_record_pcm_without_extra_raises_dependency_error():
    with pytest.raises(VoiceDependencyError, match="sounddevice"):
        voice_io_module._record_pcm(max_seconds=5, silence_timeout=2)


def test_play_pcm_without_extra_raises_dependency_error():
    with pytest.raises(VoiceDependencyError, match="sounddevice"):
        voice_io_module._play_pcm(b"\x00\x00", sample_rate=16000)


# --- LocalVoiceIO.record() delegates to _record_pcm ----------------------


def test_record_returns_pcm_bytes_from_recording(monkeypatch):
    fake_recording = voice_io_module._Recording(pcm=b"\x01\x02\x03\x04", sample_rate=16000)
    monkeypatch.setattr(voice_io_module, "_record_pcm", lambda *a, **kw: fake_recording)

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    result = vio.record(max_seconds=10, silence_timeout=2)

    assert result == b"\x01\x02\x03\x04"


def test_record_returns_none_on_silence(monkeypatch):
    monkeypatch.setattr(voice_io_module, "_record_pcm", lambda *a, **kw: None)

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    assert vio.record(max_seconds=10, silence_timeout=2) is None


# --- LocalVoiceIO.transcribe() -------------------------------------------


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    def __init__(self) -> None:
        self.calls: list = []

    def transcribe(self, samples, language):
        self.calls.append((samples, language))
        return [_FakeSegment(" hello "), _FakeSegment("world ")], object()


def test_transcribe_joins_segments_and_strips_whitespace(monkeypatch):
    fake_model = _FakeWhisperModel()
    monkeypatch.setattr(voice_io_module, "_load_whisper_model", lambda *a, **kw: fake_model)

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    # int16 PCM for a couple of samples -- transcribe() must convert this
    # to a normalized float32 waveform before handing it to the model.
    pcm = np.array([0, 16384, -16384], dtype="int16").tobytes()

    text = vio.transcribe(pcm)

    assert text == "hello world"
    (samples_seen, language_seen), = fake_model.calls
    assert language_seen == "en"
    assert samples_seen.dtype == np.float32
    assert samples_seen[1] == pytest.approx(0.5, abs=0.01)


def test_transcribe_returns_none_for_empty_result(monkeypatch):
    fake_model = _FakeWhisperModel()
    fake_model.transcribe = lambda samples, language: ([], object())
    monkeypatch.setattr(voice_io_module, "_load_whisper_model", lambda *a, **kw: fake_model)

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    assert vio.transcribe(b"\x00\x00") is None


def test_whisper_model_is_loaded_once_and_cached(monkeypatch):
    load_calls = []

    def _fake_load(model_size, cache_dir):
        load_calls.append((model_size, cache_dir))
        return _FakeWhisperModel()

    monkeypatch.setattr(voice_io_module, "_load_whisper_model", _fake_load)

    vio = LocalVoiceIO(stt_model_size="small", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    vio.transcribe(b"\x00\x00")
    vio.transcribe(b"\x00\x00")

    assert load_calls == [("small", "./cache")]


# --- LocalVoiceIO.speak() -------------------------------------------------


class _FakeAudioChunk:
    def __init__(self, audio_int16_bytes: bytes) -> None:
        self.audio_int16_bytes = audio_int16_bytes


class _FakeVoiceConfig:
    sample_rate = 22050


class _FakePiperVoice:
    def __init__(self) -> None:
        self.config = _FakeVoiceConfig()
        self.synthesize_calls: list = []

    def synthesize(self, text):
        self.synthesize_calls.append(text)
        return [_FakeAudioChunk(b"\x01\x00"), _FakeAudioChunk(b"\x02\x00")]


def test_speak_synthesizes_and_plays_concatenated_audio(monkeypatch):
    fake_voice = _FakePiperVoice()
    play_calls = []
    monkeypatch.setattr(voice_io_module, "_load_piper_voice", lambda *a, **kw: fake_voice)
    monkeypatch.setattr(
        voice_io_module, "_play_pcm", lambda pcm, sample_rate: play_calls.append((pcm, sample_rate))
    )

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    vio.speak("What does this function do?")

    assert fake_voice.synthesize_calls == ["What does this function do?"]
    assert play_calls == [(b"\x01\x00\x02\x00", 22050)]


def test_speak_is_a_noop_for_blank_text(monkeypatch):
    load_calls = []
    monkeypatch.setattr(
        voice_io_module, "_load_piper_voice", lambda *a, **kw: load_calls.append(1) or _FakePiperVoice()
    )

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_US-lessac-medium", cache_dir="./cache")
    vio.speak("   ")

    # The Piper voice should never even be loaded for empty input --
    # there's nothing to synthesize, and loading it costs real time.
    assert load_calls == []


def test_piper_voice_is_loaded_once_and_cached(monkeypatch):
    load_calls = []

    def _fake_load(voice_id, cache_dir):
        load_calls.append((voice_id, cache_dir))
        return _FakePiperVoice()

    monkeypatch.setattr(voice_io_module, "_load_piper_voice", _fake_load)
    monkeypatch.setattr(voice_io_module, "_play_pcm", lambda pcm, sample_rate: None)

    vio = LocalVoiceIO(stt_model_size="base", tts_voice="en_GB-alan-medium", cache_dir="./cache")
    vio.speak("First question")
    vio.speak("Second question")

    assert load_calls == [("en_GB-alan-medium", "./cache")]


# --- _record_pcm's energy-based silence cutoff, against a fake sounddevice


class _FakeInputStream:
    """Yields pre-scripted int16 blocks, one per `.read()` call, then
    silence forever -- enough to drive `_record_pcm`'s loop without a
    real microphone."""

    def __init__(self, blocks: list[np.ndarray], **kwargs) -> None:
        self._blocks = list(blocks)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, frames):
        if self._blocks:
            return self._blocks.pop(0), False
        return np.zeros((frames, 1), dtype="int16"), False


def _loud_block(frames: int) -> np.ndarray:
    return np.full((frames, 1), 20000, dtype="int16")


def _quiet_block(frames: int) -> np.ndarray:
    return np.zeros((frames, 1), dtype="int16")


def test_record_pcm_stops_after_silence_following_speech(monkeypatch):
    fake_sd = type(sys)("sounddevice")
    frames_per_block = 1600  # 0.1s at 16kHz, matches _record_pcm's _BLOCK_SECONDS
    scripted_blocks = [_loud_block(frames_per_block)] * 3 + [_quiet_block(frames_per_block)] * 50
    fake_sd.InputStream = lambda **kw: _FakeInputStream(scripted_blocks)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    recording = voice_io_module._record_pcm(max_seconds=30, silence_timeout=1.0)

    assert recording is not None
    # 3 loud blocks + 10 silent blocks (1.0s / 0.1s) before the cutoff --
    # stopped well short of the 30s max_seconds cap.
    assert len(recording.pcm) == (3 + 10) * frames_per_block * 2  # int16 = 2 bytes/sample


def test_record_pcm_returns_none_when_nothing_but_silence(monkeypatch):
    fake_sd = type(sys)("sounddevice")
    frames_per_block = 1600
    scripted_blocks = [_quiet_block(frames_per_block)] * 20
    fake_sd.InputStream = lambda **kw: _FakeInputStream(scripted_blocks)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    recording = voice_io_module._record_pcm(max_seconds=2.0, silence_timeout=1.0)

    assert recording is None


def test_record_pcm_wraps_hardware_failures(monkeypatch):
    fake_sd = type(sys)("sounddevice")

    class _BrokenInputStream:
        def __enter__(self):
            raise OSError("device unplugged")

        def __exit__(self, *exc_info):
            return False

    fake_sd.InputStream = lambda **kw: _BrokenInputStream()
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    with pytest.raises(VoiceIOError, match="device unplugged"):
        voice_io_module._record_pcm(max_seconds=5, silence_timeout=1)


def test_record_pcm_respects_max_seconds_hard_cap(monkeypatch):
    fake_sd = type(sys)("sounddevice")
    frames_per_block = 1600
    # Continuous speech -- would never trigger the silence cutoff, so
    # only the hard max_seconds cap should end the recording.
    fake_sd.InputStream = lambda **kw: _FakeInputStream(
        [_loud_block(frames_per_block) for _ in range(1000)]
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    recording = voice_io_module._record_pcm(max_seconds=0.5, silence_timeout=10.0)

    assert recording is not None
    assert len(recording.pcm) == 5 * frames_per_block * 2  # 0.5s / 0.1s = 5 blocks
