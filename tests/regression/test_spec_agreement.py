"""
Spec-agreement regression tests: `poisson.py` vs `GG.md`.

Epic 0 confirmed the CORE MATHEMATICS agrees with the specification. These tests
pin that agreement so a future refactor cannot silently drift away from the
documented model.

Epic 0 also found FIVE disagreements (D1-D5) between GG.md and the code. Those are
product/design decisions and are explicitly NOT resolved here. They are documented
at the bottom of this file as skipped tests so they remain visible in every test
run rather than only in a document.
"""

import math

import pytest

from config import EDGE_THRESHOLD, MAX_CLEAN_SHEET_PCT, MIN_AVG_GOALS, MIN_ODDS
from decision import calculate_edge, calculate_implied_probability
from poisson import calculate_gg_probability


@pytest.mark.spec
class TestCoreFormulasMatchGGmd:
    """
    GG.md lines 124-135:

        λ_home = (Home_GF_home × Away_GA_away) / League_Avg_Goals
        λ_away = (Away_GF_away × Home_GA_home) / League_Avg_Goals
        P(GG)  = (1 − e^(−λ_home)) × (1 − e^(−λ_away))
    """

    def test_lambda_home_matches_spec(self):
        home_gf_home, away_ga_away, league_avg = 1.62, 1.35, 1.35
        result = calculate_gg_probability(league_avg, home_gf_home, 1.1, 1.2, away_ga_away)
        assert result is not None
        assert result["lambda_home"] == pytest.approx(
            (home_gf_home * away_ga_away) / league_avg, rel=1e-15
        )

    def test_lambda_away_matches_spec(self):
        away_gf_away, home_ga_home, league_avg = 1.18, 1.35, 1.35
        result = calculate_gg_probability(league_avg, 1.5, home_ga_home, away_gf_away, 1.2)
        assert result is not None
        assert result["lambda_away"] == pytest.approx(
            (away_gf_away * home_ga_home) / league_avg, rel=1e-15
        )

    def test_gg_probability_matches_spec(self):
        result = calculate_gg_probability(1.35, 1.5, 1.2, 1.3, 1.4)
        assert result is not None
        lh, la = result["lambda_home"], result["lambda_away"]
        assert result["gg_probability"] == pytest.approx(
            (1 - math.exp(-lh)) * (1 - math.exp(-la)), rel=1e-15
        )

    def test_lambda_home_uses_away_teams_conceding_rate(self):
        """
        The spec pairs the HOME attack with the AWAY defence. A transposed
        implementation would still look plausible, so this is pinned explicitly.
        """
        result = calculate_gg_probability(
            league_avg_goals=1.0,
            home_goals_scored_home=2.0,
            home_goals_conceded_home=5.0,   # must NOT appear in lambda_home
            away_goals_scored_away=7.0,     # must NOT appear in lambda_home
            away_goals_conceded_away=3.0,
        )
        assert result is not None
        assert result["lambda_home"] == pytest.approx(2.0 * 3.0)
        assert result["lambda_away"] == pytest.approx(7.0 * 5.0)

    def test_worked_example_from_spec_is_reproducible(self):
        """
        GG.md lines 211-215 show λ_home 1.62, λ_away 1.18, GG Probability 0.56.
        Feeding those lambdas through the documented probability formula
        reproduces the stated 0.56 (to the 2dp the spec displays).
        """
        expected = (1 - math.exp(-1.62)) * (1 - math.exp(-1.18))
        assert round(expected, 2) == 0.56


@pytest.mark.spec
class TestValueRulesMatchGGmd:
    """GG.md lines 148-165: implied probability, edge and the bet rule."""

    def test_implied_probability_is_reciprocal_of_odds(self):
        # P_book = 1 / Odds
        assert calculate_implied_probability(1.80) == pytest.approx(1 / 1.80, rel=1e-15)

    def test_edge_is_probability_minus_implied(self):
        # Edge = P(GG) − P_book
        assert calculate_edge(0.56, 1.80) == pytest.approx(0.56 - 1 / 1.80, rel=1e-12)

    def test_spec_example_shows_zero_edge(self):
        # GG.md: "Odds: 1.80 (Implied: 0.56) / Edge: +0.00"
        assert round(calculate_edge(0.56, 1.80), 2) == 0.0


@pytest.mark.spec
class TestDocumentedThresholds:
    """Thresholds stated in GG.md must match config.py."""

    def test_edge_threshold(self):
        assert EDGE_THRESHOLD == 0.05      # "Edge ≥ 0.05 (5%)"

    def test_min_odds(self):
        assert MIN_ODDS == 1.60            # "Odds ≥ 1.60"

    def test_min_avg_goals(self):
        assert MIN_AVG_GOALS == 1.0        # "One team averages < 1.0 goal"

    def test_max_clean_sheet_pct(self):
        assert MAX_CLEAN_SHEET_PCT == 0.40  # "One team keeps > 40% clean sheets"


# ---------------------------------------------------------------------------
# UNRESOLVED SPEC DISAGREEMENTS (Epic 0, D1-D5)
#
# These are recorded as permanently-skipped tests so they surface in every test
# run. They are NOT failures and NOT bugs to be fixed here - each needs a product
# decision on which side (GG.md or the code) is authoritative.
#
# Full detail: docs/REPO_AUDIT.md section 7.
# ---------------------------------------------------------------------------


@pytest.mark.spec
@pytest.mark.skip(
    reason="D1 UNRESOLVED: GG.md names API-Football as the primary data source, "
    "but production uses ESPN and api_football.py is dead code. "
    "Needs a decision on which source is authoritative."
)
def test_d1_primary_data_source_disagreement():
    raise AssertionError("placeholder - see docs/REPO_AUDIT.md D1")


@pytest.mark.spec
@pytest.mark.skip(
    reason="D2 UNRESOLVED: GG.md says 'if any of these are missing -> NO BET', but "
    "espn.get_stat() returns 0 for missing stats and poisson.py accepts 0.0 as "
    "valid data. Characterized in tests/unit/test_poisson.py; fix belongs to Epic 1B."
)
def test_d2_missing_data_contract_disagreement():
    raise AssertionError("placeholder - see docs/REPO_AUDIT.md D2")


@pytest.mark.spec
@pytest.mark.skip(
    reason="D3 UNRESOLVED: GG.md calls the five hard filters mandatory, but three are "
    "hardcoded off in main.py and clean-sheet rates are hardcoded to 0 in espn.py. "
    "Characterized in tests/unit/test_filters.py; fix belongs to Epic 1B."
)
def test_d3_filters_mandatory_but_disabled_disagreement():
    raise AssertionError("placeholder - see docs/REPO_AUDIT.md D3")


@pytest.mark.spec
@pytest.mark.skip(
    reason="D4 UNRESOLVED: GG.md says 'one team averages < 1.0 goal' (goals scored), "
    "but main.py passes combined goals-per-match and analyze_all.py passes the "
    "home-only scoring rate into the same parameter. Needs a decision on the "
    "intended quantity."
)
def test_d4_goals_average_semantics_disagreement():
    raise AssertionError("placeholder - see docs/REPO_AUDIT.md D4")


@pytest.mark.spec
@pytest.mark.skip(
    reason="D5 UNRESOLVED: GG.md treats league average goals as a required model "
    "input, but espn.get_league_avg_goals() always returns the hardcoded 1.35 "
    "because /standings returns an empty body. Fix belongs to Epic 1B."
)
def test_d5_league_average_is_always_fallback_disagreement():
    raise AssertionError("placeholder - see docs/REPO_AUDIT.md D5")
