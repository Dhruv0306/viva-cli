"""Direct pure-function coverage for `viva.planning`, split out of
`orchestrator.py` (docs/plan.md Phase 22, docs/system-design/27-phase-22-
container-resumption-and-planning-split-design.md §19.2.2).

These mirror the scenarios `test_orchestrator.py`'s `_select_next_item`/
`_replenish_plan` tests already cover end-to-end through a real
`SessionStore`, but exercise `rank_pending_items()`/
`compute_replenished_plan()` directly against plain dataclasses -- no
store, no I/O -- per this project's "pure-dataclass fixtures for unit
tests" convention. `test_orchestrator.py`'s own tests are kept, not
replaced: they cover the store-I/O integration layer (already-asked-set
computation, `save_plan()` actually persisting) that these pure tests
deliberately don't touch.
"""
from __future__ import annotations

import viva.planning as planning_module
from viva.config import Config
from viva.planning import compute_replenished_plan, rank_pending_items
from viva.questiongen.models import QuestionPlanItem
from viva.storage import QARecordRow


def _row(
    question_id: str,
    category: str,
    target_module: str | None = None,
    target_file: str | None = None,
    architecture_topic: str | None = None,
    is_followup_of: str | None = None,
) -> QARecordRow:
    return QARecordRow(
        session_id="sess1", question_id=question_id, category=category,
        target_module=target_module, target_file=target_file,
        architecture_topic=architecture_topic, is_followup_of=is_followup_of,
        question_text=None, grounding_chunk_ids=[], status="pending",
        answer_text=None, asked_at=None, answered_at=None,
        eval_status="not_applicable", eval_json=None,
    )


def _config(**overrides) -> Config:
    values = dict(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_questions_explicit=True,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path="./data/chroma",
        top_k_retrieval=5, max_retrieval_distance=None, session_db_path="./data/viva.db",
        avg_time_per_category_seconds=1, question_similarity_threshold=0.90,
        eval_flush_timeout_seconds=1,
        report_max_items_per_section=10,
        voice_enabled=False, stt_model_size="base", tts_voice="en_US-lessac-medium",
        voice_cache_dir="./data/voice_models", voice_max_answer_seconds=120,
        voice_silence_timeout_seconds=2.5,
        max_concurrent_sessions=5,
    )
    values.update(overrides)
    return Config(**values)


# -- rank_pending_items ------------------------------------------------------

def test_rank_prefers_non_duplicate_target():
    """FR15: a pending item whose target has already been asked about is
    deprioritized, not dropped -- see rank_pending_items()'s own
    docstring for the real-world regression this replaced."""
    pending = [
        _row("q2", "implementation_detail", target_file="FixedWindowLimiter.java"),
        _row("q3", "testing", target_file="Other.java"),
    ]
    ranked = rank_pending_items(
        pending,
        already_asked_targets={"FixedWindowLimiter.java"},
        already_asked_categories={"architecture"},
    )
    assert [r.question_id for r in ranked] == ["q3", "q2"]  # novel target wins, q2 still present


def test_rank_does_not_penalize_distinct_targets():
    pending = [
        _row("q1", "architecture", target_file="A.java"),
        _row("q2", "testing", target_file="B.java"),
    ]
    ranked = rank_pending_items(pending, already_asked_targets=set(), already_asked_categories=set())
    assert [r.question_id for r in ranked] == ["q1", "q2"]


def test_rank_prefers_novel_category_over_repeat_when_same_phase():
    """Category-breadth preference (design.md §7) is ordering only, never
    exclusion. Both items are non-architecture (phase 1), so this
    exercises the phase-1 tie-break specifically."""
    pending = [
        _row("q2", "implementation_detail", target_module="core", target_file="b.py"),
        _row("q3", "error_handling", target_module="payments"),
    ]
    ranked = rank_pending_items(
        pending,
        already_asked_targets=set(),
        already_asked_categories={"implementation_detail"},
    )
    assert [r.question_id for r in ranked] == ["q3", "q2"]  # novel category preferred, q2 kept


def test_rank_architecture_phase_wins_even_when_repeat_category():
    """Phase 13 (§18.4): architecture is the *primary* sort key -- a
    repeat-category architecture item still outranks a completely novel
    non-architecture category."""
    pending = [
        _row("q2", "architecture", architecture_topic="pipeline"),
        _row("q3", "implementation_detail", target_module="core"),
    ]
    ranked = rank_pending_items(
        pending,
        already_asked_targets=set(),
        already_asked_categories={"architecture"},
    )
    assert [r.question_id for r in ranked] == ["q2", "q3"]


def test_rank_eventually_returns_repeat_category_when_nothing_novel_left():
    """Once every distinct category is covered, remaining same-category
    items must still rank (not be excluded) -- prefer, never exclude."""
    pending = [_row("q2", "architecture", target_file="b.py")]
    ranked = rank_pending_items(
        pending,
        already_asked_targets=set(),
        already_asked_categories={"architecture"},
    )
    assert [r.question_id for r in ranked] == ["q2"]


def test_rank_puts_followups_first_regardless_of_already_asked_sets():
    pending = [
        _row("q2", "architecture", architecture_topic="overview"),
        _row("q1_f1", "architecture", architecture_topic="overview", is_followup_of="q1"),
    ]
    ranked = rank_pending_items(
        pending, already_asked_targets=set(), already_asked_categories=set()
    )
    assert [r.question_id for r in ranked] == ["q1_f1", "q2"]


# -- compute_replenished_plan -------------------------------------------------

def test_replenish_returns_new_plan_when_it_actually_grew(monkeypatch):
    grown_plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, architecture_topic="overview"),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module="core"),
        QuestionPlanItem(id="q3", category="architecture", target_module=None, architecture_topic="security"),
    ]
    monkeypatch.setattr(planning_module, "build_coverage_plan", lambda *a, **kw: grown_plan)

    result = compute_replenished_plan(profile=object(), config=_config(), current_top_level_count=2)

    assert result == grown_plan


def test_replenish_returns_none_when_nothing_new(monkeypatch):
    existing_plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, architecture_topic="overview"),
    ]
    monkeypatch.setattr(planning_module, "build_coverage_plan", lambda *a, **kw: existing_plan)

    result = compute_replenished_plan(profile=object(), config=_config(), current_top_level_count=1)

    assert result is None


def test_replenish_raises_the_max_questions_ceiling(monkeypatch):
    """Confirms the ceiling passed to build_coverage_plan() is actually
    current_top_level_count + the replenish increment, not something
    build_coverage_plan()'s own config.max_questions already had."""
    seen_configs = []

    def spying_build(profile, config):
        seen_configs.append(config)
        return []

    monkeypatch.setattr(planning_module, "build_coverage_plan", spying_build)

    compute_replenished_plan(profile=object(), config=_config(max_questions=8), current_top_level_count=10)

    assert seen_configs[0].max_questions == 10 + planning_module._REPLENISH_INCREMENT


# -- compute_replenished_plan: near-duplicate filtering (Phase 26) ------------
# docs/system-design/31-phase-26-followups-design.md §31.2

import dataclasses  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _profile(*modules_with_file_counts: tuple[str, int]) -> SimpleNamespace:
    files = [
        SimpleNamespace(module=module, is_test=False)
        for module, count in modules_with_file_counts
        for _ in range(count)
    ]
    return SimpleNamespace(sampled_files=files)


def _item(item_id: str, category: str, module: str | None = None, file: str | None = None,
          topic: str | None = None) -> QuestionPlanItem:
    return QuestionPlanItem(
        id=item_id, category=category, target_module=module, target_file=file,
        architecture_topic=topic,
    )


def _asked(row: QARecordRow) -> QARecordRow:
    return dataclasses.replace(row, status="answered")


def _scripted_planner(monkeypatch, full_plan: list[QuestionPlanItem]) -> None:
    """build_coverage_plan() stand-in that honors the ceiling the way the
    real one does: the first `max_questions` items of one fixed plan."""
    monkeypatch.setattr(
        planning_module, "build_coverage_plan",
        lambda profile, config: full_plan[: config.max_questions],
    )


def test_replenish_drops_a_repeated_architecture_topic(monkeypatch):
    full = [
        _item("q1", "architecture", topic="overview"),
        _item("q2", "architecture", topic="overview"),  # Pass 1.5's second slot
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "architecture", architecture_topic="overview"))]

    assert compute_replenished_plan(
        _profile(), _config(), current_top_level_count=1, persisted=persisted
    ) is None


def test_replenish_drops_a_file_level_item_in_a_single_file_module(monkeypatch):
    full = [
        _item("q1", "implementation_detail", module="core"),
        _item("q2", "implementation_detail", module="core", file="core/a.py"),
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "implementation_detail", target_module="core"))]

    assert compute_replenished_plan(
        _profile(("core", 1)), _config(), current_top_level_count=1, persisted=persisted
    ) is None


def test_replenish_keeps_a_file_level_item_in_a_multi_file_module(monkeypatch):
    full = [
        _item("q1", "implementation_detail", module="core"),
        _item("q2", "implementation_detail", module="core", file="core/a.py"),
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "implementation_detail", target_module="core"))]

    result = compute_replenished_plan(
        _profile(("core", 3)), _config(), current_top_level_count=1, persisted=persisted
    )

    assert [i.id for i in result] == ["q2"]


def test_replenish_keeps_the_same_target_under_a_different_category(monkeypatch):
    full = [
        _item("q1", "implementation_detail", module="core"),
        _item("q2", "design_rationale", module="core"),
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "implementation_detail", target_module="core"))]

    result = compute_replenished_plan(
        _profile(("core", 1)), _config(), current_top_level_count=1, persisted=persisted
    )

    assert [i.id for i in result] == ["q2"]


def test_replenish_does_not_treat_a_skipped_question_as_asked(monkeypatch):
    full = [
        _item("q1", "implementation_detail", module="core"),
        _item("q2", "implementation_detail", module="core", file="core/a.py"),
    ]
    _scripted_planner(monkeypatch, full)
    skipped = dataclasses.replace(
        _row("q1", "implementation_detail", target_module="core"), status="skipped_no_grounding"
    )

    result = compute_replenished_plan(
        _profile(("core", 1)), _config(), current_top_level_count=1, persisted=[skipped]
    )

    assert [i.id for i in result] == ["q2"]


def test_replenish_finds_a_fresh_item_behind_a_window_of_duplicates(monkeypatch):
    full = [_item("q1", "architecture", topic="overview")]
    full += [_item(f"q{n}", "architecture", topic="overview") for n in range(2, 8)]
    full += [_item("q8", "implementation_detail", module="other")]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "architecture", architecture_topic="overview"))]

    result = compute_replenished_plan(
        _profile(("other", 2)), _config(), current_top_level_count=1, persisted=persisted
    )

    # The first window (ceiling 1 + increment) is all duplicates; the
    # ceiling must be raised until q8 shows up, not give up there.
    assert [i.id for i in result] == ["q8"]


def test_replenish_collapses_duplicates_within_one_batch(monkeypatch):
    full = [
        _item("q1", "architecture", topic="overview"),
        _item("q2", "implementation_detail", module="core"),
        _item("q3", "implementation_detail", module="core", file="core/a.py"),
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "architecture", architecture_topic="overview"))]

    result = compute_replenished_plan(
        _profile(("core", 1)), _config(), current_top_level_count=1, persisted=persisted
    )

    assert [i.id for i in result] == ["q2"]


def test_replenish_never_returns_an_already_persisted_item(monkeypatch):
    full = [
        _item("q1", "architecture", topic="overview"),
        _item("q2", "architecture", topic="security"),
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_row("q1", "architecture", architecture_topic="overview"),
                 _row("q2", "architecture", architecture_topic="security")]

    assert compute_replenished_plan(
        _profile(), _config(), current_top_level_count=2, persisted=persisted
    ) is None


def test_replenish_stops_when_the_planner_runs_out_of_items(monkeypatch):
    calls = []

    def planner(profile, config):
        calls.append(config.max_questions)
        return [_item("q1", "architecture", topic="overview")]

    monkeypatch.setattr(planning_module, "build_coverage_plan", planner)
    persisted = [_asked(_row("q1", "architecture", architecture_topic="overview"))]

    assert compute_replenished_plan(
        _profile(), _config(), current_top_level_count=1, persisted=persisted
    ) is None
    assert len(calls) == 1  # fewer items than the ceiling allowed: no point raising it


def test_the_reason_for_no_new_coverage_is_logged_when_only_duplicates_remain(monkeypatch, caplog):
    full = [
        _item("q1", "architecture", topic="overview"),
        _item("q2", "architecture", topic="overview"),
    ]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "architecture", architecture_topic="overview"))]

    with caplog.at_level("INFO", logger="viva.planning"):
        assert compute_replenished_plan(
            _profile(), _config(), current_top_level_count=1, persisted=persisted
        ) is None

    assert "only near-duplicates of asked questions remain" in caplog.text
    assert "planner has no further items" not in caplog.text


def test_the_reason_for_no_new_coverage_is_logged_when_the_planner_ran_out(monkeypatch, caplog):
    full = [_item("q1", "architecture", topic="overview")]
    _scripted_planner(monkeypatch, full)
    persisted = [_asked(_row("q1", "architecture", architecture_topic="overview"))]

    with caplog.at_level("INFO", logger="viva.planning"):
        assert compute_replenished_plan(
            _profile(), _config(), current_top_level_count=1, persisted=persisted
        ) is None

    assert "planner has no further items" in caplog.text
    assert "near-duplicates" not in caplog.text
