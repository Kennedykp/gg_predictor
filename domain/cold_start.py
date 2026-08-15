"""
Point-in-time cold-start estimation of the five POISSON_V1 inputs (Epic 2C).

WHAT THIS DOES, AND WHAT IT REFUSES TO DO
-----------------------------------------
It replaces five NUMBERS. It does not replace the model that consumes them:

    history available before T
        -> point-in-time priors (previous season, league)
        -> current-season observations before T
        -> Gamma-Poisson posterior means (domain.team_strength)
        -> the SAME five inputs POISSON_V1 already takes
        -> UNCHANGED poisson.calculate_gg_probability

`poisson` is not imported here. Neither is any odds, decision, filter or
threshold module. This file cannot make a betting claim.

THE THREE-LEVEL HIERARCHY (Part 3)
----------------------------------
Epic 2A recommended previous-season team -> league baseline -> current season.
Implemented as the smallest defensible version: two nested shrinkages.

    level 3   previous-season LEAGUE venue rate            (broad, stable)
    level 2   team's previous-season venue rate, shrunk toward level 3
                  with k_prev_season
    level 1   current-season venue observations before T, shrunk toward
                  level 2 with k_goals_for / k_goals_against

Each level is the prior mean of the level below, so a club with a thin previous
season is not trusted at face value either - the same estimator runs twice
rather than a special case being written for the upper level.

VENUE-SPECIFIC PRIORS, NOT ONE LEAGUE AVERAGE
---------------------------------------------
Home sides score materially more than away sides. Shrinking an away rate toward
a venue-neutral league mean would import home advantage into the away estimate
and call it a prior. The league level therefore carries two rates:

    home_per_fixture   what home teams score, and away teams concede
    away_per_fixture   what away teams score, and home teams concede

and the pairing is CROSSED deliberately: the prior for `home_goals_conceded_home`
is the league AWAY scoring rate, because that is the quantity being predicted.

The fifth input, `league_avg_goals`, keeps its existing meaning exactly - goals
per team per match, total / (2 * fixtures). Changing it would silently rescale
both lambdas, since POISSON_V1 divides by it.

"PROMOTED" IS NOT CLAIMED (Part 12)
-----------------------------------
This module never asserts that a club was promoted. It records the fact it can
verify from history available at T: the team has NO previous-season record in
THIS competition. That is `NEW_TO_LEAGUE`. A promoted club and a club whose
previous season is missing from the dataset are indistinguishable from inside
the data, and labelling both "promoted" would be a guess presented as a finding.

Second-division rates are NOT imported. Epic 2A measured promoted clubs'
second-tier scoring overstating their subsequent top-flight scoring by a wide
margin, on nine cohorts of three clubs - far too little to build a production
transform on. A new club instead falls back to the destination-league prior,
optionally scaled by a factor that is SEARCHED on development data and defaults
to 1.0, meaning no adjustment.

If the competition itself has no previous-season fixtures before T, no team can
be called new to the league: the absence is the dataset's, not the club's. That
case yields a plain league prior and is counted separately, so it can never be
read as a promotion finding.

POINT-IN-TIME SAFETY (Part 6)
-----------------------------
The cutoff is not re-implemented. `domain.historical.matches_before` is called
with the target kickoff - the same strictly-`<` comparison the dataset layer and
the Epic 2B.3 harness already use. Everything downstream reads only that window,
so it follows from the rule alone that:

    - the target fixture cannot contribute (its kickoff is not < itself)
    - later fixtures cannot contribute
    - no final-season aggregate exists anywhere in this module
    - the previous season is usable only because all of it precedes T

Season membership is decided by each match's `season` field, so "previous
season" means season - 1 of the same competition, under the ESPN convention
where 2018 labels 2018/19.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from domain.historical import HistoricalMatch, matches_before
from domain.match_records import Venue
from domain.poisson_inputs import (
    LeagueBaseline,
    PointInTimePoissonInputs,
    VenueGoalAverages,
    build_poisson_inputs,
)
from domain.team_strength import (
    ESTIMATOR_VERSION,
    EstimatorConfig,
    PriorSource,
    ShrunkRate,
    posterior_mean,
    shrink_rate,
)

__all__ = [
    "ESTIMATOR_VERSION",
    "VenueEvidence",
    "LeagueRates",
    "LeagueEstimate",
    "ColdStartInputs",
    "venue_evidence",
    "league_rates",
    "estimate_league",
    "estimate_venue_rate",
    "estimate_inputs",
]


@dataclass(frozen=True)
class VenueEvidence:
    """One team's completed record at one venue over a fixed window."""

    goals_for: int
    goals_against: int
    matches: int

    @property
    def rate_for(self) -> Optional[float]:
        """Raw goals-for rate, or None with no matches. The GG-028 quantity."""
        return None if self.matches == 0 else self.goals_for / self.matches

    @property
    def rate_against(self) -> Optional[float]:
        return None if self.matches == 0 else self.goals_against / self.matches


@dataclass(frozen=True)
class LeagueRates:
    """
    A league's goal totals over a fixed window of fixtures.

    Rates are None when the window holds no completed fixtures - never 0.0,
    which would assert a goalless league.
    """

    fixtures: int
    home_goals: int
    away_goals: int

    @property
    def total_goals(self) -> int:
        return self.home_goals + self.away_goals

    @property
    def home_per_fixture(self) -> Optional[float]:
        return None if self.fixtures == 0 else self.home_goals / self.fixtures

    @property
    def away_per_fixture(self) -> Optional[float]:
        return None if self.fixtures == 0 else self.away_goals / self.fixtures

    @property
    def per_team_game(self) -> Optional[float]:
        """Goals per team per match: the POISSON_V1 `league_avg_goals` units."""
        return None if self.fixtures == 0 else self.total_goals / (2 * self.fixtures)


@dataclass(frozen=True)
class LeagueEstimate:
    """
    The point-in-time league baseline, plus the venue rates used as team priors.

    `per_team_game` is the fifth POISSON_V1 input. `home_rate` and `away_rate`
    are the venue-specific prior means for team estimates. `source` records
    which evidence produced them.
    """

    per_team_game: Optional[float]
    home_rate: Optional[float]
    away_rate: Optional[float]
    current_fixtures: int
    previous_fixtures: int
    source: str

    @property
    def is_available(self) -> bool:
        return self.per_team_game is not None


@dataclass(frozen=True)
class ColdStartInputs:
    """
    The five inputs plus the provenance of each estimated rate (Part 5).

    `inputs` is the exact `PointInTimePoissonInputs` type the production
    derivation returns, so the adapter feeding POISSON_V1 is identical in shape
    to the raw one. The extra fields exist so that no value is anonymous: for
    any rate we can say whether it came from current-season observation, a
    previous-season team prior, a league prior, or a shrinkage of them.
    """

    inputs: PointInTimePoissonInputs
    league: LeagueEstimate
    home_for: ShrunkRate
    home_against: ShrunkRate
    away_for: ShrunkRate
    away_against: ShrunkRate
    home_new_to_league: bool
    away_new_to_league: bool
    estimator_version: str = ESTIMATOR_VERSION

    @property
    def provenance(self) -> Dict[str, str]:
        """Per-input provenance labels, for artifacts and diagnostics."""
        return {
            "league_avg_goals": self.league.source,
            "home_goals_scored_home": self.home_for.provenance,
            "home_goals_conceded_home": self.home_against.provenance,
            "away_goals_scored_away": self.away_for.provenance,
            "away_goals_conceded_away": self.away_against.provenance,
        }


# ---------------------------------------------------------------------------
# Windows. One cutoff implementation, imported - never re-derived.
# ---------------------------------------------------------------------------


def _window(
    history: Iterable[HistoricalMatch],
    *,
    cutoff: datetime,
    competition: str,
    exclude_event_id: Optional[str],
) -> List[HistoricalMatch]:
    """
    Completed, model-eligible, same-competition matches strictly before cutoff.

    `matches_before` supplies the strict `<` and the eligibility filter. The
    event-id exclusion is defence in depth against a duplicated id sharing the
    target's kickoff, matching what the harness already does.
    """
    selected = matches_before(
        history,
        cutoff,
        competition=competition,
        eligible_only=True,
    )
    return [m for m in selected if m.event_id != exclude_event_id and m.has_result]


def _split_seasons(
    window: Sequence[HistoricalMatch],
    season: int,
) -> Tuple[List[HistoricalMatch], List[HistoricalMatch]]:
    """Partition a window into (current season, immediately previous season)."""
    current = [m for m in window if m.season == season]
    previous = [m for m in window if m.season == season - 1]
    return current, previous


def venue_evidence(
    matches: Iterable[HistoricalMatch],
    team_id: str,
    venue: str,
) -> VenueEvidence:
    """
    Sum one team's goals for/against at one venue. Venue purity is the point.

    A home-venue estimate reads only home matches: padding it with away matches
    would keep the input's name and change the statistic underneath it.
    """
    goals_for = goals_against = count = 0
    for match in matches:
        if venue == Venue.HOME and match.home_team_id == team_id:
            goals_for += match.home_goals or 0
            goals_against += match.away_goals or 0
            count += 1
        elif venue == Venue.AWAY and match.away_team_id == team_id:
            goals_for += match.away_goals or 0
            goals_against += match.home_goals or 0
            count += 1
    return VenueEvidence(goals_for=goals_for, goals_against=goals_against, matches=count)


def league_rates(matches: Iterable[HistoricalMatch]) -> LeagueRates:
    """Aggregate league home/away goals over completed fixtures, counted once."""
    fixtures = home = away = 0
    for match in matches:
        home += match.home_goals or 0
        away += match.away_goals or 0
        fixtures += 1
    return LeagueRates(fixtures=fixtures, home_goals=home, away_goals=away)


# ---------------------------------------------------------------------------
# Part 4 - the league baseline, point-in-time and shrunk
# ---------------------------------------------------------------------------


def estimate_league(
    current: Sequence[HistoricalMatch],
    previous: Sequence[HistoricalMatch],
    config: EstimatorConfig,
) -> LeagueEstimate:
    """
    Combine the previous season's league baseline with current-season fixtures.

    Epic 2A found the previous season's baseline more predictive than the first
    few fixtures of the current one. That is a statement about SAMPLE SIZE, not
    about a fixed weighting, so the combination is the same Gamma-Poisson
    posterior mean used everywhere else and the previous season's influence
    decays as current fixtures accumulate. No 70/30 constant exists here.

    UNITS. `k_league` is in FIXTURES. A per-fixture venue rate therefore uses
    n = fixtures with k = k_league, while the per-team-game rate uses
    n = 2 * fixtures with k = 2 * k_league, so both express the same quantity of
    prior evidence rather than differing by a factor of two.

    Both windows precede T, so nothing here can see a final-season aggregate.
    """
    cur = league_rates(current)
    prev = league_rates(previous)

    per_team_game = posterior_mean(
        cur.total_goals,
        2 * cur.fixtures,
        prev.per_team_game,
        2.0 * config.k_league,
    )
    home_rate = posterior_mean(
        cur.home_goals, cur.fixtures, prev.home_per_fixture, config.k_league
    )
    away_rate = posterior_mean(
        cur.away_goals, cur.fixtures, prev.away_per_fixture, config.k_league
    )

    if per_team_game is None:
        source = "UNAVAILABLE"
    elif prev.fixtures == 0 or config.k_league == 0.0:
        source = "LEAGUE_CURRENT_SEASON"
    elif cur.fixtures == 0:
        source = "LEAGUE_PREV_SEASON"
    else:
        source = "LEAGUE_SHRUNK"

    return LeagueEstimate(
        per_team_game=per_team_game,
        home_rate=home_rate,
        away_rate=away_rate,
        current_fixtures=cur.fixtures,
        previous_fixtures=prev.fixtures,
        source=source,
    )


# ---------------------------------------------------------------------------
# Part 3 - one team venue rate through the two-level prior
# ---------------------------------------------------------------------------


def estimate_venue_rate(
    *,
    current: VenueEvidence,
    previous: VenueEvidence,
    league_prior: Optional[float],
    prior_strength: float,
    prev_prior_strength: float,
    league_has_previous_season: bool,
    new_team_factor: float,
    for_goals: bool,
) -> Tuple[ShrunkRate, bool]:
    """
    One estimated venue rate, and whether the team is new to this competition.

    LEVEL 2. The team's previous-season venue rate is itself shrunk toward the
    league prior with `prev_prior_strength`, so a club with three away matches
    last season does not supply a confident prior for this one.

    LEVEL 1. Current-season observations are then shrunk toward that result.

    NEW TO LEAGUE. Claimed only when the competition HAS previous-season
    fixtures and this team has none of them. The prior is then the
    destination-league rate scaled by `new_team_factor`; no second-division rate
    is transformed and imported.
    """
    goals = current.goals_for if for_goals else current.goals_against
    prev_goals = previous.goals_for if for_goals else previous.goals_against

    new_to_league = league_has_previous_season and previous.matches == 0

    if previous.matches > 0:
        prior_mean = posterior_mean(
            prev_goals, previous.matches, league_prior, prev_prior_strength
        )
        source = (
            PriorSource.PREV_SEASON_TEAM
            if prior_mean is not None
            else PriorSource.UNAVAILABLE
        )
    elif new_to_league and league_prior is not None:
        prior_mean = league_prior * new_team_factor
        source = PriorSource.NEW_TO_LEAGUE
    else:
        # No previous season for the team AND none for the league in this
        # window: the gap belongs to the dataset, so this is a plain league
        # prior and must not be reported as a promotion signal.
        prior_mean = league_prior
        source = (
            PriorSource.LEAGUE_BASELINE
            if league_prior is not None
            else PriorSource.UNAVAILABLE
        )

    estimate = shrink_rate(
        goals,
        current.matches,
        prior_mean=prior_mean,
        prior_strength=prior_strength,
        prior_source=source,
    )
    return estimate, new_to_league


# ---------------------------------------------------------------------------
# Part 5 - the five inputs
# ---------------------------------------------------------------------------


def estimate_inputs(
    history: Iterable[HistoricalMatch],
    *,
    competition: str,
    season: int,
    target_kickoff: datetime,
    home_team_id: str,
    away_team_id: str,
    config: EstimatorConfig,
    exclude_event_id: Optional[str] = None,
) -> ColdStartInputs:
    """
    Estimate the five POISSON_V1 inputs for one target fixture.

    Deterministic: no randomness, no iteration-order dependence, no mutable
    module state. The same history and config always produce the same numbers.

    SAMPLE COUNTS on the returned `inputs` are CURRENT-SEASON venue matches -
    the evidence whose sparsity this Epic is about. The raw baseline counts all
    prior venue matches in the competition, so the two are not the same
    statistic and must not be compared as though they were; the evidence-bucket
    analysis derives one shared bucket for both arms instead.
    """
    window = _window(
        history,
        cutoff=target_kickoff,
        competition=competition,
        exclude_event_id=exclude_event_id,
    )
    current, previous = _split_seasons(window, season)

    league = estimate_league(current, previous, config)
    league_has_previous_season = bool(previous)

    home_current = venue_evidence(current, home_team_id, Venue.HOME)
    home_previous = venue_evidence(previous, home_team_id, Venue.HOME)
    away_current = venue_evidence(current, away_team_id, Venue.AWAY)
    away_previous = venue_evidence(previous, away_team_id, Venue.AWAY)

    # The crossed pairing is deliberate: what a home side CONCEDES is predicted
    # by the league's AWAY scoring rate, not by its home rate.
    home_for, home_new = estimate_venue_rate(
        current=home_current,
        previous=home_previous,
        league_prior=league.home_rate,
        prior_strength=config.k_goals_for,
        prev_prior_strength=config.k_prev_season,
        league_has_previous_season=league_has_previous_season,
        new_team_factor=config.new_team_attack_factor,
        for_goals=True,
    )
    home_against, _ = estimate_venue_rate(
        current=home_current,
        previous=home_previous,
        league_prior=league.away_rate,
        prior_strength=config.k_goals_against,
        prev_prior_strength=config.k_prev_season,
        league_has_previous_season=league_has_previous_season,
        new_team_factor=config.new_team_defence_factor,
        for_goals=False,
    )
    away_for, away_new = estimate_venue_rate(
        current=away_current,
        previous=away_previous,
        league_prior=league.away_rate,
        prior_strength=config.k_goals_for,
        prev_prior_strength=config.k_prev_season,
        league_has_previous_season=league_has_previous_season,
        new_team_factor=config.new_team_attack_factor,
        for_goals=True,
    )
    away_against, _ = estimate_venue_rate(
        current=away_current,
        previous=away_previous,
        league_prior=league.home_rate,
        prior_strength=config.k_goals_against,
        prev_prior_strength=config.k_prev_season,
        league_has_previous_season=league_has_previous_season,
        new_team_factor=config.new_team_defence_factor,
        for_goals=False,
    )

    # Reuse the production assembler so the "five inputs" contract keeps exactly
    # one implementation. A None rate propagates as None and the fixture becomes
    # unevaluable, precisely as it does today: the missing-data safeguard is
    # untouched, not weakened.
    inputs = build_poisson_inputs(
        VenueGoalAverages(
            avg_goals_for=home_for.value,
            avg_goals_against=home_against.value,
            sample_size=home_current.matches,
            venue=Venue.HOME,
            competition=competition,
            cutoff=target_kickoff,
        ),
        VenueGoalAverages(
            avg_goals_for=away_for.value,
            avg_goals_against=away_against.value,
            sample_size=away_current.matches,
            venue=Venue.AWAY,
            competition=competition,
            cutoff=target_kickoff,
        ),
        LeagueBaseline(
            avg_goals_per_team=league.per_team_game,
            fixtures=league.current_fixtures,
            total_goals=league_rates(current).total_goals,
            competition=competition,
            cutoff=target_kickoff,
        ),
        target_kickoff=target_kickoff,
        competition=competition,
        season=season,
    )

    return ColdStartInputs(
        inputs=inputs,
        league=league,
        home_for=home_for,
        home_against=home_against,
        away_for=away_for,
        away_against=away_against,
        home_new_to_league=home_new,
        away_new_to_league=away_new,
    )
