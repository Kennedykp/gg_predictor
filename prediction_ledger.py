"""
The append-only prediction ledger (Epic 2G).

    from prediction_ledger import record_predictions
    record_predictions(results, target_date)   # after the decision is final

WHAT THIS GUARANTEES

1. APPEND ONLY. Every write opens with mode `"a"`. There is no update function,
   no delete function, and no code path that opens a ledger file for writing.
   That is not a convention to be remembered - `open(..., "w")` does not appear
   in this module, and `tests/unit/test_prediction_ledger.py` asserts it never
   does.

2. CAPTURE CANNOT CHANGE A PREDICTION. `record_predictions` reads a finished
   list of result dicts and returns a report. It never mutates a result, and
   `main.py` calls it AFTER `run_daily_workflow` has completed, outside
   `process_fixture`. A failure here is reported and swallowed by the caller.

3. NO OVERWRITE ON RE-RUN. Files are keyed by the month a prediction was
   CREATED, not by the fixture date, and records are appended. Running the same
   date twice produces two records with two `prediction_id`s and one shared
   `run_id` each - both observations survive. `output_{date}.json` overwrites in
   place; this deliberately does not.

WHAT THIS MODULE MUST NEVER IMPORT: `poisson`, `filters`, `decision`. It reads
`espn` only for two pure helpers (`parse_kickoff`, `resolve_season`) so that
kickoff parsing and season resolution keep one source of truth. Enforced by
`tests/regression/test_ledger_isolation.py`.

Not imported by `poisson.py`, `filters.py`, `decision.py`, `output.py`, or the
evaluation layer.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from domain.prediction_log import (
    LedgerRecord,
    PredictionProvenance,
    build_provenance,
    from_result_dict,
    to_jsonl_line,
)

# One file per calendar month of CREATION. Small enough to read by eye, bounded
# without a rotation policy, and greppable - matching the JSONL convention
# `historical_dataset.py` and `evaluation_harness.write_artifacts` already use.
# A database is deliberately not introduced: five leagues at ~10 fixtures a day
# is a rounding error, and a schema migration is a cost to pay when volume
# demands it, not before.
DEFAULT_LEDGER_DIR = Path("data/predictions")


@dataclass
class CaptureReport:
    """
    What capture did. Returned rather than printed so a caller decides.

    `skipped` exists because a malformed result must not cost the whole batch:
    one fixture missing `fixture_id` should lose one record, not nineteen.
    """

    path: Optional[Path] = None
    run_id: str = ""
    written: int = 0
    skipped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.skipped

    def summary(self) -> str:
        where = "(nothing written)" if self.path is None else str(self.path)
        text = f"ledger: {self.written} prediction(s) -> {where}"
        if self.skipped:
            text += f"; {len(self.skipped)} skipped"
        return text


def new_run_id() -> str:
    """One id per execution. Groups the records a single run produced."""
    return uuid.uuid4().hex


def new_prediction_id() -> str:
    """
    One id per prediction.

    Random, deliberately not a content hash. Two runs of the same fixture with
    identical inputs are two genuine observations made at two different times; a
    content hash would collapse them into one and silently discard the second,
    which is the same class of mistake as the same-date overwrite this module
    exists to stop.
    """
    return uuid.uuid4().hex


def code_revision() -> Optional[str]:
    """
    The current commit, with a dirty marker. Best-effort: `None`, never a guess.

    Answers "which code produced this?" without inference. If git is absent or
    this is not a checkout, the field is absent - an unknown revision recorded as
    unknown is useful; a fabricated one is not.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode != 0:
            return None
        revision = head.stdout.strip()
        if not revision:
            return None

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        # A dirty tree means the commit alone does not describe what ran.
        if status.returncode == 0 and status.stdout.strip():
            revision += "-dirty"
        return revision
    except (OSError, subprocess.SubprocessError):
        return None


def ledger_filename(moment: datetime) -> str:
    """`2026-08.jsonl`, from the CREATION time - not the fixture date."""
    return f"{moment.astimezone(timezone.utc).strftime('%Y-%m')}.jsonl"


def ledger_path(moment: datetime, ledger_dir: Path = DEFAULT_LEDGER_DIR) -> Path:
    return Path(ledger_dir) / ledger_filename(moment)


def _kickoff_and_season(
    result: Mapping[str, Any],
    target_date: Optional[date],
) -> tuple:
    """
    Parse the kickoff instant and resolve the season, via `espn` only.

    Imported locally and wrapped: these are conveniences for provenance, and a
    provider hiccup here must not cost the record. An unparseable kickoff is
    `None` and `kickoff_raw` still carries the provider's original string, so
    nothing is lost and nothing is invented.
    """
    kickoff = None
    season = None
    try:
        import espn

        raw = result.get("datetime")
        if raw:
            kickoff = espn.parse_kickoff(raw)
        league = result.get("league_id")
        if league:
            season = espn.resolve_season(str(league), target_date)
    except Exception:
        # Provenance is best-effort; the prediction record is not.
        return kickoff, season
    return kickoff, season


def build_records(
    results: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    created_at: datetime,
    provenance: PredictionProvenance,
    target_date: Optional[date] = None,
) -> tuple:
    """
    Turn finished results into ledger records. Returns `(records, skipped)`.

    Pure: no IO, no mutation of `results`. A result that cannot be adapted is
    reported by fixture id rather than raising, so one bad row cannot cost the
    batch.
    """
    records: List[LedgerRecord] = []
    skipped: List[str] = []

    for result in results:
        identifier = str(result.get("fixture_id", "<no fixture_id>"))
        try:
            kickoff, season = _kickoff_and_season(result, target_date)
            records.append(
                from_result_dict(
                    result,
                    prediction_id=new_prediction_id(),
                    run_id=run_id,
                    created_at=created_at,
                    provenance=provenance,
                    kickoff=kickoff,
                    season=season,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not cost the batch
            skipped.append(f"{identifier}: {type(exc).__name__}: {exc}")

    return records, skipped


def append_records(
    records: Sequence[LedgerRecord],
    *,
    created_at: datetime,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
) -> Optional[Path]:
    """
    Append records to the month's ledger file. The only writer in this module.

    Mode `"a"` and `newline="\\n"`: a build on another platform produces the same
    bytes. Nothing is read first, nothing is rewritten, and an existing file is
    never truncated - so an earlier run's records cannot be destroyed by a later
    one.
    """
    if not records:
        return None

    path = ledger_path(created_at, ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(to_jsonl_line(record))
            handle.write("\n")

    return path


def record_predictions(
    results: Sequence[Mapping[str, Any]],
    target_date: Optional[date] = None,
    *,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    created_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
) -> CaptureReport:
    """
    Record every prediction from one run. The single entry point for capture.

    Called AFTER the decision is final and outside `process_fixture`, so it
    cannot participate in producing a prediction. `results` is read and never
    written. `created_at` and `run_id` are injectable so a test can pin both.

    Refused fixtures are recorded too, with a named status - "the system was
    asked and declined" is evidence, and dropping it would make coverage
    unmeasurable.
    """
    moment = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run = run_id or new_run_id()

    if not results:
        return CaptureReport(path=None, run_id=run, written=0)

    provenance = build_provenance(code_revision=code_revision())
    records, skipped = build_records(
        results,
        run_id=run,
        created_at=moment,
        provenance=provenance,
        target_date=target_date,
    )
    path = append_records(records, created_at=moment, ledger_dir=ledger_dir)

    return CaptureReport(path=path, run_id=run, written=len(records), skipped=skipped)


def load_records(
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    *,
    month: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Read the ledger back as raw dicts, in file (append) order.

    Dicts rather than `LedgerRecord`s on purpose: a reader must be able to load
    records written under an OLDER `schema_version` without this module refusing
    them. Reconstructing a typed record would impose today's shape on yesterday's
    data, and the `schema_version` on every line is what lets a future consumer
    decide what to do instead.
    """
    import json

    directory = Path(ledger_dir)
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
