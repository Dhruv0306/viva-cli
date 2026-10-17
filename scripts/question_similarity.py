"""Print how similar one session's asked questions are to each other.

Phase 27 (docs/system-design/32-phase-27-duplicate-questions-design.md
§32.4): QUESTION_SIMILARITY_THRESHOLD (default 0.90) has never been set from
real data. Run this on a session that asked repeated questions (the Phase 26
`viva-cli-test-c` session, for one) and read where the real duplicates and
the genuinely different pairs fall:

    python scripts/question_similarity.py data/viva.db a0d3c56cb4ba

Needs the same Ollama and embedding model `viva` itself uses (it reads the
usual .env), so it runs on your machine. Prints every pair, highest first,
with the embedding cosine, the word overlap the Orchestrator also checks,
and a marker where either layer would call the pair a duplicate. Reports
only; it changes no setting.
"""
from __future__ import annotations

import itertools
import math
import sys
from collections.abc import Callable, Sequence

# Importable both as `python scripts/question_similarity.py` and by tests.
from viva.similarity import DEFAULT_LEXICAL_DUPLICATE_THRESHOLD, word_overlap


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def pairwise(
    questions: dict[str, str],
    embed: Callable[[list[str]], list[list[float]]],
) -> list[tuple[str, str, float, float]]:
    """(id_a, id_b, cosine, word_overlap) for every pair, highest cosine first."""
    ids = list(questions)
    vectors = dict(zip(ids, embed([questions[i] for i in ids]), strict=True))
    rows = [
        (a, b, cosine(vectors[a], vectors[b]), word_overlap(questions[a], questions[b]))
        for a, b in itertools.combinations(ids, 2)
    ]
    return sorted(rows, key=lambda row: row[2], reverse=True)


def render(rows: list[tuple[str, str, float, float]], threshold: float, limit: int = 25) -> str:
    if not rows:
        return "Fewer than two asked questions; nothing to compare."
    lines = [
        f"embedding threshold in use: {threshold:.2f}   word-overlap threshold: "
        f"{DEFAULT_LEXICAL_DUPLICATE_THRESHOLD:.2f}",
        f"{'pair':<22}{'cosine':>8}{'overlap':>9}  flagged by",
        "-" * 58,
    ]
    for a, b, cos, overlap in rows[:limit]:
        flagged = [
            name
            for name, hit in (
                ("embedding", cos >= threshold),
                ("word overlap", overlap >= DEFAULT_LEXICAL_DUPLICATE_THRESHOLD),
            )
            if hit
        ]
        lines.append(f"{a + ' / ' + b:<22}{cos:>8.3f}{overlap:>9.2f}  {', '.join(flagged) or '-'}")
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} lower pairs not shown")
    lines.append("")
    lines.append("Read it as: mark the pairs you consider the same question; the threshold")
    lines.append("belongs between the lowest of those and the highest pair you do not.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/question_similarity.py <viva.db> <session-id-prefix>", file=sys.stderr)
        return 2
    db_path, prefix = argv

    from viva.config import Config
    from viva.embedding_client import OllamaEmbeddingClient
    from viva.storage import SessionStore

    config = Config.load()
    store = SessionStore(db_path)
    matches = [s for s in store.list_sessions() if s.session_id.startswith(prefix)]
    if len(matches) != 1:
        print(f"expected exactly one session starting with {prefix!r}, found {len(matches)}", file=sys.stderr)
        return 1
    asked = {
        r.question_id: r.question_text
        for r in store.get_qa_records(matches[0].session_id)
        if r.question_text and r.status in ("asked", "answered")
    }
    client = OllamaEmbeddingClient(model=config.embedding_model, host=config.ollama_host)
    print(render(pairwise(asked, client.embed), config.question_similarity_threshold))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
