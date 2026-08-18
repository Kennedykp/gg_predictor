#!/usr/bin/env python3
"""
The operational entry point: settle, then evaluate, then report (Epic 2H-4).

    data/predictions/*.jsonl   (Epic 2G, read-only here)
            |
            v
    settle_predictions.settle()      <-- Epic 2H-2, unchanged
            |
            v
    data/settlements/*.jsonl   (append-only)
            |
            v
    evaluate_settled.evaluate()      <-- Epic 2H-3, unchanged
            |
            v
    data/evaluation/lifecycle_<ts>.json

WHY A THIRD SCRIPT AND NOT A FLAG ON AN EXISTING ONE
----------------------------------------------------
Settling and grading are separately useful and must stay separately runnable:
settlement needs the network and is retried, grading is pure and cheap. What did
not exist was a single command that does both IN THE RIGHT ORDER and reports the
whole lifecycle, which is what a scheduled job needs. This script adds only the
orchestration and the combined artifact. It re-implements no settlement rule and
no metric.

WHAT IT WILL NOT DO
-------------------
It never generates a prediction. `main.py` writes the ledger; this reads it.
There is deliberately no code path here that could call the model, so a
mis-ordered run cannot silently mint fresh predictions for fixtures whose
results are already known - the one mistake that would poison the ledger beyond
repair.

It never writes to the ledger directory. The ledger digest is recorded before
and after the run and compared; a change aborts with a non-zero exit, so the
guarantee is enforced at runtime, not merely asserted in a docstring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from domain.evaluation import MetricSummary
from domain.evaluation_input import (
    EVALUATION_INPUT_SCHEMA_VERSION,
    EvaluationInput,
    JoinReport,
)
from domain.lifecycle import (
    DEFAULT_SETTLEMENT_GRACE,
    LIFECYCLE_SCHEMA_VERSION,
    LifecycleReport,
    LifecycleRow,
    Stage,
    reconcile,
)
from domain.settlement import SettlementRecord, SettlementStatus

# `_summary_dict` is imported despite the underscore, deliberately. It is the one
# place that decides which metric keys reach an artifact, and that choice is
# constrained by the leakage firewall in
# `tests/regression/test_evaluation_leakage.py` (no `odds`, `edge`, `stake`...).
# A local copy here would be a second place for that list to drift, and the drift
# would be a leak. Reused rather than re-declared.
from evaluate_settled import (
    DEFAULT_EVALUATION_DIR,
    _summary_dict,
    evaluate,
    summarise_by_model,
)
from prediction_ledger import DEFAULT_LEDGER_DIR, load_records
from settle_predictions import (
    DATASET_SOURCE,
    DEFAULT_SETTLEMENT_DIR,
    ESPN_SOURCE,
    ResultSource,
    dataset_result_source,
    espn_result_source,
    load_settlements,
    settle,
)

__all__ = [
    "LIFECYCLE_RUN_SCHEMA_VERSION",
    "EXIT_OK",
    "EXIT_CONFLICT",
    "EXIT_LEDGER_MUTATED",
    "EXIT_BACKLOG",
    "ledger_digest",
    "run",
    "build_operations_report",
    "write_operations_report",
    "main",
]

LIFECYCLE_RUN_SCHEMA_VERSION = "2h4.1"

# Exit codes are distinct because the responses are different: a conflict needs a
# human to read two records, a mutated ledger is an emergency, and a backlog
# needs the settlement job re-run. One generic `1` would collapse all three.
EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_LEDGER_MUTATED = 2
EXIT_BACKLOG = 3


def ledger_digest(ledger_dir: Path = DEFAULT_LEDGER_DIR) -> str:
    """
    A digest over the ledger's raw BYTES, filename included.

    Bytes, not parsed records: re-serialising would normalise key order and
    float formatting, and would therefore report a rewritten file as unchanged -
    hiding exactly the mutation this exists to catch. Filenames are hashed too,
    so deleting a whole month is caught as well as editing a line.
    """
    directory = Path(ledger_dir)
    if not directory.exists():
        return "sha256:empty"

    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.jsonl")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def run(
    *,
    result_source: ResultSource,
    source: str,
    now: datetime,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
    month: Optional[str] = None,
    grace: timedelta = DEFAULT_SETTLEMENT_GRACE,
    bin_count: int = 10,
    settle_first: bool = True,
    dry_run: bool = False,
) -> Tuple[
    List[SettlementRecord],
    List[LifecycleRow],
    LifecycleReport,
    List[EvaluationInput],
    JoinReport,
    Dict[Tuple[str, str], MetricSummary],
]:
    """
    One operational pass. Settlement runs BEFORE evaluation, on purpose.

    The order is the entire value of this script. Grading first would score a
    stale settlement log and report today's finished fixtures as pending, so
    every metric would silently describe yesterday.

    Idempotence is inherited, not re-implemented: `settle()` filters through
    `unsettled()`, which drops predictions already terminally settled. A second
    run therefore asks about nothing and appends nothing. That behaviour lives in
    Epic 2H-2 and is deliberately not duplicated here.
    """
    settled: List[SettlementRecord] = []
    if settle_first:
        settled = settle(
            result_source=result_source,
            source=source,
            settled_at=now,
            ledger_dir=ledger_dir,
            settlement_dir=settlement_dir,
            month=month,
            dry_run=dry_run,
        )

    # Re-read from disk rather than reusing the in-memory `settled` list: the
    # artifact must describe what is actually stored, including lines written by
    # earlier runs. Under --dry-run nothing was appended, so this correctly
    # reports the pre-existing state.
    predictions = load_records(ledger_dir, month=month)
    settlements = load_settlements(settlement_dir)
    rows, lifecycle = reconcile(predictions, settlements, now=now, grace=grace)

    inputs, join_report, _ = evaluate(
        ledger_dir=ledger_dir,
        settlement_dir=settlement_dir,
        month=month,
        bin_count=bin_count,
    )
    summaries = summarise_by_model(inputs, bin_count=bin_count)
    return settled, rows, lifecycle, inputs, join_report, summaries


def _settlement_sources(settlements: Sequence[Mapping[str, Any]]) -> List[str]:
    """Which providers actually produced the stored settlements."""
    seen = {
        str(record.get("source"))
        for record in settlements
        if record.get("source") not in (None, "")
    }
    return sorted(seen)


def build_operations_report(
    lifecycle: LifecycleReport,
    join_report: JoinReport,
    summaries: Mapping[Tuple[str, str], MetricSummary],
    *,
    generated_at: datetime,
    ledger_digest_before: str,
    ledger_digest_after: str,
    settlement_sources: Sequence[str],
    settled_now: int,
    month: Optional[str] = None,
    grace: timedelta = DEFAULT_SETTLEMENT_GRACE,
) -> Dict[str, Any]:
    """
    The combined artifact: lifecycle, then metrics, then proof.

    `lifecycle` and `join` are both present and are NOT the same numbers.
    Lifecycle counts every ledger record by where it is in its life; join counts
    what could be paired with a result and graded. Publishing only one would make
    an operational gap and a model limitation indistinguishable - the specific
    confusion this Epic exists to remove.
    """
    return {
        "schema_version": LIFECYCLE_RUN_SCHEMA_VERSION,
        "lifecycle_schema_version": LIFECYCLE_SCHEMA_VERSION,
        "input_schema_version": EVALUATION_INPUT_SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "month": month,
        # Provenance. `probability_source` is the load-bearing one: it states
        # that every graded number was READ from the ledger, never recomputed.
        "probability_source": "ledger",
        "replay_used": False,
        "settlement_sources": list(settlement_sources),
        "settlement_grace_hours": grace.total_seconds() / 3600.0,
        # Immutability proof, in the artifact rather than only in a test.
        "ledger_digest_before": ledger_digest_before,
        "ledger_digest_after": ledger_digest_after,
        "ledger_unchanged": ledger_digest_before == ledger_digest_after,
        "lifecycle": {
            "key": ["prediction_id"],
            "discovered": lifecycle.discovered,
            "settled": lifecycle.settled,
            "unresolved": lifecycle.unresolved,
            "pending": lifecycle.pending,
            "awaiting_settlement": lifecycle.awaiting_settlement,
            "undated": lifecycle.undated,
            "by_stage": dict(sorted(lifecycle.by_stage.items())),
            "settlement_backlog": lifecycle.settlement_backlog,
            "accounted_for": lifecycle.accounted_for,
            "settled_this_run": settled_now,
            "ledger_conflicts": list(lifecycle.ledger_conflicts),
        },
        "join": {
            "key": ["competition", "season", "fixture_id"],
            "predictions": join_report.predictions,
            "joined": join_report.joined,
            "evaluated": join_report.scored,
            "excluded_from_evaluation": join_report.predictions - join_report.scored,
            "settled": join_report.settled,
            "unresolved": join_report.unresolved,
            "missing_settlement": join_report.missing_settlement,
            "join_rate": join_report.join_rate,
            "settlement_coverage": join_report.settlement_coverage,
            "unjoinable": join_report.unjoinable,
            "settlement_conflicts": list(join_report.settlement_conflicts),
        },
        "models": [_summary_dict(summaries[key]) for key in sorted(summaries)],
    }


def write_operations_report(
    payload: Mapping[str, Any],
    *,
    generated_at: datetime,
    evaluation_dir: Path = DEFAULT_EVALUATION_DIR,
) -> Path:
    """
    Write a NEW timestamped artifact. Never an overwrite.

    Two runs are two observations of pipeline health, and the second is not a
    correction of the first. The filename is prefixed `lifecycle_` so these never
    collide with `evaluate_settled.py`'s `evaluation_` files, which remain
    independently useful.

    If a same-second file somehow exists, a counter is appended rather than
    clobbering it: losing evidence to a filename collision is worse than an
    ugly filename.
    """
    directory = Path(evaluation_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"lifecycle_{stamp}.json"
    counter = 2
    while path.exists():
        path = directory / f"lifecycle_{stamp}_{counter}.json"
        counter += 1
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _print_lifecycle(lifecycle: LifecycleReport, join_report: JoinReport, settled_now: int) -> None:
    print(f"\nsettlement: {settled_now} prediction(s) processed this run")
    print(f"lifecycle:  {lifecycle.summary()}")
    print(f"evaluation: {join_report.summary()}")

    if lifecycle.awaiting_settlement:
        # Worded as a pipeline fault, not a football one: these fixtures have
        # finished and no result was recorded. The percentage is the share of DUE
        # predictions affected, which is what says whether this is a handful of
        # stragglers or the settlement job having not run at all.
        backlog = lifecycle.settlement_backlog
        share = f" ({backlog:.1%} of due predictions)" if backlog is not None else ""
        print(
            f"\nWARNING: {lifecycle.awaiting_settlement} fixture(s) finished more than "
            f"the grace period ago with no settlement record{share}. Re-run settlement."
        )

    for stage in (Stage.SETTLED, Stage.UNRESOLVED):
        count = lifecycle.count(stage)
        if count:
            print(f"  {stage.value:<20} {count}")


def _print_models(summaries: Mapping[Tuple[str, str], MetricSummary]) -> None:
    for key in sorted(summaries):
        summary = summaries[key]
        print(f"\n{summary.model_id} {summary.model_version}")
        print(f"  scored      {summary.scored}/{summary.targets}")
        if summary.brier is not None:
            print(f"  brier       {summary.brier:.4f}")
        if summary.log_loss is not None:
            print(f"  log loss    {summary.log_loss:.4f}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Settle then evaluate stored predictions (Epic 2H-4)"
    )
    parser.add_argument("--month", default=None, help="ledger month, e.g. 2026-08 (default: all)")
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    parser.add_argument("--settlement-dir", default=str(DEFAULT_SETTLEMENT_DIR))
    parser.add_argument("--out", default=str(DEFAULT_EVALUATION_DIR))
    parser.add_argument("--bins", type=int, default=10, help="calibration bins (default: 10)")
    parser.add_argument(
        "--dataset",
        default=None,
        help="settle offline from a local historical dataset instead of ESPN",
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=DEFAULT_SETTLEMENT_GRACE.total_seconds() / 3600.0,
        help="hours after kickoff before a missing result is a fault (default: 3)",
    )
    parser.add_argument(
        "--no-settle",
        action="store_true",
        help="evaluate only; do not contact any result source",
    )
    parser.add_argument(
        "--fail-on-backlog",
        action="store_true",
        help=f"exit {EXIT_BACKLOG} if any due prediction is unsettled",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    if args.dataset:
        result_source: ResultSource = dataset_result_source(Path(args.dataset))
        source = DATASET_SOURCE
    else:
        result_source = espn_result_source
        source = ESPN_SOURCE

    ledger_dir = Path(args.ledger_dir)
    settlement_dir = Path(args.settlement_dir)
    generated_at = datetime.now(timezone.utc)
    grace = timedelta(hours=args.grace_hours)

    digest_before = ledger_digest(ledger_dir)

    settled, _rows, lifecycle, inputs, join_report, summaries = run(
        result_source=result_source,
        source=source,
        now=generated_at,
        ledger_dir=ledger_dir,
        settlement_dir=settlement_dir,
        month=args.month,
        grace=grace,
        bin_count=args.bins,
        settle_first=not args.no_settle,
        dry_run=args.dry_run,
    )

    digest_after = ledger_digest(ledger_dir)

    _print_lifecycle(lifecycle, join_report, len(settled))
    _print_models(summaries)

    settled_count = sum(
        1 for record in settled if record.settlement_status is SettlementStatus.SETTLED
    )
    payload = build_operations_report(
        lifecycle,
        join_report,
        summaries,
        generated_at=generated_at,
        ledger_digest_before=digest_before,
        ledger_digest_after=digest_after,
        settlement_sources=_settlement_sources(load_settlements(settlement_dir)),
        settled_now=settled_count,
        month=args.month,
        grace=grace,
    )

    if args.dry_run:
        print("\nDRY RUN: nothing written.")
    else:
        print(f"\nWrote {write_operations_report(payload, generated_at=generated_at, evaluation_dir=Path(args.out))}")

    # Ordering of the failure checks is deliberate: report first (the artifact is
    # the evidence needed to diagnose any of these), then fail on the most
    # serious finding.
    #
    # A mutated ledger outranks everything. Predictions are the one thing this
    # system cannot reconstruct, so any change under a read-only job is treated
    # as corruption rather than a warning.
    if digest_before != digest_after:
        print(
            f"\nFAIL: the prediction ledger changed during this run.\n"
            f"  before {digest_before}\n  after  {digest_after}",
            file=sys.stderr,
        )
        return EXIT_LEDGER_MUTATED

    # Conflicts are never reconciled automatically. Two records describing one
    # thing differently is a question only a human can answer, and picking one
    # would publish metrics that silently inherit the guess.
    if lifecycle.ledger_conflicts or join_report.settlement_conflicts:
        print("\nFAIL: conflicting records.", file=sys.stderr)
        for message in lifecycle.ledger_conflicts:
            print(f"  ledger:     {message}", file=sys.stderr)
        for message in join_report.settlement_conflicts:
            print(f"  settlement: {message}", file=sys.stderr)
        return EXIT_CONFLICT

    if args.fail_on_backlog and lifecycle.awaiting_settlement:
        print(
            f"\nFAIL: {lifecycle.awaiting_settlement} due prediction(s) unsettled.",
            file=sys.stderr,
        )
        return EXIT_BACKLOG

    if not inputs:
        print("\nNothing to evaluate yet.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
