# viva-cli

**A local-LLM RAG tool that analyzes a GitHub project and conducts a timed, code-grounded viva (oral exam) on it — then reports what you knew, what you missed, and how to improve.**

Point it at a repo, and it clones the project, builds a real understanding of its architecture via retrieval-augmented analysis, then runs a configurable timed Q&A session grounded entirely in your actual code — not generic interview questions. Every question is traceable to a real file/function, and every evaluation is judged against that same code, not against the model's general opinions.

Runs entirely on a local LLM (via [Ollama](https://ollama.com)) — no API keys, no cost, no code ever leaves your machine.

## Why

Explaining your own project out loud — to an interviewer, a thesis committee, a code reviewer — is a different skill from having built it. This tool is a practice partner for that: it knows your codebase because it actually read it, asks about the parts that matter (architecture, design decisions, edge cases, testing), and tells you specifically what to go back and re-learn.

## Features

- 🔗 **Just a GitHub URL** — clone, filter, and analyze up to 500 files per repo, with representative sampling across modules for larger projects
- 🧠 **Grounded RAG pipeline** — AST-based code chunking (tree-sitter), local embeddings, project-level architecture summary via map-reduce analysis
- ⏱️ **Timed viva** — configurable duration (default 30 min), shown as a live countdown; the clock only counts your answering time, never LLM thinking time
- 🔁 **Adaptive follow-ups** — weak answers get probed further, bounded by configurable depth
- ✅ **Grounded evaluation** — every "you missed this" or "this was wrong" verdict cites the specific file/function it's based on; ungrounded criticism is discarded, not shown
- 📄 **Structured per-question feedback** — summary, what you did well, what you missed, what you got wrong, and how to improve, for every question
- 💻 **Zero cost** — entirely local inference via Ollama, no external API calls required
- 💾 **Crash-resumable** — session state is persisted continuously; an interrupted viva can be resumed

## How it works

```
GitHub URL → Ingest & sample (≤500 files) → Analyze (Project Profile)
           → Index (RAG) → Plan question coverage
           → Timed viva (adaptive Q&A, grounded in your code)
           → Evaluate each answer against the code
           → Summary report
```

See [`docs/design.md`](docs/design.md) for the full architecture, and [`docs/system-design/`](docs/system-design/) for the detailed design rationale and iteration history behind it.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) installed and running locally
- `git`

## Installation

```bash
git clone https://github.com/<your-username>/viva-cli.git
cd viva-cli
pip install -r requirements.txt
cp .env.example .env
```

Pull the models used by default:

```bash
ollama pull gemma4:e4b
ollama pull nomic-embed-text
```

## Configuration

All tunables live in `.env`:

```ini
VIVA_DURATION_MINUTES=30
LLM_MODEL=gemma4:e4b
EMBEDDING_MODEL=nomic-embed-text
VECTOR_DB_PATH=./data/chroma
MAX_QUESTIONS=8
TOP_K_RETRIEVAL=5
MAX_FILES=500
TEST_FILE_QUOTA_PCT=10
MAX_FOLLOWUP_DEPTH=1
SESSION_RETENTION_DAYS=7
MAP_REDUCE_BATCH_SIZE=8
GITHUB_TOKEN=
TEMPERATURE=0.3
LINE_WINDOW_SIZE=60
LINE_WINDOW_OVERLAP=15
```

## Usage

```bash
viva start https://github.com/<owner>/<repo> [--branch main] [--duration 30] [--session-name my-project]
```

List past/resumable sessions (session IDs aren't shown anywhere else after the initial run):

```bash
viva list [--status in_progress|complete|...]
```

Resume an interrupted session:

```bash
viva resume <session-id>
```

View a past report:

```bash
viva report <session-id> [--format md|json] [--output report.md] [--allow-partial]
```

Full CLI contract, including exit codes: [`docs/system-design/06-cli-contract-and-profile-scaling.md`](docs/system-design/06-cli-contract-and-profile-scaling.md) §6.1.

Two Phase 2/3 smoke-test commands also exist for manually exercising ingestion and analysis against a real repo ahead of the real `viva start`:

```bash
viva ingest https://github.com/<owner>/<repo> [--branch main]
viva analyze https://github.com/<owner>/<repo> [--branch main] [--output project_profile.json]
```

## Project status

Early build stage — see [`docs/plan.md`](docs/plan.md) for the phased build plan, starting from a Phase 0 walking skeleton through to polish. Not yet ready for general use.

**Phases 0-3 (walking skeleton through Analysis) are implemented.** Config
now validates every tunable, and an `LLM_MODEL` pressure-test harness
(`scripts/pressure_test_llm_model.py`) is in place — see
[`docs/system-design/07-llm-model-pressure-test-results.md`](docs/system-design/07-llm-model-pressure-test-results.md)
for results once run locally. Phase 3 added tree-sitter AST extraction and
map-reduce Project Profile generation, including the hierarchical-reduce
fallback for repos with many modules — see
[`docs/system-design/08-phase-3-analyzer-design.md`](docs/system-design/08-phase-3-analyzer-design.md).
There's still no real `viva start` yet — only a throwaway harness that
exercises the two riskiest assumptions end-to-end (local-model
structured-output reliability, and a timer that excludes LLM latency):

```bash
pip install -e ".[dev]"
cp .env.example .env   # then set LLM_MODEL to a model you've pulled
viva demo
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for full dev setup and how to run the test suite.

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/requirements.md`](docs/requirements.md) | Functional and non-functional requirements |
| [`docs/design.md`](docs/design.md) | Canonical, build-facing system design |
| [`docs/plan.md`](docs/plan.md) | Phased build plan with exit criteria |
| [`docs/system-design/`](docs/system-design/) | Detailed design reference: resolved decisions, iteration log, full architecture, open questions |

## License

TBD.