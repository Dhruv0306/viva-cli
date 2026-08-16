"""viva-cli: a local-LLM RAG tool that conducts a code-grounded viva.

Phase 0 (walking skeleton) note: most of the package described in
docs/design.md does not exist yet. This module currently only contains the
thin slice needed to de-risk the two assumptions flagged in docs/plan.md
Phase 0 — local-model structured-output reliability, and a timer that
excludes LLM latency from the user-facing clock — before deeper component
work (Phases 1-9) begins.
"""

__version__ = "0.0.1"
