"""The Orchestrator (docs/plan.md Phase 6, docs/design.md §1/§2).

The first place multiple components get called by one caller, per
design.md's component rule ("no service calls another directly --
mediated by the Orchestrator"): Ingest -> Analyzer -> Indexer -> QuestionGen
in sequence for `start`/`resume`, driving the session state machine
(design.md §2) and persisting every transition through `SessionStore`
(docs/system-design/11-phase-6-session-loop-design.md).

Public entrypoints: `Orchestrator.start()` and `Orchestrator.resume()`.
`cli.py`'s `start`/`resume`/`list` commands are thin wrappers around this
class plus a `SessionUI` (see `session_ui.py`).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from viva.analyzer import analyze_repo
from viva.classification import ClassificationProvider, NullClassificationProvider
from viva.config import Config
from viva.embedding_client import EmbeddingClient, OllamaEmbeddingClient
from viva.indexer import index_repo
from viva.indexer.store import VectorStore
from viva.ingest import ingest_repo
from viva.ingest.clone import CloneError
from viva.llm_client import LLMClient, OllamaClient
from viva.profile import ProjectProfile
from viva.questiongen import build_coverage_plan, generate_question
from viva.questiongen.models import QuestionPlanItem
from viva.session_ui import SessionSummary, SessionUI
from viva.storage import QARecordRow, SessionStore
from viva.storage.session_store import ANSWERED, ASKED, SKIPPED_NO_GROUNDING, SKIPPED_TIME_COLLAPSE
from viva.timer import AnswerTimer

# Terminal states that make a session's `IN_PROGRESS` loop stop asking new
# questions (design.md §2).
_LOOP_EXIT_STATUSES = {"TIME_EXPIRED", "QUESTIONS_EXHAUSTED"}

# Statuses before IN_PROGRESS that Phase 6 doesn't attempt to resume into --
# see docs/system-design/11-phase-6-session-loop-design.md "Known
# limitations / deliberate scope-narrowing" for why.
_NOT_RESUMABLE_PRE_SESSION_STATUSES = {"INGESTING", "ANALYZING", "INDEXING", "PLANNING"}


class OrchestratorError(RuntimeError):
    """Base for errors the CLI translates into a specific exit code."""


class SessionNotFoundError(OrchestratorError):
    pass


class SessionAlreadyCompleteError(OrchestratorError):
    pass


class SessionNotResumableError(OrchestratorError):
    """Raised for a session that crashed before reaching `IN_PROGRESS` --
    see `_NOT_RESUMABLE_PRE_SESSION_STATUSES`."""


class Orchestrator:
    def __init__(
        self,
        config: Config,
        session_store: SessionStore,
        ui: SessionUI,
        llm_client: LLMClient | None = None,
        embedding_client: EmbeddingClient | None = None,
        vector_store: VectorStore | None = None,
        classification_provider: ClassificationProvider | None = None,
    ) -> None:
        self.config = config
        self.store = session_store
        self.ui = ui
        self.llm_client = llm_client or OllamaClient(
            model=config.llm_model, temperature=config.temperature, host=config.ollama_host
        )
        self.embedding_client = embedding_client or OllamaEmbeddingClient(
            model=config.embedding_model, host=config.ollama_host
        )
        self.vector_store = vector_store or VectorStore(config.vector_db_path)
        self.classification_provider = classification_provider or NullClassificationProvider()

    # -- viva start ----------------------------------------------------------

    def start(
        self,
        repo_url: str,
        branch: str | None = None,
        duration_minutes: int | None = None,
        session_name: str | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex[:12]
        duration_seconds = float((duration_minutes or self.config.viva_duration_minutes) * 60)

        # Row created (status=INGESTING) and session_id handed to the UI
        # before cloning starts, so it's captured even if this crashes --
        # CLI contract §6.1's "prints session_id immediately."
        self.store.create_session(
            session_id, repo_url=repo_url, branch=branch,
            session_name=session_name, duration_seconds=duration_seconds,
        )
        self.ui.session_started(session_id)

        try:
            profile, collection_name = self._run_setup_pipeline(session_id, repo_url, branch)
            plan = self._run_planning(session_id, profile)
        except Exception as exc:
            self.store.set_failed(session_id, str(exc))
            self.ui.error(f"Session setup failed: {exc}")
            raise

        self._run_live_session(session_id, profile, collection_name, duration_seconds,
                                initial_elapsed_seconds=0.0)
        return session_id

    def _run_setup_pipeline(
        self, session_id: str, repo_url: str, branch: str | None
    ) -> tuple[ProjectProfile, str]:
        self.ui.stage_started("Cloning and sampling repo")
        try:
            ingest_result = ingest_repo(repo_url, self.config, branch=branch)
        except CloneError as exc:
            raise OrchestratorError(f"Clone failed: {exc}") from exc
        self.ui.stage_completed(
            "Ingest",
            f"{ingest_result.files_analyzed}/{ingest_result.files_total} files sampled",
        )

        self.store.update_status(session_id, "ANALYZING")
        self.ui.stage_started("Analyzing codebase")
        analysis_result = analyze_repo(ingest_result, self.config, self.llm_client)
        self.ui.stage_completed("Analysis", f"{len(analysis_result.modules)} module(s) summarized")

        profile = ProjectProfile.build(ingest_result, analysis_result)

        self.store.update_status(session_id, "INDEXING")
        self.ui.stage_started("Indexing for retrieval")
        index_result = index_repo(profile, self.config, self.embedding_client, self.vector_store)
        self.ui.stage_completed("Indexing", f"{index_result.stats.chunks_built} chunk(s) indexed")

        profile_path = self._profile_path(session_id)
        profile.save(profile_path)
        self.store.set_pipeline_artifacts(
            session_id,
            repo_slug=profile.repo_slug,
            commit_sha=profile.commit_sha,
            collection_name=index_result.collection_name,
            profile_path=str(profile_path),
        )
        return profile, index_result.collection_name

    def _run_planning(self, session_id: str, profile: ProjectProfile) -> list[QuestionPlanItem]:
        self.store.update_status(session_id, "PLANNING")
        self.ui.stage_started("Planning question coverage")
        plan = build_coverage_plan(profile, self.config)
        self.store.save_plan(session_id, plan)
        self.ui.stage_completed("Planning", f"{len(plan)} question(s) planned")
        return plan

    def _profile_path(self, session_id: str) -> Path:
        return Path(self.config.session_db_path).parent / f"{session_id}-profile.json"

    # -- viva resume -----------------------------------------------------------

    def resume(self, session_id: str) -> None:
        record = self.store.get_session(session_id)
        if record is None:
            raise SessionNotFoundError(f"No session found with id {session_id!r}.")
        if record.status == "COMPLETE":
            raise SessionAlreadyCompleteError(
                f"Session {session_id} is already complete. Use `viva report {session_id}` instead."
            )
        if record.status == "FAILED":
            raise SessionNotResumableError(
                f"Session {session_id} failed before completing setup: {record.error_message}. "
                "Start a new session instead."
            )
        if record.status in _NOT_RESUMABLE_PRE_SESSION_STATUSES:
            raise SessionNotResumableError(
                f"Session {session_id} was interrupted during {record.status} pipeline setup, "
                "before the live Q&A session began, and can't be resumed in-place -- "
                "start a new session instead."
            )

        self.ui.session_started(session_id)
        self.ui.stage_started("Reloading persisted session state")
        profile = ProjectProfile.load(record.profile_path)
        self.ui.stage_completed("Reload", f"resuming at {record.status}")

        elapsed_seconds = self._elapsed_answer_seconds(session_id)
        self._run_live_session(
            session_id, profile, record.collection_name,
            duration_seconds=record.duration_seconds,
            initial_elapsed_seconds=elapsed_seconds,
        )

    def _elapsed_answer_seconds(self, session_id: str) -> float:
        """Best-effort reconstruction of time already spent answering, from
        persisted `asked_at`/`answered_at` timestamps -- see
        docs/system-design/11-phase-6-session-loop-design.md for why this is
        approximate (it can't recover time spent mid-answer when the
        process was interrupted, only completed question/answer pairs)."""
        import datetime as _dt

        total = 0.0
        for record in self.store.get_qa_records(session_id):
            if record.status == ANSWERED and record.asked_at and record.answered_at:
                asked = _dt.datetime.fromisoformat(record.asked_at)
                answered = _dt.datetime.fromisoformat(record.answered_at)
                total += (answered - asked).total_seconds()
        return total

    # -- the live Q&A loop (IN_PROGRESS) ----------------------------------------

    def _run_live_session(
        self,
        session_id: str,
        profile: ProjectProfile,
        collection_name: str,
        duration_seconds: float,
        initial_elapsed_seconds: float,
    ) -> None:
        self.store.update_status(session_id, "IN_PROGRESS")
        timer = AnswerTimer(duration_seconds)
        timer.start(initial_elapsed_seconds=initial_elapsed_seconds)

        question_number = self._already_asked_count(session_id)
        while True:
            if timer.expired():
                self.store.update_status(session_id, "TIME_EXPIRED")
                break

            pending = self.store.get_pending_plan_items(session_id)
            if not pending:
                self.store.update_status(session_id, "QUESTIONS_EXHAUSTED")
                break

            item = self._select_next_item(session_id, pending, timer)
            if item is None:
                # every remaining item got collapsed away by the time-budget
                # check below; nothing left worth asking
                self.store.update_status(session_id, "TIME_EXPIRED")
                break

            plan_item = QuestionPlanItem(
                id=item.question_id, category=item.category,
                target_module=item.target_module, target_file=item.target_file,
                is_followup_of=item.is_followup_of,
            )
            with timer.excluding():
                generated = generate_question(
                    plan_item, profile, self.config, self.vector_store,
                    collection_name, self.embedding_client, self.llm_client,
                )
            if generated is None:
                self.store.mark_item_status(session_id, item.question_id, SKIPPED_NO_GROUNDING)
                continue

            question_number += 1
            self.store.record_question_asked(
                session_id, item.question_id, generated.question_text, generated.grounding_chunk_ids
            )
            self.ui.ask_question(generated.question_text, item.category, question_number)
            answer_text = self.ui.read_answer(timer)
            self.store.record_answer(session_id, item.question_id, answer_text)

            self._maybe_queue_followup(session_id, item, answer_text)

        self.store.update_status(session_id, "FINALIZING_EVALS")
        # Phase 6: nothing to finalize -- no Evaluator exists yet (see
        # viva.classification). Every record's eval_status stays "deferred."
        self.store.update_status(session_id, "SUMMARIZING")
        # Phase 6: no report generation yet (Phase 8) -- pass straight through.
        self.store.update_status(session_id, "COMPLETE")

        self.ui.session_complete(self._build_summary(session_id))

    def _already_asked_count(self, session_id: str) -> int:
        return sum(
            1 for r in self.store.get_qa_records(session_id) if r.status in (ASKED, ANSWERED)
        )

    def _select_next_item(
        self, session_id: str, pending: list[QARecordRow], timer: AnswerTimer
    ) -> QARecordRow | None:
        """design.md §7's time-budget collapse: if the remaining time can't
        realistically cover one question per remaining uncovered category,
        collapse to at most one pending item per category (favoring full
        coverage over depth) and skip the rest with
        `skipped_time_collapse` rather than silently truncating the plan.

        Follow-up items (`is_followup_of` set) are prioritized first when
        present, since they're specifically probing a weak answer rather
        than covering new ground -- see `_maybe_queue_followup`. This
        branch is unreachable in Phase 6 (see `viva.classification`) but
        is written for Phase 7.
        """
        followups = [p for p in pending if p.is_followup_of is not None]
        if followups:
            return followups[0]

        categories_remaining = {p.category for p in pending}
        budget_needed = len(categories_remaining) * self.config.avg_time_per_category_seconds
        if timer.remaining() >= budget_needed:
            return pending[0]

        # Collapsed: keep only the first pending item per category, in
        # plan order; skip the rest of this round without asking them.
        first_per_category: dict[str, QARecordRow] = {}
        for item in pending:
            first_per_category.setdefault(item.category, item)
        keep_ids = {item.question_id for item in first_per_category.values()}
        for item in pending:
            if item.question_id not in keep_ids:
                self.store.mark_item_status(session_id, item.question_id, SKIPPED_TIME_COLLAPSE)

        remaining_after_collapse = [p for p in pending if p.question_id in keep_ids]
        return remaining_after_collapse[0] if remaining_after_collapse else None

    def _maybe_queue_followup(self, session_id: str, item: QARecordRow, answer_text: str) -> None:
        """FR14 seam. `classify()` always returns `None` in Phase 6 (see
        `viva.classification`), so this never actually queues a follow-up
        yet -- the mechanism exists so Phase 7 only has to swap the
        injected `ClassificationProvider`."""
        classification = self.classification_provider.classify(item.question_id, answer_text)
        if classification not in ("partial", "incorrect"):
            return
        followup_depth = self._followup_depth(session_id, item)
        if followup_depth >= self.config.max_followup_depth:
            return
        followup = QuestionPlanItem(
            id=f"{item.question_id}_f{followup_depth + 1}",
            category=item.category,
            target_module=item.target_module,
            target_file=item.target_file,
            is_followup_of=item.question_id,
        )
        self.store.add_followup_item(session_id, followup)

    def _followup_depth(self, session_id: str, item: QARecordRow) -> int:
        """How many follow-ups already exist under this item's root
        question -- unreachable in Phase 6 (see `_maybe_queue_followup`),
        kept simple since it's exercised directly by unit tests instead."""
        root_id = item.is_followup_of or item.question_id
        return sum(
            1 for r in self.store.get_qa_records(session_id) if r.is_followup_of == root_id
        )

    def _build_summary(self, session_id: str) -> SessionSummary:
        records = self.store.get_qa_records(session_id)
        session = self.store.get_session(session_id)
        skipped_statuses = {SKIPPED_NO_GROUNDING, SKIPPED_TIME_COLLAPSE}
        return SessionSummary(
            session_id=session_id,
            status=session.status if session else "UNKNOWN",
            questions_asked=sum(1 for r in records if r.status in (ASKED, ANSWERED)),
            questions_answered=sum(1 for r in records if r.status == ANSWERED),
            questions_skipped=sum(1 for r in records if r.status in skipped_statuses),
        )
