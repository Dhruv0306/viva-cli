"""QuestionGen component (docs/plan.md Phase 5, docs/design.md §1
"QuestionGen (planner + generator)").

Coverage plan (FR12, `planner.py`) -> per-item retrieval with query
reformulation (FR13 + open question #6, `retrieval.py`) -> just-in-time
LLM question generation (FR13, `viva.llm_client`).

Public entrypoints: `build_coverage_plan()` (re-exported from
`planner.py`) and `generate_question()`. Per design.md's component rule
("no service calls another directly"), these are the seams the future
Orchestrator (Phase 6) will call -- everything else in this package is an
internal implementation detail.

Follow-up generation (FR14) and live duplicate-avoidance tracking (FR15)
are deliberately NOT implemented here -- both are live-session concepts
that need session state, which doesn't exist until Phase 6. This
component's contract to Phase 6 is a plan-building function plus a
per-item generation function; the session loop drives when/how often
each is called.
"""
from __future__ import annotations

from viva.config import Config
from viva.embedding_client import EmbeddingClient
from viva.indexer.store import VectorStore
from viva.llm_client import LLMClient
from viva.profile import ProjectProfile
from viva.questiongen.models import GeneratedQuestion, QuestionGenStats, QuestionPlanItem
from viva.questiongen.planner import build_coverage_plan
from viva.questiongen.retrieval import retrieve_grounding_chunks

__all__ = ["build_coverage_plan", "generate_question", "generate_all"]


def generate_question(
    plan_item: QuestionPlanItem,
    profile: ProjectProfile,
    config: Config,
    vector_store: VectorStore,
    collection_name: str,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
) -> GeneratedQuestion | None:
    """Retrieve grounding chunks for one plan item and generate its
    question text (FR13).

    Returns `None` if no grounding chunks could be retrieved -- FR13
    forbids generating a question ungrounded in retrieved code, so a
    plan item with nothing to ground against is skipped, not faked.
    """
    module_summary = _module_summary(profile, plan_item.target_module)
    chunks = retrieve_grounding_chunks(
        plan_item=plan_item,
        module_summary=module_summary,
        vector_store=vector_store,
        collection_name=collection_name,
        embedding_client=embedding_client,
        top_k=config.top_k_retrieval,
    )
    if not chunks:
        return None

    grounding_context = "\n\n---\n\n".join(
        f"[{c['metadata']['filepath']}:{c['metadata']['start_line']}-{c['metadata']['end_line']}]\n{c['text']}"
        for c in chunks
    )
    question_text = llm_client.generate_question(
        category=plan_item.category,
        target_module=plan_item.target_module,
        grounding_context=grounding_context,
    )
    return GeneratedQuestion(
        plan_item=plan_item,
        question_text=question_text,
        grounding_chunk_ids=[c["id"] for c in chunks],
    )


def generate_all(
    profile: ProjectProfile,
    config: Config,
    vector_store: VectorStore,
    collection_name: str,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
) -> tuple[list[GeneratedQuestion], QuestionGenStats]:
    """Build the full coverage plan and generate every question in it --
    the whole Phase 5 pipeline in one call, for the `viva questiongen`
    smoke-test command (mirrors `indexer.index_repo()`'s all-in-one
    shape). Phase 6's real session loop will call `build_coverage_plan()`
    and `generate_question()` separately instead, since it needs to
    interleave generation with the live timed session rather than
    generating everything up front.
    """
    plan = build_coverage_plan(profile, config)
    stats = QuestionGenStats(plan_items_built=len(plan))

    questions: list[GeneratedQuestion] = []
    for item in plan:
        generated = generate_question(
            item, profile, config, vector_store, collection_name, embedding_client, llm_client
        )
        if generated is None:
            stats.plan_items_skipped_no_grounding += 1
            continue
        questions.append(generated)
        stats.questions_generated += 1

    return questions, stats


def _module_summary(profile: ProjectProfile, target_module: str | None) -> str | None:
    if target_module is None:
        return profile.architecture_summary
    for module in profile.modules:
        if module.module == target_module:
            return module.summary
    return None
