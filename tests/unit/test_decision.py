"""
Unit / characterization tests for GG decision logic (`decision.py`).

Documents current behaviour of `calculate_implied_probability`, `calculate_edge`
and `make_decision`. No threshold and no decision semantic was changed.

Note: every committed Epic 0 output shows "NO BET / No odds available" for all 39
fixtures, because odds never resolved. The FLAG GG path has therefore never run in
production. It IS reachable through direct pure-function inputs, and is covered here.
"""

import pytest

from config import EDGE_THRESHOLD, MIN_ODDS
from decision import calculate_edge, calculate_implied_probability, make_decision


class TestThresholdsUnchanged:
    def test_edge_threshold_is_five_percent(self):
        assert EDGE_THRESHOLD == 0.05

    def test_min_odds_is_one_point_six(self):
        assert MIN_ODDS == 1.60


class TestImpliedProbability:
    @pytest.mark.parametrize(
        ("odds", "expected"),
        [
            (2.00, 0.50),
            (1.60, 0.625),
            (1.80, 0.5555555555555556),
            (4.00, 0.25),
            (1.01, 0.9900990099009901),
        ],
    )
    def test_reciprocal_of_odds(self, odds, expected):
        assert calculate_implied_probability(odds) == pytest.approx(expected, rel=1e-12)

    @pytest.mark.characterization
    def test_zero_odds_returns_zero_not_none(self):
        """
        CHARACTERIZATION: non-positive odds return 0.0 rather than signalling an
        error. An edge computed against this would be P(GG) - 0 = P(GG), which
        looks like an enormous value bet. Currently unreachable because odds
        never arrive, but the behaviour is latent.
        """
        assert calculate_implied_probability(0) == 0.0

    @pytest.mark.characterization
    def test_negative_odds_returns_zero_not_none(self):
        assert calculate_implied_probability(-2.5) == 0.0

    def test_odds_just_above_zero_is_computed_normally(self):
        assert calculate_implied_probability(0.001) == pytest.approx(1000.0)


class TestEdge:
    def test_edge_is_probability_minus_implied(self):
        # 0.60 - (1/1.80) = 0.60 - 0.5555... = 0.0444...
        assert calculate_edge(0.60, 1.80) == pytest.approx(0.6 - 1 / 1.8, rel=1e-12)

    def test_positive_edge(self):
        assert calculate_edge(0.70, 1.80) > 0

    def test_negative_edge(self):
        assert calculate_edge(0.40, 1.80) < 0

    def test_zero_edge_when_probability_equals_implied(self):
        assert calculate_edge(0.50, 2.00) == pytest.approx(0.0, abs=1e-15)

    @pytest.mark.characterization
    def test_edge_against_zero_odds_equals_full_probability(self):
        # Consequence of calculate_implied_probability returning 0.0 for odds<=0.
        assert calculate_edge(0.62, 0) == 0.62


class TestMakeDecisionResultShape:
    def test_all_expected_keys_present(self):
        result = make_decision(0.62, 1.80, True)
        assert set(result) == {
            "gg_probability",
            "odds",
            "implied_probability",
            "edge",
            "passes_filters",
            "decision",
            "reasons",
        }

    def test_inputs_are_echoed_back(self):
        result = make_decision(0.62, 1.80, True)
        assert result["gg_probability"] == 0.62
        assert result["odds"] == 1.80
        assert result["passes_filters"] is True


class TestFiltersFailedShortCircuit:
    def test_failed_filters_give_no_bet(self):
        result = make_decision(0.95, 3.00, False)
        assert result["decision"] == "NO BET"
        assert result["reasons"] == ["Failed hard filters"]

    def test_failed_filters_skip_edge_calculation_entirely(self):
        # Returns before implied_probability/edge are populated, even though
        # the odds supplied would have produced a large positive edge.
        result = make_decision(0.95, 3.00, False)
        assert result["implied_probability"] is None
        assert result["edge"] is None


class TestMissingOdds:
    def test_none_odds_gives_no_bet(self):
        result = make_decision(0.62, None, True)
        assert result["decision"] == "NO BET"
        assert result["reasons"] == ["No odds available"]

    def test_none_odds_leaves_edge_unset(self):
        result = make_decision(0.62, None, True)
        assert result["implied_probability"] is None
        assert result["edge"] is None

    @pytest.mark.characterization
    def test_this_is_the_path_every_committed_run_took(self):
        """
        All 39 fixtures in the committed Epic 0 outputs took this exact path:
        filters passed, odds were None, so the reason was "No odds available".
        """
        result = make_decision(0.5405082766461281, None, True)
        assert result["decision"] == "NO BET"
        assert result["reasons"] == ["No odds available"]
        assert result["passes_filters"] is True


class TestEdgeThresholdBoundary:
    def test_edge_below_threshold_gives_no_bet(self):
        # P=0.60, odds=1.80 -> edge 0.0444 < 0.05
        result = make_decision(0.60, 1.80, True)
        assert result["decision"] == "NO BET"
        assert any("Edge" in r for r in result["reasons"])

    def test_edge_exactly_at_threshold_gives_flag_gg(self):
        # Comparison is `edge < EDGE_THRESHOLD`, so exactly 0.05 passes.
        # implied(2.0) = 0.5, so P = 0.55 gives edge of exactly 0.05.
        result = make_decision(0.55, 2.00, True)
        assert result["edge"] == pytest.approx(0.05, abs=1e-15)
        assert result["decision"] == "FLAG GG"
        assert result["reasons"] == []

    def test_edge_above_threshold_gives_flag_gg(self):
        result = make_decision(0.70, 2.00, True)
        assert result["decision"] == "FLAG GG"
        assert result["reasons"] == []

    def test_negative_edge_gives_no_bet(self):
        result = make_decision(0.30, 2.00, True)
        assert result["decision"] == "NO BET"
        assert any("Edge" in r for r in result["reasons"])


class TestMinOddsBoundary:
    def test_odds_below_minimum_gives_no_bet(self):
        # Large edge, but odds too short.
        result = make_decision(0.95, 1.50, True)
        assert result["decision"] == "NO BET"
        assert any("Odds" in r for r in result["reasons"])

    def test_odds_exactly_at_minimum_can_flag(self):
        # Comparison is `odds < MIN_ODDS`, so exactly 1.60 passes.
        # implied(1.60) = 0.625, so P = 0.70 gives edge 0.075 >= 0.05.
        result = make_decision(0.70, 1.60, True)
        assert result["decision"] == "FLAG GG"
        assert result["reasons"] == []

    def test_odds_just_below_minimum_fails(self):
        result = make_decision(0.90, 1.599999, True)
        assert result["decision"] == "NO BET"
        assert any("Odds" in r for r in result["reasons"])


class TestBothConditionsFail:
    def test_two_reasons_reported(self):
        # Short odds AND insufficient edge: implied(1.50)=0.6667, P=0.68
        # -> edge 0.0133 < 0.05, and odds 1.50 < 1.60.
        result = make_decision(0.68, 1.50, True)
        assert result["decision"] == "NO BET"
        assert len(result["reasons"]) == 2
        assert any("Edge" in r for r in result["reasons"])
        assert any("Odds" in r for r in result["reasons"])

    def test_edge_and_implied_still_populated_when_rejected(self):
        # Unlike the failed-filters path, these are computed before rejection.
        result = make_decision(0.68, 1.50, True)
        assert result["implied_probability"] is not None
        assert result["edge"] is not None


class TestSuccessfulDecisionPath:
    """The FLAG GG path - reachable here, never yet reached in production."""

    def test_realistic_value_bet(self):
        # P(GG) 0.62 vs odds 1.90 (implied 0.5263) -> edge 0.0937
        result = make_decision(0.62, 1.90, True)
        assert result["decision"] == "FLAG GG"
        assert result["reasons"] == []
        assert result["edge"] == pytest.approx(0.62 - 1 / 1.9, rel=1e-12)
        assert result["implied_probability"] == pytest.approx(1 / 1.9, rel=1e-12)

    def test_all_three_conditions_are_required(self):
        # Same probability and odds, only the filter flag differs.
        assert make_decision(0.62, 1.90, True)["decision"] == "FLAG GG"
        assert make_decision(0.62, 1.90, False)["decision"] == "NO BET"


class TestDeterminism:
    def test_repeated_calls_are_identical(self):
        first = make_decision(0.62, 1.90, True)
        for _ in range(20):
            assert make_decision(0.62, 1.90, True) == first

    def test_reasons_list_is_not_shared_between_calls(self):
        first = make_decision(0.30, 2.00, True)["reasons"]
        second = make_decision(0.30, 2.00, True)["reasons"]
        assert first == second
        assert first is not second
