"""
Settlement job (Epic 2H-2).

    python settle_predictions.py --month 2026-08
    python settle_predictions.py --dataset data/historical      # offline
    python settle_predictions.py --dry-run

Reads the prediction ledger, asks a result source what happened, and appends one
settlement record per prediction to `data/settlements/YYYY-MM.jsonl`.

THREE PROPERTIES THIS JOB GUARANTEES:

  1. It never writes to the ledger. It imports `prediction_ledger` for
     `load_records` only. A prediction is evidence of what was believed before
     kickoff, and a settler that could edit it could rewrite history to agree
     with the result.

  2. It is append-only. Every `open()` here uses mode "a" or "r". A corrected
     settlement is a NEW line, not an overwrite: the fact that we once believed
     otherwise is itself worth keeping.

  3. It never runs the model. It does not import `poisson`, `filters`,
     `decision` or `evaluation_harness`. In particular NOT
     `evaluation_harness.replay()`, which recomputes probabilities from today's
     data — a settlement job that called it would produce a hindsight number
     indistinguishable in the file from the one that was actually published.

Not imported by `main.py`, `analyze_all.py` or `run3/`. Settlement lags
prediction and must never be able to affect it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from domain.historical import HistoricalMatch
from domain.settlement import (
    SettlementRecord,
    SettlementStatus,
    UnresolvedReason,
    candidate_lookup_keys,
    lookup_key,
    settle_one,
    to_json_dict,
)
from prediction_ledger import DEFAULT_LEDGER_DIR, load_records

__all__ = [
    "DEFAULT_SETTLEMENT_DIR",
    "ESPN_SOURCE",
    "DATASET_SOURCE",
    "settlement_filename",
    "settlement_path",
    "load_settlements",
    "latest_by_prediction",
    "unsettled",
    "espn_result_source",
    "dataset_result_source",
    "build_settlements",
    "append_settlements",
    "settle",
    "main",
]

DEFAULT_SETTLEMENT_DIR = Path("data/settlements")

# Named, not inferred. A second provider must be distinguishable from the first
# in the file, and "which source answered" is part of the evidence.
ESPN_SOURCE = "espn/scoreboard"
DATASET_SOURCE = "dataset/historical"

# One league-season readout, plus whether the provider actually answered.
# `None` matches vs an empty list is the distinction `espn.get_league_history`
# already makes load-bearing: "we do not know" is not "there is nothing here".
Readout = Tuple[Optional[List[HistoricalMatch]], bool]
ResultSource = Callable[[str, Optional[int]], Readout]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def settlement_filename(moment: datetime) -> str:
    """`2026-08.jsonl`, from the SETTLEMENT time - not the fixture date."""
    return f"{moment.astimezone(timezone.utc).strftime('%Y-%m')}.jsonl"


def settlement_path(
    moment: datetime,
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
) -> Path:
    return Path(settlement_dir) / settlement_filename(moment)


def load_settlements(
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
    *,
    month: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Read settlements back as raw dicts, in file (append) order.

    Dicts rather than `SettlementRecord`s, for the reason `load_records` gives:
    a reader must be able to load lines written under an older `schema_version`
    without this module refusing them.
    """
    directory = Path(settlement_dir)
    if not directory.exists():
        return []

    pattern = f"{month}.jsonl" if month else "*.jsonl"
    out: List[Dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def latest_by_prediction(settlements: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    """
    Resolve an append-only log to one current view: last line wins per prediction.

    Append order is settlement order, so a later line is a later belief. This is
    what makes correction possible without mutation.
    """
    out: Dict[str, Mapping[str, Any]] = {}
    for record in settlements:
        prediction_id = record.get("prediction_id")
        if prediction_id:
            out[str(prediction_id)] = record
    return out


def _is_terminal(settlement: Optional[Mapping[str, Any]]) -> bool:
    """
    Whether this prediction is finished with settlement forever.

    Settled is terminal. So are CANCELLED and ABANDONED: ESPN never moves a
    replayed fixture's score onto the original event id, so retrying those is
    unbounded work with a guaranteed outcome. POSTPONED is deliberately retried.
    """
    if settlement is None:
        return False
    if settlement.get("settlement_status") == SettlementStatus.SETTLED.value:
        return True
    reason = settlement.get("unresolved_reason")
    return reason in {UnresolvedReason.CANCELLED.value, UnresolvedReason.ABANDONED.value}


def unsettled(
    predictions: Sequence[Mapping[str, Any]],
    settlements: Iterable[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """
    Which predictions still need asking about. Idempotence lives here.

    Filtered by `prediction_id`, never by `fixture_id`: two predictions of one
    fixture are two independent things to settle.
    """
    done = latest_by_prediction(settlements)
    return [
        prediction
        for prediction in predictions
        if prediction.get("prediction_id")
        and not _is_terminal(done.get(str(prediction["prediction_id"])))
    ]


# ---------------------------------------------------------------------------
# Result sources
# ---------------------------------------------------------------------------
def espn_result_source(competition: str, season: Optional[int]) -> Readout:
    """
    Ask ESPN for one league-season of history.

    Imported inside the function so that importing this module never imports a
    network client - the offline path must not pay for the online one.

    A `None` readout is reported as "provider unavailable" rather than "no
    fixtures". `get_league_history` returns `None` for a failed fetch AND for a
    possibly-truncated season (ESPN silently caps the scoreboard at 100 events),
    and a truncated season is unknown, not empty.
    """
    if season is None:
        return None, False
    import espn

    readout = espn.get_league_history(competition, season)
    if readout is None:
        return None, False
    return list(readout.matches), True


def dataset_result_source(dataset_dir: Path) -> ResultSource:
    """
    Serve results from a local historical dataset instead of the network.

    Offline, checksummed and reproducible: the same corpus twice gives the same
    settlements. The whole dataset is loaded once and indexed by
    `(competition, season)`, so a per-league-season miss is a genuine "not in the
    corpus" rather than a re-read.
    """
    from historical_dataset import load_dataset

    matches = load_dataset(Path(dataset_dir))
    index: Dict[Tuple[str, int], List[HistoricalMatch]] = {}
    for match in matches:
        index.setdefault((match.competition, match.season), []).append(match)

    def source(competition: str, season: Optional[int]) -> Readout:
        if season is None:
            return None, False
        # A dataset on disk is always "available"; absence is a real answer.
        return index.get((competition, season), []), True

    return source


# ---------------------------------------------------------------------------
# The settlement pass
# ---------------------------------------------------------------------------
def _group_by_league_season(
    predictions: Iterable[Mapping[str, Any]],
) -> Dict[Tuple[str, Optional[int]], List[Mapping[str, Any]]]:
    """One fetch per league-season, not one per fixture."""
    groups: Dict[Tuple[str, Optional[int]], List[Mapping[str, Any]]] = {}
    for prediction in predictions:
        competition = prediction.get("competition")
        if not competition:
            continue
        season = prediction.get("season")
        groups.setdefault((str(competition), season), []).append(prediction)
    return groups


def build_settlements(
    predictions: Sequence[Mapping[str, Any]],
    *,
    result_source: ResultSource,
    settled_at: datetime,
    source: str,
) -> List[SettlementRecord]:
    """
    Turn predictions into settlement records. One record per prediction, always.

    An unresolved prediction is RECORDED, not skipped. A skipped prediction is
    indistinguishable from one that was never attempted, and that difference is
    exactly what tells an operator whether the job is working.

    The season-mismatch tolerance (2H-F3) is applied here, at the lookup
    boundary only: `candidate_lookup_keys` widens the question to the adjacent
    season, and `matched_season` on the record says which one answered. Nothing
    in the ledger is touched.
    """
    records: List[SettlementRecord] = []

    for (competition, season), group in _group_by_league_season(predictions).items():
        matches, available = result_source(competition, season)

        # Widen to the adjacent seasons only if the requested one was fetchable
        # but some fixture was not in it. Widening after an outage would
        # attribute the outage to the wrong season.
        index: Dict[Tuple[str, Optional[int], str], HistoricalMatch] = {}
        if available and matches is not None:
            for match in matches:
                index[lookup_key(match.competition, match.season, match.event_id)] = match

            # ANY missing fixture triggers the retry, not all of them. The 2H-F3
            # case is a matchday straddling the 1 July rollover, where some
            # fixtures are filed under the stored season and some under the next.
            # An "all missing" guard would skip exactly that mixed group and
            # report FIXTURE_NOT_FOUND for the half that drifted.
            missing = {
                str(p.get("fixture_id"))
                for p in group
                if lookup_key(competition, season, str(p.get("fixture_id"))) not in index
            }
            if season is not None and missing:
                # Bounded: two extra readouts per league-season per run, never
                # per fixture. Stops as soon as everything is accounted for.
                for adjacent in (season + 1, season - 1):
                    if not missing:
                        break
                    extra, extra_available = result_source(competition, adjacent)
                    if not extra_available or extra is None:
                        continue
                    for match in extra:
                        index.setdefault(
                            lookup_key(match.competition, match.season, match.event_id),
                            match,
                        )
                    missing = {
                        fixture_id
                        for fixture_id in missing
                        if lookup_key(competition, adjacent, fixture_id) not in index
                    }

        for prediction in group:
            found: Optional[HistoricalMatch] = None
            if available:
                for key in candidate_lookup_keys(
                    competition, season, str(prediction.get("fixture_id"))
                ):
                    found = index.get(key)
                    if found is not None:
                        break

            records.append(
                settle_one(
                    prediction,
                    found,
                    settled_at=settled_at,
                    source=source,
                    provider_available=available,
                )
            )

    return records


def append_settlements(
    records: Sequence[SettlementRecord],
    *,
    settled_at: datetime,
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
) -> Optional[Path]:
    """
    Append settlement records as JSONL. Never truncates, never rewrites.

    A trailing newline per line: without it the next append lands on the same
    line and corrupts both records - the classic JSONL append bug.
    """
    if not records:
        return None

    directory = Path(settlement_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / settlement_filename(settled_at)

    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(to_json_dict(record), separators=(",", ":")) + "\n")
    return path


def settle(
    *,
    result_source: ResultSource,
    source: str,
    settled_at: datetime,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    settlement_dir: Path = DEFAULT_SETTLEMENT_DIR,
    month: Optional[str] = None,
    dry_run: bool = False,
) -> List[SettlementRecord]:
    """One full pass: read, ask, record. Returns what was built."""
    predictions = load_records(ledger_dir, month=month)
    existing = load_settlements(settlement_dir)
    todo = unsettled(predictions, existing)

    records = build_settlements(
        todo,
        result_source=result_source,
        settled_at=settled_at,
        source=source,
    )
    if not dry_run:
        append_settlements(records, settled_at=settled_at, settlement_dir=settlement_dir)
    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _report(records: Sequence[SettlementRecord]) -> None:
    """
    Counts by status and reason, then by league-season.

    The per-league-season breakdown is not decoration. If a season key is wrong,
    EVERY fixture in that league-season reports FIXTURE_NOT_FOUND - which looks
    exactly like a provider gap while actually being a join bug. Printing the
    grouping is what makes the two distinguishable at a glance.
    """
    if not records:
        print("Nothing to settle: no unsettled predictions found.")
        return

    settled = [r for r in records if r.settlement_status is SettlementStatus.SETTLED]
    print(f"\n{len(records)} prediction(s) processed, {len(settled)} settled.")

    reasons = Counter(
        r.unresolved_reason.value for r in records if r.unresolved_reason is not None
    )
    if reasons:
        print("  unresolved:")
        for reason, count in sorted(reasons.items()):
            print(f"    {reason:<22} {count}")

    print("  by league-season:")
    per_group: Dict[Tuple[str, Optional[int]], Counter] = {}
    for record in records:
        key = (record.competition, record.season)
        per_group.setdefault(key, Counter())[record.settlement_status.value] += 1
    for (competition, season), counts in sorted(
        per_group.items(), key=lambda item: (item[0][0], item[0][1] or 0)
    ):
        total = sum(counts.values())
        ok = counts.get(SettlementStatus.SETTLED.value, 0)
        flag = "  <- 0 settled: check the season key" if ok == 0 else ""
        print(f"    {competition} {season}: {ok}/{total} settled{flag}")

    drifted = [r for r in records if r.matched_season is not None and r.matched_season != r.season]
    if drifted:
        print(
            f"  NOTE {len(drifted)} fixture(s) matched an adjacent season "
            "(ledger season disagrees with the provider - see 2H-F3)."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Settle stored predictions (Epic 2H-2)")
    parser.add_argument("--month", default=None, help="ledger month, e.g. 2026-08 (default: all)")
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    parser.add_argument("--out", default=str(DEFAULT_SETTLEMENT_DIR))
    parser.add_argument(
        "--dataset",
        default=None,
        help="settle offline from a local historical dataset instead of ESPN",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    if args.dataset:
        result_source: ResultSource = dataset_result_source(Path(args.dataset))
        source = DATASET_SOURCE
    else:
        result_source = espn_result_source
        source = ESPN_SOURCE

    records = settle(
        result_source=result_source,
        source=source,
        settled_at=datetime.now(timezone.utc),
        ledger_dir=Path(args.ledger_dir),
        settlement_dir=Path(args.out),
        month=args.month,
        dry_run=args.dry_run,
    )

    _report(records)
    if records and not args.dry_run:
        print(f"\nAppended to {Path(args.out) / settlement_filename(records[0].settled_at)}")
    elif args.dry_run:
        print("\nDRY RUN: nothing written.")

    # A pass where the provider never answered is not a successful pass.
    if records and all(
        r.unresolved_reason is UnresolvedReason.PROVIDER_UNAVAILABLE for r in records
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
