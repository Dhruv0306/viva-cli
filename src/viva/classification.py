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
lands `"deferred"` (see `storage/session_store.py`). Phase 7 swaps in a real
provider backed by the synchronous classification call, with no changes to
the Orchestrator's control flow.
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


class NullClassificationProvider(ClassificationProvider):
    """Phase 6's injected provider -- see module docstring."""

    def classify(self, question_id: str, answer_text: str) -> Classification | None:
        return None
