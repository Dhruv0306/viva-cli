"""Citation validation for evaluator output (docs/plan.md Phase 26,
docs/system-design/31-phase-26-followups-design.md §31.5).

FR22 says a `partial`/`incorrect` verdict, and every `missed`/`did_wrong`
entry, must cite the code that grounds it, and that one without a citation
is dropped or flagged. Until Phase 26 "has a citation" meant "the model
wrote something in `cited_file`", so a model that wrote `N/A (Not present
in the policy file)` passed as grounded and the report printed it as a
citation. `normalize_citation()` is the single place that decides what a
real citation is.
"""
from __future__ import annotations

import re

# Text a model writes when it has nothing to cite. Matched at the start,
# ignoring leading brackets and whitespace, so "N/A", "(none)" and
# "N/A (Not present in the policy file)" are all caught.
_NO_CITATION = re.compile(
    r"^[\s(\[\"']*(?:"
    r"n\s*/\s*a\b|na\b|none\b|null\b|nil\b|unknown\b|unspecified\b|"
    r"not\s+(?:applicable|present|found|available|specified|provided|shown|mentioned|in\b)|"
    r"no\s+(?:citation|file|specific|relevant|reference|code)\b"
    r")",
    re.IGNORECASE,
)

# `src/pay/handler.py`, `handler.py`, `Calculator.java` -- anything with a
# file extension. A trailing `:42` or `:10-20` line reference is not part
# of the path, so it is excluded by construction.
_PATH_LIKE = re.compile(r"[\w.\-/\\]*[\w\-]\.[A-Za-z][A-Za-z0-9]*")


def _slashes(text: str) -> str:
    return text.replace("\\", "/")


def normalize_citation(raw: str | None, context: str | None = None) -> str | None:
    """Return `raw` (stripped) if it is a usable citation, else `None`.

    Without `context` this only rejects text that says there is no
    citation. With `context` (the retrieved code the model was shown, whose
    chunks start with a `[path:start-end]` header) it also rejects a
    citation that names nothing in that context, because the model cannot
    have grounded a verdict in code it was never shown:

    - a citation containing a file path is accepted if any such path
      appears in the context;
    - one with no path (a bare function name, say) is accepted if the text,
      minus brackets and a trailing line reference, appears in the context.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or not re.search(r"\w", text) or _NO_CITATION.match(text):
        return None
    if context is None:
        return text

    haystack = _slashes(context)
    paths = [_slashes(p) for p in _PATH_LIKE.findall(text)]
    if paths:
        return text if any(path in haystack for path in paths) else None

    bare = re.sub(r":\d+(?:-\d+)?$", "", text.strip("()[] \t\"'"))
    return text if bare and bare in context else None
