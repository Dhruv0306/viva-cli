"""Phase 10 web UI (docs/plan.md Phase 10, docs/system-design/
15-phase-10-web-ui-design.md).

Sits entirely on top of `Orchestrator`/`SessionStore`/`ReportBuilder`/
`run_cleanup`, the same components `cli.py` already drives -- no
pipeline logic lives in this package (design doc §3.7/§15.4).
"""
from __future__ import annotations
