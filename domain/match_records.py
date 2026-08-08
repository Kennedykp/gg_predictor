"""
Exact derivations from completed match records (Epic 1B.3, TASK 5 / 6 / 15 / 16).

WHY THIS EXISTS SEPARATELY FROM THE STANDINGS AGGREGATES
--------------------------------------------------------
Clean-sheet percentage and BTTS percentage CANNOT be recovered from the season
aggregates `espn.get_team_stats` currently reads. That is a mathematical fact,
not a limitation of the parsing:

    goals conceded = 5 over 5 matches

is consistent with conceding 1,1,1,1,1 (zero clean sheets) and with conceding
5,0,0,0,0 (four clean sheets). The aggregate does not determine the count of
zero-conceded matches. Any function mapping (GA, matches) -> clean-sheet % is
therefore an approximation, and TASK 4 forbids classifying an approximation as
DERIVED.

Both statistics need MATCH-LEVEL records. This module defines that derivation
exactly, over an explicit record type, with no network and no provider coupling
- so it is deterministic and hand-checkable.

STATUS (Epic 1B.4): the per-team schedule endpoint identified as the next step
in docs/EPIC_1B3_FILTER_WIRING.md is now wired. `espn.get_team_match_records`
supplies the records; `derive_history` below is the boundary the pipeline must
pass through, because it is the only place the point-in-time cutoff is applied.
Until a provider supplies records for a given team, the statistics remain
UNAVAILABLE rather than approximated.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Sequence

__all__ = [
    "Venue",
    "MatchRecord",
    "DerivedHistory",
    "completed_matches",
    "clean_sheet_pct",
    "both_teams_scored_pct",
    "eligible_history",
    "derive_history",
]


class Venue:
    """Which side of a fixture a team played on. Plain constants; no behaviour."""

    HOME = "home"
    AWAY = "away"


@dataclass(frozen=True)
class MatchRecord:
    """
    One fixture from a single team's perspective.

    `venue` matters and must not be flattened. TASK 16 requires that a home
    clean-sheet percentage counts only actual home matches; mixing perspectives
    silently averages two different statistics.

    `completed` is carried explicitly rather than inferred from the presence of
    a score, because a postponed or abandoned fixture can still arrive with a
    score of 0-0 and would otherwise be counted as a genuine goalless draw.

    IDENTITY FIELDS (Epic 1B.4)
    ---------------------------
    They default to None so the type stays usable in pure derivation tests that
    care only about scorelines, and so Epic 1B.3 call sites keep working. A
    provider that CAN supply them should: `kickoff` is required by the
    point-in-time cutoff, `event_id` by deduplication and target-fixture
    exclusion, and `competition` to keep league statistics free of cup and
    friendly results. A record missing one of those is simply not eligible for
    the corresponding guard - it is never assumed to pass it.

    `kickoff` MUST be timezone-aware. A naive datetime is rejected at
    construction rather than at comparison time, because the failure mode
    otherwise is a TypeError deep inside the cutoff or - worse - a silent
    misordering if someone "helpfully" strips the tzinfo (TASK 10).
    """

    venue: str
    goals_for: Optional[int]
    goals_against: Optional[int]
    completed: bool
    kickoff: Optional[datetime] = None
    event_id: Optional[str] = None
    competition: Optional[str] = None
    team_id: Optional[str] = None
    opponent_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kickoff is not None and self.kickoff.tzinfo is None:
            raise ValueError(
                "MatchRecord.kickoff must be timezone-aware; got a naive datetime. "
                "Parse provider timestamps to UTC before constructing the record."
            )

    @property
    def is_usable(self) -> bool:
        """Completed AND carrying both scores. A half-recorded result is not data."""
        return self.completed and self.goals_for is not None and self.goals_against is not None

    @property
    def is_clean_sheet(self) -> bool:
        """Conceded exactly zero. Only meaningful when `is_usable`."""
        return self.goals_against == 0

    @property
    def both_teams_scored(self) -> bool:
        """Both sides scored at least once. Only meaningful when `is_usable`."""
        return bool(self.goals_for and self.goals_against)


@dataclass(frozen=True)
class DerivedHistory:
    """
    The outcome of applying the cutoff and deriving both statistics over one
    team's eligible record set.

    `sample_size` is part of the result, not an afterthought. A 100% clean-sheet
    rate over one match and over twenty matches are different claims, and a
    consumer that only receives the percentage cannot tell them apart (TASK 15).
    Both percentages are computed over the SAME eligible set, so one sample size
    describes both.

    `clean_sheet_pct is None` means UNAVAILABLE - no eligible matches - and is
    deliberately distinct from 0.0, which means "played, never kept one".
    """

    clean_sheet_pct: Optional[float]
    both_teams_scored_pct: Optional[float]
    sample_size: int
    venue: str
    competition: Optional[str] = None

    @property
    def is_available(self) -> bool:
        """True when at least one eligible match backed the derivation."""
        return self.sample_size > 0


def completed_matches(
    records: Iterable[MatchRecord],
    venue: Optional[str] = None,
) -> Sequence[MatchRecord]:
    """
    Usable records, optionally restricted to one venue.

    Scheduled, postponed, cancelled and abandoned fixtures are dropped here, in
    one place, so no caller has to remember to exclude them.
    """
    usable = [record for record in records if record.is_usable]
    if venue is None:
        return usable
    return [record for record in usable if record.venue == venue]


def clean_sheet_pct(
    records: Iterable[MatchRecord],
    venue: Optional[str] = None,
) -> Optional[float]:
    """
    Fraction of completed matches in which the team conceded zero.

    Returns None when there are no completed matches. That is the whole point of
    this Epic: no matches played means the rate is UNDEFINED. Returning 0.0 would
    assert "this team has never kept a clean sheet", which is a specific and
    much stronger claim than "we have no matches to judge".
    """
    matches = completed_matches(records, venue)
    if not matches:
        return None
    return sum(1 for match in matches if match.is_clean_sheet) / len(matches)


def both_teams_scored_pct(
    records: Iterable[MatchRecord],
    venue: Optional[str] = None,
) -> Optional[float]:
    """
    Fraction of completed matches in which BOTH sides scored.

    Counted from actual scorelines - `goals_for > 0 AND goals_against > 0` - and
    never inferred from aggregate goals scored/conceded. A team averaging 2
    scored and 2 conceded per game may have a BTTS rate anywhere from 0% to
    100%; the aggregates simply do not contain the answer.

    Returns None when there are no completed matches, for the same reason as
    `clean_sheet_pct`.
    """
    matches = completed_matches(records, venue)
    if not matches:
        return None
    return sum(1 for match in matches if match.both_teams_scored) / len(matches)


def eligible_history(
    records: Iterable[MatchRecord],
    *,
    target_kickoff: datetime,
    venue: str,
    competition: Optional[str] = None,
    exclude_event_id: Optional[str] = None,
) -> List[MatchRecord]:
    """
    The records that may be used as evidence for a fixture kicking off at
    `target_kickoff` (Epic 1B.4, TASKS 8/9/11/12).

    Every argument after `records` is keyword-only, and `target_kickoff` and
    `venue` are REQUIRED. That is deliberate: there is no overload of this
    function that forgets the cutoff, so a caller cannot accidentally derive a
    statistic from matches that had not been played yet.

    Applied in order:

    1. Completion + both scores present (`completed_matches`).
    2. Venue - the filters are venue-specific and samples are never merged to
       inflate n.
    3. Competition, when requested. A record whose competition is unknown is
       EXCLUDED, not assumed to match; `None != "eng.1"`.
    4. Strict `kickoff < target_kickoff`. A record with no kickoff cannot prove
       it happened first, so it is excluded. `<` not `<=` - a match kicking off
       at exactly T is not evidence about T.
    5. The target event ID, when known (defence in depth behind the cutoff).
    6. Deduplication on `event_id`, first occurrence wins.

    Raises ValueError on a naive `target_kickoff` rather than comparing it to
    aware record timestamps, which would raise TypeError halfway through.
    """
    if target_kickoff.tzinfo is None:
        raise ValueError(
            "target_kickoff must be timezone-aware; got a naive datetime. "
            "An ambiguous cutoff cannot be compared safely against UTC records."
        )

    eligible: List[MatchRecord] = []
    seen_event_ids: set = set()

    for record in completed_matches(records, venue):
        if competition is not None and record.competition != competition:
            continue
        if record.kickoff is None or not record.kickoff < target_kickoff:
            continue
        if exclude_event_id is not None and record.event_id == exclude_event_id:
            continue
        if record.event_id is not None:
            if record.event_id in seen_event_ids:
                continue
            seen_event_ids.add(record.event_id)
        eligible.append(record)

    return eligible


def derive_history(
    records: Iterable[MatchRecord],
    *,
    target_kickoff: datetime,
    venue: str,
    competition: Optional[str] = None,
    exclude_event_id: Optional[str] = None,
) -> DerivedHistory:
    """
    Apply the cutoff and derive both statistics over what survives.

    This is the single entry point the pipeline uses. Both percentages come from
    the same eligible set, so `sample_size` describes both, and both are None
    when that set is empty.
    """
    eligible = eligible_history(
        records,
        target_kickoff=target_kickoff,
        venue=venue,
        competition=competition,
        exclude_event_id=exclude_event_id,
    )
    return DerivedHistory(
        clean_sheet_pct=clean_sheet_pct(eligible),
        both_teams_scored_pct=both_teams_scored_pct(eligible),
        sample_size=len(eligible),
        venue=venue,
        competition=competition,
    )
