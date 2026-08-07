"""
Unit tests for POISSON_V1 (`poisson.py`).

These tests describe the CURRENT implementation exactly as it exists. No formula,
constant or threshold was changed to make a test pass.

Offline and deterministic: no network, no API keys, no .env, no clock dependency.
"""

import math

import pytest

from poisson import calculate_gg_probability

# A typical, valid set of inputs reused as a baseline across several tests.
VALID = {
    "league_avg_goals": 1.35,
    "home_goals_scored_home": 1.50,
    "home_goals_conceded_home": 1.20,
    "away_goals_scored_away": 1.30,
    "away_goals_conceded_away": 1.40,
}


class TestValidInputs:
    """Normal operation."""

    def test_returns_expected_keys(self):
        result = calculate_gg_probability(**VALID)
        assert result is not None
        assert set(result) == {"lambda_home", "lambda_away", "gg_probability"}

    def test_lambda_home_formula(self):
        # lambda_home = (home_scored_home * away_conceded_away) / league_avg
        result = calculate_gg_probability(**VALID)
        expected = (1.50 * 1.40) / 1.35
        assert result["lambda_home"] == pytest.approx(expected, rel=1e-12)

    def test_lambda_away_formula(self):
        # lambda_away = (away_scored_away * home_conceded_home) / league_avg
        result = calculate_gg_probability(**VALID)
        expected = (1.30 * 1.20) / 1.35
        assert result["lambda_away"] == pytest.approx(expected, rel=1e-12)

    def test_gg_probability_formula(self):
        # P(GG) = (1 - e^-lambda_home) * (1 - e^-lambda_away)
        result = calculate_gg_probability(**VALID)
        expected = (1 - math.exp(-result["lambda_home"])) * (1 - math.exp(-result["lambda_away"]))
        assert result["gg_probability"] == pytest.approx(expected, rel=1e-12)

    def test_lambdas_are_asymmetric_by_design(self):
        # Home lambda uses the AWAY team's conceding rate, and vice versa.
        # Swapping only the conceding rates must change both lambdas.
        base = calculate_gg_probability(**VALID)
        swapped = calculate_gg_probability(
            league_avg_goals=1.35,
            home_goals_scored_home=1.50,
            home_goals_conceded_home=1.40,
            away_goals_scored_away=1.30,
            away_goals_conceded_away=1.20,
        )
        assert base["lambda_home"] != swapped["lambda_home"]
        assert base["lambda_away"] != swapped["lambda_away"]


class TestInvalidInputs:
    """Inputs the current implementation rejects by returning None."""

    @pytest.mark.parametrize(
        "field",
        [
            "league_avg_goals",
            "home_goals_scored_home",
            "home_goals_conceded_home",
            "away_goals_scored_away",
            "away_goals_conceded_away",
        ],
    )
    def test_none_in_any_field_returns_none(self, field):
        args = dict(VALID)
        args[field] = None
        assert calculate_gg_probability(**args) is None

    @pytest.mark.parametrize(
        "field",
        [
            "league_avg_goals",
            "home_goals_scored_home",
            "home_goals_conceded_home",
            "away_goals_scored_away",
            "away_goals_conceded_away",
        ],
    )
    def test_negative_in_any_field_returns_none(self, field):
        args = dict(VALID)
        args[field] = -0.5
        assert calculate_gg_probability(**args) is None

    def test_zero_league_average_returns_none(self):
        # Explicit division-by-zero guard in the implementation.
        args = dict(VALID)
        args["league_avg_goals"] = 0
        assert calculate_gg_probability(**args) is None

    def test_zero_league_average_as_float_returns_none(self):
        args = dict(VALID)
        args["league_avg_goals"] = 0.0
        assert calculate_gg_probability(**args) is None

    def test_all_none_returns_none(self):
        assert calculate_gg_probability(None, None, None, None, None) is None


class TestBoundaryConditions:
    """Values sitting exactly on the implementation's accept/reject boundaries."""

    def test_smallest_negative_is_rejected(self):
        args = dict(VALID)
        args["home_goals_scored_home"] = -1e-12
        assert calculate_gg_probability(**args) is None

    def test_zero_is_accepted_not_rejected(self):
        # The guard is `val < 0`, so exactly 0.0 is on the accepted side.
        args = dict(VALID)
        args["home_goals_scored_home"] = 0.0
        assert calculate_gg_probability(**args) is not None

    def test_tiny_positive_league_average_is_accepted(self):
        args = dict(VALID)
        args["league_avg_goals"] = 1e-9
        result = calculate_gg_probability(**args)
        assert result is not None
        # Dividing by a tiny average inflates lambda enormously; no upper bound exists.
        assert result["lambda_home"] > 1e8


class TestZeroValuesCurrentlyPermitted:
    """
    CHARACTERIZATION — see docs/TECHNICAL_DEBT.md GG-001.

    `espn.get_stat()` returns 0 for any statistic the API omits, and this model
    accepts 0.0 as valid data (the guard is `val < 0`, not `val <= 0`).
    A missing statistic is therefore indistinguishable from a genuine zero.

    These tests document that CURRENT behaviour. They are NOT a statement that
    it is correct. Epic 1B is expected to change this, at which point these
    tests should be updated deliberately.
    """

    @pytest.mark.characterization
    def test_zero_home_scoring_yields_zero_probability(self):
        args = dict(VALID)
        args["home_goals_scored_home"] = 0.0
        result = calculate_gg_probability(**args)
        assert result is not None, "legacy behaviour: 0.0 is accepted as real data"
        assert result["lambda_home"] == 0.0
        assert result["gg_probability"] == 0.0

    @pytest.mark.characterization
    def test_zero_away_conceding_yields_zero_home_lambda(self):
        args = dict(VALID)
        args["away_goals_conceded_away"] = 0.0
        result = calculate_gg_probability(**args)
        assert result is not None
        assert result["lambda_home"] == 0.0
        assert result["gg_probability"] == 0.0

    @pytest.mark.characterization
    def test_all_stats_zero_returns_zero_probability_not_none(self):
        # The most important case: a team with NO data available produces a
        # confident-looking 0.0 probability rather than "unavailable".
        result = calculate_gg_probability(1.35, 0.0, 0.0, 0.0, 0.0)
        assert result is not None
        assert result["gg_probability"] == 0.0


class TestLambdaMagnitudes:
    """Very low and moderately high scoring intensities."""

    def test_extremely_low_lambda(self):
        result = calculate_gg_probability(1.35, 0.01, 0.01, 0.01, 0.01)
        assert result is not None
        assert 0 < result["lambda_home"] < 0.001
        assert 0 < result["gg_probability"] < 1e-6

    def test_moderately_high_lambda(self):
        result = calculate_gg_probability(1.35, 2.40, 2.20, 2.30, 2.50)
        assert result is not None
        assert result["lambda_home"] > 4.0
        # With both lambdas high, P(GG) approaches but never reaches 1.
        assert 0.95 < result["gg_probability"] < 1.0

    def test_no_upper_bound_on_lambda(self):
        # CHARACTERIZATION: implausible lambdas are accepted silently.
        # The committed Epic 0 output contains lambda_home = 4.36.
        result = calculate_gg_probability(0.5, 5.0, 5.0, 5.0, 5.0)
        assert result is not None
        assert result["lambda_home"] == pytest.approx(50.0)


class TestNoRounding:
    """The model performs no rounding; callers round at output time."""

    def test_full_float_precision_is_returned(self):
        result = calculate_gg_probability(**VALID)
        # 1.5 * 1.4 / 1.35 is not exactly representable - a rounded
        # implementation would return a short decimal instead.
        assert result["lambda_home"] != round(result["lambda_home"], 4)
        assert repr(result["lambda_home"]) == "1.5555555555555551"


class TestDeterminism:
    """Required for the golden regression suite to be meaningful."""

    def test_repeated_calls_are_identical(self):
        first = calculate_gg_probability(**VALID)
        for _ in range(50):
            assert calculate_gg_probability(**VALID) == first

    def test_no_hidden_state_between_different_inputs(self):
        a = calculate_gg_probability(**VALID)
        calculate_gg_probability(1.80, 2.10, 1.90, 2.00, 1.85)
        assert calculate_gg_probability(**VALID) == a
