#!/usr/bin/env python3
"""LLM_MODEL selection pressure-test (Phase 1, docs/plan.md).

Closes docs/system-design/04-open-questions.md item 5: Phase 0 manual
testing (n=1-2 per model) surfaced two distinct reliability concerns on the
classification call --

  1. Within-model inconsistency: the same answer classified differently
     across repeated runs on the same model.
  2. Between-model citation compliance: some models omit `cited_file` on a
     partial/incorrect verdict far more often than others.

This script turns that into an actual n=4-5 measurement: it runs a fixed set
of hand-written sample answers (tests/fixtures/pressure_test_samples.json),
repeated N times each, against every candidate model, using the *real*
`OllamaClient.evaluate_answer` from Phase 0 -- no new evaluation code, this
just loops and logs the existing call, per the open-questions.md
recommendation that this is cheap precisely because the Phase 0 call
already does the work.

Requires a local Ollama with every `--model` already pulled:

    ollama pull qwen2.5-coder:7b
    ollama pull qwen3.5:latest

Usage:

    python scripts/pressure_test_llm_model.py
    python scripts/pressure_test_llm_model.py --model qwen2.5-coder:7b --model qwen3.5:latest --repetitions 5
    python scripts/pressure_test_llm_model.py --output docs/system-design/07-llm-model-pressure-test-results.md

The classification-stability and citation-compliance calculation
(`compute_model_stats`) is pure and unit-tested in
tests/test_pressure_test_llm_model.py against a mocked LLMClient -- per
CONTRIBUTING.md, no network call happens in the test suite. Only running
this script directly hits real Ollama.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from viva.llm_client import LLMClient, OllamaClient  # noqa: E402
from viva.schemas import EvaluationResult  # noqa: E402

DEFAULT_SAMPLES_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pressure_test_samples.json"
)
DEFAULT_MODELS = ["qwen2.5-coder:7b", "qwen3.5:latest"]
DEFAULT_REPETITIONS = 5


@dataclass(frozen=True)
class SampleStats:
    sample_id: str
    expected_classification: str
    classifications: list[str]
    modal_classification: str
    stability_rate: float  # fraction of runs matching the modal classification
    citation_eligible_runs: int  # runs classified partial/incorrect
    citation_present_runs: int  # of those, runs that included cited_file


@dataclass(frozen=True)
class ModelStats:
    model: str
    sample_stats: list[SampleStats]

    @property
    def mean_stability_rate(self) -> float:
        if not self.sample_stats:
            return 0.0
        return sum(s.stability_rate for s in self.sample_stats) / len(self.sample_stats)

    @property
    def citation_compliance_rate(self) -> float | None:
        eligible = sum(s.citation_eligible_runs for s in self.sample_stats)
        if eligible == 0:
            return None
        present = sum(s.citation_present_runs for s in self.sample_stats)
        return present / eligible


def load_samples(path: Path = DEFAULT_SAMPLES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_repetitions(
    client: LLMClient, sample: dict, repetitions: int
) -> list[EvaluationResult]:
    """Call evaluate_answer `repetitions` times for one sample, same inputs."""
    results = []
    for _ in range(repetitions):
        call_result = client.evaluate_answer(
            question=sample["question"],
            ground_truth_context=sample["ground_truth_context"],
            user_answer=sample["user_answer"],
        )
        results.append(call_result.result)
    return results


def compute_sample_stats(sample: dict, results: list[EvaluationResult]) -> SampleStats:
    classifications = [r.classification for r in results]
    counts = Counter(classifications)
    modal_classification, modal_count = counts.most_common(1)[0]
    stability_rate = modal_count / len(results) if results else 0.0

    eligible = [r for r in results if r.classification in ("partial", "incorrect")]
    with_citation = [r for r in eligible if r.cited_file]

    return SampleStats(
        sample_id=sample["id"],
        expected_classification=sample["expected_classification"],
        classifications=classifications,
        modal_classification=modal_classification,
        stability_rate=stability_rate,
        citation_eligible_runs=len(eligible),
        citation_present_runs=len(with_citation),
    )


def compute_model_stats(
    model: str, samples: list[dict], results_by_sample_id: dict[str, list[EvaluationResult]]
) -> ModelStats:
    sample_stats = [
        compute_sample_stats(sample, results_by_sample_id[sample["id"]]) for sample in samples
    ]
    return ModelStats(model=model, sample_stats=sample_stats)


def render_markdown_report(all_model_stats: list[ModelStats], repetitions: int) -> str:
    lines = [
        "# LLM_MODEL Pressure-Test Results",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} by "
        "`scripts/pressure_test_llm_model.py`. Closes "
        "`docs/system-design/04-open-questions.md` item 5.",
        "",
        f"N={repetitions} repetitions per sample per model.",
        "",
        "## Summary",
        "",
        "| Model | Mean classification stability | Citation-compliance rate |",
        "|---|---|---|",
    ]
    for stats in all_model_stats:
        compliance = (
            f"{stats.citation_compliance_rate:.0%}"
            if stats.citation_compliance_rate is not None
            else "n/a (no partial/incorrect verdicts)"
        )
        lines.append(f"| `{stats.model}` | {stats.mean_stability_rate:.0%} | {compliance} |")

    for stats in all_model_stats:
        lines += ["", f"## `{stats.model}`", "", "| Sample | Expected | Classifications | Stability | Citations |", "|---|---|---|---|---|"]
        for s in stats.sample_stats:
            citation_col = (
                f"{s.citation_present_runs}/{s.citation_eligible_runs}"
                if s.citation_eligible_runs
                else "n/a"
            )
            lines.append(
                f"| {s.sample_id} | {s.expected_classification} | "
                f"{', '.join(s.classifications)} | {s.stability_rate:.0%} | {citation_col} |"
            )

    lines += [
        "",
        "## Recommendation",
        "",
        "_Fill in after reviewing the tables above: which model becomes the new "
        "`LLM_MODEL` default in `.env.example`, and why. A model that is more "
        "stable but has worse citation compliance (or vice versa) is a real "
        "trade-off worth writing down here, not just picking the higher number "
        "on one axis._",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=None,
        help="Candidate model tag to test (repeatable). Default: "
        f"{DEFAULT_MODELS}",
    )
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_PATH)
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the markdown report here instead of only printing it.",
    )
    args = parser.parse_args()

    models = args.models or DEFAULT_MODELS
    samples = load_samples(args.samples)

    all_model_stats: list[ModelStats] = []
    for model in models:
        print(f"== {model} ==", file=sys.stderr)
        client = OllamaClient(model=model, temperature=args.temperature, host=args.ollama_host)
        results_by_sample_id: dict[str, list[EvaluationResult]] = {}
        for sample in samples:
            print(f"  sample {sample['id']!r} x{args.repetitions}...", file=sys.stderr)
            results_by_sample_id[sample["id"]] = run_repetitions(
                client, sample, args.repetitions
            )
        all_model_stats.append(compute_model_stats(model, samples, results_by_sample_id))

    report = render_markdown_report(all_model_stats, args.repetitions)
    print(report)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"\nWritten to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
