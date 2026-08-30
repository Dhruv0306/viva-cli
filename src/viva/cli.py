"""CLI entrypoint.

`viva start` / `resume` / `list` (Phase 6, docs/system-design/06-cli-contract-and-profile-scaling.md
§6.1) are the real command surface, built on `Orchestrator` (`orchestrator.py`)
and `RichSessionUI` (`session_ui.py`). `viva report`/`cleanup` are still
Phase 8/9 scope. `ingest`/`analyze`/`index`/`questiongen` remain as the
Phase 2-5 smoke-test harnesses they always were -- not stubs of `start`.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from viva import __version__
from viva.analyzer import analyze_repo
from viva.config import Config, ConfigError
from viva.embedding_client import OllamaEmbeddingClient
from viva.indexer import index_repo
from viva.indexer.store import VectorStore
from viva.ingest import ingest_repo
from viva.ingest.clone import CloneError
from viva.llm_client import OllamaClient
from viva.orchestrator import (
    Orchestrator,
    OrchestratorError,
    SessionAlreadyCompleteError,
    SessionNotFoundError,
    SessionNotResumableError,
)
from viva.phase0_demo import run_demo
from viva.profile import ProjectProfile
from viva.questiongen import generate_all
from viva.session_ui import RichSessionUI
from viva.storage import SessionStore

app = typer.Typer(
    help="viva-cli: a local-LLM RAG tool for a code-grounded project viva.",
    no_args_is_help=True,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"viva-cli {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the viva-cli version and exit.",
    ),
) -> None:
    """viva-cli"""


@app.command()
def demo(
    duration: int = typer.Option(
        120,
        "--duration",
        help="Seconds allotted to answer the hardcoded Phase 0 question.",
    ),
) -> None:
    """Run the Phase 0 walking skeleton: one hardcoded question, one real
    schema-validated Ollama evaluation call, one bare-bones report.

    Not the real `viva start` -- see docs/plan.md Phase 0 and
    docs/system-design/06-cli-contract-and-profile-scaling.md for what the
    eventual command contract looks like.
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    llm_client = OllamaClient(
        model=config.llm_model,
        temperature=config.temperature,
        host=config.ollama_host,
    )

    try:
        run_demo(llm_client=llm_client, duration_seconds=float(duration), console=console)
    except Exception as exc:  # noqa: BLE001 - Phase 0 harness, not prod error handling
        console.print(f"[red]Demo failed:[/red] {exc}")
        console.print(
            "[dim]Is Ollama running, and has the configured LLM_MODEL been "
            "pulled? See README.md 'Installation'.[/dim]"
        )
        raise typer.Exit(code=1)


@app.command()
def ingest(
    repo_url: str = typer.Argument(..., help="GitHub repo URL to clone and sample, e.g. https://github.com/owner/repo"),
    branch: str = typer.Option(None, "--branch", help="Branch to clone (defaults to the repo's default branch)."),
) -> None:
    """Clone a repo and run Phase 2 ingestion (hard exclusion, priority
    sampling, stack detection) without doing anything else.

    This is a Phase 2 smoke-test command, not the real `viva start` --
    see docs/system-design/06-cli-contract-and-profile-scaling.md §6.1 for
    the eventual command contract. Useful for manually verifying ingestion
    behavior against a real repo (docs/plan.md Phase 2 exit criteria).
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    console.print(f"Cloning [bold]{repo_url}[/bold]...")
    try:
        result = ingest_repo(repo_url, config, branch=branch)
    except CloneError as exc:
        console.print(f"[red]Clone failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]Cloned[/green] {result.repo_slug} @ {result.commit_sha} (branch: {result.branch})")
    console.print(f"Local path: {result.local_path}")
    console.print(f"Detected stack: {', '.join(result.detected_stack) or '(none detected)'}")
    console.print(f"Files: {result.files_analyzed}/{result.files_total} analyzed")
    console.print(f"Sampling note: {result.sampling_note}")
    if result.excluded_notable:
        console.print("Exclusion summary:")
        for note in result.excluded_notable:
            console.print(f"  - {note}")


@app.command()
def analyze(
    repo_url: str = typer.Argument(..., help="GitHub repo URL to clone, ingest, and analyze, e.g. https://github.com/owner/repo"),
    branch: str = typer.Option(None, "--branch", help="Branch to clone (defaults to the repo's default branch)."),
    output: str = typer.Option(
        "./project_profile.json", "--output", help="Path to write the resulting Project Profile as JSON."
    ),
) -> None:
    """Clone, ingest, and run Phase 3 analysis (tree-sitter extraction +
    map-reduce Project Profile generation) against a repo.

    This is a Phase 3 smoke-test command for manually reviewing Project
    Profile quality (docs/plan.md Phase 3 exit criteria), not the real
    `viva start` -- see
    docs/system-design/06-cli-contract-and-profile-scaling.md §6.1 for the
    eventual command contract. Runs one LLM call per sampled file plus the
    module/architecture reduce calls, so expect this to take noticeably
    longer than `viva ingest` alone on anything but a small repo.
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    console.print(f"Cloning [bold]{repo_url}[/bold]...")
    try:
        ingest_result = ingest_repo(repo_url, config, branch=branch)
    except CloneError as exc:
        console.print(f"[red]Clone failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Ingested[/green] {ingest_result.files_analyzed}/{ingest_result.files_total} files "
        f"(stack: {', '.join(ingest_result.detected_stack) or '(none detected)'})"
    )

    console.print("Analyzing (this runs one LLM call per file, plus reduce calls)...")
    llm_client = OllamaClient(model=config.llm_model, temperature=config.temperature, host=config.ollama_host)
    try:
        analysis_result = analyze_repo(ingest_result, config, llm_client)
    except Exception as exc:  # noqa: BLE001 - Phase 3 smoke-test command, not prod error handling
        console.print(f"[red]Analysis failed:[/red] {exc}")
        console.print(
            "[dim]Is Ollama running, and has the configured LLM_MODEL been "
            "pulled? See README.md 'Installation'.[/dim]"
        )
        raise typer.Exit(code=1)

    profile = ProjectProfile.build(ingest_result, analysis_result)

    console.print(f"\n[bold]Architecture summary:[/bold]\n{profile.architecture_summary}\n")
    console.print(f"[bold]Modules[/bold] ({len(profile.modules)}):")
    for module in profile.modules:
        console.print(f"  - {module.module or '(root)'} ({module.file_count} files): {module.summary}")
    console.print(f"[bold]Entry points:[/bold] {', '.join(profile.entry_points) or '(none detected)'}")
    console.print(f"[bold]Test coverage present:[/bold] {profile.test_coverage_present}")
    stats = profile.analysis_stats
    console.print(
        f"[bold]Parse method:[/bold] {stats.ast_parsed} AST / {stats.line_window_fallback} line-window "
        f"(of {stats.files_analyzed} analyzed)"
    )

    output_path = Path(output)
    output_path.write_text(json.dumps(_profile_to_dict(profile), indent=2, default=str))
    console.print(f"\nWrote Project Profile to [bold]{output_path}[/bold]")


@app.command()
def index(
    repo_url: str = typer.Argument(..., help="GitHub repo URL to clone, ingest, analyze, and index, e.g. https://github.com/owner/repo"),
    branch: str = typer.Option(None, "--branch", help="Branch to clone (defaults to the repo's default branch)."),
    query: str = typer.Option(
        None, "--query", help="After indexing, run one sample retrieval query against the resulting collection."
    ),
) -> None:
    """Clone, ingest, analyze, and run Phase 4 indexing (function/class-
    granularity chunking, local embedding, Chroma vector store) against a
    repo.

    This is a Phase 4 smoke-test command for manually reviewing retrieval
    quality (docs/plan.md Phase 4 exit criteria: "manual retrieval
    queries return relevant, correctly-scoped chunks"), not the real
    `viva start` -- see
    docs/system-design/06-cli-contract-and-profile-scaling.md §6.1 for
    the eventual command contract. Runs one embedding call per file plus
    the map-reduce analysis calls, so expect this to take at least as
    long as `viva analyze` alone on anything but a small repo -- unless
    the exact commit was already indexed, in which case indexing is
    skipped entirely (see docs/system-design/09-phase-4-indexing-design.md
    §9.4).
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    console.print(f"Cloning [bold]{repo_url}[/bold]...")
    try:
        ingest_result = ingest_repo(repo_url, config, branch=branch)
    except CloneError as exc:
        console.print(f"[red]Clone failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Ingested[/green] {ingest_result.files_analyzed}/{ingest_result.files_total} files "
        f"(stack: {', '.join(ingest_result.detected_stack) or '(none detected)'})"
    )

    console.print("Analyzing (this runs one LLM call per file, plus reduce calls)...")
    llm_client = OllamaClient(model=config.llm_model, temperature=config.temperature, host=config.ollama_host)
    try:
        analysis_result = analyze_repo(ingest_result, config, llm_client)
    except Exception as exc:  # noqa: BLE001 - Phase 4 smoke-test command, not prod error handling
        console.print(f"[red]Analysis failed:[/red] {exc}")
        console.print(
            "[dim]Is Ollama running, and has the configured LLM_MODEL been "
            "pulled? See README.md 'Installation'.[/dim]"
        )
        raise typer.Exit(code=1)

    profile = ProjectProfile.build(ingest_result, analysis_result)

    console.print("Indexing (chunking + one embedding call per file)...")
    embedding_client = OllamaEmbeddingClient(model=config.embedding_model, host=config.ollama_host)
    try:
        index_result = index_repo(profile, config, embedding_client)
    except Exception as exc:  # noqa: BLE001 - Phase 4 smoke-test command, not prod error handling
        console.print(f"[red]Indexing failed:[/red] {exc}")
        console.print(
            "[dim]Is Ollama running, and has the configured EMBEDDING_MODEL "
            "been pulled? See README.md 'Installation'.[/dim]"
        )
        raise typer.Exit(code=1)

    console.print(f"\n[bold]Collection:[/bold] {index_result.collection_name}")
    if index_result.stats.reused_existing_collection:
        console.print(
            "[yellow]Reused existing collection[/yellow] for this exact commit -- skipped re-embedding."
        )
    else:
        console.print(
            f"[bold]Chunks indexed:[/bold] {index_result.stats.chunks_built} "
            f"(from {index_result.stats.files_processed} files)"
        )

    if query:
        console.print(f"\n[bold]Retrieval query:[/bold] {query!r}")
        [query_embedding] = embedding_client.embed([query])
        store = VectorStore(config.vector_db_path)
        results = store.query(index_result.collection_name, query_embedding, n_results=config.top_k_retrieval)
        if not results:
            console.print("[dim](no results)[/dim]")
        for rank, r in enumerate(results, start=1):
            meta = r["metadata"]
            label = meta["symbol_name"] or meta["kind"]
            console.print(
                f"  {rank}. {meta['filepath']}:{meta['start_line']}-{meta['end_line']} "
                f"({label}, {meta['parse_method']}) [distance={r['distance']:.4f}]"
            )
            preview = r["text"].strip().splitlines()[0][:100]
            console.print(f"     {preview}")


@app.command()
def questiongen(
    repo_url: str = typer.Argument(..., help="GitHub repo URL to clone, ingest, analyze, index, and generate questions for, e.g. https://github.com/owner/repo"),
    branch: str = typer.Option(None, "--branch", help="Branch to clone (defaults to the repo's default branch)."),
) -> None:
    """Clone, ingest, analyze, index, and run Phase 5 question generation
    (coverage plan + just-in-time grounded question generation) against a
    repo, printing every generated question.

    This is a Phase 5 smoke-test command for manually reviewing question
    grounding accuracy and category coverage (docs/plan.md Phase 5 exit
    criteria), not the real `viva start` -- see
    docs/system-design/06-cli-contract-and-profile-scaling.md §6.1 for the
    eventual command contract. Runs one embedding call per retrieval plus
    one LLM call per generated question, on top of everything `viva index`
    already does.
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    console.print(f"Cloning [bold]{repo_url}[/bold]...")
    try:
        ingest_result = ingest_repo(repo_url, config, branch=branch)
    except CloneError as exc:
        console.print(f"[red]Clone failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Ingested[/green] {ingest_result.files_analyzed}/{ingest_result.files_total} files "
        f"(stack: {', '.join(ingest_result.detected_stack) or '(none detected)'})"
    )

    console.print("Analyzing (this runs one LLM call per file, plus reduce calls)...")
    llm_client = OllamaClient(model=config.llm_model, temperature=config.temperature, host=config.ollama_host)
    try:
        analysis_result = analyze_repo(ingest_result, config, llm_client)
    except Exception as exc:  # noqa: BLE001 - Phase 5 smoke-test command, not prod error handling
        console.print(f"[red]Analysis failed:[/red] {exc}")
        console.print(
            "[dim]Is Ollama running, and has the configured LLM_MODEL been "
            "pulled? See README.md 'Installation'.[/dim]"
        )
        raise typer.Exit(code=1)

    profile = ProjectProfile.build(ingest_result, analysis_result)

    console.print("Indexing (chunking + one embedding call per file)...")
    embedding_client = OllamaEmbeddingClient(model=config.embedding_model, host=config.ollama_host)
    try:
        index_result = index_repo(profile, config, embedding_client)
    except Exception as exc:  # noqa: BLE001 - Phase 5 smoke-test command, not prod error handling
        console.print(f"[red]Indexing failed:[/red] {exc}")
        console.print(
            "[dim]Is Ollama running, and has the configured EMBEDDING_MODEL "
            "been pulled? See README.md 'Installation'.[/dim]"
        )
        raise typer.Exit(code=1)

    console.print("Generating questions (one embedding + one LLM call per question)...")
    store = VectorStore(config.vector_db_path)
    try:
        questions, stats = generate_all(
            profile, config, store, index_result.collection_name, embedding_client, llm_client
        )
    except Exception as exc:  # noqa: BLE001 - Phase 5 smoke-test command, not prod error handling
        console.print(f"[red]Question generation failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[bold]Plan:[/bold] {stats.plan_items_built} planned, "
        f"{stats.questions_generated} generated, "
        f"{stats.plan_items_skipped_no_grounding} skipped (no grounding)"
    )
    for q in questions:
        item = q.plan_item
        label = f"({item.category} / {item.target_module or '(project-level)'}"
        label += f" / {item.target_file})" if item.target_file else ")"
        console.print(f"\n[bold]{item.id}[/bold] {label}")
        console.print(f"  {q.question_text}")
        console.print(f"  [dim]grounded in: {', '.join(q.grounding_chunk_ids)}[/dim]")


def _profile_to_dict(profile: ProjectProfile) -> dict:
    return asdict(profile)


def _build_orchestrator(config: Config) -> tuple[Orchestrator, SessionStore]:
    store = SessionStore(config.session_db_path)
    orchestrator = Orchestrator(config=config, session_store=store, ui=RichSessionUI(console))
    return orchestrator, store


@app.command()
def start(
    repo_url: str = typer.Argument(..., help="GitHub repo URL, e.g. https://github.com/owner/repo"),
    branch: str = typer.Option(None, "--branch", help="Branch to clone (defaults to the repo's default branch)."),
    duration: int = typer.Option(None, "--duration", help="Session duration in minutes (overrides VIVA_DURATION_MINUTES for this session only)."),
    session_name: str = typer.Option(None, "--session-name", help="Optional human-friendly label shown in `viva list`."),
) -> None:
    """Clone, analyze, index, plan, and run a full timed viva session
    end-to-end (docs/plan.md Phase 6, CLI contract §6.1).
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    orchestrator, store = _build_orchestrator(config)
    try:
        orchestrator.start(repo_url, branch=branch, duration_minutes=duration, session_name=session_name)
    except CloneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    except OrchestratorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001 - top-level command boundary
        console.print(f"[red]Session failed:[/red] {exc}")
        raise typer.Exit(code=1)
    finally:
        store.close()


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session ID printed by `viva start` or shown in `viva list`."),
) -> None:
    """Resume an interrupted session strictly from persisted state --
    never re-touches the remote repo (05-repo-lifecycle-and-language-coverage.md §5.3).
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    orchestrator, store = _build_orchestrator(config)
    try:
        orchestrator.resume(session_id)
    except (SessionNotFoundError, SessionAlreadyCompleteError, SessionNotResumableError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=3)
    except OrchestratorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001 - top-level command boundary
        console.print(f"[red]Session failed:[/red] {exc}")
        raise typer.Exit(code=1)
    finally:
        store.close()


@app.command(name="list")
def list_sessions(
    status: str = typer.Option(None, "--status", help="Filter by session status, e.g. IN_PROGRESS, COMPLETE."),
) -> None:
    """List past/resumable sessions -- session IDs aren't shown anywhere
    else after the initial `viva start` run (CLI contract §6.1).
    """
    try:
        config = Config.load()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    store = SessionStore(config.session_db_path)
    try:
        sessions = store.list_sessions(status=status)
    finally:
        store.close()

    if not sessions:
        console.print("[dim]No sessions found.[/dim]")
        return

    table = Table()
    table.add_column("session_id")
    table.add_column("repo")
    table.add_column("commit")
    table.add_column("status")
    table.add_column("created_at")
    table.add_column("duration budget (s)")
    for record in sessions:
        table.add_row(
            record.session_id,
            record.repo_slug or "(unknown)",
            (record.commit_sha or "")[:12],
            record.status,
            record.created_at,
            str(int(record.duration_seconds)),
        )
    console.print(table)


if __name__ == "__main__":
    app()
