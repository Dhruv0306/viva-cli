# viva-cli

**A local-LLM RAG tool that analyzes a GitHub project and conducts a timed, code-grounded viva (oral exam) on it — then reports what you knew, what you missed, and how to improve.**

Point it at a repo, and it clones the project, builds a real understanding of its architecture via retrieval-augmented analysis, then runs a configurable timed Q&A session grounded entirely in your actual code — not generic interview questions. Every question is traceable to a real file/function, and every evaluation is judged against that same code, not against the model's general opinions.

Runs entirely on a local LLM (via [Ollama](https://ollama.com)) — no API keys, no cost, no code ever leaves your machine.

## Why

Explaining your own project out loud — to an interviewer, a thesis committee, a code reviewer — is a different skill from having built it. This tool is a practice partner for that: it knows your codebase because it actually read it, asks about the parts that matter (architecture, design decisions, edge cases, testing), and tells you specifically what to go back and re-learn.

## Features

- 🔗 **Just a GitHub URL** — clone, filter, and analyze up to 500 files per repo, with representative sampling across modules for larger projects
- 🧠 **Grounded RAG pipeline** — AST-based code chunking (tree-sitter), local embeddings, project-level architecture summary via map-reduce analysis
- ⏱️ **Timed viva** — configurable duration (default 30 min); the clock only counts your answering time, never LLM thinking time
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
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

## Configuration

All tunables live in `.env`:

```ini
VIVA_DURATION_MINUTES=30
LLM_MODEL=qwen2.5-coder:7b
EMBEDDING_MODEL=nomic-embed-text
VECTOR_DB_PATH=./data/chroma
MAX_QUESTIONS=8
TOP_K_RETRIEVAL=5
MAX_FILES=500
TEST_FILE_QUOTA_PCT=10
GITHUB_TOKEN=
TEMPERATURE=0.3
```

## Usage

```bash
viva start https://github.com/<owner>/<repo>
```

Resume an interrupted session:

```bash
viva resume <session-id>
```

View a past report:

```bash
viva report <session-id>
```

## Project status

Early build stage — see [`docs/plan.md`](docs/plan.md) for the phased build plan, starting from a Phase 0 walking skeleton through to polish. Not yet ready for general use.

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/requirements.md`](docs/requirements.md) | Functional and non-functional requirements |
| [`docs/design.md`](docs/design.md) | Canonical, build-facing system design |
| [`docs/plan.md`](docs/plan.md) | Phased build plan with exit criteria |
| [`docs/system-design/`](docs/system-design/) | Detailed design reference: resolved decisions, iteration log, full architecture, open questions |

## License

TBD.