"""
Point-in-time POISSON_V1 inputs, derived from match records (Epic 1B.5).

WHY THIS MODULE EXISTS
----------------------
POISSON_V1 needs exactly five numbers:

    lambda_home = (home_GF_home * away_GA_away) / league_avg_goals
    lambda_away = (away_GF_away * home_GA_home) / league_avg_goals

Until now all five came from ESPN endpoints that report the CURRENT state of a
season. Evaluating a fixture from 1 December with those numbers folds in matches
played in January, February and March - the model is told the future and then
asked to predict it. That is LEAK-001.

This module rebuilds the same five numbers from individual completed matches
that kicked off strictly before the target fixture.

THE MODEL DOES NOT CHANGE. Not one arithmetic expression in `poisson.py` is
touched. The semantics of each input are preserved exactly; only the provenance
moves, from "whatever the season looks like today" to "what was known before
kickoff". A statistic with the same name and a different meaning would be a far
worse outcome than the leak itself, because it would be invisible.

UNITS ARE THE WHOLE GAME HERE
-----------------------------
`league_avg_goals` is goals per TEAM per match, not goals per fixture. Two
independent sources agree, and they are quoted rather than paraphrased because
getting this wrong silently halves or doubles every lambda the system produces:

    poisson.py:26   "league_avg_goals: League average goals per team per match"
    GG.md:110       "League average goals (per team)"

So a 2-1 fixture contributes 3 goals across 2 team-games, not 3 across 1. The
divisor is `2 * fixtures`. Epic 1B.2 measured EPL 2025-26 at 1045/760 = 1.3750
from standings; the same season derived here from 380 match records gives
1045/(2*380) = 1.3750. The two agree exactly, which is the cross-check that this
module reproduces the established quantity rather than inventing a near neighbour.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

from domain.match_records import MatchRecord, Venue, eligible_history

__all__ = [
    "VenueGoalAverages",
    "LeagueBaseline",
    "PointInTimePoissonInputs",
    "derive_venue_averages",
    "derive_league_baseline",
    "build_poisson_inputs",
]


@dataclass(frozen=True)
class VenueGoalAverages:
    """
    One team's scoring and conceding rate at ONE venue, before a cutoff.

    Two of these plus a league baseline are the complete POISSON_V1 input set.

    `sample_size` is carried, not discarded. A 3.0 goals-per-game average over
    one match and over nineteen are different claims, and a consumer holding
    only the float cannot tell them apart. No minimum is imposed here (TASK 17):
    n=1 is reported honestly as n=1, and whether that is enough evidence is a
    calibration question for a later Epic, not a rule to smuggle in silently.

    `avg_goals_for is None` means UNAVAILABLE - no eligible matches - and is
    deliberately distinct from 0.0, which means "played, and genuinely scored
    none". Collapsing those two was GG-001.
    """

    avg_goals_for: Optional[float]
    avg_goals_against: Optional[float]
    sample_size: int
    venue: str
    competition: Optional[str] = None
    cutoff: Optional[datetime] = None

    @property
    def is_available(self) -> bool:
        """True when at least one eligible match backed these averages."""
        return self.sample_size > 0


@dataclass(frozen=True)
class LeagueBaseline:
    """
    The league's goals-per-team-per-match figure, before a cutoff.

    `total_goals` and `fixtures` are kept alongside the rate so the arithmetic
    stays auditable: a reader can confirm the divisor was `2 * fixtures` and not
    `fixtures`. That unit error does not raise, does not look wrong in a log, and
    would scale every prediction the system makes by exactly 2.
    """

    avg_goals_per_team: Optional[float]
    fixtures: int
    total_goals: int
    competition: Optional[str] = None
    cutoff: Optional[datetime] = None

    @property
    def is_available(self) -> bool:
        """True when at least one eligible fixture backed the baseline."""
        return self.fixtures > 0


@dataclass(frozen=True)
class PointInTimePoissonInputs:
    """
    The complete, validated POISSON_V1 input set for one fixture.

    Either every one of the five values is present, or `is_complete` is False and
    `missing` names precisely what was absent. There is no partially-usable
    state: POISSON_V1 multiplies its inputs together, so a single missing term
    makes the result meaningless rather than merely less precise.
    """

    league_avg_goals: Optional[float]
    home_goals_scored_home: Optional[float]
    home_goals_conceded_home: Optional[float]
    away_goals_scored_away: Optional[float]
    away_goals_conceded_away: Optional[float]

    # Provenance (TASK 24). Recorded from what actually happened, never inferred
    # from the values themselves - 1.375 looks identical whether it was derived
    # from records or hardcoded, which is how the 1.35 constant survived so long.
    target_kickoff: Optional[datetime] = None
    competition: Optional[str] = None
    season: Optional[int] = None
    home_sample: int = 0
    away_sample: int = 0
    league_sample: int = 0

    @property
    def missing(self) -> List[str]:
        """Names of the absent inputs, in the order POISSON_V1 declares them."""
        names = (
            "league_avg_goals",
            "home_goals_scored_home",
            "home_goals_conceded_home",
            "away_goals_scored_away",
            "away_goals_conceded_away",
        )
        return [name for name in names if getattr(self, name) is None]

    @property
    def is_complete(self) -> bool:
        """True only when all five model inputs are present."""
        return not self.missing


def _mean(total: int, count: int) -> Optional[float]:
    """
    `total / count`, or None when there is nothing to average.

    Guarded by an explicit `count <= 0` test rather than by catching
    ZeroDivisionError and returning 0.0 (TASK 16). Those differ in meaning: the
    exception says "this rate does not exist", while 0.0 asserts "this team
    scores zero goals per match" - a strong, false, and entirely fabricated
    claim about a team that has simply not played yet.
    """
    if count <= 0:
        return None
    return total / count


def derive_venue_averages(
    records: Iterable[MatchRecord],
    *,
    target_kickoff: datetime,
    venue: str,
    competition: Optional[str] = None,
    exclude_event_id: Optional[str] = None,
) -> VenueGoalAverages:
    """
    Goals for/against per match at one venue, over eligible prior records.

    The cutoff, venue purity, competition boundary, target-fixture exclusion and
    deduplication all come from `eligible_history` (Epic 1B.4) rather than being
    re-implemented here. One cutoff implementation means one place to audit, and
    no second copy to drift out of step with the first.

    `target_kickoff` and `venue` are required keyword arguments, inherited from
    that same function: there is no call signature that quietly averages a
    team's entire season (TASK 7).

    Worked example - a home team's prior HOME matches 2-0, 1-1, 3-2, 0-0, 2-1:
        GF = 8, GA = 4, matches = 5  ->  1.60 for, 0.80 against
    """
    eligible = eligible_history(
        records,
        target_kickoff=target_kickoff,
        venue=venue,
        competition=competition,
        exclude_event_id=exclude_event_id,
    )

    # `eligible_history` has already dropped anything without both scores, so
    # these `or 0` fallbacks are unreachable defensive noise for the type
    # checker, not a silent substitution of zero for a missing goal count.
    goals_for = sum(record.goals_for or 0 for record in eligible)
    goals_against = sum(record.goals_against or 0 for record in eligible)
    count = len(eligible)

    return VenueGoalAverages(
        avg_goals_for=_mean(goals_for, count),
        avg_goals_against=_mean(goals_against, count),
        sample_size=count,
        venue=venue,
        competition=competition,
        cutoff=target_kickoff,
    )


def derive_league_baseline(
    records: Iterable[MatchRecord],
    *,
    target_kickoff: datetime,
    competition: Optional[str] = None,
    exclude_event_id: Optional[str] = None,
) -> LeagueBaseline:
    """
    League goals per team per match, over eligible prior fixtures.

    Records are read from the HOME perspective, which is what makes this correct
    when league history is assembled from individual team schedules (TASK 12).
    Every fixture then appears twice - once as one team's HOME record and once as
    the opponent's AWAY record - and taking only the HOME side counts each
    fixture exactly once. `eligible_history` additionally deduplicates on event
    ID, so a repeated payload entry cannot inflate the denominator either.

    Counting a fixture twice would not raise or look wrong; it would just quietly
    weight some fixtures double.

    THE DIVISOR. A single record's `goals_for + goals_against` is that fixture's
    total goals, spread across TWO teams, so:

        avg_goals_per_team = total_goals / (2 * fixtures)

    Worked example - eligible fixtures 2-1, 1-1, 0-2, 3-0:
        total goals = 3 + 2 + 2 + 3 = 10
        fixtures    = 4  ->  team-games = 8
        baseline    = 10 / 8 = 1.25   (NOT 10/4 = 2.5)
    """
    eligible = eligible_history(
        records,
        target_kickoff=target_kickoff,
        venue=Venue.HOME,
        competition=competition,
        exclude_event_id=exclude_event_id,
    )

    total_goals = sum(
        (record.goals_for or 0) + (record.goals_against or 0) for record in eligible
    )
    fixtures = len(eligible)

    return LeagueBaseline(
        avg_goals_per_team=_mean(total_goals, 2 * fixtures),
        fixtures=fixtures,
        total_goals=total_goals,
        competition=competition,
        cutoff=target_kickoff,
    )


def build_poisson_inputs(
    home_averages: Optional[VenueGoalAverages],
    away_averages: Optional[VenueGoalAverages],
    league_baseline: Optional[LeagueBaseline],
    *,
    target_kickoff: Optional[datetime] = None,
    competition: Optional[str] = None,
    season: Optional[int] = None,
) -> PointInTimePoissonInputs:
    """
    Assemble the five model inputs from the three derived pieces (TASK 18).

    THE VENUE ASYMMETRY IS THE POINT. The home team contributes only its HOME
    record and the away team only its AWAY record, because that is what the
    parameter names in `poisson.py` already mean:

        home_goals_scored_home    <- home team, HOME matches
        home_goals_conceded_home  <- home team, HOME matches
        away_goals_scored_away    <- away team, AWAY matches
        away_goals_conceded_away  <- away team, AWAY matches

    Padding a thin venue sample with the other venue's matches would keep the
    parameter name and change the statistic underneath it (TASK 6). Small samples
    are a modelling problem for a later Epic; a mislabelled input is a defect now.

    A None piece propagates as None, never as a substituted default. There is no
    fallback to current-season aggregates here (TASK 25): that would reintroduce
    the leak precisely when history is thin, which is exactly when a caller is
    least likely to notice.
    """
    return PointInTimePoissonInputs(
        league_avg_goals=(
            league_baseline.avg_goals_per_team if league_baseline is not None else None
        ),
        home_goals_scored_home=(
            home_averages.avg_goals_for if home_averages is not None else None
        ),
        home_goals_conceded_home=(
            home_averages.avg_goals_against if home_averages is not None else None
        ),
        away_goals_scored_away=(
            away_averages.avg_goals_for if away_averages is not None else None
        ),
        away_goals_conceded_away=(
            away_averages.avg_goals_against if away_averages is not None else None
        ),
        target_kickoff=target_kickoff,
        competition=competition,
        season=season,
        home_sample=home_averages.sample_size if home_averages is not None else 0,
        away_sample=away_averages.sample_size if away_averages is not None else 0,
        league_sample=league_baseline.fixtures if league_baseline is not None else 0,
    )
