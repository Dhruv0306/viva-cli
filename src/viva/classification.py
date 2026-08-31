"""Classification-provider seam for FR14 adaptive follow-ups (docs/plan.md
Phase 6 / Phase 7 boundary).

docs/design.md §7 ties the follow-up decision to the fast classification
call ("whether/how to generate a follow-up depends on it"), but that call
is the Evaluator's job, and the Evaluator is Phase 7 scope (`docs/plan.md`:
Phase 6 has "no evaluation yet"). Rather than have `orchestrator.py`
hardcode "no follow-ups in Phase 6" as a special case, the follow-up
decision is written against this thin interface now -- mirroring the
`LLMClient`/`EmbeddingClient` seam pattern (NFR5, "LLM backend ... must sit
behind thin interfaces") -- and Phase 6 injects `NullClassificationProvider`,
which always returns `None`.

The result: the follow-up branch in `orchestrator.py` is real code, not a
TODO, but it structurally never fires in Phase 6 -- every `qa_records.eval_status`
lands `"deferred"` (see `storage/session_store.py`). Phase 7's `Evaluator`
(`viva.evaluator`) is the real provider, backed by the two-call
classification/feedback split (docs/system-design/
12-phase-7-evaluator-design.md), with no changes to the Orchestrator's
control flow around `classify()` itself.

`bind_session`/`requeue_unfinished`/`flush` are session-lifecycle hooks
the Orchestrator calls unconditionally (entering `IN_PROGRESS`, on
`resume()`, and at `FINALIZING_EVALS`) regardless of which provider is
injected -- default no-ops here so `NullClassificationProvider` and any
test double needs no changes to keep satisfying the interface; only
`Evaluator` overrides them with real behavior.
"""
from __future__ import annotations

import abc

from viva.schemas import Classification


class ClassificationProvider(abc.ABC):
    @abc.abstractmethod
    def classify(self, question_id: str, answer_text: str) -> Classification | None:
        """Return the fast classification for one just-answered question,
        or `None` if no classification is available (Phase 6: always
        `None` -- there is no Evaluator to call yet)."""
        raise NotImplementedError

    def bind_session(self, session_id: str, collection_name: str) -> None:
        """Called once, right where the Orchestrator enters
        `IN_PROGRESS`, before the first `classify()`. No-op by default."""

    def requeue_unfinished(self) -> None:
        """Called on `viva resume`, after `bind_session()`, before the
        live loop resumes. No-op by default."""

    def flush(self, timeout: float) -> None:
        """Called at `FINALIZING_EVALS`. No-op by default."""


class NullClassificationProvider(ClassificationProvider):
    """Phase 6's injected provider -- see module docstring."""

    def classify(self, question_id: str, answer_text: str) -> Classification | None:
        return None
