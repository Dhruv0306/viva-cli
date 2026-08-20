#!/usr/bin/env python3
"""LLM_MODEL selection pressure-test (Phase 1, docs/plan.md).

Closes docs/system-design/04-open-questions.md item 5: Phase 0 manual
testing (n=1-2 per model) surfaced two distinct reliability concerns on the
classification call --

  1. Within-model inconsistency: the same answer classified differently
     across repeated runs on the same model.
  2. Between-model citation compliance: some models omit `cited_file` on a
     partial/incorrect verdict far more often than others.

This script turns that into an actual n=4-10 measurement: it runs a fixed set
of hand-written sample answers (tests/fixtures/pressure_test_samples.json),
repeated N times each, against every candidate model, using the *real*
`OllamaClient.evaluate_answer` from Phase 0 -- no new evaluation code, this
just loops and logs the existing call, per the open-questions.md
recommendation that this is cheap precisely because the Phase 0 call
already does the work.

Requires a local Ollama with every `--model` already pulled:

    ollama pull qwen2.5-coder:7b
    ollama pull qwen3.5:latest
    ollama pull deepseek-r1:latest
    ollama pull llama3:latest
    ollama pull nemotron-mini:latest
    ollama pull gemma4:e4b
    ollama pull mistral-nemo:12b

Usage:

    python scripts/pressure_test_llm_model.py
    python scripts/pressure_test_llm_model.py --model qwen2.5-coder:7b --model qwen3.5:latest --repetitions 10
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
import time
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
DEFAULT_MODELS = ["qwen2.5-coder:7b", "qwen3.5:latest", "deepseek-r1:latest", "llama3:latest", "nemotron-mini:latest", "gemma4:e4b", "mistral-nemo:12b"]
DEFAULT_REPETITIONS = 10


@dataclass(frozen=True)
class RepetitionOutcome:
    """One repetition's result, tagged with *why* it looks the way it does.

    `classification="not_attempted"` can mean three genuinely different
    things that the old report flattened into one indistinguishable string:

      1. The model genuinely judged the answer as blank/not attempted --
         a real, meaningful result.
      2. `OllamaClient.evaluate_answer`'s own repair loop was exhausted
         (bad JSON twice in a row) and fell back internally -- this *is* a
         real finding about the model's structured-output reliability
         (docs/system-design/01-resolved-decisions.md §1.2), not noise, so
         it stays in the stats, but needs to be visibly flagged rather than
         silently counted as a normal verdict.
      3. This harness's own call wrapper caught an exception (timeout,
         connection drop) -- this says nothing about the model and must be
         excluded from stability/citation stats entirely, or it corrupts
         the very numbers the pressure test exists to produce.

    (1) and (2) are both visible via `result.needs_review`, which
    `OllamaClient` already sets correctly for (2). (3) is new: `is_call_error`
    is only ever set by `run_repetitions`'s except block below, never by the
    client itself.
    """

    result: EvaluationResult
    is_call_error: bool = False
    error_message: str | None = None


@dataclass(frozen=True)
class SampleStats:
    sample_id: str
    expected_classification: str
    classifications: list[str]
    modal_classification: str
    stability_rate: float  # fraction of *valid* runs matching the modal classification
    citation_eligible_runs: int  # valid runs classified partial/incorrect
    citation_present_runs: int  # of those, runs that included cited_file
    needs_review_runs: int  # valid runs flagged needs_review (repair-loop exhausted, or missing citation)
    call_error_runs: int  # runs excluded entirely: harness-level failure, not a model result
    accurate_runs: int  # valid runs where classification matched expected_classification

    @property
    def accuracy_rate(self) -> float | None:
        total = len(self.classifications)
        if total == 0:
            return None
        return self.accurate_runs / total


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

    @property
    def total_needs_review_runs(self) -> int:
        return sum(s.needs_review_runs for s in self.sample_stats)

    @property
    def total_call_error_runs(self) -> int:
        return sum(s.call_error_runs for s in self.sample_stats)

    @property
    def mean_accuracy_rate(self) -> float:
        # Note: this is a coarse, evenly-weighted average across the 5
        # samples, not weighted by how many valid runs each sample had --
        # good enough for a directional pressure-test comparison, not a
        # substitute for a larger, properly-weighted accuracy benchmark.
        rates = [s.accuracy_rate for s in self.sample_stats if s.accuracy_rate is not None]
        if not rates:
            return 0.0
        return sum(rates) / len(rates)


def load_samples(path: Path = DEFAULT_SAMPLES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_repetitions(
    client: LLMClient, sample: dict, repetitions: int
) -> list[RepetitionOutcome]:
    """Call evaluate_answer `repetitions` times for one sample, same inputs.

    Prints per-repetition progress to stderr (with elapsed time) so a slow
    or hung call is visible in real time rather than showing nothing until
    the whole sample finishes. A call that raises (e.g. a timeout) is
    tagged `is_call_error=True` rather than aborting the run -- results
    collected so far for other samples/models are worth more than a clean
    crash -- but it is NOT reported as if the model had produced a
    `not_attempted` verdict; see `RepetitionOutcome`.
    """
    outcomes = []
    for i in range(1, repetitions + 1):
        rep_start = time.monotonic()
        try:
            call_result = client.evaluate_answer(
                question=sample["question"],
                ground_truth_context=sample["ground_truth_context"],
                user_answer=sample["user_answer"],
            )
            outcome = RepetitionOutcome(result=call_result.result)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: keep the run alive
            placeholder = EvaluationResult(
                classification="not_attempted",
                summary=f"Call failed: {exc}",
                cited_file=None,
                needs_review=True,
            )
            outcome = RepetitionOutcome(
                result=placeholder, is_call_error=True, error_message=str(exc)
            )
        elapsed = time.monotonic() - rep_start
        if outcome.is_call_error:
            print(f"    rep {i}/{repetitions}: ERROR ({elapsed:.1f}s): {outcome.error_message}", file=sys.stderr)
        else:
            flag = " [needs_review]" if outcome.result.needs_review else ""
            print(
                f"    rep {i}/{repetitions}: {outcome.result.classification}{flag} ({elapsed:.1f}s)",
                file=sys.stderr,
            )
        outcomes.append(outcome)
    return outcomes


def compute_sample_stats(sample: dict, outcomes: list[RepetitionOutcome]) -> SampleStats:
    valid = [o for o in outcomes if not o.is_call_error]
    call_error_runs = len(outcomes) - len(valid)

    results = [o.result for o in valid]
    classifications = [r.classification for r in results]

    if classifications:
        counts = Counter(classifications)
        modal_classification, modal_count = counts.most_common(1)[0]
        stability_rate = modal_count / len(classifications)
    else:
        # Every repetition for this sample errored out -- no model data at
        # all, not "the model was 0% stable".
        modal_classification = "n/a (all calls failed)"
        stability_rate = 0.0

    eligible = [r for r in results if r.classification in ("partial", "incorrect")]
    with_citation = [r for r in eligible if r.cited_file]
    needs_review_runs = sum(1 for r in results if r.needs_review)
    accurate_runs = sum(
        1 for r in results if r.classification == sample["expected_classification"]
    )

    return SampleStats(
        sample_id=sample["id"],
        expected_classification=sample["expected_classification"],
        classifications=classifications,
        modal_classification=modal_classification,
        stability_rate=stability_rate,
        citation_eligible_runs=len(eligible),
        citation_present_runs=len(with_citation),
        needs_review_runs=needs_review_runs,
        call_error_runs=call_error_runs,
        accurate_runs=accurate_runs,
    )


def compute_model_stats(
    model: str, samples: list[dict], outcomes_by_sample_id: dict[str, list[RepetitionOutcome]]
) -> ModelStats:
    sample_stats = [
        compute_sample_stats(sample, outcomes_by_sample_id[sample["id"]]) for sample in samples
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
        "Stability and citation-compliance are computed only over valid model "
        "responses. Runs where the harness itself failed to get a response "
        "(timeout, connection error) are excluded from both metrics and "
        "reported separately as **Call errors** -- they say nothing about "
        "model quality. Runs where the model's own repair loop was exhausted "
        "(bad JSON twice) are NOT excluded -- that's a real structured-output "
        "reliability finding -- but are counted in **Needs review** so they "
        "stay visible rather than hiding inside a normal-looking verdict.",
        "",
        "**Accuracy** (new) checks classification against the sample's "
        "expected label. A model can be perfectly self-consistent while "
        "being consistently wrong -- e.g. always classifying an incorrect "
        "answer as `correct` -- and stability alone will not catch that. "
        "Don't pick a model from the stability/citation columns alone; check "
        "accuracy first.",
        "",
        "## Summary",
        "",
        "| Model | Mean accuracy vs expected | Mean classification stability | Citation-compliance rate | Needs review | Call errors |",
        "|---|---|---|---|---|---|",
    ]
    for stats in all_model_stats:
        compliance = (
            f"{stats.citation_compliance_rate:.0%}"
            if stats.citation_compliance_rate is not None
            else "n/a (no partial/incorrect verdicts)"
        )
        lines.append(
            f"| `{stats.model}` | {stats.mean_accuracy_rate:.0%} | {stats.mean_stability_rate:.0%} | {compliance} | "
            f"{stats.total_needs_review_runs} | {stats.total_call_error_runs} |"
        )

    for stats in all_model_stats:
        lines += [
            "",
            f"## `{stats.model}`",
            "",
            "| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in stats.sample_stats:
            citation_col = (
                f"{s.citation_present_runs}/{s.citation_eligible_runs}"
                if s.citation_eligible_runs
                else "n/a"
            )
            classifications_col = ", ".join(s.classifications) if s.classifications else "n/a"
            accuracy_col = f"{s.accuracy_rate:.0%}" if s.accuracy_rate is not None else "n/a"
            lines.append(
                f"| {s.sample_id} | {s.expected_classification} | "
                f"{classifications_col} | {accuracy_col} | {s.stability_rate:.0%} | {citation_col} | "
                f"{s.needs_review_runs} | {s.call_error_runs} |"
            )

    lines += [
        "",
        "## Recommendation",
        "",
        "_Fill in after reviewing the tables above: which model becomes the new "
        "`LLM_MODEL` default in `.env.example`, and why. A model that is more "
        "stable but has worse citation compliance (or vice versa) is a real "
        "trade-off worth writing down here, not just picking the higher number "
        "on one axis. Also weigh Call errors (infra reliability on your "
        "hardware, not model quality) and Needs review (how often the model's "
        "own structured-output reliability broke down) -- a model that wins "
        "on stability/citations but needs review constantly is still a bad "
        "pick for an unattended pipeline._",
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
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-call timeout in seconds. A hung/very slow model surfaces "
        "as a call error excluded from stats, instead of hanging the whole "
        "run forever.",
    )
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
        print(f"\n== {model} ==\n", file=sys.stderr)
        client = OllamaClient(
            model=model, temperature=args.temperature, host=args.ollama_host, timeout=args.timeout
        )
        results_by_sample_id: dict[str, list[RepetitionOutcome]] = {}
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
