"""
Epic 2I — reconciling the fixture schedule against the prediction ledger.

THE PROBLEM THIS EXISTS FOR

`main.py` swallows every exception from ledger capture (`main.py:313-318`), and
that is the right call: a full disk must degrade to "predictions were not
recorded", never to "the run failed". The cost is that the failure is invisible
downstream. A run can print recommendations, write `output_<date>.json`, exit 0 —
and record nothing. Every existing tool reports that state as healthy, because an
absent ledger and a quiet day produce the same value: `load_records` returns `[]`
and the lifecycle runner prints "Nothing to evaluate yet." and exits 0.

A prediction not recorded when it was made cannot be reconstructed, so this
module cannot repair anything. It can only make the loss *visible*, by asking one
question the ledger cannot answer alone: were there fixtures on this date, and is
there any evidence a prediction was captured for them?

WHY THIS IS NOT A COUNT COMPARISON

`is_predictable()` is unwired (GG-013), so a fixture legitimately goes unpredicted
when its inputs are insufficient — a real, frequent, correct outcome. A verifier
that alerted whenever `len(fixtures) != len(records)` would fire on most days,
and a check that cries wolf daily is one that gets switched off. That is worse
than no check, because it is a check someone believes they have.

So the reconciliation classifies instead of counting, and only ONE state is
unambiguous enough to alert on: fixtures existed for a date and the ledger holds
**zero** records for any of them. A partial capture cannot be distinguished from
a legitimate skip without asking the model what it would have predicted — which
is precisely the recomputation this Epic forbids. So partial is reported and
never alerted, and the doc says so rather than implying the tool is smarter than
it is.

WHAT THIS MODULE MAY NOT DO

It never asks "what would the model have predicted?". It asks only "is there
evidence a prediction was captured?". No probability, no odds, no outcome, no
model import — enforced on the AST in
`tests/regression/test_capture_audit_isolation.py`.

Pure: no IO, no network, no clock. Every input is supplied by the caller, so the
same schedule and the same ledger always produce the same audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CAPTURE_AUDIT_SCHEMA_VERSION",
    "FixtureOutcome",
    "DayVerdict",
    "ExpectedFixture",
    "FixtureRow",
    "DayAudit",
    "CaptureAudit",
    "expected_from_matches",
    "index_records",
    "reconcile_day",
    "reconcile",
]

CAPTURE_AUDIT_SCHEMA_VERSION = "2i.1"

# Schedule statuses that mean the fixture was never playable. A prediction for
# one of these is not expected, and its absence is football, not a defect.
#
# Lower-cased on comparison: ESPN supplies "STATUS_POSTPONED" while a historical
# dataset may carry "postponed", and a verifier that treated those as different
# answers would report a postponement as a capture gap.
NOT_PLAYABLE_STATUSES = frozenset(
    {
        "postponed",
        "cancelled",
        "canceled",
        "abandoned",
        "suspended",
        "status_postponed",
        "status_canceled",
        "status_cancelled",
        "status_abandoned",
        "status_suspended",
    }
)


class FixtureOutcome(str, Enum):
    """
    What the ledger says about one expected fixture.

    Three values, not two. `UNACCOUNTED` and `NOT_PLAYABLE` both mean "no
    prediction record", and collapsing them would file every postponement as
    missing evidence — the daily false alarm that gets a verifier ignored.
    """

    CAPTURED = "CAPTURED"
    NOT_PLAYABLE = "NOT_PLAYABLE"
    UNACCOUNTED = "UNACCOUNTED"


class DayVerdict(str, Enum):
    """
    The state of one date.

    `ZERO_CAPTURE` is the only value that indicates a capture gap. It is separate
    from `PARTIAL` because the two have different causes and different responses:
    zero records for a date with playable fixtures cannot be explained by
    per-fixture data gaps, whereas a partial capture usually is exactly that.
    """

    NO_FIXTURES = "NO_FIXTURES"
    NO_PLAYABLE_FIXTURES = "NO_PLAYABLE_FIXTURES"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    ZERO_CAPTURE = "ZERO_CAPTURE"


@dataclass(frozen=True)
class ExpectedFixture:
    """
    One fixture that existed, from the schedule side.

    Deliberately minimal. Team names, venues and odds are all irrelevant to "was
    this captured", and carrying them would invite a future reader to reconcile on
    a team NAME — the known-defective pattern GG-008 tracks. The id is sufficient:
    2H-F1 established that a live `fixture_id` and a historical `event_id` are the
    same ESPN identifier from the same endpoint.
    """

    fixture_id: str
    competition: Optional[str] = None
    season: Optional[int] = None
    kickoff: Optional[datetime] = None
    status: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.fixture_id:
            raise ValueError("ExpectedFixture requires a fixture_id")

    @property
    def playable(self) -> bool:
        """False when the schedule says the fixture was never going to be played."""
        if self.status is None:
            return True
        return self.status.strip().lower() not in NOT_PLAYABLE_STATUSES

    @property
    def kickoff_date(self) -> Optional[date]:
        """The UTC calendar date of kickoff, or `None` if undated."""
        if self.kickoff is None:
            return None
        return _as_utc(self.kickoff).date()


@dataclass(frozen=True)
class FixtureRow:
    """One expected fixture, with what the ledger had to say about it."""

    fixture_id: str
    outcome: FixtureOutcome
    competition: Optional[str] = None
    season: Optional[int] = None
    prediction_ids: Tuple[str, ...] = ()

    @property
    def captured(self) -> bool:
        return self.outcome is FixtureOutcome.CAPTURED

    @property
    def duplicated(self) -> bool:
        """
        More than one prediction for one fixture.

        Not an error. 2G made re-runs deliberately distinguishable, so a fixture
        predicted twice in one day is a re-run, not a defect — but it must not be
        counted twice, or a day could report more captures than it had fixtures.
        """
        return len(self.prediction_ids) > 1


@dataclass(frozen=True)
class DayAudit:
    """The reconciliation for a single date."""

    day: Optional[date]
    verdict: DayVerdict
    rows: Tuple[FixtureRow, ...] = ()

    @property
    def expected(self) -> int:
        return len(self.rows)

    @property
    def playable(self) -> int:
        return sum(1 for row in self.rows if row.outcome is not FixtureOutcome.NOT_PLAYABLE)

    @property
    def captured(self) -> int:
        return sum(1 for row in self.rows if row.captured)

    @property
    def unaccounted(self) -> int:
        return sum(1 for row in self.rows if row.outcome is FixtureOutcome.UNACCOUNTED)

    @property
    def not_playable(self) -> int:
        return sum(1 for row in self.rows if row.outcome is FixtureOutcome.NOT_PLAYABLE)

    @property
    def duplicates(self) -> int:
        return sum(1 for row in self.rows if row.duplicated)

    @property
    def is_gap(self) -> bool:
        """The one alertable condition. See `DayVerdict`."""
        return self.verdict is DayVerdict.ZERO_CAPTURE

    @property
    def accounted_for(self) -> bool:
        """Every expected fixture landed in exactly one bucket."""
        return self.captured + self.unaccounted + self.not_playable == self.expected


@dataclass(frozen=True)
class CaptureAudit:
    """The reconciliation across every date examined."""

    days: Tuple[DayAudit, ...] = ()
    unknown_fixture_records: int = 0
    undated_expected: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def expected(self) -> int:
        return sum(day.expected for day in self.days)

    @property
    def captured(self) -> int:
        return sum(day.captured for day in self.days)

    @property
    def unaccounted(self) -> int:
        return sum(day.unaccounted for day in self.days)

    @property
    def not_playable(self) -> int:
        return sum(day.not_playable for day in self.days)

    @property
    def duplicates(self) -> int:
        return sum(day.duplicates for day in self.days)

    @property
    def gap_days(self) -> Tuple[DayAudit, ...]:
        return tuple(day for day in self.days if day.is_gap)

    @property
    def has_gap(self) -> bool:
        return bool(self.gap_days)

    def summary(self) -> str:
        parts = [
            f"{len(self.days)} day(s)",
            f"{self.expected} fixture(s)",
            f"{self.captured} captured",
            f"{self.unaccounted} unaccounted",
            f"{self.not_playable} not playable",
        ]
        if self.duplicates:
            parts.append(f"{self.duplicates} re-predicted")
        if self.unknown_fixture_records:
            parts.append(f"{self.unknown_fixture_records} record(s) off-schedule")
        gaps = len(self.gap_days)
        parts.append(f"{gaps} capture gap(s)" if gaps else "no capture gap")
        return ", ".join(parts)


def _as_utc(moment: datetime) -> datetime:
    """
    Normalise to UTC, treating a naive datetime as already UTC.

    Every stored timestamp in this system is UTC (`_iso` in
    `domain/prediction_log.py`), but a hand-built dataset may omit the offset. A
    naive value compared against an aware one raises `TypeError`, so a single
    malformed row would crash the audit rather than be reported by it.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_moment(value: Any) -> Optional[datetime]:
    """
    Best-effort ISO-8601 parse. Never raises.

    A record whose kickoff cannot be read is reported as undated rather than
    dropped, because a dropped record is one this tool would silently stop
    checking — the exact class of invisibility Epic 2I exists to remove.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def expected_from_matches(matches: Sequence[Any]) -> List[ExpectedFixture]:
    """
    Adapt `HistoricalMatch`-shaped objects into `ExpectedFixture`s.

    Duck-typed on purpose: this module must not import `domain.historical`, or the
    pure core would acquire a dependency on the dataset layer and, through it, on
    provider parsing. `event_id` is the fixture id (2H-F1).
    """
    fixtures: List[ExpectedFixture] = []
    for match in matches:
        fixture_id = getattr(match, "event_id", None) or getattr(match, "fixture_id", None)
        if not fixture_id:
            continue
        fixtures.append(
            ExpectedFixture(
                fixture_id=str(fixture_id),
                competition=getattr(match, "competition", None),
                season=getattr(match, "season", None),
                kickoff=_parse_moment(getattr(match, "kickoff", None)),
                status=getattr(match, "status", None),
            )
        )
    return fixtures


def index_records(records: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """
    Map `fixture_id` -> the prediction ids recorded for it.

    Keyed by fixture rather than by prediction because the question is "was this
    fixture captured", and a fixture may hold several records from several runs.
    Ids are de-duplicated: an append-only ledger can legitimately contain the same
    line twice after an interrupted write, and counting it twice would inflate
    the capture figure.
    """
    index: Dict[str, List[str]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        fixture_id = record.get("fixture_id")
        if fixture_id is None or fixture_id == "":
            continue
        prediction_id = record.get("prediction_id")
        bucket = index.setdefault(str(fixture_id), [])
        token = str(prediction_id) if prediction_id else ""
        if token and token in bucket:
            continue
        bucket.append(token)
    return index


def _verdict(rows: Sequence[FixtureRow]) -> DayVerdict:
    """
    Classify one date. The decision that keeps this tool credible.

    A date with playable fixtures and zero captures is the only state that cannot
    be explained by insufficient per-fixture data, because GG-013 skips are a
    property of individual fixtures — it would take every fixture on the card
    failing at once to look like this, and the far likelier cause is that capture
    never ran or never wrote.
    """
    if not rows:
        return DayVerdict.NO_FIXTURES

    playable = [row for row in rows if row.outcome is not FixtureOutcome.NOT_PLAYABLE]
    if not playable:
        return DayVerdict.NO_PLAYABLE_FIXTURES

    captured = sum(1 for row in playable if row.captured)
    if captured == 0:
        return DayVerdict.ZERO_CAPTURE
    if captured == len(playable):
        return DayVerdict.COMPLETE
    return DayVerdict.PARTIAL


def _row(fixture: ExpectedFixture, index: Mapping[str, Sequence[str]]) -> FixtureRow:
    prediction_ids = tuple(index.get(fixture.fixture_id, ()))
    if prediction_ids:
        outcome = FixtureOutcome.CAPTURED
    elif not fixture.playable:
        outcome = FixtureOutcome.NOT_PLAYABLE
    else:
        outcome = FixtureOutcome.UNACCOUNTED
    return FixtureRow(
        fixture_id=fixture.fixture_id,
        outcome=outcome,
        competition=fixture.competition,
        season=fixture.season,
        prediction_ids=prediction_ids,
    )


def reconcile_day(
    day: Optional[date],
    expected: Sequence[ExpectedFixture],
    records: Sequence[Mapping[str, Any]],
) -> DayAudit:
    """
    Reconcile one date's fixtures against the ledger.

    Rows are sorted by `(competition, season, fixture_id)` — the join key 2H-3
    established — so the same inputs always produce the same order regardless of
    schedule or file order.
    """
    index = index_records(records)
    rows = tuple(sorted((_row(fixture, index) for fixture in expected), key=_row_sort_key))
    return DayAudit(day=day, verdict=_verdict(rows), rows=rows)


def _row_sort_key(row: FixtureRow) -> Tuple[str, int, str]:
    # `season` is Optional[int]; `None` cannot be compared with `int`, and
    # substituting 0 would sort an unknown season before every real one. -1 keeps
    # unknowns together and last-resort ordered by id.
    return (row.competition or "", row.season if row.season is not None else -1, row.fixture_id)


def reconcile(
    expected: Sequence[ExpectedFixture],
    records: Sequence[Mapping[str, Any]],
    *,
    days: Optional[Sequence[date]] = None,
) -> CaptureAudit:
    """
    Reconcile a schedule against the ledger, grouped by UTC kickoff date.

    `days` forces dates into the report even when the schedule has no fixture for
    them. Without it, a date whose schedule came back empty would be
    indistinguishable from a date nobody asked about — and "we have no fixtures
    for that day" is a different statement from silence.

    Records are matched on `fixture_id` alone, NOT on the ledger's monthly file.
    The monthly file is named from the prediction's CREATION time
    (`prediction_ledger.ledger_filename`), so a Saturday fixture predicted on the
    Friday of a new month lands in the previous month's file. Filtering by month
    would report that prediction as missing.
    """
    grouped: Dict[Optional[date], List[ExpectedFixture]] = {}
    undated: List[str] = []
    for fixture in expected:
        key = fixture.kickoff_date
        if key is None:
            undated.append(fixture.fixture_id)
        grouped.setdefault(key, []).append(fixture)

    for forced in days or ():
        grouped.setdefault(forced, [])

    audits = [
        reconcile_day(day, fixtures, records)
        for day, fixtures in sorted(grouped.items(), key=lambda item: (item[0] is None, item[0]))
    ]

    scheduled = {fixture.fixture_id for fixture in expected}
    off_schedule = sum(
        1
        for record in records
        if isinstance(record, Mapping)
        and record.get("fixture_id") is not None
        and str(record.get("fixture_id")) not in scheduled
    )

    return CaptureAudit(
        days=tuple(audits),
        unknown_fixture_records=off_schedule,
        undated_expected=tuple(sorted(undated)),
    )
