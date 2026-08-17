"""
Evaluate settled predictions (Epic 2H-3).

    python evaluate_settled.py                      # everything settled so far
    python evaluate_settled.py --month 2026-08
    python evaluate_settled.py --out data/evaluation

Reads the prediction ledger and the settlement log, joins them on
`(competition, season, fixture_id)`, and grades the STORED probabilities with the
metrics in `domain/evaluation.py`.

    data/predictions/*.jsonl  --+
                                +--> join --> summarise --> report
    data/settlements/*.jsonl  --+

WHAT THIS DELIBERATELY DOES NOT DO

`evaluation_harness.replay()` is NOT called, and this module does not import it.
Replay regenerates a probability by running the model over a dataset; the number
it produces is a hindsight number computed from data that may not have existed
before kickoff. Once written to a report the two are indistinguishable, so the
only safe rule is that the evaluated probability comes from the ledger and
nowhere else. This module therefore never imports `poisson`, `filters`,
`decision` or `evaluation_harness`, and performs no probability arithmetic of its
own - it hands stored floats to the referee.

It also never writes to `data/predictions` or `data/settlements`. Evaluation is a
reader of both. A grader that could edit either could make itself look good.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from domain.evaluation import (
    EVALUATION_SCHEMA_VERSION,
    MetricSummary,
    summarise,
)
from domain.evaluation_input import (
    EVALUATION_INPUT_SCHEMA_VERSION,
    EvaluationInput,
    JoinReport,
    join_for_evaluation,
    to_prediction_records,
)
from prediction_ledger import DEFAULT_LEDGER_DIR, load_records
from settle_predictions import DEFAULT_SETTLEMENT_DIR, load_settlements

__all__ = [
    "DEFAULT_EVALUATION_DIR",
    "evaluate",
    "group_by_model",
    "summarise_by_model",
    "build_report",
    "write_report",
    "main",
]

DEFAULT_EVALUATION_DIR = Path("data/evaluation")


def group_by_model(
    inputs: Sequence[EvaluationInput],
) -> Dict[Tuple[str, str], List[EvaluationInput]]:
    """
    Split by `(model_id, model_version)` from the STORED provenance.

    Never merged into one aggregate. Two model versions pooled into a single
    Brier score produce a number describing neither, and the pooled figure moves
    when the traffic mix changes rather than when the model does. The version
    comes from the ledger, so a record written by an older model is graded as
    that model.
    """
    groups: Dict[Tuple[str, str], List[EvaluationInput]] = {}
    for item in inputs:
        key = (item.provenance.model_id, item.provenance.model_version)
        groups.setdefault(key, []).append(item)
    return groups


def summarise_by_model(
    inputs: Sequence[EvaluationInput],
    *,
    bin_count: int = 10,
) -> Dict[Tuple[str, str], MetricSummary]:
    """
    One `MetricSummary` per model version.

    Every joined input is passed through, including the unresolved ones:
    `summarise` counts them as targets so `coverage` stays honest, and its own
    `is_scored` filter keeps them out of Brier and log loss. Filtering them here
    would report coverage as 100% of whatever happened to survive.
    """
    return {
        (model_id, model_version): summarise(
            to_prediction_records(group),
            model_id=model_id,
            model_version=model_version,
            bin_count=bin_count,
        )
        for (model_id, model_version), group in sorted(group_by_model(inputs).items())
    }


def evaluate(
    *,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
    month: Optional[str] = None,
    bin_count: int = 10,
) -> Tuple[List[EvaluationInput], JoinReport, Dict[Tuple[str, str], MetricSummary]]:
    """
    One full pass: read both logs, join, grade.

    `month` filters the LEDGER only. Settlements are read in full because a
    prediction made in August is often settled in September, and filtering both
    by the same month would silently drop those - reporting a real result as
    "awaiting settlement".
    """
    predictions = load_records(ledger_dir, month=month)
    settlements = load_settlements(settlement_dir)
    inputs, report = join_for_evaluation(predictions, settlements)
    return inputs, report, summarise_by_model(inputs, bin_count=bin_count)


def _summary_dict(summary: MetricSummary) -> Dict[str, Any]:
    """
    Serialise one model's metrics.

    NO ODDS, NO EDGE, NO STAKE. The keys here are the same shape
    `evaluation_harness` writes and are constrained by the same firewall
    (`tests/regression/test_evaluation_leakage.py` bans the name components
    `odds`, `price`, `edge`, `stake`, `value`, `bookmaker`, `ev`, `roi`,
    `profit` from evaluation artifacts).
    """
    return {
        "model_id": summary.model_id,
        "model_version": summary.model_version,
        "scored": summary.scored,
        "targets": summary.targets,
        "coverage": summary.coverage,
        "brier": summary.brier,
        "log_loss": summary.log_loss,
        "mean_predicted": summary.mean_predicted,
        "observed_rate": summary.observed_rate,
        "accuracy_at_half": summary.accuracy_at_half,
        "unevaluable": summary.unevaluable,
        "calibration": [
            {
                "label": b.label,
                "lower": b.lower,
                "upper": b.upper,
                "count": b.count,
                "mean_predicted": b.mean_predicted,
                "observed_rate": b.observed_rate,
                "gap": b.gap,
            }
            for b in summary.calibration
        ],
    }


def build_report(
    report: JoinReport,
    summaries: Mapping[Tuple[str, str], MetricSummary],
    *,
    generated_at: datetime,
    month: Optional[str] = None,
) -> Dict[str, Any]:
    """
    The full artifact.

    `probability_source` is recorded explicitly. A reader months from now must be
    able to tell a ledger-graded report from a replayed one without reading this
    code, and the distinction is the entire point of the Epic.
    """
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "input_schema_version": EVALUATION_INPUT_SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "month": month,
        "probability_source": "ledger",
        "replay_used": False,
        "join": {
            "key": ["competition", "season", "fixture_id"],
            "predictions": report.predictions,
            "joined": report.joined,
            "scored": report.scored,
            "settled": report.settled,
            "unresolved": report.unresolved,
            "missing_settlement": report.missing_settlement,
            "join_rate": report.join_rate,
            "settlement_coverage": report.settlement_coverage,
            "unjoinable": report.unjoinable,
            "settlement_conflicts": list(report.settlement_conflicts),
        },
        "models": [_summary_dict(summaries[key]) for key in sorted(summaries)],
    }


def write_report(
    payload: Mapping[str, Any],
    *,
    generated_at: datetime,
    evaluation_dir: Path = DEFAULT_EVALUATION_DIR,
) -> Path:
    """
    Write the report as JSON.

    A NEW timestamped file per run, never an overwrite: two evaluations of the
    same ledger at different times are two observations, and `output_{date}.json`
    overwriting in place is the behaviour Epic 2G exists to avoid repeating.
    """
    directory = Path(evaluation_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"evaluation_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _print_report(report: JoinReport, summaries: Mapping[Tuple[str, str], MetricSummary]) -> None:
    print(f"\njoin: {report.summary()}")
    if report.settlement_conflicts:
        print("  CONFLICTING SETTLEMENTS (two different scores for one fixture):")
        for line in report.settlement_conflicts:
            print(f"    {line}")

    if not summaries:
        print("\nNo predictions to evaluate.")
        return

    for (model_id, model_version), summary in sorted(summaries.items()):
        print(f"\n{model_id} {model_version}")
        print(f"  scored      {summary.scored}/{summary.targets}", end="")
        if summary.coverage is not None:
            print(f"  (coverage {summary.coverage:.1%})")
        else:
            print()
        # `is not None` throughout: a Brier of 0.0 is a perfect score, and
        # truthiness would print it as "n/a" (GG-007).
        if summary.brier is not None:
            print(f"  brier       {summary.brier:.4f}")
        if summary.log_loss is not None:
            print(f"  log loss    {summary.log_loss:.4f}")
        if summary.mean_predicted is not None and summary.observed_rate is not None:
            print(
                f"  predicted   {summary.mean_predicted:.3f} vs "
                f"observed {summary.observed_rate:.3f}"
            )
        if summary.brier is None:
            print("  (nothing settled yet - no scoring metrics)")
        if summary.unevaluable:
            for reason, count in sorted(summary.unevaluable.items()):
                print(f"    {reason:<22} {count}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate settled predictions (Epic 2H-3)")
    parser.add_argument("--month", default=None, help="ledger month, e.g. 2026-08 (default: all)")
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    parser.add_argument("--settlement-dir", default=str(DEFAULT_SETTLEMENT_DIR))
    parser.add_argument("--out", default=str(DEFAULT_EVALUATION_DIR))
    parser.add_argument("--bins", type=int, default=10, help="calibration bins (default: 10)")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    generated_at = datetime.now(timezone.utc)
    inputs, report, summaries = evaluate(
        ledger_dir=Path(args.ledger_dir),
        settlement_dir=Path(args.settlement_dir),
        month=args.month,
        bin_count=args.bins,
    )
    _print_report(report, summaries)

    if inputs and not args.dry_run:
        payload = build_report(report, summaries, generated_at=generated_at, month=args.month)
        print(f"\nWrote {write_report(payload, generated_at=generated_at, evaluation_dir=Path(args.out))}")
    elif args.dry_run:
        print("\nDRY RUN: nothing written.")

    # Conflicting settlements mean two sources described one fixture differently.
    # Every metric above inherits that, so the run is reported as failed rather
    # than published as if the disagreement had been resolved.
    if report.settlement_conflicts:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
