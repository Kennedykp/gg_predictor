"""
Point-in-time safety and fair-comparison guards for the Epic 2C estimator.

Covers Part 14 items 6-13, 15 and 16. Everything here is a LEAKAGE OR FAIRNESS
guard: each test describes a way the Epic could produce an impressive number
that means nothing, and pins the behaviour that prevents it.

Several tests are MUTATION TESTS (Part 14, closing note). Rather than only
asserting that correct input gives correct output, they inject the forbidden
information and require the estimate to be unchanged. A guard that is merely
"probably applied somewhere" passes the first style and fails the second.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from domain.cold_start import estimate_inputs, venue_evidence
from domain.comparison import (
    compare,
    evidence_bucket,
    evidence_bucket_table,
    extreme_probability_stats,
    intersect,
)
from domain.evaluation import BttsOutcome, PredictionRecord
from domain.historical import HistoricalMatch
from domain.match_records import Venue
from domain.team_strength import EstimatorConfig, PriorSource

UTC = timezone.utc

CONFIG = EstimatorConfig(
    k_goals_for=6.0,
    k_goals_against=6.0,
    k_prev_season=6.0,
    k_league=40.0,
)

SEASON_START = datetime(2020, 9, 12, 15, 0, tzinfo=UTC)
TARGET_KICKOFF = SEASON_START + timedelta(days=60)


def _match(
    event_id: str,
    kickoff: datetime,
    home: str,
    away: str,
    home_goals: int = 1,
    away_goals: int = 1,
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


def _league(
    *,
    season: int = 2020,
    start: datetime = SEASON_START,
    count: int = 30,
    prefix: str = "cur",
) -> list[HistoricalMatch]:
    """Filler fixtures so the league baseline is well determined."""
    return [
        _match(
            f"{prefix}-{index}",
            start + timedelta(days=index),
            f"{40 + index}",
            f"{70 + index}",
            1,
            1,
            season=season,
        )
        for index in range(count)
    ]


def _estimate(history, **overrides):
    kwargs = dict(
        competition="eng.1",
        season=2020,
        target_kickoff=TARGET_KICKOFF,
        home_team_id="2",
        away_team_id="20",
        config=CONFIG,
        exclude_event_id="TARGET",
    )
    kwargs.update(overrides)
    return estimate_inputs(history, **kwargs)


# ---------------------------------------------------------------------------
# Part 14.6 - 14.8: cutoff, target leakage, future leakage
# ---------------------------------------------------------------------------


def test_cutoff_is_strictly_before_target_kickoff():
    """
    Part 14.6. A match kicking off at EXACTLY the target time is excluded.

    Simultaneous kickoffs are routine on a final matchday, and `<=` would let a
    concurrent result inform a prediction that in reality could not have seen it.
    """
    history = _league()
    history.append(_match("simultaneous", TARGET_KICKOFF, "2", "99", 5, 0))

    estimated = _estimate(history)

    # The home side's only "evidence" was simultaneous, so it contributed nothing.
    assert estimated.home_for.observed_matches == 0
    assert estimated.home_for.provenance.startswith("PRIOR_ONLY:")


def test_target_match_itself_cannot_inform_its_own_prediction():
    """
    Part 14.7. The target's own result is excluded even if it appears in history.

    Belt and braces: the harness already removes it by event id AND the kickoff
    rule excludes it. Here the target is deliberately planted in the history with
    a wildly atypical scoreline, and the estimate must not move.
    """
    history = _league()
    history.extend(
        _match(f"h-{i}", SEASON_START + timedelta(days=1 + i * 7), "2", f"{30 + i}", 1, 1)
        for i in range(4)
    )

    without_target = _estimate(history)
    with_target = _estimate(
        history + [_match("TARGET", TARGET_KICKOFF, "2", "20", 9, 9)],
    )

    assert with_target.home_for.value == without_target.home_for.value
    assert with_target.home_for.observed_matches == without_target.home_for.observed_matches


def test_future_matches_cannot_inform_the_estimate():
    """
    Part 14.8. Mutation test: adding later fixtures must change nothing at all.

    Ten emphatic future results for both teams are appended. If any cutoff were
    `<=`, mis-ordered, or applied after aggregation, these would move the
    estimate; the assertion is exact equality of the five inputs.
    """
    history = _league()
    history.extend(
        _match(f"h-{i}", SEASON_START + timedelta(days=1 + i * 7), "2", f"{30 + i}", 2, 1)
        for i in range(4)
    )
    history.extend(
        _match(f"a-{i}", SEASON_START + timedelta(days=3 + i * 7), f"{50 + i}", "20", 1, 1)
        for i in range(4)
    )

    before = _estimate(history)
    future = [
        _match(f"future-{i}", TARGET_KICKOFF + timedelta(days=1 + i), "2", "20", 7, 7)
        for i in range(10
        )
    ]
    after = _estimate(history + future)

    assert after.inputs.home_goals_scored_home == before.inputs.home_goals_scored_home
    assert after.inputs.home_goals_conceded_home == before.inputs.home_goals_conceded_home
    assert after.inputs.away_goals_scored_away == before.inputs.away_goals_scored_away
    assert after.inputs.away_goals_conceded_away == before.inputs.away_goals_conceded_away
    assert after.inputs.league_avg_goals == before.inputs.league_avg_goals


def test_future_seasons_cannot_inform_the_estimate():
    """A NEXT-season fixture is future data even though its season label differs."""
    history = _league()
    before = _estimate(history)

    next_season = _league(
        season=2021,
        start=TARGET_KICKOFF + timedelta(days=300),
        count=20,
        prefix="next",
    )
    after = _estimate(history + next_season)

    assert after.inputs.league_avg_goals == before.inputs.league_avg_goals
    assert after.league.current_fixtures == before.league.current_fixtures


# ---------------------------------------------------------------------------
# Part 14.9 - 14.10: previous season and league prior safety
# ---------------------------------------------------------------------------


def test_previous_season_history_is_used_only_when_chronologically_prior():
    """
    Part 14.9. Last season's matches inform the prior; they precede the target.

    The distinguishing assertion is on PROVENANCE, not merely on the value: a
    numerically different estimate could arise for many reasons, but the label
    PREV_SEASON_TEAM asserts specifically that the team's own earlier season was
    the prior's source.
    """
    previous_start = SEASON_START - timedelta(days=365)
    previous = _league(season=2019, start=previous_start, count=30, prefix="prev")
    previous.extend(
        _match(
            f"prev-away-{i}",
            previous_start + timedelta(days=2 + i * 7),
            f"{50 + i}",
            "20",
            1,
            2,
            season=2019,
        )
        for i in range(10)
    )

    estimated = _estimate(_league() + previous)

    assert estimated.away_for.prior_source is PriorSource.PREV_SEASON_TEAM
    assert estimated.away_new_to_league is False
    assert estimated.league.previous_fixtures > 0


def test_league_prior_is_point_in_time_safe():
    """
    Part 14.10. The league baseline never uses the season's final average.

    Mutation test: high-scoring fixtures are added AFTER the target. A league
    baseline computed over the whole season - the classic version of this
    mistake, and the one Epic 2A specifically warned about - would rise. It must
    not move at all.
    """
    history = _league()
    before = _estimate(history)

    late_goals = [
        _match(f"late-{i}", TARGET_KICKOFF + timedelta(days=i + 1), f"{80 + i}", f"{90 + i}", 6, 5)
        for i in range(15)
    ]
    after = _estimate(history + late_goals)

    assert after.league.per_team_game == before.league.per_team_game
    assert after.league.home_rate == before.league.home_rate
    assert after.league.away_rate == before.league.away_rate


def test_previous_season_league_baseline_contributes_before_current_season_matures():
    """
    Part 4. Early in a season, the previous season's baseline carries weight.

    Epic 2A found last season's baseline more predictive than the first few
    current fixtures. With k_league in team-games, three fixtures (6 team-games)
    against k_league=40 means the prior dominates - which is asserted here as a
    labelled source rather than assumed from the weighting.
    """
    previous_start = SEASON_START - timedelta(days=365)
    previous = _league(season=2019, start=previous_start, count=30, prefix="prev")
    # A deliberately high-scoring previous season, so its influence is visible.
    previous = [
        _match(m.event_id, m.kickoff, m.home_team_id, m.away_team_id, 3, 2, season=2019)
        for m in previous
    ]
    sparse_current = _league(count=3)

    estimated = _estimate(sparse_current + previous)

    assert estimated.league.source == "LEAGUE_SHRUNK"
    assert estimated.league.current_fixtures == 3
    assert estimated.league.previous_fixtures == 30
    # Pulled well above the current season's own 1.0 per team by the previous
    # season's 2.5, because 6 team-games cannot outweigh k_league = 40.
    assert estimated.league.per_team_game > 2.0



# ---------------------------------------------------------------------------
# Part 14.11: promoted / new-to-league handling
# ---------------------------------------------------------------------------


def test_team_new_to_the_competition_falls_back_to_destination_league_prior():
    """
    Part 14.11. A club with no prior season here is labelled NEW_TO_LEAGUE.

    The prior is the DESTINATION league's rate, never a second-tier rate carried
    over unchanged. The flag is asserted so downstream analysis can report
    promoted clubs separately without re-deriving the status.
    """
    previous_start = SEASON_START - timedelta(days=365)
    previous = _league(season=2019, start=previous_start, count=30, prefix="prev")
    # Team "20" appears nowhere in 2019: it is new to eng.1 at this cutoff.

    estimated = _estimate(_league() + previous)

    assert estimated.away_new_to_league is True
    assert estimated.away_for.prior_source is PriorSource.NEW_TO_LEAGUE
    assert estimated.away_for.prior_mean is not None


def test_new_team_factors_scale_the_prior_in_the_expected_direction():
    """
    A promotion adjustment below 1.0 lowers the attack prior; 1.0 is inert.

    Documents that the factor is a SEARCHED parameter with a defined effect, not
    a hardcoded 0.605 or 0.72. The neutral setting must be exactly inert, or the
    "no adjustment" arm of the search would not be a true control.
    """
    previous_start = SEASON_START - timedelta(days=365)
    history = _league() + _league(season=2019, start=previous_start, count=30, prefix="prev")

    neutral = _estimate(history, config=CONFIG.with_values(new_team_attack_factor=1.0))
    damped = _estimate(history, config=CONFIG.with_values(new_team_attack_factor=0.75))

    assert neutral.away_for.prior_mean is not None
    assert damped.away_for.prior_mean is not None
    assert damped.away_for.prior_mean < neutral.away_for.prior_mean
    assert damped.away_for.prior_mean == pytest.approx(0.75 * neutral.away_for.prior_mean)


# ---------------------------------------------------------------------------
# Part 14.15: provenance
# ---------------------------------------------------------------------------


def test_every_input_carries_provenance():
    """
    Part 14.15. All five inputs report where their value came from.

    No silent substitutions: a reader can tell which of the five was observed,
    which came from a team prior, which from the league, and which was shrunk.
    """
    previous_start = SEASON_START - timedelta(days=365)
    history = _league() + _league(season=2019, start=previous_start, count=30, prefix="prev")

    provenance = _estimate(history).provenance

    assert set(provenance) == {
        "league_avg_goals",
        "home_goals_scored_home",
        "home_goals_conceded_home",
        "away_goals_scored_away",
        "away_goals_conceded_away",
    }
    assert all(label for label in provenance.values())


def test_venue_evidence_counts_only_the_requested_venue():
    """
    Home and away evidence must not be pooled - the whole model is venue-split.

    A silent pooling here would raise sample sizes and make the sparse buckets
    disappear from the analysis without any estimate actually improving.
    """
    history = [
        _match("home", SEASON_START, "2", "9", 3, 0),
        _match("away", SEASON_START + timedelta(days=1), "9", "2", 0, 1),
    ]

    home = venue_evidence(history, "2", Venue.HOME)
    away = venue_evidence(history, "2", Venue.AWAY)

    assert (home.matches, home.goals_for, home.goals_against) == (1, 3, 0)
    assert (away.matches, away.goals_for, away.goals_against) == (1, 1, 0)


# ---------------------------------------------------------------------------
# Part 14.13: identical-fixture intersection
# ---------------------------------------------------------------------------


def _record(event_id: str, probability, outcome=BttsOutcome.YES, model_id="A", **kwargs):
    return PredictionRecord(
        model_id=model_id,
        model_version="1",
        competition="eng.1",
        season=2020,
        event_id=event_id,
        kickoff=datetime(2021, 1, 1, tzinfo=UTC),
        home_team_id="1",
        away_team_id="2",
        outcome=outcome,
        probability=probability,
        **kwargs,
    )


def test_intersection_contains_only_fixtures_both_models_scored():
    """
    Part 14.13. Differing coverage cannot leak into a metric comparison.

    Model A scores three fixtures, B scores three, and they overlap on two. Both
    returned lists must contain exactly those two, aligned.
    """
    left = [_record("e1", 0.5), _record("e2", 0.5), _record("e3", 0.5)]
    right = [
        _record("e2", 0.6, model_id="B"),
        _record("e3", 0.6, model_id="B"),
        _record("e4", 0.6, model_id="B"),
    ]

    left_paired, right_paired = intersect(left, right)

    assert [r.event_id for r in left_paired] == ["e2", "e3"]
    assert [r.event_id for r in right_paired] == ["e2", "e3"]


def test_unevaluable_records_never_enter_an_intersection():
    """A refusal is not a prediction; pairing one would compare against a blank."""
    from domain.evaluation import UnevaluableReason

    left = [_record("e1", 0.5), _record("e2", None, unevaluable_reason=UnevaluableReason.NO_RESULT)]
    right = [_record("e1", 0.6, model_id="B"), _record("e2", 0.6, model_id="B")]

    left_paired, right_paired = intersect(left, right)

    assert [r.event_id for r in left_paired] == ["e1"]
    assert [r.event_id for r in right_paired] == ["e1"]


def test_comparison_reports_coverage_and_intersection_separately():
    """
    Coverage differences are REPORTED, never folded into the quality metrics.

    This is the Epic 2B.3 mistake made structurally impossible: the metrics come
    from the intersection while raw coverage stays a distinct field.
    """
    left = [_record("e1", 0.5), _record("e2", 0.5)]
    right = [_record("e1", 0.6, model_id="B"), _record("e2", 0.6, model_id="B"), _record("e3", 0.6, model_id="B")]

    comparison = compare(left, right)

    assert comparison.intersection_size == 2
    assert comparison.right_only == 1
    assert comparison.left_only == 0
    assert comparison.left.raw_scored == 2
    assert comparison.right.raw_scored == 3
    # Both summaries were computed over the SAME two fixtures.
    assert comparison.left.summary.scored == 2
    assert comparison.right.summary.scored == 2


def test_comparison_rejects_duplicate_predictions():
    """A duplicated fixture would double-count in one arm's mean, silently."""
    left = [_record("e1", 0.5), _record("e1", 0.7)]
    right = [_record("e1", 0.6, model_id="B")]

    with pytest.raises(ValueError, match="duplicate"):
        intersect(left, right)


def test_evidence_buckets_use_one_shared_count_for_both_arms():
    """
    Part 9. The same fixture lands in the same bucket for both models.

    The two arms count evidence differently (competition-wide vs current-season),
    so bucketing each by its own field would scatter one fixture across two rows
    and make the comparison meaningless. The count is supplied once, externally.
    """
    left = [_record("e1", 0.5, home_sample=40), _record("e2", 0.5, home_sample=40)]
    right = [
        _record("e1", 0.6, model_id="B", home_sample=1),
        _record("e2", 0.6, model_id="B", home_sample=1),
    ]

    rows = evidence_bucket_table(
        left,
        right,
        evidence_of={("eng.1", 2020, "e1"): 1, ("eng.1", 2020, "e2"): 12},
    )

    by_bucket = {row.bucket: row for row in rows}
    assert set(by_bucket) == {"1-2", "10+"}
    assert by_bucket["1-2"].n == 1
    assert by_bucket["10+"].n == 1
    # Both arms in a row describe the identical fixture.
    assert by_bucket["1-2"].baseline.scored == by_bucket["1-2"].shrunk.scored == 1


def test_missing_evidence_count_is_an_error_not_a_zero_bucket():
    """
    An unknown evidence level must not be silently filed under "0".

    "0" is the bucket whose behaviour this Epic claims to have fixed, so quietly
    padding it with unknown fixtures would corrupt the headline result.
    """
    left = [_record("e1", 0.5)]
    right = [_record("e1", 0.6, model_id="B")]

    with pytest.raises(KeyError):
        evidence_bucket_table(left, right, evidence_of={})


def test_evidence_bucket_boundaries_are_exact():
    """The Part 9 bin edges, pinned."""
    assert [evidence_bucket(n) for n in (0, 1, 2, 3, 5, 6, 9, 10, 40)] == [
        "0",
        "1-2",
        "1-2",
        "3-5",
        "3-5",
        "6-9",
        "6-9",
        "10+",
        "10+",
    ]


def test_extreme_probability_stats_separate_certainty_from_confidence():
    """
    Part 10. Exactly 0.0 is counted apart from merely <= 0.05.

    An estimate of 0.02 is aggressive; 0.0 asserts impossibility. Collapsing the
    two would hide whether GG-028's signature outcome still occurs.
    """
    records = [
        _record("e1", 0.0),
        _record("e2", 0.03),
        _record("e3", 0.5),
        _record("e4", 0.97),
        _record("e5", 1.0),
    ]

    stats = extreme_probability_stats(records)

    assert stats.scored == 5
    assert stats.at_or_below_05 == 2  # 0.0 and 0.03
    assert stats.at_or_above_95 == 2  # 0.97 and 1.0
    assert stats.exactly_zero == 1
    assert stats.exactly_one == 1
    assert stats.certain == 2
    assert stats.extreme_rate == pytest.approx(4 / 5)
