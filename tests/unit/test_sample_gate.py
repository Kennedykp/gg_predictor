"""
Unit and regression tests for Option A: Minimum venue sample gate (N >= 3).

Requirement:
min(home_sample, away_sample) >= 3 must be satisfied in addition to filter passing
before a recommendation can be published.
When the sample requirement fails:
- recommendation = NO BET (or RECOMMEND_NO_PLAY in analyze_all)
- probability remains normal POISSON_V1 probability
- home_sample and away_sample remain recorded
- existing filter results remain recorded
- explicit rejection reason includes "Insufficient venue sample (<3)"
"""

from typing import Any, Dict, List, Optional

import pytest
from conftest import espn_event, utc

import analyze_all
import espn
import main
import shared.odds
from domain.prediction_log import DECISION_VERSION
from poisson import calculate_gg_probability

FIXTURE: Dict[str, Any] = {
    "fixture_id": "900",
    "league_id": "eng.1",
    "league_name": "English Premier League",
    "home_team_id": "359",
    "home_team_name": "Arsenal",
    "away_team_id": "360",
    "away_team_name": "Chelsea",
    "datetime": "2026-02-08T15:00Z",
    "status": "STATUS_SCHEDULED",
    "kickoff_utc": utc(2026, 2, 8, 15, 0),
}


def stats_payload(
    games: int = 20,
    home_games: int = 10,
    away_games: int = 10,
    home_for: int = 18,
    home_against: int = 8,
    away_for: int = 14,
    away_against: int = 10,
) -> Dict[str, Any]:
    """A complete ESPN team record that easily passes hard filters."""
    return {
        "team": {
            "record": {
                "items": [
                    {
                        "type": "total",
                        "stats": [
                            {"name": "gamesPlayed", "value": games},
                            {"name": "pointsFor", "value": home_for + away_for},
                            {"name": "pointsAgainst", "value": home_against + away_against},
                            {"name": "homeGamesPlayed", "value": home_games},
                            {"name": "awayGamesPlayed", "value": away_games},
                            {"name": "homePointsFor", "value": home_for},
                            {"name": "homePointsAgainst", "value": home_against},
                            {"name": "awayPointsFor", "value": away_for},
                            {"name": "awayPointsAgainst", "value": away_against},
                        ],
                    }
                ]
            }
        }
    }


def make_history(team_id: str, count: int, is_home: bool) -> List[Dict[str, Any]]:
    """Build count completed match events where team_id played at venue."""
    events = []
    for i in range(count):
        if is_home:
            # team_id is home team
            events.append(
                espn_event(f"e_h_{team_id}_{i}", utc(2025, 9, i + 1), team_id, "999", 2, 1)
            )
        else:
            # team_id is away team
            events.append(
                espn_event(f"e_a_{team_id}_{i}", utc(2025, 9, i + 1), "999", team_id, 1, 2)
            )
    return events


@pytest.fixture
def espn_stats(monkeypatch):
    def _install(by_team: Dict[str, Optional[Dict[str, Any]]]):
        def fake_request(url: str, params: Optional[dict] = None):
            for team_id, response in by_team.items():
                if f"/teams/{team_id}" in url:
                    return response
            return None

        monkeypatch.setattr(espn, "_make_request", fake_request)

    return _install


@pytest.fixture
def generous_odds(monkeypatch):
    """Odds of 2.50 ensure edge is positive and passes decision criteria."""
    monkeypatch.setattr(shared.odds, "find_odds_for_match", lambda *a, **k: 2.50)
    monkeypatch.setattr(main, "get_btts_odds", lambda **k: 2.50)


def setup_feeds(espn_feed, espn_stats, n_home: int, n_away: int):
    """Configure ESPN history with specified sample counts."""
    home_hist = make_history("359", n_home, is_home=True)
    away_hist = make_history("360", n_away, is_home=False)
    # Add some league baseline events so league average is valid
    league_hist = home_hist + away_hist + [
        espn_event("l1", utc(2025, 8, 1), "801", "802", 2, 1),
        espn_event("l2", utc(2025, 8, 2), "803", "804", 1, 1),
    ]
    espn_feed(
        team_events={"359": home_hist, "360": away_hist},
        league_events=league_hist,
    )
    espn_stats({"359": stats_payload(), "360": stats_payload()})


class TestSampleGateVersion:
    def test_decision_version_bumped_to_1_1_0(self):
        assert DECISION_VERSION == "1.1.0"


class TestSampleGateScenarios:
    def test_scenario_1_home_2_away_5_filters_pass_odds_qualify(
        self, espn_feed, espn_stats, generous_odds
    ):
        """home_sample=2, away_sample=5, filters pass, qualifying odds -> NO BET with reason."""
        setup_feeds(espn_feed, espn_stats, n_home=2, n_away=5)
        res = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert res["model_input_samples"]["home"] == 2
        assert res["model_input_samples"]["away"] == 5
        assert res["passes_filters"] is True
        assert res["decision"] == "NO BET"
        assert "Insufficient venue sample (<3)" in res["rejection_reasons"]
        assert res["gg_probability"] is not None
        assert res["gg_probability"] > 0

    def test_scenario_2_home_5_away_2_filters_pass_odds_qualify(
        self, espn_feed, espn_stats, generous_odds
    ):
        """home_sample=5, away_sample=2, filters pass, qualifying odds -> NO BET with reason."""
        setup_feeds(espn_feed, espn_stats, n_home=5, n_away=2)
        res = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert res["model_input_samples"]["home"] == 5
        assert res["model_input_samples"]["away"] == 2
        assert res["passes_filters"] is True
        assert res["decision"] == "NO BET"
        assert "Insufficient venue sample (<3)" in res["rejection_reasons"]
        assert res["gg_probability"] is not None

    def test_scenario_3_home_3_away_3_filters_pass_odds_qualify(
        self, espn_feed, espn_stats, generous_odds
    ):
        """home_sample=3, away_sample=3, filters pass, qualifying odds -> recommendation intact."""
        setup_feeds(espn_feed, espn_stats, n_home=3, n_away=3)
        res = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert res["model_input_samples"]["home"] == 3
        assert res["model_input_samples"]["away"] == 3
        assert res["passes_filters"] is True
        assert res["decision"] == "FLAG GG"
        assert "Insufficient venue sample (<3)" not in res["rejection_reasons"]

    def test_scenario_4_home_2_away_2(
        self, espn_feed, espn_stats, generous_odds
    ):
        """home_sample=2, away_sample=2 -> NO BET."""
        setup_feeds(espn_feed, espn_stats, n_home=2, n_away=2)
        res = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert res["model_input_samples"]["home"] == 2
        assert res["model_input_samples"]["away"] == 2
        assert res["decision"] == "NO BET"
        assert "Insufficient venue sample (<3)" in res["rejection_reasons"]

    def test_scenario_5_probability_is_unchanged_when_gate_blocks(
        self, espn_feed, espn_stats, generous_odds
    ):
        """Probability calculated under POISSON_V1 is identical regardless of recommendation."""
        setup_feeds(espn_feed, espn_stats, n_home=2, n_away=5)
        res = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        # Verify POISSON_V1 probability calculation remains identical to direct Poisson call
        # with the exact point-in-time model inputs
        from espn import get_league_baseline, get_team_venue_averages
        from shared.match_history import build_fixture_poisson_inputs

        model_inputs = build_fixture_poisson_inputs(
            FIXTURE, get_team_venue_averages, get_league_baseline
        )
        direct_prob = calculate_gg_probability(
            league_avg_goals=model_inputs.league_avg_goals,
            home_goals_scored_home=model_inputs.home_goals_scored_home,
            home_goals_conceded_home=model_inputs.home_goals_conceded_home,
            away_goals_scored_away=model_inputs.away_goals_scored_away,
            away_goals_conceded_away=model_inputs.away_goals_conceded_away,
        )
        assert direct_prob is not None
        assert res["gg_probability"] == direct_prob["gg_probability"]
        assert res["lambda_home"] == direct_prob["lambda_home"]
        assert res["lambda_away"] == direct_prob["lambda_away"]
        assert res["decision"] == "NO BET"

    def test_scenario_6_main_and_analyze_all_parity(
        self, espn_feed, espn_stats, generous_odds
    ):
        """Both main.py and analyze_all.py produce consistent behavior across sample boundaries."""
        home_team_stats = stats_payload()
        away_team_stats = stats_payload()

        # Subtest A: Sample insufficient (2, 5) -> both refuse
        setup_feeds(espn_feed, espn_stats, n_home=2, n_away=5)
        home_team_stats = espn.get_team_stats(FIXTURE["home_team_id"], "eng.1")
        away_team_stats = espn.get_team_stats(FIXTURE["away_team_id"], "eng.1")
        main_res = main.process_fixture(FIXTURE, league_avg_goals=1.35)
        analyze_rows = analyze_all.analyze_gg_match(
            FIXTURE, home_team_stats, away_team_stats, league_avg=1.35
        )
        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")

        assert main_res["decision"] == "NO BET"
        assert gg_yes["system_recommendation"] == "RECOMMEND_NO_PLAY"
        assert "Insufficient venue sample (<3)" in main_res["rejection_reasons"]
        assert "Insufficient venue sample (<3)" in gg_yes["filter_reasons"]
        assert gg_yes["model_input_samples"]["home"] == 2
        assert gg_yes["model_input_samples"]["away"] == 5

        # Subtest B: Sample sufficient (3, 3) -> both recommend
        setup_feeds(espn_feed, espn_stats, n_home=3, n_away=3)
        home_team_stats = espn.get_team_stats(FIXTURE["home_team_id"], "eng.1")
        away_team_stats = espn.get_team_stats(FIXTURE["away_team_id"], "eng.1")
        main_res = main.process_fixture(FIXTURE, league_avg_goals=1.35)
        analyze_rows = analyze_all.analyze_gg_match(
            FIXTURE, home_team_stats, away_team_stats, league_avg=1.35
        )
        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")

        assert main_res["decision"] == "FLAG GG"
        assert gg_yes["system_recommendation"] == "RECOMMEND_PLAY"
        assert "Insufficient venue sample (<3)" not in main_res["rejection_reasons"]
        assert "Insufficient venue sample (<3)" not in gg_yes["filter_reasons"]
