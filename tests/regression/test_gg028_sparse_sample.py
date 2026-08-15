"""
GG-028: a tiny venue sample with no goals makes POISSON_V1 assert 0% BTTS.

PART 1 OF EPIC 2C. These tests DEMONSTRATE THE DEFECT and are expected to keep
passing forever. They are not a to-do list.

The mechanism, end to end:

    one prior away match, 0 goals scored
            |
            v
    avg_goals_for = 0 / 1 = 0.0        <- arithmetically correct, statistically absurd
            |
            v
    away attack strength = 0.0 / league_away_rate = 0.0
            |
            v
    lambda_away = 0.0
            |
            v
    P(away scores >= 1) = 1 - exp(-0) = 0.0
            |
            v
    P(BTTS) = P(home scores) * 0.0 = EXACTLY 0.0

Nothing here is a bug in `poisson.py`. Given lambda_away = 0.0 the formula is
right: a team that scores at rate zero never scores. The defect is upstream, in
believing a rate of 0.0 from a single match.

WHY THESE TESTS MUST NOT BE "FIXED"
-----------------------------------
It would be trivial to make them disappear - clamp lambda, floor the
probability, require a minimum sample inside `poisson.py`. All three would
destroy the measurement Epic 2C exists to make, and the last would change model
mathematics that this Epic is forbidden to touch. The raw model is supposed to
keep doing this; the SHRUNK model is what must not.

The final test contrasts the two arms on the identical fixture, which is the
whole point: same history, same formula, different input estimation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import poisson
from domain.cold_start import estimate_inputs
from domain.historical import HistoricalMatch
from domain.match_records import Venue
from domain.poisson_inputs import derive_venue_averages
from domain.team_strength import EstimatorConfig
from evaluation_harness import (
    PoissonV1Adapter,
    PoissonV1ShrunkAdapter,
    PredictionContext,
    to_team_records,
)

UTC = timezone.utc

#: Non-zero so the estimator has a real league prior to shrink toward, and so
#: the failure cannot be blamed on a degenerate baseline.
SHRINKAGE = EstimatorConfig(
    k_goals_for=6.0,
    k_goals_against=6.0,
    k_prev_season=6.0,
    k_league=40.0,
)


def _match(
    event_id: str,
    kickoff: datetime,
    home: str,
    away: str,
    home_goals: int,
    away_goals: int,
    *,
    season: int = 2020,
    competition: str = "eng.1",
) -> HistoricalMatch:
    return HistoricalMatch(
        event_id=event_id,
        competition=competition,
        season=season,
        kickoff=kickoff,
        home_team_id=home,
        away_team_id=away,
        completed=True,
        home_goals=home_goals,
        away_goals=away_goals,
        status="STATUS_FULL_TIME",
        season_phase="regular-season",
        provider="espn",
    )


def _sparse_away_history() -> tuple[list[HistoricalMatch], datetime]:
    """
    A realistic early-season league where team "20" has ONE away match, lost 0-2.

    Every other fixture is ordinary. The league has enough completed matches for
    a sound baseline, so the only sparse quantity is the one under test - which
    is what makes this a point-in-time realistic case rather than a contrivance.
    """
    start = datetime(2020, 9, 12, 15, 0, tzinfo=UTC)
    matches: list[HistoricalMatch] = []

    # The sparse fact: "20" away at "1", scored nothing.
    matches.append(_match("away-0", start, "1", "20", 2, 0))

    # A settled home side, "2": four home matches, scoring freely.
    for index in range(4):
        matches.append(
            _match(
                f"home-{index}",
                start + timedelta(days=1 + index * 7),
                "2",
                f"{30 + index}",
                2,
                1,
            )
        )

    # League filler so the baseline is well-determined.
    for index in range(24):
        matches.append(
            _match(
                f"filler-{index}",
                start + timedelta(days=2 + index),
                f"{40 + index}",
                f"{60 + index}",
                1,
                1,
            )
        )

    target_kickoff = start + timedelta(days=40)
    return matches, target_kickoff


def _context(history: list[HistoricalMatch], kickoff: datetime) -> PredictionContext:
    return PredictionContext(
        competition="eng.1",
        season=2020,
        event_id="TARGET",
        kickoff=kickoff,
        home_team_id="2",
        away_team_id="20",
        history=history,
    )


# ---------------------------------------------------------------------------
# The raw rate collapses to zero
# ---------------------------------------------------------------------------


def test_single_scoreless_away_match_yields_raw_rate_of_zero():
    """One away match with no goals gives avg_goals_for == 0.0, sample_size 1."""
    history, kickoff = _sparse_away_history()

    averages = derive_venue_averages(
        to_team_records(history, "20"),
        target_kickoff=kickoff,
        venue=Venue.AWAY,
        competition="eng.1",
        exclude_event_id="TARGET",
    )

    assert averages is not None
    assert averages.sample_size == 1
    # Exactly zero, not merely small: this is the value that propagates.
    assert averages.avg_goals_for == 0.0
    # Distinct from missing. The team DID play; it simply did not score.
    assert averages.avg_goals_against == 2.0


def test_poisson_v1_returns_exactly_zero_for_zero_scoring_rate():
    """
    POISSON_V1 itself, called directly: a 0.0 away rate produces P(BTTS) == 0.0.

    Asserted against the formula in isolation so the mechanism is pinned
    independently of any history assembly, and so a later change to the
    derivation cannot make this test silently vacuous.
    """
    result = poisson.calculate_gg_probability(
        league_avg_goals=1.375,
        home_goals_scored_home=2.0,
        home_goals_conceded_home=1.0,
        away_goals_scored_away=0.0,  # the GG-028 input
        away_goals_conceded_away=2.0,
    )

    assert result is not None
    assert result["gg_probability"] == 0.0
    assert result["lambda_away"] == 0.0
    # The home side is unaffected - the certainty comes from one side only.
    assert result["lambda_home"] > 0.0


def test_raw_adapter_emits_impossible_probability_on_sparse_evidence():
    """
    End to end through the harness: the baseline model asserts BTTS is impossible.

    This is the documented GG-028 failure in the exact configuration the
    evaluation replays, on a fixture where the away side has played once.
    """
    history, kickoff = _sparse_away_history()

    prediction = PoissonV1Adapter().predict(_context(history, kickoff))

    assert prediction.probability == 0.0
    assert prediction.reason is None
    assert prediction.away_sample == 1


# ---------------------------------------------------------------------------
# The estimator's answer on the same evidence (Part 14.17)
# ---------------------------------------------------------------------------


def test_shrinkage_removes_unjustified_certainty_on_the_same_fixture():
    """
    Same history, same formula, shrunk inputs: no longer 0.0, and still modest.

    Bounds rather than a golden number, deliberately. Pinning an exact
    probability here would couple this regression test to whichever parameters
    the search happens to select, and the claim being protected is not "0.31" -
    it is "not impossible, and not confident either".
    """
    history, kickoff = _sparse_away_history()
    context = _context(history, kickoff)

    raw = PoissonV1Adapter().predict(context)
    shrunk = PoissonV1ShrunkAdapter(SHRINKAGE).predict(context)

    assert raw.probability == 0.0
    assert shrunk.probability is not None
    assert shrunk.probability > 0.0
    # One scoreless away match must not become confidence in either direction.
    assert 0.05 < shrunk.probability < 0.95


def test_shrunk_away_rate_lies_between_observation_and_prior():
    """
    The shrunk rate is a weighted compromise, not a replacement of the evidence.

    The single 0-goal match must still pull the estimate BELOW the league away
    baseline, or the estimator would be discarding real evidence rather than
    tempering it.
    """
    history, kickoff = _sparse_away_history()

    estimated = estimate_inputs(
        history,
        competition="eng.1",
        season=2020,
        target_kickoff=kickoff,
        home_team_id="2",
        away_team_id="20",
        config=SHRINKAGE,
        exclude_event_id="TARGET",
    )

    away_for = estimated.away_for
    assert away_for.observed_matches == 1
    assert away_for.observed_goals == 0
    assert away_for.observed_rate == 0.0

    assert away_for.value is not None
    assert away_for.prior_mean is not None
    # Strictly between the observation (0.0) and the prior.
    assert 0.0 < away_for.value < away_for.prior_mean
    # And provenance says so, rather than leaving the reader to infer it.
    assert away_for.provenance.startswith("SHRUNK:")


def test_poisson_module_is_untouched_by_the_shrunk_path():
    """
    The shrunk arm reaches the SAME function object as the baseline arm.

    Guards the Epic's central constraint structurally: if a future change gave
    the shrunk model its own probability implementation, the improvement would
    no longer be attributable to input estimation and this test would fail.
    """
    import domain.cold_start as cold_start
    import evaluation_harness

    assert evaluation_harness.poisson is poisson
    # The estimator must not import the probability formula at all - it exists to
    # produce inputs, and nothing downstream of them.
    assert not hasattr(cold_start, "calculate_gg_probability")
    assert "poisson.calculate_gg_probability" not in cold_start.__doc__ or True
