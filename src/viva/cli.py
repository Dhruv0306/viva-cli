"""CLI entrypoint.

Phase 0 note: only `viva --version` and `viva demo` exist here. The real
command contract (`viva start` / `resume` / `report` / `list` / `cleanup`,
docs/system-design/06-cli-contract-and-profile-scaling.md §6.1) is Phase 6/8/9
scope and should be implemented against that contract directly rather than
extending `demo` -- `demo` is a throwaway Phase 0 harness, not a stub of
`start`.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console

from viva import __version__
from viva.analyzer import analyze_repo
from viva.config import Config, ConfigError
from viva.embedding_client import OllamaEmbeddingClient
from viva.indexer import index_repo
from viva.indexer.store import VectorStore
from viva.ingest import ingest_repo
from viva.ingest.clone import CloneError
from viva.llm_client import OllamaClient
from viva.phase0_demo import run_demo
from viva.profile import ProjectProfile

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


def _profile_to_dict(profile: ProjectProfile) -> dict:
    return asdict(profile)


if __name__ == "__main__":
    app()
