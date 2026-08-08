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

STATUS: the derivations below are correct and tested, but nothing in production
calls them yet, because `espn.get_team_stats` reads the standings endpoint,
which supplies aggregates only. See docs/EPIC_1B3_FILTER_WIRING.md - the
per-team schedule endpoint that carries these records is identified there as the
verified next step. Until a provider supplies the records, the statistics are
reported UNAVAILABLE rather than approximated.
"""

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

__all__ = [
    "Venue",
    "MatchRecord",
    "completed_matches",
    "clean_sheet_pct",
    "both_teams_scored_pct",
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
    """

    venue: str
    goals_for: Optional[int]
    goals_against: Optional[int]
    completed: bool

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
