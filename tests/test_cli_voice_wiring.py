"""Tests for `viva.cli._build_session_ui` (docs/system-design/
16-phase-11-voice-io-design.md \u00a716.6): wires a `LocalVoiceIO` into
`RichSessionUI` when VOICE_ENABLED=true, warming both models up front so
a missing `voice` extra or un-pulled model falls the whole session back
to text mode before any ingest work starts, rather than failing
mid-question.

`LocalVoiceIO` is mocked throughout -- no real faster-whisper/Piper/
sounddevice import or model load happens here, matching test_voice_io.py
and test_cli_voice.py's approach.
"""
from __future__ import annotations

import io

from rich.console import Console

import viva.cli as cli_module
from viva.config import Config
from viva.voice_io import VoiceDependencyError


def _config(tmp_path, **overrides) -> Config:
    values = dict(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path="./data/chroma",
        top_k_retrieval=5, session_db_path=str(tmp_path / "viva.db"),
        avg_time_per_category_seconds=1, question_similarity_threshold=0.90,
        eval_flush_timeout_seconds=1,
        report_max_items_per_section=10,
        voice_enabled=False, stt_model_size="base", tts_voice="en_US-lessac-medium",
        voice_cache_dir="./data/voice_models", voice_max_answer_seconds=120,
        voice_silence_timeout_seconds=2.5,
    )
    values.update(overrides)
    return Config(**values)


class _FakeLocalVoiceIO:
    def __init__(self, stt_model_size, tts_voice, cache_dir, ensure_ready_exc=None):
        self.stt_model_size = stt_model_size
        self.tts_voice = tts_voice
        self.cache_dir = cache_dir
        self._ensure_ready_exc = ensure_ready_exc
        self.ensure_ready_called = False

    def ensure_ready(self):
        self.ensure_ready_called = True
        if self._ensure_ready_exc is not None:
            raise self._ensure_ready_exc


def test_voice_disabled_never_constructs_local_voice_io(mocker, tmp_path):
    voice_ctor = mocker.patch("viva.cli.LocalVoiceIO")
    config = _config(tmp_path, voice_enabled=False)

    ui = cli_module._build_session_ui(config)

    voice_ctor.assert_not_called()
    assert ui._voice is None


def test_voice_enabled_wires_a_ready_local_voice_io(mocker, tmp_path):
    fake_voice = _FakeLocalVoiceIO("small", "en_GB-alan-medium", "./data/voice_models")
    voice_ctor = mocker.patch("viva.cli.LocalVoiceIO", return_value=fake_voice)
    config = _config(
        tmp_path, voice_enabled=True, stt_model_size="small", tts_voice="en_GB-alan-medium",
        voice_max_answer_seconds=90, voice_silence_timeout_seconds=3.0,
    )

    ui = cli_module._build_session_ui(config)

    voice_ctor.assert_called_once_with(
        stt_model_size="small", tts_voice="en_GB-alan-medium", cache_dir="./data/voice_models",
    )
    assert fake_voice.ensure_ready_called is True
    assert ui._voice is fake_voice
    assert ui._voice_max_answer_seconds == 90
    assert ui._voice_silence_timeout_seconds == 3.0


def test_voice_dependency_error_falls_back_to_text_mode(mocker, tmp_path):
    fake_voice = _FakeLocalVoiceIO(
        "base", "en_US-lessac-medium", "./data/voice_models",
        ensure_ready_exc=VoiceDependencyError('Run pip install -e ".[voice]"'),
    )
    mocker.patch("viva.cli.LocalVoiceIO", return_value=fake_voice)
    config = _config(tmp_path, voice_enabled=True)
    buffer = io.StringIO()
    mocker.patch.object(cli_module, "console", Console(file=buffer, no_color=True, width=100))

    ui = cli_module._build_session_ui(config)

    # Falls back rather than raising -- the whole session stays usable,
    # just without voice, instead of crashing before ingest even starts.
    assert ui._voice is None
    assert "voice" in buffer.getvalue().lower()
