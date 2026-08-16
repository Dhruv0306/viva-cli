# Requirements — AI Project Viva Preparation Tool

## 1. Overview
A locally-run tool that takes a GitHub repository URL, builds a grounded understanding of the project via RAG, conducts a time-boxed spoken/typed viva (oral exam) about that project, and produces a per-question and overall evaluation report — all using a local LLM at zero API cost.

## 2. Functional Requirements

### 2.1 Ingestion
- FR1: Accept a GitHub URL (public or private, via configured token) and clone the repository locally.
- FR2: Exclude non-source artifacts (`.git`, `node_modules`, `venv`, build/dist output, lockfiles, binaries, oversized generated files) before any analysis.
- FR3: Cap analysis at a configurable file limit (default 500). If the filtered file count exceeds the limit, apply prioritized, directory-stratified sampling (see design.md §Sampling) rather than truncating arbitrarily.
- FR4: Record which files were excluded/sampled-out and why, for transparency in the final Project Profile.

### 2.2 Project Analysis
- FR5: Detect the primary technology stack(s) from manifest files and file-extension distribution.
- FR6: Extract structured code units (functions, classes, signatures, docstrings) via AST parsing (tree-sitter) for a defined language allowlist. Files outside the allowlist, or files that fail to parse, must fall back to line-window chunking rather than being dropped from analysis (see design.md §Language Coverage & Fallback).
- FR7: Produce a map-reduce style Project Profile: per-module summaries reduced into one project-level summary, including detected architecture pattern, entry points, and module responsibilities.
- FR8: Project Profile must be stored separately from the retrieval index and be injectable into any LLM call as always-available context (not retrieved on demand).

### 2.3 Indexing / RAG
- FR9: Chunk code at function/class granularity with metadata (filepath, symbol name, module role).
- FR10: Embed chunks locally and store in a persistent local vector store, scoped per session/repo.
- FR11: Support retrieval by both semantic similarity and metadata filter (e.g. "chunks belonging to module X").

### 2.4 Question Generation
- FR12: Build a question/coverage plan from the Project Profile spanning multiple categories: architecture/design decisions, specific implementation detail, technology-choice rationale, error handling/edge cases, and testing strategy.
- FR13: Generate each question just-in-time, grounded in retrieved chunk(s) relevant to its category/target module — never generate a question ungrounded in actual retrieved code.
- FR14: Support adaptive follow-up questions based on the strength of the previous answer, bounded by a configurable max follow-up depth per topic (default 1, via `MAX_FOLLOWUP_DEPTH`).
- FR15: Track asked topics/files to avoid duplicate questioning and to enforce category coverage across the session.

### 2.5 Viva Session
- FR16: Run a single timed session per repository, duration configurable via environment variable (default 30 minutes).
- FR17: The user-facing timer must reflect answering time only — LLM generation/evaluation latency must not consume the user's allotted time. It must be displayed as a live, continuously updating countdown during `IN_PROGRESS`, not hidden or shown only periodically.
- FR18: Persist every question, answer, and timestamp immediately as it occurs (not buffered only in memory).
- FR19: Support resuming an interrupted session from the last persisted state.
- FR20: Gracefully end the session on time expiry, always preserving and evaluating the in-flight answer if one was in progress.

### 2.6 Evaluation
- FR21: Evaluate each answer strictly against the retrieved code context used to generate its question ("ground truth" = the code, not general best-practice knowledge).
- FR22: Any claim that the user "missed" or got something "wrong" must cite the specific file/function it's grounded in; ungrounded criticisms must be discarded, not surfaced.
- FR23: Evaluation output must be structured (schema-validated), covering: summary, what was done well, what was missed, what was wrong, and an improvement suggestion per question.
- FR24: Evaluation runs asynchronously relative to the live session so it does not block or consume the user's timed session (see FR17).

### 2.7 Reporting
- FR25: Produce a final report aggregating per-question evaluations into overall strengths, overall weaknesses, and topics to revisit.
- FR26: Report output format: Markdown by default; must be viewable without additional tooling.
- FR27: Any evaluation left unfinished at session end must complete before the report is generated, not be dropped.

### 2.8 Configuration
- FR28: All tunable parameters (viva duration, max questions, file cap, top-k retrieval, model names, temperature) must be environment-file configurable, not hardcoded.

## 3. Non-Functional Requirements

- NFR1 (Cost): Must run entirely on a local LLM/embedding stack with no required paid API calls for core functionality.
- NFR2 (Resource bounds): Must operate within the memory/context constraints of commodity local models (7B–14B class); analysis and evaluation must be chunked/map-reduced to avoid single-call context overflow.
- NFR3 (Resilience): A crash or interruption at any point after `INGESTING` must not lose already-completed work; session state must be recoverable from persisted storage.
- NFR4 (Determinism of grounding): Every LLM-generated question and evaluation must be traceable to specific source chunks/files used to produce it.
- NFR5 (Extensibility): LLM backend and vector store must sit behind thin interfaces so they can be swapped without touching pipeline logic.
- NFR6 (Security): Cloned repository code must never be executed as part of analysis — static parsing (tree-sitter) only.
- NFR7 (Data hygiene): Cloned repository source must be deleted immediately once indexing completes. SQLite session records, the Project Profile, and vector-store collections must expire on a configurable retention window (`SESSION_RETENTION_DAYS`) or be removable on demand (`viva cleanup`) — none of this may accumulate unbounded on disk across sessions.
- NFR8 (Observability): Each pipeline stage must log enough to diagnose failures (e.g. which file caused a parse error, which LLM call failed schema validation) without requiring a full re-run to reproduce.
- NFR9 (Usability): Session must clearly communicate to the user when it's in a non-interactive stage (analyzing, indexing) vs. waiting on their input, so the fixed viva clock doesn't appear to include idle system time.

## 4. Assumptions & Constraints

- Single user, single active session at a time (v1) — no multi-tenant/concurrency requirements.
- Target repos are primarily source-code projects (not data-only or docs-only repos); a fallback "generic" analysis mode exists for atypical repos but is not a primary use case.
- CLI is the primary interface for v1; a web UI is an explicit future phase, not a v1 requirement.

## 5. Out of Scope (v1)

- Multi-repository or multi-session comparison/analytics.
- Real-time voice input/output (text-based Q&A for v1).
- Hosted/cloud LLM backend (structurally supported via NFR5, but not implemented in v1).
- Team/multi-user accounts or auth beyond the GitHub token for private repo access.
