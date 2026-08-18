"""
Prediction lifecycle reconciliation (Epic 2H-4).

PURE. No IO, no network, no clock, no model. `now` is injected, so the same
inputs always produce the same answer.

WHAT THIS ADDS THAT DID NOT EXIST
---------------------------------
`domain/evaluation_input.py` already reports `missing_settlement`: a prediction
with no settlement record. That single number conflates three completely
different situations:

  1. the match has not kicked off yet          -> nothing is wrong
  2. the match is being played right now       -> nothing is wrong
  3. the match finished hours ago and we never asked for the result
                                               -> the settlement job is broken

Only (3) is an operational fault. Reporting all three as one figure means the
number is either ignored (because it is usually large and benign) or panicked
over. Neither is monitoring, so this module splits them.

It also detects a corruption the join cannot see. The evaluation join is keyed on
`(competition, season, fixture_id)` and deliberately allows two predictions for
one fixture - a re-run is legitimate evidence. But two records sharing a
`prediction_id` is different: ids come from `uuid4()`, so a repeat means the same
prediction was written twice, or written differently twice. That is a conflict to
fail on, never to reconcile.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No re-deriving of settlement state. `settlement_status` is read from the
settlement record exactly as `domain/settlement.py` wrote it. No probability is
read, computed, or even looked at here: this module answers "where is this
prediction in its life?", never "was it any good?".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "LIFECYCLE_SCHEMA_VERSION",
    "DEFAULT_SETTLEMENT_GRACE",
    "Stage",
    "LifecycleRow",
    "LifecycleReport",
    "ledger_conflicts",
    "stage_of",
    "reconcile",
]

LIFECYCLE_SCHEMA_VERSION = "2h4.1"

# How long after kickoff a fixture is given before its absence counts as an
# operational fault. Ninety minutes of football plus half-time plus stoppages is
# roughly two hours; providers then need time to publish a final score. Three
# hours is deliberately generous, because the cost of the two errors is not
# symmetric: crying wolf on every in-play fixture would make the alarm useless,
# whereas noticing a genuine gap an hour later costs nothing.
DEFAULT_SETTLEMENT_GRACE = timedelta(hours=3)


class Stage(str, Enum):
    """
    Where one prediction sits in its life.

    The three "no settlement record" stages are separate members rather than one
    MISSING, because they demand different responses: wait, wait, and investigate.
    """

    AWAITING_KICKOFF = "AWAITING_KICKOFF"      # not due: kickoff is in the future
    IN_PLAY = "IN_PLAY"                        # not due: inside the grace window
    AWAITING_SETTLEMENT = "AWAITING_SETTLEMENT"  # DUE and absent -> operational gap
    UNRESOLVED = "UNRESOLVED"                  # settled as postponed/cancelled/abandoned
    SETTLED = "SETTLED"                        # a real result exists
    UNDATED = "UNDATED"                        # no usable kickoff: cannot be judged


# Stages that mean "we are still waiting, and that is fine".
PENDING_STAGES = frozenset({Stage.AWAITING_KICKOFF, Stage.IN_PLAY})

# The one stage that indicates OUR pipeline is behind, as opposed to football
# being football. This is the number worth alerting on.
OPERATIONAL_GAP_STAGES = frozenset({Stage.AWAITING_SETTLEMENT})


@dataclass(frozen=True)
class LifecycleRow:
    """One prediction's position, with the provenance needed to chase it up."""

    prediction_id: str
    fixture_id: Optional[str]
    competition: Optional[str]
    season: Optional[int]
    kickoff: Optional[datetime]
    stage: Stage
    settlement_status: Optional[str] = None
    unresolved_reason: Optional[str] = None
    settlement_source: Optional[str] = None


@dataclass(frozen=True)
class LifecycleReport:
    """
    Counts by stage, plus the conflicts that must stop a run.

    `discovered` is the denominator and is always the number of ledger records
    read. Every record lands in exactly one stage, which is what
    `accounted_for()` exists to prove: a silently shrinking denominator would
    make a broken pipeline look like an improving model.
    """

    discovered: int
    by_stage: Dict[str, int]
    ledger_conflicts: Tuple[str, ...] = ()

    def count(self, stage: Stage) -> int:
        return self.by_stage.get(stage.value, 0)

    @property
    def settled(self) -> int:
        return self.count(Stage.SETTLED)

    @property
    def unresolved(self) -> int:
        """Football-unresolved ONLY. Never includes an operational gap."""
        return self.count(Stage.UNRESOLVED)

    @property
    def pending(self) -> int:
        """Not due yet: awaiting kickoff or in play. Not a fault."""
        return sum(self.count(stage) for stage in PENDING_STAGES)

    @property
    def awaiting_settlement(self) -> int:
        """Due and absent. THE operational alarm."""
        return self.count(Stage.AWAITING_SETTLEMENT)

    @property
    def undated(self) -> int:
        return self.count(Stage.UNDATED)

    @property
    def accounted_for(self) -> bool:
        return sum(self.by_stage.values()) == self.discovered

    @property
    def settlement_backlog(self) -> Optional[float]:
        """
        Share of DUE predictions still unsettled.

        Pending fixtures are excluded from the denominator: including them would
        make the figure track the fixture calendar rather than pipeline health.
        `None` when nothing is due, because a rate over zero cases is not 0.0 -
        it is unknown, and reporting 0.0 would look like perfect health.
        """
        due = self.discovered - self.pending - self.undated
        if due <= 0:
            return None
        return self.awaiting_settlement / due

    def summary(self) -> str:
        parts = [
            f"{self.discovered} discovered",
            f"{self.settled} settled",
            f"{self.unresolved} unresolved",
            f"{self.pending} pending",
            f"{self.awaiting_settlement} awaiting settlement",
        ]
        if self.undated:
            parts.append(f"{self.undated} undated")
        if self.ledger_conflicts:
            parts.append(f"{len(self.ledger_conflicts)} LEDGER CONFLICTS")
        return ", ".join(parts)


def _parse_moment(value: Any) -> Optional[datetime]:
    """
    Parse an ISO timestamp, refusing anything not timezone-aware.

    GG-014: a naive timestamp compared against an aware `now` raises, and
    "helpfully" assuming UTC would put a 23:30 kickoff on the wrong matchday.
    An unusable value is reported as unusable.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ledger_conflicts(predictions: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
    """
    Prediction ids that appear more than once.

    `prediction_id` is a `uuid4()`, so a repeat is not a re-run - it is the same
    record written twice, or two different records claiming one identity. Either
    way a grader would have to choose, and every metric downstream would inherit
    the choice, so the caller is expected to fail rather than pick.

    The message names what actually differs, so the operator can tell a harmless
    duplicated line from two genuinely contradictory records.
    """
    seen: Dict[str, Mapping[str, Any]] = {}
    conflicts: List[str] = []
    for record in predictions:
        prediction_id = _optional_str(record.get("prediction_id"))
        if prediction_id is None:
            continue
        previous = seen.get(prediction_id)
        if previous is None:
            seen[prediction_id] = record
            continue
        differences = [
            field
            for field in ("probability", "fixture_id", "competition", "season", "status")
            if previous.get(field) != record.get(field)
        ]
        if differences:
            conflicts.append(
                f"{prediction_id}: duplicate ledger records disagree on "
                f"{', '.join(sorted(differences))}"
            )
        else:
            conflicts.append(f"{prediction_id}: duplicate ledger record")
    return tuple(conflicts)


def stage_of(
    prediction: Mapping[str, Any],
    settlement: Optional[Mapping[str, Any]],
    *,
    now: datetime,
    grace: timedelta = DEFAULT_SETTLEMENT_GRACE,
) -> Stage:
    """
    Classify one prediction.

    A settlement record, if present, decides the answer outright: it is a
    statement of fact from `domain/settlement.py` and is never second-guessed
    against the clock. Only in its ABSENCE does the kickoff time matter, and then
    purely to separate "too early to expect a result" from "we should have one".
    """
    if settlement is not None:
        status = _optional_str(settlement.get("settlement_status"))
        if status == "SETTLED":
            return Stage.SETTLED
        # Any non-SETTLED settlement record is football-unresolved. Not
        # re-derived from the score: settlement already refused to record an
        # outcome contradicting its own score, and a second derivation here
        # would be a second place for that rule to drift.
        return Stage.UNRESOLVED

    kickoff = _parse_moment(prediction.get("kickoff"))
    if kickoff is None:
        # Cannot be judged either way. Reported as its own stage rather than
        # guessed into a benign one, because a malformed kickoff is a
        # data-quality problem that would otherwise hide inside "pending".
        return Stage.UNDATED

    if now < kickoff:
        return Stage.AWAITING_KICKOFF
    if now < kickoff + grace:
        return Stage.IN_PLAY
    return Stage.AWAITING_SETTLEMENT


def reconcile(
    predictions: Sequence[Mapping[str, Any]],
    settlements: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    grace: timedelta = DEFAULT_SETTLEMENT_GRACE,
) -> Tuple[List[LifecycleRow], LifecycleReport]:
    """
    Position every ledger record, in ledger order.

    Settlements are matched by `prediction_id` - the same key
    `settle_predictions.unsettled()` uses, so this report and that job's idea of
    "still needs asking about" cannot drift apart. The evaluation join keys on
    `(competition, season, fixture_id)` because it is answering a different
    question: which RESULT grades this prediction.

    Later settlement lines win, because the log is append-only and a correction
    is a new line.
    """
    latest: Dict[str, Mapping[str, Any]] = {}
    for record in settlements:
        prediction_id = _optional_str(record.get("prediction_id"))
        if prediction_id is not None:
            latest[prediction_id] = record

    rows: List[LifecycleRow] = []
    counts: Counter = Counter()

    for prediction in predictions:
        prediction_id = _optional_str(prediction.get("prediction_id"))
        settlement = latest.get(prediction_id) if prediction_id else None
        stage = stage_of(prediction, settlement, now=now, grace=grace)
        counts[stage.value] += 1
        rows.append(
            LifecycleRow(
                prediction_id=prediction_id or "",
                fixture_id=_optional_str(prediction.get("fixture_id")),
                competition=_optional_str(prediction.get("competition")),
                season=prediction.get("season") if isinstance(prediction.get("season"), int) else None,
                kickoff=_parse_moment(prediction.get("kickoff")),
                stage=stage,
                settlement_status=(
                    _optional_str(settlement.get("settlement_status")) if settlement else None
                ),
                unresolved_reason=(
                    _optional_str(settlement.get("unresolved_reason")) if settlement else None
                ),
                settlement_source=(_optional_str(settlement.get("source")) if settlement else None),
            )
        )

    return rows, LifecycleReport(
        discovered=len(predictions),
        by_stage=dict(counts),
        ledger_conflicts=ledger_conflicts(predictions),
    )
