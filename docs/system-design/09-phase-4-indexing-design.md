# 09. Phase 4 Indexer/RAG: Implementation Design

Design decisions locked in before Phase 4 implementation begins, covering
the choices `docs/plan.md`'s Phase 4 entry ("Chunking, embedding, vector
store population (FR9–FR11)") leaves open. Mirrors the shape of
`08-phase-3-analyzer-design.md`.

## 9.1 Chunk content source: re-extract at `INDEXING`, don't thread `FileAnalysis` through

`analyzer/models.py`'s `CodeUnit.body_excerpt` was built for the Map
step's summarization prompt, not for embedding — it's capped at 800
chars and, per its own docstring, "only meaningfully used ... when
`docstring` is `None`." Neither property is acceptable for a RAG chunk:
FR9 needs the *actual* function/class text to embed and later show back
to the Evaluator as grounding, not a conditionally-truncated excerpt.

Two ways to get full-fidelity chunk text were considered:

- **(a)** Extend `FileAnalysis`/`CodeUnit` to carry full node text, and
  thread the full `list[FileAnalysis]` from `analyze_repo()` (`ANALYZING`)
  through into a new indexing step (`INDEXING`), avoiding a second parse
  pass.
- **(b)** Re-run `analyze_file()` at `INDEXING` time against the
  still-on-disk repo (NFR7: raw source isn't deleted until `INDEXING`
  completes), and slice full text directly from the source file using
  each `CodeUnit`'s `start_line`/`end_line`.

**Decision: (b).** `design.md` §1's component rule — "components never
call each other directly... every other component is a service the
orchestrator calls" — is really a statement about *stages reading from
persisted state*, not about passing live objects between in-memory
pipeline steps. `ANALYZING` and `INDEXING` are separate state-machine
stages (`design.md` §2); each should be independently invocable against
what's on disk (`local_path`, pinned `commit_sha`) rather than depending
on the previous stage's in-memory return value still being around. This
also keeps `viva index` (§9.5) usable as a standalone smoke-test command
the same way `viva ingest`/`viva analyze` are, without needing to plumb
`AnalysisResult` internals through the CLI layer.

The cost is a second tree-sitter parse pass per file. This is a non-issue
in practice — tree-sitter parsing is fast (it's what powers editor
syntax highlighting on every keystroke), and it's happening once per
`viva start`, not in a hot loop.

**Consequence:** `extract.py`'s `analyze_file()` is reused as-is for
boundary/kind/name detection; the only new code is slicing
`content.splitlines()[start_line-1:end_line]` to get full text per unit,
plus the `line_window` path, which already produces full-fidelity chunk
text today (`raw_windows` is real content, not an excerpt) and needs no
changes at all.

## 9.2 Chunk boundary summary (satisfies FR9)

| Source | Chunk = | Metadata |
|---|---|---|
| `parse_method == "ast"` | full text of one `CodeUnit` (`start_line`–`end_line`, re-sliced from disk per §9.1) | `filepath, module, symbol_name, kind, parse_method="ast", language, start_line, end_line` |
| `parse_method == "line_window"` | one `raw_windows` entry, already sized via `Config.line_window_size`/`line_window_overlap` (Phase 3) | `filepath, module, symbol_name=None, kind="line_window", parse_method="line_window", language, start_line, end_line` |

No new chunk-sizing config is needed — `LINE_WINDOW_SIZE`/
`LINE_WINDOW_OVERLAP` (Phase 3) already govern the one case that needs a
tunable size; AST-unit chunks are bounded by whatever the function/class
actually is, per FR9's "chunk at function/class granularity."
`start_line`/`end_line` on every chunk (not just AST ones) double as the
citation anchor FR22/NFR4 need downstream ("cited_file", e.g.
`src/payments/handler.py:42`) — reusing the same line-range shape
`CodeUnit` already established rather than inventing a second one.

## 9.3 `EmbeddingClient`: new thin interface, same shape as `LLMClient`

Per NFR5 ("LLM backend and vector store must sit behind thin interfaces
so they can be swapped without touching pipeline logic"), embedding gets
its own interface rather than being bolted onto `LLMClient` — embedding
and chat completion are different Ollama API calls (`embed` vs `chat`)
with different failure modes, and conflating them would make `LLMClient`
harder to fake in tests that only need one or the other.

```python
class EmbeddingClient(abc.ABC):
    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Ollama implementation uses `Config.embedding_model` (already present
since Phase 1) via `ollama.Client.embed(model=..., input=texts)`, which
accepts a batch natively — no need to hand-roll batching logic.

## 9.4 Vector store: Chroma, collection keying, and reuse

**New dependency:** `chromadb` — not yet in `pyproject.toml`/
`requirements.txt`. `PersistentClient(path=config.vector_db_path)` is the
right mode (not the in-memory/ephemeral client): FR10 requires a
*persistent* local store, and `VECTOR_DB_PATH` is already a real `Config`
field waiting for this.

**Collection key:** `{repo_slug}-{commit_sha}`, exactly as
`05-repo-lifecycle-and-language-coverage.md` §5.2 and `design.md` §8
already specify — no new decision here, just the first phase to actually
implement it.

**Reuse (implementing the "enables reuse without re-indexing" rationale
`design.md` §8 already commits to):** before embedding, check whether a
collection with the computed key already exists
(`client.get_or_create_collection` semantics, or an explicit
`list_collections()` check first so a fresh commit's key — which by
construction can't collide with an older commit's — always triggers a
real build). If it exists, `INDEXING` is a no-op past the existence
check: no re-parse, no re-embed. This is what makes `commit_sha`-keying
worth doing rather than decorative — without reuse, the key still gives
correct staleness *detection*, but every `viva start` on an unchanged
repo would pay full re-indexing cost for no reason.

**Metadata filtering (FR11's "chunks belonging to module X"):** Chroma's
native `collection.query(query_texts=..., where={"module": "payments"})`
covers this directly — no custom filtering layer needed. Every field in
§9.2's metadata table is a valid `where` key.

## 9.5 `viva index` smoke-test command

Following the `viva ingest` (Phase 2) / `viva analyze` (Phase 3)
precedent: clone → ingest → analyze → index, then run one or more sample
retrieval queries against the resulting collection and print the results
(chunk text preview + metadata + distance score). This is what the Phase
4 exit criteria ("manual retrieval queries return relevant,
correctly-scoped chunks") actually needs a command to *do* — the same
way `viva analyze`'s printed output is what Phase 3's manual
profile-quality review runs against.

Not the real `viva start` — same caveat as `ingest`/`analyze`, this is a
Phase 4 smoke-test command against
`06-cli-contract-and-profile-scaling.md` §6.1's eventual contract, not an
implementation of it.

## 9.6 Other decisions (lower-stakes, standard-practice defaults)

- **Chunk ID scheme:** `{repo_slug}-{commit_sha}-{path}-{start_line}` —
  deterministic and stable across re-runs of the same commit (important
  for `get_or_create_collection`-style idempotency: re-embedding the same
  unchanged file twice should upsert to the same ID, not duplicate).
- **Embedding batching:** one `embed()` call per file's full chunk list
  rather than one call per chunk — `ollama.Client.embed` already accepts
  a batch, and batching by file keeps the call count proportional to
  files analyzed (same order of magnitude as Phase 3's one-LLM-call-per-file
  Map step) rather than proportional to chunk count.
- **Concurrency:** sequential per file, same rationale as Phase 3 §8.5 —
  one local Ollama model serving one request at a time on typical
  hardware means concurrency wouldn't reduce wall-clock time here either.
- **Empty/near-empty files:** a file with zero `CodeUnit`s and zero
  `raw_windows` (e.g. an empty `__init__.py`) simply produces zero
  chunks — not an error, nothing to embed.
- **Excluded/sampled-out files:** never indexed, matching `design.md` §3
  ("the Question Generator is not permitted to target excluded files") —
  `INDEXING` only ever iterates `ProjectProfile.sampled_files`, the same
  set `ANALYZING` already operated on.
