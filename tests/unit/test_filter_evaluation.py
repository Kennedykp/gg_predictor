"""
GG-002 — the hard filters must evaluate real statistics (Epic 1B.3).

Epic 1A characterized the defect: three of the five GG.md filters were passed
hardcoded constants and the clean-sheet rates were a fabricated 0, so the filter
layer approved essentially everything. These tests pin the fixed behaviour.

Every threshold is imported from `config`, never retyped as a literal. A test
that hardcoded `1.0` would keep passing if someone edited MIN_AVG_GOALS, which
would defeat the point of Task 19 (no threshold may change).

Fully deterministic: no network, no clock, no provider.
"""

import pytest

from config import MAX_CLEAN_SHEET_PCT, MIN_AVG_GOALS
from domain import (
    FILTER_DATA_UNAVAILABLE,
    FilterOutcome,
    FilterStats,
    build_filter_stats,
    evaluate_filters,
)

# A fixture that comfortably clears every threshold. Individual tests override
# one field at a time, so any failure names exactly one cause.
PASSING = dict(
    home_avg_goals_scored=1.80,
    away_avg_goals_scored=1.40,
    home_clean_sheet_pct=0.20,
    away_clean_sheet_pct=0.25,
)


def stats(**overrides) -> FilterStats:
    return FilterStats(**{**PASSING, **overrides})


class TestPassingAndFailing:
    """Genuine values on both sides of each threshold."""

    def test_genuinely_good_fixture_passes(self):
        result = evaluate_filters(stats())
        assert result.passed is True
        assert result.outcome is FilterOutcome.PASSED
        assert result.reasons == []
        assert result.allows_recommendation is True

    def test_low_home_scoring_fails(self):
        result = evaluate_filters(stats(home_avg_goals_scored=0.60))
        assert result.passed is False
        assert result.outcome is FilterOutcome.FAILED
        assert any("Home team averages" in r for r in result.reasons)
        assert result.allows_recommendation is False

    def test_low_away_scoring_fails(self):
        result = evaluate_filters(stats(away_avg_goals_scored=0.42))
        assert result.passed is False
        assert any("Away team averages" in r for r in result.reasons)

    def test_high_home_clean_sheet_rate_fails(self):
        result = evaluate_filters(stats(home_clean_sheet_pct=0.65))
        assert result.passed is False
        assert any("Home team keeps" in r and "clean sheets" in r for r in result.reasons)

    def test_high_away_clean_sheet_rate_fails(self):
        result = evaluate_filters(stats(away_clean_sheet_pct=0.80))
        assert result.passed is False
        assert any("Away team keeps" in r and "clean sheets" in r for r in result.reasons)


    def test_multiple_simultaneous_failures_are_all_reported(self):
        """
        All four reasons, not just the first. An operator reviewing a rejection
        needs the full picture, and short-circuiting would hide three of them.
        """
        result = evaluate_filters(
            FilterStats(
                home_avg_goals_scored=0.10,
                away_avg_goals_scored=0.20,
                home_clean_sheet_pct=0.90,
                away_clean_sheet_pct=0.95,
            )
        )
        assert result.passed is False
        assert len(result.reasons) == 4


class TestThresholdBoundaries:
    """
    The exact comparison semantics. `filters.py` uses `<` and `>`, so the
    threshold value itself PASSES. These tests pin that inclusivity - an
    accidental flip to `<=` would silently change which fixtures qualify while
    leaving the constant untouched, which is exactly the kind of drift Task 19
    is meant to catch.
    """

    def test_avg_goals_exactly_at_threshold_passes(self):
        assert evaluate_filters(stats(home_avg_goals_scored=MIN_AVG_GOALS)).passed is True

    def test_avg_goals_just_below_threshold_fails(self):
        assert evaluate_filters(stats(home_avg_goals_scored=MIN_AVG_GOALS - 0.01)).passed is False

    def test_avg_goals_just_above_threshold_passes(self):
        assert evaluate_filters(stats(home_avg_goals_scored=MIN_AVG_GOALS + 0.01)).passed is True

    def test_clean_sheet_exactly_at_threshold_passes(self):
        assert evaluate_filters(stats(home_clean_sheet_pct=MAX_CLEAN_SHEET_PCT)).passed is True

    def test_clean_sheet_just_above_threshold_fails(self):
        assert (
            evaluate_filters(stats(home_clean_sheet_pct=MAX_CLEAN_SHEET_PCT + 0.01)).passed
            is False
        )

    def test_clean_sheet_just_below_threshold_passes(self):
        assert (
            evaluate_filters(stats(home_clean_sheet_pct=MAX_CLEAN_SHEET_PCT - 0.01)).passed
            is True
        )


class TestGenuineZeroIsRealData:
    """
    The Epic 1B.1 invariant, restated for filters. A genuine 0.0 is a
    measurement and must be evaluated, not treated as absent.
    """

    def test_genuine_zero_clean_sheet_rate_passes(self):
        """A team that has never kept a clean sheet is ideal for GG - it passes."""
        result = evaluate_filters(stats(home_clean_sheet_pct=0.0))
        assert result.passed is True
        assert result.outcome is FilterOutcome.PASSED
        assert result.unavailable_fields == ()

    def test_genuine_zero_scoring_rate_fails_on_merit(self):
        """
        A team averaging 0.0 goals fails - but as a FAILURE, not as missing data.
        The distinction matters: one is a verdict, the other is an absence.
        """
        result = evaluate_filters(stats(home_avg_goals_scored=0.0))
        assert result.passed is False
        assert result.outcome is FilterOutcome.FAILED
        assert result.was_evaluated is True
        assert result.unavailable_fields == ()

    def test_zero_and_missing_reach_different_outcomes(self):
        """The single assertion this Epic exists to make true."""
        genuine_zero = evaluate_filters(stats(home_clean_sheet_pct=0.0))
        unavailable = evaluate_filters(stats(home_clean_sheet_pct=None))

        assert genuine_zero.outcome is FilterOutcome.PASSED
        assert unavailable.outcome is FilterOutcome.UNEVALUATED
        assert genuine_zero.passed != unavailable.passed


class TestUnavailableDataBlocksRecommendation:
    """
    TASK 10. Absent data must not pass and must not be substituted.

    Pre-fix, `clean_sheet_pct` arrived as a hardcoded 0.0, which is below
    MAX_CLEAN_SHEET_PCT, so the filter passed every fixture on a statistic that
    had never been measured.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "home_avg_goals_scored",
            "away_avg_goals_scored",
            "home_clean_sheet_pct",
            "away_clean_sheet_pct",
        ],
    )
    def test_any_unavailable_field_prevents_a_pass(self, field):
        result = evaluate_filters(stats(**{field: None}))
        assert result.passed is False
        assert result.allows_recommendation is False
        assert result.outcome is FilterOutcome.UNEVALUATED
        assert field in result.unavailable_fields

    def test_unavailable_is_not_conflated_with_failure(self):
        """TASK 13: 'failed' and 'could not be evaluated' are different facts."""
        failed = evaluate_filters(stats(home_avg_goals_scored=0.10))
        uneval = evaluate_filters(stats(home_avg_goals_scored=None))

        assert failed.outcome is FilterOutcome.FAILED
        assert failed.was_evaluated is True
        assert uneval.outcome is FilterOutcome.UNEVALUATED
        assert uneval.was_evaluated is False

    def test_reason_names_the_missing_field(self):
        result = evaluate_filters(stats(away_clean_sheet_pct=None))
        assert len(result.reasons) == 1
        assert result.reasons[0].startswith(FILTER_DATA_UNAVAILABLE)
        assert "away_clean_sheet_pct" in result.reasons[0]

    def test_all_missing_fields_are_listed(self):
        result = evaluate_filters(
            FilterStats(
                home_avg_goals_scored=None,
                away_avg_goals_scored=1.4,
                home_clean_sheet_pct=None,
                away_clean_sheet_pct=None,
            )
        )
        assert set(result.unavailable_fields) == {
            "home_avg_goals_scored",
            "home_clean_sheet_pct",
            "away_clean_sheet_pct",
        }

    def test_unavailable_data_does_not_raise(self):
        """`None < 1.0` is a TypeError. Absence must be detected before comparison."""
        result = evaluate_filters(stats(home_avg_goals_scored=None))  # must not raise
        assert result.passed is False

    def test_missing_data_never_reaches_a_threshold_comparison(self):
        """
        Even when every OTHER statistic is excellent, one absent field blocks the
        recommendation. Unknown is not pass.
        """
        result = evaluate_filters(
            FilterStats(
                home_avg_goals_scored=3.00,
                away_avg_goals_scored=2.80,
                home_clean_sheet_pct=0.00,
                away_clean_sheet_pct=None,
            )
        )
        assert result.passed is False
        assert result.outcome is FilterOutcome.UNEVALUATED


class TestUnitAndRangeValidation:
    """
    Percentages are fractions in [0, 1]. A value of 40 meaning "40%" would sail
    past `> 0.40` forever, so the contract rejects it at construction.
    """

    @pytest.mark.parametrize("bad", [40, 100, 1.01, -0.01, -1.0])
    def test_out_of_range_percentage_is_rejected(self, bad):
        with pytest.raises(ValueError, match="fraction"):
            FilterStats(
                home_avg_goals_scored=1.5,
                away_avg_goals_scored=1.5,
                home_clean_sheet_pct=bad,
                away_clean_sheet_pct=0.2,
            )

    @pytest.mark.parametrize("edge", [0.0, 1.0, 0.5])
    def test_valid_fractions_are_accepted(self, edge):
        FilterStats(
            home_avg_goals_scored=1.5,
            away_avg_goals_scored=1.5,
            home_clean_sheet_pct=edge,
            away_clean_sheet_pct=edge,
        )

    def test_negative_goal_average_is_rejected(self):
        with pytest.raises(ValueError, match=">= 0"):
            FilterStats(
                home_avg_goals_scored=-0.5,
                away_avg_goals_scored=1.5,
                home_clean_sheet_pct=0.2,
                away_clean_sheet_pct=0.2,
            )


class TestNoFakeConstantsReachFilters:
    """
    TASK 11. The specific fabricated values Epic 0 found, pinned so they cannot
    return. Each previously produced a silent PASS.
    """

    def test_espn_clean_sheet_is_unavailable_not_zero(self):
        """
        espn.get_team_stats() hardcoded 0 for both clean-sheet rates. Since ESPN
        gives only aggregate goals-against, the honest answer is None.
        """
        home = {"home_goals_scored": 1.8, "home_clean_sheet_pct": None}
        away = {"away_goals_scored": 1.2, "away_clean_sheet_pct": None}
        built = build_filter_stats(home, away)

        assert built.home_clean_sheet_pct is None
        assert built.away_clean_sheet_pct is None
        assert evaluate_filters(built).outcome is FilterOutcome.UNEVALUATED

    def test_absent_provider_key_becomes_unavailable_not_a_default(self):
        """A provider omitting the key entirely must not yield a usable number."""
        built = build_filter_stats({}, {})
        assert built.home_avg_goals_scored is None
        assert built.home_clean_sheet_pct is None
        assert evaluate_filters(built).passed is False

    def test_the_old_hardcoded_zero_would_have_passed(self):
        """
        Demonstrates why the fabricated value was dangerous rather than merely
        untidy: 0.0 is below MAX_CLEAN_SHEET_PCT, so the filter approved the
        fixture. The same fixture with honest data is now refused.
        """
        fabricated = evaluate_filters(stats(home_clean_sheet_pct=0.0))
        honest = evaluate_filters(stats(home_clean_sheet_pct=None))

        assert fabricated.passed is True    # what used to happen
        assert honest.passed is False       # what happens now
