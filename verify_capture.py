"""
Epic 2I — `verify_capture.py`: did we actually record what we predicted?

Strictly read-only. It reads a fixture schedule and the prediction ledger,
reconciles one against the other, prints the result and writes an audit artifact.
It writes nothing to the ledger, settles nothing, evaluates nothing and computes
no probability. The ledger's SHA-256 is taken before and after and compared, so
"read-only" is verified on every run rather than asserted in a docstring.

WHY THIS IS A SEPARATE COMMAND

`run_lifecycle.py` answers "is the pipeline healthy right now" and it is allowed
to fetch and to settle. It cannot answer this question, because it starts from the
LEDGER: an absent ledger yields `[]`, no check fires, and it exits 0 reporting
"Nothing to evaluate yet." Detecting a capture gap requires starting from the
SCHEDULE — the one input the ledger cannot supply — and that is a different data
flow, not a flag on an existing one.

THE FALSE-ALARM RULE

A schedule that could not be loaded is NOT a capture gap. If ESPN is down, or a
dataset lacks a competition-season, the honest answer is "unknown", and reporting
it as missing evidence would blame our pipeline for someone else's outage. Days
whose schedule is unavailable are counted, named in the artifact, and excluded
from gap detection entirely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from domain.capture_audit import (
    CAPTURE_AUDIT_SCHEMA_VERSION,
    CaptureAudit,
    DayAudit,
    ExpectedFixture,
    expected_from_matches,
    reconcile,
)
from prediction_ledger import DEFAULT_LEDGER_DIR, load_records

VERIFY_SCHEMA_VERSION = "2i.1"

DEFAULT_AUDIT_DIR = Path("data/evaluation")

# Named sources, so an artifact read months later says where its schedule came
# from. A number derived from a cached corpus and one derived from a live fetch
# are not interchangeable, and the artifact must not be ambiguous about which.
LIVE_SOURCE = "espn"
DATASET_SOURCE = "dataset"

EXIT_OK = 0
# A capture gap was found AND --fail-on-gap was given. Never returned otherwise:
# this tool is an observer by default, and an audit that fails a scheduled job on
# a football-side quirk is one that gets removed from the schedule.
EXIT_GAP = 1
# The ledger changed while we were reading it. Nothing here writes, so this means
# another process was writing concurrently — most likely a prediction run. The
# audit is reported but its numbers describe a moving target.
EXIT_LEDGER_MUTATED = 2
# No schedule could be loaded for any requested date. Not a gap: an unknown.
EXIT_NO_SCHEDULE = 3

# (fixtures, schedule_available) for one date.
ScheduleSource = Callable[[date], Tuple[List[ExpectedFixture], bool]]


def ledger_digest(ledger_dir: Path = DEFAULT_LEDGER_DIR) -> str:
    """
    SHA-256 over the ledger's raw bytes, filenames included.

    Bytes rather than parsed records: re-serialising would normalise key order and
    float formatting, so a rewritten file would hash as unchanged — hiding exactly
    the mutation this exists to catch. Filenames are hashed too, so deleting a
    whole month is caught as well as editing a line.

    Deliberately duplicated from `run_lifecycle.ledger_digest` rather than
    imported: importing that module would pull settlement and evaluation into the
    verifier's import graph and break the isolation this Epic guarantees. Six
    lines of hashing is a smaller cost than that dependency.
    """
    directory = Path(ledger_dir)
    if not directory.exists():
        return "sha256:empty"

    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.jsonl")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def dataset_schedule_source(dataset_dir: Path) -> ScheduleSource:
    """
    Serve the schedule from a local historical dataset. Offline and reproducible.

    The corpus is loaded once and indexed by UTC kickoff date, so a date with no
    fixtures is a genuine "nothing scheduled" rather than a re-read. A dataset on
    disk is always *available*: absence of a date is a real answer, not an outage,
    which is what makes offline runs safe to gate on.
    """
    from historical_dataset import load_dataset

    index: Dict[date, List[ExpectedFixture]] = {}
    for fixture in expected_from_matches(load_dataset(Path(dataset_dir))):
        key = fixture.kickoff_date
        if key is None:
            continue
        index.setdefault(key, []).append(fixture)

    def source(day: date) -> Tuple[List[ExpectedFixture], bool]:
        return index.get(day, []), True

    return source


def espn_schedule_source() -> ScheduleSource:
    """
    Serve the schedule from ESPN, one date at a time.

    Imported lazily so that `--dataset` runs — and the entire test suite — never
    import a provider module at all. `get_fixtures` returns `[]` both for "no
    fixtures today" and for a failed fetch, and those must not be conflated: an
    empty list is therefore reported as UNAVAILABLE rather than as an empty
    schedule. That is the conservative direction. Calling a real empty day
    "unknown" costs one uninformative row; calling an outage "no fixtures" would
    let a genuine capture gap pass as a quiet day.
    """
    import espn

    def source(day: date) -> Tuple[List[ExpectedFixture], bool]:
        raw = espn.get_fixtures(day)
        if not raw:
            return [], False
        fixtures: List[ExpectedFixture] = []
        for item in raw:
            fixture_id = item.get("fixture_id")
            if not fixture_id:
                continue
            fixtures.append(
                ExpectedFixture(
                    fixture_id=str(fixture_id),
                    competition=item.get("league_id"),
                    season=None,
                    kickoff=item.get("kickoff_utc"),
                    status=item.get("status"),
                )
            )
        return fixtures, True

    return source


def month_days(month: str) -> List[date]:
    """Every calendar date in `YYYY-MM`."""
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    year, mon = (int(part) for part in month.split("-"))
    if not 1 <= mon <= 12:
        raise ValueError(f"month out of range: {month!r}")
    start = date(year, mon, 1)
    end = date(year + (mon == 12), (mon % 12) + 1, 1)
    return [start + timedelta(days=offset) for offset in range((end - start).days)]


def since_days(spec: str, today: date) -> List[date]:
    """
    The last N days, `7d` style, ending yesterday.

    Today is excluded: fixtures later today have not kicked off, so a prediction
    for them may legitimately not exist yet and would be reported as a gap that
    resolves itself within hours.
    """
    match = re.fullmatch(r"(\d+)d", spec.strip().lower())
    if not match:
        raise ValueError(f"--since must look like '7d', got {spec!r}")
    count = int(match.group(1))
    if count < 1:
        raise ValueError("--since must be at least 1 day")
    return [today - timedelta(days=offset) for offset in range(count, 0, -1)]


def collect_schedule(
    days: Sequence[date],
    source: ScheduleSource,
) -> Tuple[List[ExpectedFixture], List[date], List[date]]:
    """Fetch each date, separating dates that answered from dates that could not."""
    fixtures: List[ExpectedFixture] = []
    resolved: List[date] = []
    unavailable: List[date] = []
    for day in days:
        found, available = source(day)
        if not available:
            unavailable.append(day)
            continue
        resolved.append(day)
        fixtures.extend(found)
    return fixtures, resolved, unavailable


def _day_dict(day: DayAudit) -> Dict[str, Any]:
    return {
        "day": day.day.isoformat() if day.day else None,
        "verdict": day.verdict.value,
        "is_capture_gap": day.is_gap,
        "counts": {
            "expected": day.expected,
            "playable": day.playable,
            "captured": day.captured,
            "unaccounted": day.unaccounted,
            "not_playable": day.not_playable,
            "duplicates": day.duplicates,
            "accounted_for": day.accounted_for,
        },
        "fixtures": [
            {
                "fixture_id": row.fixture_id,
                "outcome": row.outcome.value,
                "competition": row.competition,
                "season": row.season,
                "predictions": len(row.prediction_ids),
            }
            for row in day.rows
        ],
    }


def build_payload(
    audit: CaptureAudit,
    *,
    generated_at: datetime,
    schedule_source: str,
    requested: Sequence[date],
    resolved: Sequence[date],
    unavailable: Sequence[date],
    ledger_dir: Path,
    dataset: Optional[Path],
    digest_before: str,
    digest_after: str,
) -> Dict[str, Any]:
    """
    Assemble the artifact.

    `generated_at` is the only time-dependent field, and it is passed in rather
    than read here so the whole payload is a pure function of its inputs and can
    be diffed between two runs.
    """
    return {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "capture_audit_schema_version": CAPTURE_AUDIT_SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "schedule_source": schedule_source,
        "probability_source": None,
        "replay_used": False,
        "inputs": {
            "ledger_dir": str(ledger_dir),
            "dataset": str(dataset) if dataset else None,
            "days_requested": [day.isoformat() for day in requested],
            "days_resolved": [day.isoformat() for day in resolved],
            "days_schedule_unavailable": [day.isoformat() for day in unavailable],
        },
        "ledger_integrity": {
            "digest_before": digest_before,
            "digest_after": digest_after,
            "unchanged": digest_before == digest_after,
        },
        "totals": {
            "days": len(audit.days),
            "expected": audit.expected,
            "captured": audit.captured,
            "unaccounted": audit.unaccounted,
            "not_playable": audit.not_playable,
            "duplicates": audit.duplicates,
            "off_schedule_records": audit.unknown_fixture_records,
            "capture_gaps": len(audit.gap_days),
            "undated_expected": list(audit.undated_expected),
        },
        "days": [_day_dict(day) for day in audit.days],
    }


def write_audit(
    payload: Mapping[str, Any],
    *,
    generated_at: datetime,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
) -> Path:
    """
    Write `capture_<timestamp>.json`, never overwriting.

    The `capture_` prefix keeps these distinct from `evaluate_settled.py`'s files,
    2H-4's `lifecycle_` files and 2H-5's `report_` files, so four tools can share
    one directory without clobbering each other's history.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = audit_dir / f"capture_{stamp}.json"
    counter = 2
    while path.exists():
        path = audit_dir / f"capture_{stamp}_{counter}.json"
        counter += 1
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def render(payload: Mapping[str, Any]) -> str:
    """A short human summary. The artifact carries the detail."""
    totals = payload["totals"]
    inputs = payload["inputs"]
    lines = [
        f"capture audit  schedule={payload['schedule_source']}  days={totals['days']}",
        (
            f"  expected {totals['expected']}"
            f"   captured {totals['captured']}"
            f"   unaccounted {totals['unaccounted']}"
            f"   not-playable {totals['not_playable']}"
        ),
    ]
    if inputs["days_schedule_unavailable"]:
        count = len(inputs["days_schedule_unavailable"])
        lines.append(f"  schedule unavailable for {count} day(s) - not counted as gaps")
    if totals["off_schedule_records"]:
        lines.append(
            f"  {totals['off_schedule_records']} ledger record(s) for fixtures not on the schedule"
        )
    for day in payload["days"]:
        if day["verdict"] == "NO_FIXTURES":
            continue
        marker = "  GAP " if day["is_capture_gap"] else "      "
        counts = day["counts"]
        lines.append(
            f"{marker}{day['day']}  {day['verdict']:<21}"
            f" captured {counts['captured']}/{counts['playable']}"
        )
    if totals["capture_gaps"]:
        lines.append(
            f"\n  {totals['capture_gaps']} capture gap(s): fixtures existed and NO prediction"
            " was recorded. Those predictions cannot be reconstructed."
        )
    else:
        lines.append("\n  no capture gap detected")
    return "\n".join(lines)


def _requested_days(args: argparse.Namespace, today: date) -> List[date]:
    if args.date:
        return [datetime.strptime(args.date, "%Y-%m-%d").date()]
    if args.month:
        return month_days(args.month)
    if args.since:
        return since_days(args.since, today)
    return [today - timedelta(days=1)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile the fixture schedule against the prediction ledger.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", default=None, help="a single date, YYYY-MM-DD")
    group.add_argument("--month", default=None, help="every date in YYYY-MM")
    group.add_argument("--since", default=None, help="the last N days, e.g. 7d")
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    parser.add_argument("--out", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument(
        "--dataset",
        default=None,
        help="read the schedule from a local historical dataset instead of ESPN",
    )
    parser.add_argument(
        "--fail-on-gap",
        action="store_true",
        help="exit non-zero when a capture gap is found",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    # The clock is read ONCE. Two reads could name the artifact with one instant
    # and stamp its contents with another, and a --since window could straddle a
    # midnight boundary mid-run.
    now = datetime.now(timezone.utc)

    try:
        requested = _requested_days(args, now.date())
    except ValueError as exc:
        parser.error(str(exc))

    ledger_dir = Path(args.ledger_dir)
    dataset = Path(args.dataset) if args.dataset else None
    source = dataset_schedule_source(dataset) if dataset else espn_schedule_source()
    schedule_source = DATASET_SOURCE if dataset else LIVE_SOURCE

    digest_before = ledger_digest(ledger_dir)
    fixtures, resolved, unavailable = collect_schedule(requested, source)
    records = load_records(ledger_dir)

    audit = reconcile(fixtures, records, days=resolved)
    digest_after = ledger_digest(ledger_dir)

    payload = build_payload(
        audit,
        generated_at=now,
        schedule_source=schedule_source,
        requested=requested,
        resolved=resolved,
        unavailable=unavailable,
        ledger_dir=ledger_dir,
        dataset=dataset,
        digest_before=digest_before,
        digest_after=digest_after,
    )

    print(render(payload))

    if not args.dry_run:
        path = write_audit(payload, generated_at=now, audit_dir=Path(args.out))
        print(f"\nwrote {path}")

    if digest_before != digest_after:
        print(
            "\nERROR: the ledger changed while this audit ran. Nothing here writes to"
            " it, so another process did - probably a prediction run. Re-run when idle.",
        )
        return EXIT_LEDGER_MUTATED

    if not resolved:
        print("\nERROR: no schedule could be loaded for any requested date.")
        return EXIT_NO_SCHEDULE

    if audit.has_gap and args.fail_on_gap:
        return EXIT_GAP

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
