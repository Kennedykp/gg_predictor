"""
Epic 2H-5 — the reporting entry point.

READ-ONLY BY CONSTRUCTION
-------------------------
This script settles nothing, fetches nothing and writes nothing except its own
artifact. It is the tool you reach for when a metric looks wrong and you want to
know *where*, on data that has already been settled.

That separation is the point. `run_lifecycle.py` (2H-4) contacts a provider and
appends settlements; it is an operational job with side effects. Reporting is a
question, asked repeatedly, often at a laptop, and it must be safe to run at any
time — including while a settlement job is running. So it re-reads the same
ledger and settlement directories through the frozen `evaluate()` and cannot
write to either.

WHAT IT ADDS OVER 2H-3
----------------------
`evaluate_settled.py` answers "how good is the model". This answers "where".
Same inputs, same frozen metrics, regrouped along dimensions the ledger already
records. No metric is computed here — `_summary_dict` and `summarise` are reused
from the modules that own them.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from domain.evaluation import EVALUATION_SCHEMA_VERSION
from domain.evaluation_input import EVALUATION_INPUT_SCHEMA_VERSION, EvaluationInput, JoinReport
from domain.quality import (
    MINIMUM_REPORTABLE_N,
    QUALITY_SCHEMA_VERSION,
    QualityAssessment,
    ReportStatus,
)
from domain.reporting import (
    REPORTING_SCHEMA_VERSION,
    Dimension,
    Group,
    summarise_dimensions,
)

# `_summary_dict` is imported despite the underscore, deliberately — the same
# decision `run_lifecycle.py` documents. It is the single place that decides which
# metric keys reach an artifact, and that choice is constrained by the leakage
# firewall in `tests/regression/test_evaluation_leakage.py`. A local copy would be
# a second place for that list to drift, and the drift would be a leak.
from evaluate_settled import DEFAULT_EVALUATION_DIR, _summary_dict, evaluate
from prediction_ledger import DEFAULT_LEDGER_DIR
from settle_predictions import DEFAULT_SETTLEMENT_DIR

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "EXIT_OK",
    "EXIT_CONFLICT",
    "EXIT_NO_DATA",
    "DEFAULT_DIMENSIONS",
    "build_report",
    "write_report",
    "report",
    "main",
]

# 2j.1 (Epic 2J): every breakdown row now carries a `quality` block. Bumped in
# step with `REPORTING_SCHEMA_VERSION` so a reader cannot encounter an artifact
# whose two version stamps disagree about whether the quality gate was applied.
REPORT_SCHEMA_VERSION = "2j.1"

EXIT_OK = 0

# A contradiction in the settlement data. Same policy as 2H-4: a report built on
# two different scores for one fixture is worse than no report, because it looks
# authoritative and is not reproducible.
EXIT_CONFLICT = 1

# Opt-in via `--fail-on-empty`. Silence is normal before the first settled
# matchday, so an empty report is a success by default; a scheduled job that
# expects data can ask for the stricter behaviour.
EXIT_NO_DATA = 2

# `overall` first so the headline is at the top, then progressively finer cuts.
# `competition_season` is not a default: on a mature ledger it is the widest
# breakdown by row count and mostly noise until a season has accumulated enough
# settled fixtures to say anything.
DEFAULT_DIMENSIONS: Tuple[Dimension, ...] = (
    Dimension.OVERALL,
    Dimension.MODEL,
    Dimension.COMPETITION,
    Dimension.SEASON,
)


def _quality_dict(quality: QualityAssessment) -> Dict[str, Any]:
    """
    The quality block: the evidence, and whether it licenses a claim.

    `auc` and `auc_undefined_reason` are BOTH emitted, and exactly one of them is
    ever non-null. A consumer that finds `auc: null` therefore always has the
    reason next to it, which is what stops the absence being filled in with 0.5
    downstream — the specific failure 2G-R5 forbids.

    `brier` is repeated here even though it is already in `metrics`. That is
    deliberate duplication of a VALUE, not of a computation: it is read straight
    off the same `MetricSummary`. The point is that the number and its baseline
    cannot be separated by a consumer that reads only one block, which is how a
    bare Brier escaped in the first place.
    """
    return {
        "status": quality.status.value,
        "is_quality_claim": quality.is_quality_claim,
        "headline": quality.headline,
        "scored": quality.scored,
        "minimum_n": quality.minimum_n,
        "shortfall": quality.shortfall,
        "positives": quality.positives,
        "negatives": quality.negatives,
        "brier": quality.summary.brier,
        "auc": quality.auc,
        "auc_undefined_reason": quality.auc_undefined_reason,
        "constant_predictor_brier": quality.constant_brier,
        "brier_delta_vs_baseline": quality.brier_delta,
        "baseline_verdict": quality.baseline_verdict.value,
    }


def _group_dict(group: Group) -> Dict[str, Any]:
    """
    One breakdown row as JSON.

    `counts` sits beside `metrics` rather than inside it: the counts describe the
    lifecycle (how much evidence exists, and what is still missing), the metrics
    describe probability quality. Merging them invites reading `missing` as a
    model property when it is an operational one.

    `quality` (Epic 2J) is a third sibling for the same reason: it describes what
    may be CLAIMED about the metrics. `metrics` is retained in full even when the
    gate refuses the claim — the raw evidence stays auditable, only the licence to
    conclude from it is withheld.
    """
    return {
        "dimension": group.dimension.value,
        "key": list(group.key),
        "label": group.label,
        "reportable": group.is_reportable,
        "counts": {
            "total": group.counts.total,
            "settled": group.counts.settled,
            "unresolved": group.counts.unresolved,
            "missing": group.counts.missing,
            "accounted_for": group.counts.accounted_for,
        },
        "quality": _quality_dict(group.quality),
        "metrics": _summary_dict(group.summary),
    }


def build_report(
    inputs: Sequence[EvaluationInput],
    join: JoinReport,
    breakdowns: Mapping[Dimension, List[Group]],
    *,
    generated_at: datetime,
    ledger_dir: Path,
    settlement_dir: Path,
    month: Optional[str],
    bin_count: int,
) -> Dict[str, Any]:
    """
    Assemble the artifact.

    `generated_at` is injected, never read from the clock here, so a caller can
    produce a byte-identical report for a fixed instant. It is the ONLY
    intentionally time-dependent field, and it is documented as such.

    Every schema version in play is stamped. A report is evidence read months
    later, and "which contract produced this" is not recoverable afterwards.
    """
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "reporting_schema_version": REPORTING_SCHEMA_VERSION,
        "quality_schema_version": QUALITY_SCHEMA_VERSION,
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_input_schema_version": EVALUATION_INPUT_SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        # Restates 2H-4's guarantee in the artifact itself: these numbers grade
        # the probability that was published, not one recomputed today.
        "probability_source": "ledger",
        "replay_used": False,
        "inputs": {
            "ledger_dir": str(ledger_dir),
            "settlement_dir": str(settlement_dir),
            "month": month,
            "bin_count": bin_count,
            "minimum_reportable_n": MINIMUM_REPORTABLE_N,
        },
        "join": {
            "key": ["competition", "season", "fixture_id"],
            "predictions": join.predictions,
            "joined": join.joined,
            "evaluated": join.scored,
            "excluded_from_evaluation": join.predictions - join.scored,
            "settled": join.settled,
            "unresolved": join.unresolved,
            "missing_settlement": join.missing_settlement,
            "join_rate": join.join_rate,
            "settlement_coverage": join.settlement_coverage,
            "unjoinable": dict(sorted(join.unjoinable.items())),
            "settlement_conflicts": list(join.settlement_conflicts),
        },
        "dimensions": [dimension.value for dimension in breakdowns],
        "breakdowns": {
            dimension.value: [_group_dict(group) for group in groups]
            for dimension, groups in breakdowns.items()
        },
    }


def write_report(
    payload: Mapping[str, Any],
    *,
    generated_at: datetime,
    evaluation_dir: Path = DEFAULT_EVALUATION_DIR,
) -> Path:
    """
    Write `report_<timestamp>.json`, never overwriting.

    The `report_` prefix keeps these distinct from `evaluate_settled.py`'s output
    and 2H-4's `lifecycle_` artifacts, so three tools can share one directory
    without one silently clobbering another's history.

    A counter suffix is appended if the timestamp collides, which happens when two
    reports are generated inside the same second — usually a script loop over
    several months. Overwriting there would lose whichever finished first.
    """
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = evaluation_dir / f"report_{stamp}.json"
    counter = 2
    while path.exists():
        path = evaluation_dir / f"report_{stamp}_{counter}.json"
        counter += 1
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def report(
    *,
    dimensions: Sequence[Dimension] = DEFAULT_DIMENSIONS,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
    month: Optional[str] = None,
    bin_count: int = 10,
) -> Tuple[List[EvaluationInput], JoinReport, Mapping[Dimension, List[Group]]]:
    """
    Load, join and regroup. No settlement, no network, no writes.

    `evaluate()` is reused wholesale rather than reimplemented: it owns the join
    and the conflict detection, and a second loader here would be a second place
    for the join key to drift out of step.
    """
    inputs, join, _summaries = evaluate(
        ledger_dir=ledger_dir,
        settlement_dir=settlement_dir,
        month=month,
        bin_count=bin_count,
    )
    breakdowns = summarise_dimensions(inputs, dimensions, bin_count=bin_count)
    return inputs, join, breakdowns


def _format(value: Optional[float], places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _baseline_line(quality: QualityAssessment) -> str:
    """
    The baseline comparison, with its direction spelled out in words.

    A signed delta on its own is ambiguous to a reader who does not remember that
    lower Brier is better, and the consequence of misreading it is believing a
    model that is losing to a constant. So the sign and the word always travel
    together.
    """
    if quality.constant_brier is None:
        return "baseline n/a"
    delta = quality.brier_delta
    if delta is None:
        return f"baseline {_format(quality.constant_brier)}"
    return (
        f"baseline {_format(quality.constant_brier)} "
        f"(delta {delta:+.4f} — {quality.baseline_verdict.value} than baseline)"
    )


def _print(join: JoinReport, breakdowns: Mapping[Dimension, List[Group]]) -> None:
    """
    Console rendering.

    Unreportable groups are printed with their counts and no metrics rather than
    hidden: "this competition has 12 predictions and nothing settled yet" is
    exactly the operational signal worth surfacing, and omitting the row would
    make an unsettled competition look like one that does not exist.

    EPIC 2J. A Brier score is never printed on a line by itself. Every row that
    carries one also carries, on the same row or the continuation beneath it, the
    sample size, the AUC (or the reason there is none) and the constant-predictor
    baseline. Where the gate refuses the claim the metrics are still shown, under
    an explicit INSUFFICIENT_SAMPLE marker — the reader sees the evidence and is
    told plainly what it does not establish.
    """
    print(
        f"join: {join.joined}/{join.predictions} joined, "
        f"{join.scored} evaluated, {join.unresolved} unresolved, "
        f"{join.missing_settlement} awaiting settlement"
    )
    print(f"minimum reportable n: {MINIMUM_REPORTABLE_N} scored observations per group")
    for dimension, groups in breakdowns.items():
        print(f"\n{dimension.value}")
        if not groups:
            print("  (nothing to report)")
            continue
        for group in groups:
            counts = group.counts
            quality = group.quality

            if quality.status is ReportStatus.NOT_MEASURABLE:
                print(
                    f"  {group.label:<28} scored {0:>4}/{counts.total:<4} "
                    f"not yet measurable "
                    f"({counts.unresolved} unresolved, {counts.missing} awaiting)"
                )
                continue

            # The metric line. Identical in shape whether or not the claim is
            # licensed, because suppressing the numbers below the threshold would
            # hide the evidence rather than qualify it. The qualification is the
            # marker that follows.
            marker = "" if quality.is_quality_claim else f"  [{quality.status.value}]"
            print(
                f"  {group.label:<28} scored {quality.scored:>4}/{counts.total:<4} "
                f"brier {_format(quality.summary.brier)}  "
                f"log loss {_format(quality.summary.log_loss)}{marker}"
            )

            # AUC never appears as a bare `n/a`: when it is undefined the reason
            # is printed in its place, so no reader can mistake absence for 0.5.
            auc_text = (
                f"auc {_format(quality.auc)}"
                if quality.auc is not None
                else f"auc undefined — {quality.auc_undefined_reason}"
            )
            print(
                f"  {'':<28} {auc_text}  "
                f"({quality.positives} YES / {quality.negatives} NO)  "
                f"{_baseline_line(quality)}"
            )

            if not quality.is_quality_claim:
                print(f"  {'':<28} {quality.headline}")


def _parse_dimensions(values: Optional[Sequence[str]]) -> Tuple[Dimension, ...]:
    if not values:
        return DEFAULT_DIMENSIONS
    return tuple(Dimension(value) for value in values)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report settled-prediction evaluation, broken down by stored dimensions.",
    )
    parser.add_argument("--month", default=None, help="ledger month, e.g. 2026-08 (default: all)")
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    parser.add_argument("--settlement-dir", default=str(DEFAULT_SETTLEMENT_DIR))
    parser.add_argument("--out", default=str(DEFAULT_EVALUATION_DIR))
    parser.add_argument("--bins", type=int, default=10, help="calibration bins (default: 10)")
    parser.add_argument(
        "--dimension",
        action="append",
        choices=[d.value for d in Dimension],
        help="breakdown axis; repeatable (default: overall, model, competition, season)",
    )
    parser.add_argument(
        "--fail-on-empty",
        action="store_true",
        help=f"exit {EXIT_NO_DATA} if no prediction was evaluated",
    )
    parser.add_argument("--dry-run", action="store_true", help="print without writing")
    args = parser.parse_args(argv)

    # The single clock read for the whole run, as in 2H-4. Two reads could stamp
    # the artifact with one instant and name the file with another.
    generated_at = datetime.now(timezone.utc)

    inputs, join, breakdowns = report(
        dimensions=_parse_dimensions(args.dimension),
        ledger_dir=Path(args.ledger_dir),
        settlement_dir=Path(args.settlement_dir),
        month=args.month,
        bin_count=args.bins,
    )

    # Refuse to publish a report built on contradictory settlements. Reported
    # before any artifact is written, so a conflicting run leaves no misleading
    # file behind for someone to find later and trust.
    if join.settlement_conflicts:
        print(
            f"settlement conflicts on {len(join.settlement_conflicts)} fixture(s); "
            f"refusing to report",
            file=sys.stderr,
        )
        for conflict in join.settlement_conflicts:
            print(f"  {conflict}", file=sys.stderr)
        return EXIT_CONFLICT

    _print(join, breakdowns)

    payload = build_report(
        inputs,
        join,
        breakdowns,
        generated_at=generated_at,
        ledger_dir=Path(args.ledger_dir),
        settlement_dir=Path(args.settlement_dir),
        month=args.month,
        bin_count=args.bins,
    )

    if args.dry_run:
        print("\nDRY RUN: nothing written.")
    else:
        path = write_report(payload, generated_at=generated_at, evaluation_dir=Path(args.out))
        print(f"\nWrote {path}")

    if args.fail_on_empty and join.scored == 0:
        print("no prediction was evaluated", file=sys.stderr)
        return EXIT_NO_DATA
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
