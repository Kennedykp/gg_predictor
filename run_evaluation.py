"""
Evaluation runner CLI (Epic 2B.3).

    python run_evaluation.py --dataset data/historical --out data/evaluation
    python run_evaluation.py --dataset data/historical --model POISSON_V1 --league eng.1

Offline by default and by design: it reads a dataset already on disk and never
touches the network. Building the dataset is Epic 2B.2's job
(`historical_dataset.py`), and keeping the two apart is what lets many models be
scored against byte-identical inputs.

Not imported by main.py, analyze_all.py or run3/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from domain.evaluation import MetricSummary
from domain.historical import HistoricalMatch
from evaluation_harness import (
    EvaluationRun,
    evaluate,
    get_model,
    write_artifacts,
)
from historical_dataset import load_dataset


def _format_optional(value: Optional[float], places: int = 4) -> str:
    """`n/a` for absent, never 0.0. A missing metric is not a zero metric."""
    return "n/a" if value is None else f"{value:.{places}f}"


def _print_summary(summary: MetricSummary, *, label: str = "") -> None:
    heading = f"{summary.model_id} {summary.model_version}"
    if label:
        heading = f"{heading}  [{label}]"
    print(f"\n{heading}")
    print(f"  targets            {summary.targets}")
    print(f"  scored             {summary.scored}")
    print(f"  coverage           {_format_optional(summary.coverage)}")
    print(f"  Brier              {_format_optional(summary.brier)}")
    print(f"  log loss           {_format_optional(summary.log_loss)}")
    print(f"  mean predicted     {_format_optional(summary.mean_predicted)}")
    print(f"  observed BTTS      {_format_optional(summary.observed_rate)}")
    print(f"  accuracy @0.5      {_format_optional(summary.accuracy_at_half)}  (diagnostic only)")
    if summary.unevaluable:
        print("  unevaluable:")
        for reason, count in summary.unevaluable.items():
            print(f"    {reason:<24} {count}")


def _print_calibration(summary: MetricSummary) -> None:
    print("\n  calibration ([lower, upper), final bin closed):")
    print(f"    {'bin':<16}{'n':>7}{'predicted':>12}{'observed':>11}{'gap':>9}")
    for bucket in summary.calibration:
        if bucket.count == 0:
            print(f"    {bucket.label:<16}{0:>7}{'-':>12}{'-':>11}{'-':>9}")
            continue
        print(
            f"    {bucket.label:<16}{bucket.count:>7}"
            f"{_format_optional(bucket.mean_predicted, 3):>12}"
            f"{_format_optional(bucket.observed_rate, 3):>11}"
            f"{_format_optional(bucket.gap, 3):>9}"
        )


def run(
    dataset: Sequence[HistoricalMatch],
    model_ids: Sequence[str],
    *,
    league: Optional[str] = None,
    seasons: Optional[Sequence[int]] = None,
) -> List[EvaluationRun]:
    runs: List[EvaluationRun] = []
    for model_id in model_ids:
        model = get_model(model_id)
        runs.append(
            evaluate(
                dataset,
                model,
                competition=league,
                seasons=seasons,
            )
        )
    return runs


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time model evaluation (Epic 2B.3)")
    parser.add_argument("--dataset", required=True, help="directory of dataset JSONL files")
    parser.add_argument("--out", help="directory for evaluation artifacts")
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="model id to evaluate (repeatable); default POISSON_V1 + reference",
    )
    parser.add_argument("--league", help="restrict targets to one competition")
    parser.add_argument("--season", action="append", type=int, help="restrict target seasons")
    parser.add_argument("--breakdown", choices=["competition", "season", "evidence"])
    parser.add_argument("--calibration", action="store_true", help="print calibration table")
    args = parser.parse_args(argv)

    dataset_dir = Path(args.dataset)
    if not dataset_dir.exists():
        print(f"dataset directory not found: {dataset_dir}", file=sys.stderr)
        return 2

    dataset = load_dataset(dataset_dir)
    if not dataset:
        # An empty dataset is not an evaluation of zero quality; it is nothing
        # to evaluate, and is reported as such rather than as a 0.0 Brier.
        print(f"no historical records found under {dataset_dir}", file=sys.stderr)
        return 2

    model_ids = args.model or ["POISSON_V1", "REFERENCE_BASE_RATE"]
    runs = run(dataset, model_ids, league=args.league, seasons=args.season)

    print(f"dataset: {len(dataset)} records from {dataset_dir}")
    for evaluation in runs:
        _print_summary(evaluation.summary)
        if args.calibration:
            _print_calibration(evaluation.summary)
        if args.breakdown:
            for name, summary in evaluation.breakdown(args.breakdown).items():
                _print_summary(summary, label=f"{args.breakdown}={name}")

    if args.out:
        written = write_artifacts(runs, Path(args.out))
        print("\nartifacts:")
        for kind, path in written.items():
            print(f"  {kind:<12} {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
