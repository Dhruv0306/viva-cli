"""CLI entrypoint.

Phase 0 note: only `viva --version` and `viva demo` exist here. The real
command contract (`viva start` / `resume` / `report` / `list` / `cleanup`,
docs/system-design/06-cli-contract-and-profile-scaling.md §6.1) is Phase 6/8/9
scope and should be implemented against that contract directly rather than
extending `demo` -- `demo` is a throwaway Phase 0 harness, not a stub of
`start`.
"""
from __future__ import annotations

import typer
from rich.console import Console

from viva import __version__
from viva.config import Config, ConfigError
from viva.llm_client import OllamaClient
from viva.phase0_demo import run_demo

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


if __name__ == "__main__":
    app()
