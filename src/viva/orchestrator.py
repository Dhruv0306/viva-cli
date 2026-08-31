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

import math
import uuid
from pathlib import Path

from viva.analyzer import analyze_repo
from viva.classification import ClassificationProvider
from viva.config import Config
from viva.embedding_client import EmbeddingClient, OllamaEmbeddingClient
from viva.evaluator import Evaluator
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
from viva.storage.session_store import ANSWERED, ASKED, SKIPPED_DUPLICATE_TARGET, SKIPPED_NO_GROUNDING, SKIPPED_TIME_COLLAPSE
from viva.timer import AnswerTimer

# Terminal states that make a session's `IN_PROGRESS` loop stop asking new
# questions (design.md §2).
_LOOP_EXIT_STATUSES = {"TIME_EXPIRED", "QUESTIONS_EXHAUSTED"}

# Statuses before IN_PROGRESS that Phase 6 doesn't attempt to resume into --
# see docs/system-design/11-phase-6-session-loop-design.md "Known
# limitations / deliberate scope-narrowing" for why.
_NOT_RESUMABLE_PRE_SESSION_STATUSES = {"INGESTING", "ANALYZING", "INDEXING", "PLANNING"}

# Bounded number of ranked candidates tried per round before accepting
# whichever came closest (see _run_live_session) -- keeps the worst-case
# extra LLM/embedding cost (all excluded from the user's clock) small
# rather than unbounded on a repo where duplication is pervasive.
_MAX_DEDUP_CANDIDATES = 3


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


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
        self.classification_provider = classification_provider or Evaluator(
            self.store, self.vector_store, self.llm_client
        )
        # FR15's third dedup layer (see _is_semantic_duplicate). Keyed by
        # question_id; session-scoped, since a fresh Orchestrator is
        # constructed per CLI invocation (start() or resume()).
        self._question_embeddings: dict[str, list[float]] = {}

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
        if index_result.stats.reused_existing_collection:
            detail = "reusing existing index for this commit -- no re-embedding needed"
        else:
            detail = f"{index_result.stats.chunks_built} chunk(s) indexed"
        self.ui.stage_completed("Indexing", detail)

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

        # NFR3: a crash between record_question_asked() and record_answer()
        # (process dies while a question is on screen) leaves that item
        # stuck at status='asked' forever -- pending-only lookups never
        # revisit it, silently dropping a question the person never
        # actually got to answer. Requeue it as pending before resuming the
        # loop so it's re-presented (its question_text/grounding_chunk_ids
        # are preserved, so this doesn't cost another LLM generation call).
        requeued = self.store.requeue_orphaned_asked_items(session_id)
        detail = f"resuming at {record.status}"
        if requeued:
            detail += f" ({requeued} previously-asked, unanswered question requeued)"
        self.ui.stage_completed("Reload", detail)

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
        self.classification_provider.bind_session(session_id, collection_name)
        # §12.6: re-enqueues anything a crashed prior process left
        # mid-evaluation. A no-op on a fresh start() (nothing classified/
        # feedback_pending yet) -- only resume() ever has something here,
        # but calling it unconditionally avoids a resume-only special case.
        self.classification_provider.requeue_unfinished()
        timer = AnswerTimer(duration_seconds)
        timer.start(initial_elapsed_seconds=initial_elapsed_seconds)
        with timer.excluding():
            self._seed_embedding_cache(session_id)

        question_number = self._already_asked_count(session_id)
        while True:
            if timer.expired():
                self.store.update_status(session_id, "TIME_EXPIRED")
                break

            pending = self.store.get_pending_plan_items(session_id)
            if not pending:
                self.store.update_status(session_id, "QUESTIONS_EXHAUSTED")
                break

            ranked = self._rank_pending_items(session_id, pending)
            selected_item: QARecordRow | None = None
            question_text = ""
            grounding_chunk_ids: list[str] = []
            fallback: tuple[QARecordRow, str, list[str], list[float]] | None = None

            for candidate in ranked[:_MAX_DEDUP_CANDIDATES]:
                if candidate.question_text:
                    # Requeued orphaned item (see resume()'s
                    # requeue_orphaned_asked_items call) -- already
                    # generated and grounded before the crash; re-present
                    # it as-is, no dedup check (an intentional re-ask,
                    # not a fresh candidate that might duplicate
                    # something).
                    selected_item = candidate
                    question_text = candidate.question_text
                    grounding_chunk_ids = candidate.grounding_chunk_ids
                    break

                plan_item = QuestionPlanItem(
                    id=candidate.question_id, category=candidate.category,
                    target_module=candidate.target_module, target_file=candidate.target_file,
                    is_followup_of=candidate.is_followup_of,
                )
                with timer.excluding():
                    generated = generate_question(
                        plan_item, profile, self.config, self.vector_store,
                        collection_name, self.embedding_client, self.llm_client,
                        avoid_questions=self._already_asked_question_texts(session_id),
                    )
                if generated is None:
                    self.store.mark_item_status(session_id, candidate.question_id, SKIPPED_NO_GROUNDING)
                    continue

                with timer.excluding():
                    candidate_vec = self._embed_text(generated.question_text)
                    is_duplicate = self._is_semantic_duplicate(candidate_vec)

                if not is_duplicate:
                    selected_item = candidate
                    question_text = generated.question_text
                    grounding_chunk_ids = generated.grounding_chunk_ids
                    self._question_embeddings[candidate.question_id] = candidate_vec
                    break

                # Remember as a fallback -- never permanently exclude a
                # duplicate; if nothing better turns up among the bounded
                # candidates tried, ask it anyway rather than end the
                # session early (the exact lesson from the two duplicate-
                # avoidance regressions earlier in this phase -- see
                # docs/system-design/11-phase-6-session-loop-design.md
                # §11.9).
                fallback = (candidate, generated.question_text, generated.grounding_chunk_ids, candidate_vec)

            if selected_item is None:
                if fallback is None:
                    # every tried candidate was ungrounded -- nothing to
                    # ask this round; loop back and re-fetch pending
                    # (now smaller, since those got marked skipped)
                    continue
                selected_item, question_text, grounding_chunk_ids, fallback_vec = fallback
                self._question_embeddings[selected_item.question_id] = fallback_vec

            question_number += 1
            self.store.record_question_asked(
                session_id, selected_item.question_id, question_text, grounding_chunk_ids
            )
            self.ui.ask_question(question_text, selected_item.category, question_number)
            answer_text = self.ui.read_answer(timer)
            self.store.record_answer(session_id, selected_item.question_id, answer_text)

            self._maybe_queue_followup(session_id, selected_item, answer_text)

        self.store.update_status(session_id, "FINALIZING_EVALS")
        # docs/system-design/12-phase-7-evaluator-design.md §12.6: drain
        # the Evaluator's background worker, bounded so session end can't
        # hang indefinitely on one stuck model call. No-op for
        # NullClassificationProvider/any provider that doesn't override it.
        self.classification_provider.flush(self.config.eval_flush_timeout_seconds)
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
        """Thin convenience wrapper around `_rank_pending_items` returning
        just the top pick -- kept for callers/tests that only need one
        item. `_run_live_session` uses the full ranked list directly,
        since it needs multiple candidates for the semantic-duplicate
        check (see `_is_semantic_duplicate`)."""
        ranked = self._rank_pending_items(session_id, pending)
        return ranked[0] if ranked else None

    def _rank_pending_items(
        self, session_id: str, pending: list[QARecordRow]
    ) -> list[QARecordRow]:
        """FR15 ("track asked topics/files to avoid duplicate questioning
        and to enforce category coverage across the session") + design.md
        §7's category-breadth preference, both as pure *ordering*, never
        a permanent exclusion.

        Two earlier versions of this method each made a one-time,
        pessimistic decision and permanently dropped whatever didn't fit
        it (first duplicate-target items outright, then non-first-per-
        category items when a time-budget estimate looked too tight
        compared to the *starting* time remaining -- which for any short
        session fired on the very first selection, before anything was
        even asked, locking in a fixed question count regardless of how
        fast the person actually answered or how much real time was left
        afterward). Both were found the same way: a real session against
        github.com/Dhruv0306/throttle4j stopped identically at a fixed
        question count no matter how much time was given -- see
        docs/system-design/11-phase-6-session-loop-design.md §11.9.

        The fix, both times: prefer, never exclude. Pending items are
        ranked -- follow-ups first, then items whose target hasn't been
        asked about yet, then items whose category hasn't been asked
        about yet -- and the loop's own natural exit conditions (timer
        actually expired, or truly no pending items left) are what end
        the session, not a pre-computed worst-case guess made once at
        the start. Returns the *full* ranked list (not just the top
        pick) so `_run_live_session` can try several candidates for the
        semantic-duplicate check without a second query.
        """
        followups = [p for p in pending if p.is_followup_of is not None]
        non_followups = [p for p in pending if p.is_followup_of is None]

        already_asked_targets = self._already_asked_targets(session_id)
        already_asked_categories = self._already_asked_categories(session_id)

        def _priority(item: QARecordRow) -> tuple[bool, bool]:
            target = item.target_file or item.target_module
            is_duplicate_target = bool(target) and target in already_asked_targets
            is_repeat_category = item.category in already_asked_categories
            return (is_duplicate_target, is_repeat_category)

        return followups + sorted(non_followups, key=_priority)

    def _embed_text(self, text: str) -> list[float]:
        return self.embedding_client.embed([text])[0]

    def _seed_embedding_cache(self, session_id: str) -> None:
        """Populates the in-memory duplicate-check cache from
        already-asked questions. Needed on `resume()` -- a fresh
        `Orchestrator` instance starts with an empty cache -- and a
        cheap no-op on `start()` (nothing asked yet)."""
        for record in self.store.get_qa_records(session_id):
            if (
                record.status in (ASKED, ANSWERED)
                and record.question_text
                and record.question_id not in self._question_embeddings
            ):
                self._question_embeddings[record.question_id] = self._embed_text(record.question_text)

    def _is_semantic_duplicate(self, candidate_vec: list[float]) -> bool:
        """FR15's third and most accurate duplicate-avoidance layer --
        see docs/system-design/11-phase-6-session-loop-design.md §11.9
        for why the first two (exact target-file matching, category-
        breadth ordering) weren't enough on their own: different plan
        items can carry different `target_file`/`target_module` values
        and still land on essentially the same question, because the
        underlying code region only really supports one obvious angle.

        Compares against every previously-asked question's embedding
        this session via cosine similarity. Threshold is
        `QUESTION_SIMILARITY_THRESHOLD` (default 0.90, FR28) rather than
        hardcoded, since it depends on the embedding model in use and
        hasn't been empirically calibrated against real output.

        Advisory only, same discipline as the other two layers: the
        caller (`_run_live_session`) tries a bounded number of
        alternative candidates and only falls back to a flagged
        duplicate if nothing better turns up among them, rather than
        ever blocking the session on this check.
        """
        threshold = self.config.question_similarity_threshold
        return any(
            _cosine_similarity(candidate_vec, cached_vec) >= threshold
            for cached_vec in self._question_embeddings.values()
        )

    def _already_asked_categories(self, session_id: str) -> set[str]:
        """Categories already covered this session -- only counts items
        with status `asked`/`answered`."""
        return {
            r.category
            for r in self.store.get_qa_records(session_id)
            if r.status in (ASKED, ANSWERED)
        }

    def _already_asked_targets(self, session_id: str) -> set[str]:
        """Files/modules already covered this session (FR15). Only counts
        items with status `asked`/`answered`."""
        return {
            r.target_file or r.target_module
            for r in self.store.get_qa_records(session_id)
            if r.status in (ASKED, ANSWERED) and (r.target_file or r.target_module)
        }

    def _already_asked_question_texts(self, session_id: str) -> list[str]:
        """FR15's primary defense (docs/system-design/
        11-phase-6-session-loop-design.md §11.12): the actual text of
        every question asked this session, passed to the LLM as
        [AVOID_REPEATING] context so it can actively avoid generating
        something that tests substantially the same understanding --
        stronger than catching it after the fact via the embedding
        check, which is a backstop for when the model doesn't fully
        comply, not a replacement for this."""
        return [
            r.question_text
            for r in self.store.get_qa_records(session_id)
            if r.status in (ASKED, ANSWERED) and r.question_text
        ]

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
        skipped_statuses = {SKIPPED_NO_GROUNDING, SKIPPED_TIME_COLLAPSE, SKIPPED_DUPLICATE_TARGET}
        return SessionSummary(
            session_id=session_id,
            status=session.status if session else "UNKNOWN",
            questions_asked=sum(1 for r in records if r.status in (ASKED, ANSWERED)),
            questions_answered=sum(1 for r in records if r.status == ANSWERED),
            questions_skipped=sum(1 for r in records if r.status in skipped_statuses),
        )
