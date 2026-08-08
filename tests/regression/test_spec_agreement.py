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
from domain import build_filter_stats, evaluate_filters
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
def test_d2_missing_data_now_blocks_the_prediction():
    """
    D2 RESOLVED by Epic 1B.1.

    GG.md §6: "if any of these are missing -> NO BET". Previously unenforceable,
    because `espn.get_stat()` turned every absent statistic into 0 and the code
    could not tell missing from genuinely zero.

    The provider now reports absence as None and the pipeline validates the five
    required POISSON_V1 inputs before the model call, so the spec's rule holds.

    `poisson.py` itself is unchanged - it is the frozen POISSON_V1 baseline and
    still accepts 0.0 as valid, which is correct: a genuine 0.0 IS valid. The
    fix was to stop fabricating that 0.0 upstream.

    End-to-end coverage: tests/integration/test_pipeline_missing_data.py
    Provider-level coverage: tests/unit/test_espn_missing_data.py
    """
    from domain import LeagueStats, TeamStats, validate_poisson_inputs

    incomplete = TeamStats(team_id="1", league_id="eng.1")  # nothing supplied
    result = validate_poisson_inputs(
        league=LeagueStats.calculated("eng.1", 1.35),
        home_team=incomplete,
        away_team=incomplete,
    )

    assert not result.is_complete, "missing statistics must block the prediction"
    assert result.inputs is None, "no substituted values may reach POISSON_V1"


@pytest.mark.spec
@pytest.mark.skip(
    reason="D3 PARTIALLY RESOLVED in Epic 1B.3. The clean-sheet half is fixed: the "
    "hardcoded 0 is gone and unavailable data now blocks a recommendation instead "
    "of silently passing (see TestCleanSheetDataCannotSilentlyPass). What remains "
    "is that two of the five GG.md filters - first-leg knockout and heavy-favourite "
    "mismatch - have no data source at all, so they still cannot fire. That needs a "
    "competition-format/market feed, not a wiring change. Tracked as GG-002-B."
)
def test_d3_filters_mandatory_but_disabled_disagreement():
    raise AssertionError("placeholder - see docs/REPO_AUDIT.md D3")


@pytest.mark.spec
def test_d4_goals_average_semantics_resolved():
    """
    D4 / GG-006 RESOLVED in Epic 1B.3.

    GG.md section 9 says "one team averages < 1.0 goal". main.py used to pass
    `total_goals_avg` - goals scored PLUS conceded - while analyze_all.py passed
    the team's scoring rate, into the same parameter. Both entry points now route
    through `build_filter_stats`, so the quantity is fixed in one place: goals
    SCORED by that team, at the venue it is playing at.

    The worked example below is the disagreement made concrete. It is the same
    team under both readings, and the two readings give opposite verdicts.
    """
    goals_for, goals_against, matches = 5, 30, 20
    scoring_rate = goals_for / matches                      # 0.25 - the correct input
    combined = (goals_for + goals_against) / matches        # 1.75 - the old main.py input

    assert scoring_rate < MIN_AVG_GOALS, "a team scoring 0.25/game must fail the filter"
    assert combined > MIN_AVG_GOALS, "the combined figure passes, which was the defect"

    home = {"home_goals_scored": scoring_rate, "home_clean_sheet_pct": 0.10}
    away = {"away_goals_scored": 1.40, "away_clean_sheet_pct": 0.20}

    result = evaluate_filters(build_filter_stats(home, away))

    assert result.passed is False, "the scoring rate is what reaches the threshold now"
    assert any("Home team averages" in r for r in result.reasons)



@pytest.mark.spec
def test_d5_league_average_is_no_longer_always_the_fallback(monkeypatch):
    """
    D5 RESOLVED in Epic 1B.2 (GG-003).

    GG.md treats the league average as a required model input, but the provider
    returned the hardcoded 1.35 on every call: it requested
    `/apis/site/v2/.../standings`, which answers HTTP 200 with a 2-byte `{}`, so
    the 200 status meant nothing ever raised and the fallback always won.

    Two things had to become true, and both are asserted here:
      1. a real standings table is now computed rather than assumed, and
      2. an unavailable table yields None rather than a plausible constant.
    """
    import espn

    # 1. Real data is computed. 20 goals over 20 team-games = 1.0 per team per
    #    match - deliberately not 1.35, so a lingering fallback cannot pass.
    monkeypatch.setattr(
        espn,
        "_make_request",
        lambda *a, **k: {
            "children": [
                {
                    "standings": {
                        "entries": [
                            {
                                "stats": [
                                    {"name": "pointsFor", "value": 12},
                                    {"name": "pointsAgainst", "value": 8},
                                    {"name": "gamesPlayed", "value": 10},
                                ]
                            },
                            {
                                "stats": [
                                    {"name": "pointsFor", "value": 8},
                                    {"name": "pointsAgainst", "value": 12},
                                    {"name": "gamesPlayed", "value": 10},
                                ]
                            },
                        ]
                    }
                }
            ]
        },
    )
    assert espn.get_league_avg_goals("eng.1") == pytest.approx(1.0)

    # 2. Unavailable is unavailable. Previously this exact path returned 1.35.
    monkeypatch.setattr(espn, "_make_request", lambda *a, **k: None)
    result = espn.get_league_avg_goals("eng.1")
    assert result is None
    assert result != 1.35
