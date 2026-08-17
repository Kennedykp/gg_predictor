"""
The settlement contract (Epic 2H-2).

What happened to a fixture that was predicted — and nothing else.

A settlement record answers exactly one question: *what was the final score, and
therefore did both teams score?* It does NOT answer "was the prediction any
good": that is `domain/evaluation.py`'s job, and it consumes a settlement rather
than producing one. Keeping the two apart is what stops a grading bug from
looking like a result bug.

THE RULE THIS MODULE EXISTS TO ENFORCE:

    A settlement is SETTLED only if a real final score exists.
    Everything else is UNRESOLVED, with a named reason.

There is no third option and no default. A postponed fixture is not 0-0. An
abandoned match's partial score is not a result. A provider outage is not a
goalless draw. Each of those is an *absence*, and recording an absence as an
observation would fabricate evidence — the one failure mode that cannot be
detected after the fact, because the fabricated record is indistinguishable from
a real one.

PURITY. This module performs no IO, opens no socket, reads no clock and
generates no id. `settled_at` is injected by the caller. That is what makes every
field of every settlement record a function of its inputs, and therefore pinnable
by a test with no network and no mocking. `settle_predictions.py` owns the clock,
the network and the disk.

WHAT THIS MODULE MUST NEVER IMPORT: `poisson`, `filters`, `decision`,
`evaluation_harness`, `prediction_ledger`, `espn`. The first three would let
settlement re-run the model; `evaluation_harness.replay()` would do it by
accident (it recomputes probabilities from today's data — see EPIC 2H audit
2H-F6); the last two would make this module do IO. Enforced by
`tests/regression/test_settlement_isolation.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from domain.evaluation import BttsOutcome, btts_outcome
from domain.historical import HistoricalMatch

__all__ = [
    "SETTLEMENT_SCHEMA_VERSION",
    "SettlementStatus",
    "UnresolvedReason",
    "SettlementRecord",
    "lookup_key",
    "candidate_lookup_keys",
    "classify",
    "settle_one",
    "to_json_dict",
    "FIELD_ORDER",
    "TERMINAL_REASONS",
]

SETTLEMENT_SCHEMA_VERSION = "2h.1"

# ---------------------------------------------------------------------------
# Provider status names
#
# Mirrored from `espn._NOT_PLAYABLE` rather than imported, because importing
# `espn` would put a network-capable module inside a contract that must stay
# pure. Mirroring risks drift, so `test_settlement_isolation.py` asserts these
# sets still partition `espn._NOT_PLAYABLE` exactly. If ESPN adds a status, that
# test fails and names the new one.
#
# Both spellings of cancelled are present because ESPN uses both.
# ---------------------------------------------------------------------------
_POSTPONED_NAMES = frozenset({"STATUS_POSTPONED"})
_CANCELLED_NAMES = frozenset({"STATUS_CANCELED", "STATUS_CANCELLED"})
_ABANDONED_NAMES = frozenset({"STATUS_ABANDONED"})


class SettlementStatus(str, Enum):
    """
    Whether a trustworthy final score exists. Two values, deliberately.

    SETTLED carries a score and a YES/NO outcome. UNRESOLVED carries neither and
    always names why. There is no "PARTIAL" and no "PENDING" that also carries a
    score: a record either has evidence or it does not.
    """

    SETTLED = "SETTLED"
    UNRESOLVED = "UNRESOLVED"


class UnresolvedReason(str, Enum):
    """
    Why there is no result. Seven values, none of them interchangeable.

    Collapsing any two of these would destroy information an operator needs:

      - PROVIDER_UNAVAILABLE vs FIXTURE_NOT_FOUND is "we could not ask" vs "we
        asked and it is not there". The first is an outage, the second is a data
        or key problem — most likely a season mismatch. Merging them makes a
        systematic join failure look like bad luck, and the join failure is the
        one that silently shrinks coverage.

      - MISSING_RESULT vs ABANDONED is "completed but no score" (a provider
        contradiction) vs "started and stopped" (a real football event).

      - NOT_YET_PLAYED is retryable and expected; CANCELLED never resolves.
    """

    NOT_YET_PLAYED = "NOT_YET_PLAYED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"
    MISSING_RESULT = "MISSING_RESULT"
    FIXTURE_NOT_FOUND = "FIXTURE_NOT_FOUND"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


# A fixture in one of these states will never acquire a result at this event id.
# ESPN does not move a replayed fixture's score onto the postponed original — it
# publishes a second event with its own id (see `domain/historical.py`, which
# keeps both rows). So a settlement job that retries every unresolved record
# forever would retry these forever. POSTPONED is deliberately NOT terminal: a
# postponement is sometimes reversed within the same event id.
TERMINAL_REASONS = frozenset({UnresolvedReason.CANCELLED, UnresolvedReason.ABANDONED})


@dataclass(frozen=True)
class SettlementRecord:
    """
    One immutable statement of what happened to one prediction.

    Keyed by `prediction_id`, NOT by `fixture_id`. `prediction_ledger` issues a
    random prediction id per run precisely so a re-run is "distinguishable, not
    duplicated"; a settlement keyed on the fixture would collapse n predictions
    of one fixture into one settlement and silently discard that evidence.

    Carries no probability, no odds, no edge, no recommendation and no stake. A
    settlement states a fact about a football match. Copying the probability in
    would create a second copy of a ledger field that could drift from the
    first, and any money field would breach the odds firewall by handing a
    price-bearing object to the evaluation layer.
    """

    prediction_id: str
    fixture_id: str
    competition: str
    season: Optional[int]
    final_home_goals: Optional[int]
    final_away_goals: Optional[int]
    gg_outcome: BttsOutcome
    settlement_status: SettlementStatus
    settled_at: datetime
    source: str
    unresolved_reason: Optional[UnresolvedReason] = None
    provider_status: Optional[str] = None
    matched_season: Optional[int] = None
    schema_version: str = SETTLEMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """
        Enforce, never trust. Every invariant below is a fabrication this record
        would otherwise be able to express.
        """
        if not self.prediction_id:
            raise ValueError("SettlementRecord requires a prediction_id: it is the join key")
        if not self.fixture_id:
            raise ValueError("SettlementRecord requires a fixture_id")
        if not self.competition:
            raise ValueError("SettlementRecord requires a competition: half of the lookup key")
        if not self.source:
            raise ValueError(
                "SettlementRecord requires a source. A result whose origin is unrecorded "
                "cannot be re-verified, and a second provider must be distinguishable "
                "from the first rather than inferred."
            )
        if self.settled_at.tzinfo is None:
            raise ValueError(
                "SettlementRecord.settled_at must be timezone-aware; got a naive datetime. "
                "A naive timestamp compares silently against local time."
            )

        settled = self.settlement_status is SettlementStatus.SETTLED

        # A record is settled, or it says why not. Never both, never neither.
        if settled and self.unresolved_reason is not None:
            raise ValueError(
                f"SETTLED record {self.prediction_id} also carries "
                f"unresolved_reason={self.unresolved_reason.value}"
            )
        if not settled and self.unresolved_reason is None:
            raise ValueError(
                f"UNRESOLVED record {self.prediction_id} names no reason. "
                "'No result' is not one fact: an outage, a cancellation and a "
                "failed lookup need different responses."
            )

        if settled:
            # THE ANTI-FABRICATION INVARIANT. Zero is never substituted for an
            # unknown result.
            if self.final_home_goals is None or self.final_away_goals is None:
                raise ValueError(
                    f"SETTLED record {self.prediction_id} is missing a score "
                    f"({self.final_home_goals!r}-{self.final_away_goals!r}). "
                    "A settled record must carry the real final score; zero is "
                    "never substituted for an unknown result."
                )
            if self.gg_outcome is BttsOutcome.UNKNOWN:
                raise ValueError(
                    f"SETTLED record {self.prediction_id} has gg_outcome UNKNOWN. "
                    "A settled fixture has a knowable outcome by definition."
                )
        else:
            # An abandoned match's partial score is not a result. Refusing to
            # store it is what stops it being graded later.
            if self.final_home_goals is not None or self.final_away_goals is not None:
                raise ValueError(
                    f"UNRESOLVED record {self.prediction_id} carries a score "
                    f"({self.final_home_goals!r}-{self.final_away_goals!r}). "
                    "A partial or provisional score is not a final result."
                )
            if self.gg_outcome is not BttsOutcome.UNKNOWN:
                raise ValueError(
                    f"UNRESOLVED record {self.prediction_id} claims outcome "
                    f"{self.gg_outcome.value}. Without a score the outcome is UNKNOWN."
                )

        for goals in (self.final_home_goals, self.final_away_goals):
            if goals is None:
                continue
            if isinstance(goals, bool) or not isinstance(goals, int) or goals < 0:
                raise ValueError(
                    f"SettlementRecord {self.prediction_id} has a non-integer or negative "
                    f"score: {self.final_home_goals!r}-{self.final_away_goals!r}"
                )

        # The stored outcome must be a checkable derivation of the stored score,
        # not an independent claim. Without this, a record could say 2-1 / NO.
        recomputed = btts_outcome(
            self.final_home_goals,
            self.final_away_goals,
            completed=settled,
        )
        if recomputed is not self.gg_outcome:
            raise ValueError(
                f"SettlementRecord {self.prediction_id} stores gg_outcome "
                f"{self.gg_outcome.value} but its score "
                f"{self.final_home_goals!r}-{self.final_away_goals!r} derives "
                f"{recomputed.value}. The outcome must follow from the score."
            )

    @property
    def is_terminal(self) -> bool:
        """Settled, or unresolved for a reason that will never change."""
        if self.settlement_status is SettlementStatus.SETTLED:
            return True
        return self.unresolved_reason in TERMINAL_REASONS


# ---------------------------------------------------------------------------
# The lookup key
# ---------------------------------------------------------------------------
def lookup_key(competition: str, season: Optional[int], event_id: str) -> Tuple[str, Optional[int], str]:
    """
    The composite identity of a fixture: (competition, season, event_id).

    NOT the bare event id. Nothing in this repo asserts that an ESPN event id is
    unique across competitions, and `domain/historical.py` deliberately keys
    duplicate detection on the same three-part composite — so the composite is
    the codebase's own unit of uniqueness. A bare-id dictionary would work in
    almost every case and fail silently in the one that matters.
    """
    return (competition, season, str(event_id))


def candidate_lookup_keys(
    competition: str,
    season: Optional[int],
    event_id: str,
) -> Tuple[Tuple[str, Optional[int], str], ...]:
    """
    The seasons this fixture might legitimately be filed under, best first.

    THE SEASON MISMATCH (Epic 2H audit 2H-F3), verified before implementation:

        the ledger stores `espn.resolve_season(league, target_date)` — a CALENDAR
        RULE that rolls over on 1 July;
        history stores the event's OWN `season.year` as reported by ESPN.

    These disagree in June. ESPN's eng.1 season `2025` runs startDate 2025-06-01
    to endDate 2026-06-01, so a fixture on 2026-06-15 belongs to ESPN season
    2026 — while `resolve_season("eng.1", 2026-06-15)` returns 2025, because its
    rollover month is July. Measured:

        resolve_season("eng.1", 2026-06-15) == 2025   # ESPN says 2026
        resolve_season("eng.1", 2026-06-30) == 2025   # ESPN says 2026
        resolve_season("eng.1", 2026-07-01) == 2026   # agrees

    Because history is fetched per league-season, a one-year disagreement means
    the fixture is looked for in the wrong season and reported as
    FIXTURE_NOT_FOUND — a join bug that looks exactly like a provider gap, and
    which would silently zero out coverage for an entire league-season.

    THE FIX IS AT THE LOOKUP BOUNDARY ONLY. The stored season on an already
    written prediction record is never altered: the ledger is immutable, and a
    prediction record is evidence of what was believed at prediction time. This
    function widens the *question* instead — try the stored season, then the
    adjacent one — and `SettlementRecord.matched_season` records which season
    actually answered, so a systematic drift is visible in the data rather than
    hidden by the retry.
    """
    if season is None:
        return (lookup_key(competition, None, event_id),)
    return (
        lookup_key(competition, season, event_id),
        lookup_key(competition, season + 1, event_id),
        lookup_key(competition, season - 1, event_id),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(
    match: Optional[HistoricalMatch],
    *,
    provider_available: bool = True,
) -> Tuple[SettlementStatus, Optional[UnresolvedReason]]:
    """
    Decide what a provider readout means. The whole judgement, in one place.

    Order matters. "We could not ask" is checked before "it is not there",
    because an outage must never be reported as a missing fixture. A named
    not-playable status is checked before `completed`, because a postponed
    fixture still reports state `pre` and would otherwise be filed as merely
    not-yet-played and retried forever with no explanation.
    """
    if not provider_available:
        return SettlementStatus.UNRESOLVED, UnresolvedReason.PROVIDER_UNAVAILABLE
    if match is None:
        return SettlementStatus.UNRESOLVED, UnresolvedReason.FIXTURE_NOT_FOUND

    status = (match.status or "").strip().upper()
    if status in _POSTPONED_NAMES:
        return SettlementStatus.UNRESOLVED, UnresolvedReason.POSTPONED
    if status in _CANCELLED_NAMES:
        return SettlementStatus.UNRESOLVED, UnresolvedReason.CANCELLED
    if status in _ABANDONED_NAMES:
        # Reached even when the payload carried a partial score: the provider
        # layer has already blanked it, and `SettlementRecord` refuses to store
        # a score on an unresolved record regardless.
        return SettlementStatus.UNRESOLVED, UnresolvedReason.ABANDONED

    if not match.completed:
        return SettlementStatus.UNRESOLVED, UnresolvedReason.NOT_YET_PLAYED

    if match.home_goals is None or match.away_goals is None:
        # A provider contradiction: completed, but no score. `HistoricalMatch`
        # normally refuses this at construction, so reaching it means the rule
        # was relaxed upstream. Recorded as its own reason rather than folded
        # into ABANDONED, because it is a data-quality signal, not football.
        return SettlementStatus.UNRESOLVED, UnresolvedReason.MISSING_RESULT

    return SettlementStatus.SETTLED, None


def settle_one(
    prediction: Mapping[str, Any],
    match: Optional[HistoricalMatch],
    *,
    settled_at: datetime,
    source: str,
    provider_available: bool = True,
) -> SettlementRecord:
    """
    Build one settlement record. Pure: same inputs, same record, always.

    `prediction` is a raw ledger dict, not a typed record — the same choice
    `prediction_ledger.load_records` makes, and for the same reason: a settler
    must be able to read records written under an older `schema_version` without
    imposing today's shape on yesterday's data.

    `match` is a validated `HistoricalMatch` rather than a raw payload. Its own
    `__post_init__` already refuses a completed match with a missing score, a
    naive kickoff, and negative or boolean goals, so settlement inherits all of
    that and cannot be handed a malformed result. Accepting a payload would mean
    re-implementing those checks — a second derivation, which is the thing to
    avoid.

    Note what is NOT here: no model, no probability, no odds. The stored
    probability stays in the ledger and is read by the reporting layer directly.
    Settlement never computes one.
    """
    status, reason = classify(match, provider_available=provider_available)
    settled = status is SettlementStatus.SETTLED

    home_goals = match.home_goals if (settled and match is not None) else None
    away_goals = match.away_goals if (settled and match is not None) else None

    return SettlementRecord(
        prediction_id=str(prediction["prediction_id"]),
        fixture_id=str(prediction["fixture_id"]),
        competition=str(prediction["competition"]),
        season=prediction.get("season"),
        final_home_goals=home_goals,
        final_away_goals=away_goals,
        gg_outcome=btts_outcome(home_goals, away_goals, completed=settled),
        settlement_status=status,
        settled_at=settled_at,
        source=source,
        unresolved_reason=reason,
        provider_status=match.status if match is not None else None,
        matched_season=match.season if match is not None else None,
    )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
# Written in this order on every line, so two runs over the same data produce
# byte-identical output and a diff means a real change.
FIELD_ORDER: Tuple[str, ...] = (
    "schema_version",
    "prediction_id",
    "fixture_id",
    "competition",
    "season",
    "matched_season",
    "final_home_goals",
    "final_away_goals",
    "gg_outcome",
    "settlement_status",
    "unresolved_reason",
    "provider_status",
    "settled_at",
    "source",
)


def to_json_dict(record: SettlementRecord) -> Dict[str, Any]:
    """One JSON-ready mapping, fixed key order, timestamp normalised to UTC."""
    values: Dict[str, Any] = {
        "schema_version": record.schema_version,
        "prediction_id": record.prediction_id,
        "fixture_id": record.fixture_id,
        "competition": record.competition,
        "season": record.season,
        "matched_season": record.matched_season,
        "final_home_goals": record.final_home_goals,
        "final_away_goals": record.final_away_goals,
        "gg_outcome": record.gg_outcome.value,
        "settlement_status": record.settlement_status.value,
        "unresolved_reason": (
            record.unresolved_reason.value if record.unresolved_reason is not None else None
        ),
        "provider_status": record.provider_status,
        "settled_at": record.settled_at.astimezone(timezone.utc).isoformat(),
        "source": record.source,
    }
    return {key: values[key] for key in FIELD_ORDER}
