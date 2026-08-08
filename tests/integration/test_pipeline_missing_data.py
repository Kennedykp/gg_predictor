"""
End-to-end proof that missing data can no longer produce a bet (Epic 1B.1).

The pre-fix failure chain, which these tests close:

    ESPN omits a statistic
      -> provider returns 0
      -> POISSON_V1 accepts 0 as real
      -> lambda_home = 0, so P(GG_YES) = 0.0
      -> analyze_all.py: P(GG_NO) = 1 - 0.0 = 1.0
      -> a 100%-confident GG_NO, priced against real odds
      -> STRONG_VALUE / RECOMMEND_PLAY on a statistic that never arrived

No network: `espn._make_request` and the odds lookup are both monkeypatched.
"""

from typing import Any, Dict, List, Optional

import pytest

import analyze_all
import espn
import main
import shared.odds

FULL_STATS: List[Dict[str, Any]] = [
    {"name": "gamesPlayed", "value": 20},
    {"name": "pointsFor", "value": 30},
    {"name": "pointsAgainst", "value": 20},
    {"name": "homeGamesPlayed", "value": 10},
    {"name": "awayGamesPlayed", "value": 10},
    {"name": "homePointsFor", "value": 18},
    {"name": "homePointsAgainst", "value": 8},
    {"name": "awayPointsFor", "value": 12},
    {"name": "awayPointsAgainst", "value": 12},
]

FIXTURE: Dict[str, Any] = {
    "fixture_id": "700",
    "league_id": "eng.1",
    "league_name": "English Premier League",
    "home_team_id": "359",
    "home_team_name": "Arsenal",
    "away_team_id": "360",
    "away_team_name": "Chelsea",
    "datetime": "2026-02-08T15:00Z",
    "status": "STATUS_SCHEDULED",
}


def payload(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"team": {"record": {"items": [{"type": "total", "stats": stats}]}}}


def without(*names: str) -> List[Dict[str, Any]]:
    return [s for s in FULL_STATS if s["name"] not in names]


@pytest.fixture
def espn_stats(monkeypatch):
    """Serve a per-team payload from the mocked ESPN team endpoint."""

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
    """
    Real, attractive odds on both sides.

    Deliberately generous: if a fabricated probability ever reaches the odds
    layer again, this pricing guarantees it classifies as STRONG_VALUE and the
    tests below fail loudly instead of quietly passing.
    """
    monkeypatch.setattr(shared.odds, "find_odds_for_match", lambda *a, **k: 1.80)
    monkeypatch.setattr(main, "get_btts_odds", lambda **k: 1.80)


class TestAnalyzeAllRefusesIncompleteData:
    def test_missing_home_statistic_produces_no_probability(self, espn_stats, generous_odds):
        espn_stats(
            {
                "359": payload(without("homePointsFor")),
                "360": payload(FULL_STATS),
            }
        )
        home = espn.get_team_stats("359", "eng.1")
        away = espn.get_team_stats("360", "eng.1")

        results = analyze_all.analyze_gg_match(FIXTURE, home, away, league_avg=1.35)

        assert len(results) == 2
        for row in results:
            assert row["model_probability"] is None
            assert row["filter_status"] == "MISSING_DATA"
            assert row["system_recommendation"] == "RECOMMEND_NO_PLAY"

    def test_no_hundred_percent_gg_no_from_missing_data(self, espn_stats, generous_odds):
        """The specific pre-fix danger: `1 - 0.0` published as certainty."""
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})
        results = analyze_all.analyze_gg_match(
            FIXTURE,
            espn.get_team_stats("359", "eng.1"),
            espn.get_team_stats("360", "eng.1"),
            league_avg=1.35,
        )
        gg_no = next(r for r in results if r["market"] == "GG_NO")
        assert gg_no["model_probability"] != 1.0
        assert gg_no["model_probability"] is None
        assert gg_no["classification"] != "STRONG_VALUE"

    def test_missing_data_is_never_recommended(self, espn_stats, generous_odds):
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})
        results = analyze_all.analyze_gg_match(
            FIXTURE,
            espn.get_team_stats("359", "eng.1"),
            espn.get_team_stats("360", "eng.1"),
            league_avg=1.35,
        )
        assert not any(r["system_recommendation"] == "RECOMMEND_PLAY" for r in results)

    def test_rejection_reason_names_the_absent_input(self, espn_stats, generous_odds):
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})
        results = analyze_all.analyze_gg_match(
            FIXTURE,
            espn.get_team_stats("359", "eng.1"),
            espn.get_team_stats("360", "eng.1"),
            league_avg=1.35,
        )
        assert "home_goals_scored_home" in results[0]["filter_reasons"][0]

    def test_missing_away_statistic_also_blocks(self, espn_stats, generous_odds):
        espn_stats({"359": payload(FULL_STATS), "360": payload(without("awayPointsAgainst"))})
        results = analyze_all.analyze_gg_match(
            FIXTURE,
            espn.get_team_stats("359", "eng.1"),
            espn.get_team_stats("360", "eng.1"),
            league_avg=1.35,
        )
        assert all(r["model_probability"] is None for r in results)
        assert "away_goals_conceded_away" in results[0]["filter_reasons"][0]

    def test_complete_data_still_produces_a_probability(self, espn_stats, generous_odds):
        """
        The other half of the contract: the fix must not block valid fixtures.
        Complete data must still flow through POISSON_V1 exactly as before.
        """
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})
        results = analyze_all.analyze_gg_match(
            FIXTURE,
            espn.get_team_stats("359", "eng.1"),
            espn.get_team_stats("360", "eng.1"),
            league_avg=1.35,
        )
        assert len(results) == 2
        for row in results:
            assert row["model_probability"] is not None
            assert 0.0 <= row["model_probability"] <= 1.0
            assert row["filter_status"] != "MISSING_DATA"

        yes = next(r for r in results if r["market"] == "GG_YES")
        no = next(r for r in results if r["market"] == "GG_NO")
        assert yes["model_probability"] + no["model_probability"] == pytest.approx(1.0, abs=1e-4)

    def test_genuine_zero_scoring_is_still_modelled(self, espn_stats, generous_odds):
        """A goalless team is real data - it must be predicted, not refused."""
        goalless = [
            {**s, "value": 0} if s["name"] == "homePointsFor" else s for s in FULL_STATS
        ]
        espn_stats({"359": payload(goalless), "360": payload(FULL_STATS)})
        results = analyze_all.analyze_gg_match(
            FIXTURE,
            espn.get_team_stats("359", "eng.1"),
            espn.get_team_stats("360", "eng.1"),
            league_avg=1.35,
        )
        assert all(r["filter_status"] != "MISSING_DATA" for r in results)
        assert results[0]["model_probability"] is not None


class TestMainRefusesIncompleteData:
    def test_missing_statistic_yields_no_bet(self, espn_stats, generous_odds):
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})
        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["gg_probability"] is None
        assert result["decision"] == "NO BET"
        assert result["passes_filters"] is False
        assert any("home_goals_scored_home" in r for r in result["rejection_reasons"])

    def test_unavailable_league_average_yields_no_bet(self, espn_stats, generous_odds):
        """
        POISSON_V1 divides by this value, so an absent league average must stop
        the prediction rather than be replaced.
        """
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})
        # None is deliberately outside the declared `float` parameter type: the
        # point of the test is that an unavailable average is refused, not coerced.
        result = main.process_fixture(FIXTURE, league_avg_goals=None)

        assert result["gg_probability"] is None
        assert result["decision"] == "NO BET"
        assert any("league_avg_goals" in r for r in result["rejection_reasons"])

    def test_unavailable_team_record_yields_no_bet(self, espn_stats, generous_odds):
        espn_stats({"359": None, "360": payload(FULL_STATS)})
        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["decision"] == "NO BET"
        assert "Missing or unreliable team stats" in result["rejection_reasons"]

    def test_complete_data_still_produces_a_prediction(self, espn_stats, generous_odds):
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})
        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["gg_probability"] is not None
        assert result["lambda_home"] is not None
        assert result["lambda_away"] is not None
        assert 0.0 <= result["gg_probability"] <= 1.0

    def test_no_typeerror_when_filter_inputs_are_unavailable(self, espn_stats, generous_odds):
        """
        `total_goals_avg` can now be None, and `None < 1.0` raises TypeError.
        The fixture must be rejected cleanly instead of crashing the run.
        """
        espn_stats(
            {
                "359": payload(without("pointsFor")),  # kills total_goals_avg only
                "360": payload(FULL_STATS),
            }
        )
        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["passes_filters"] is False
        assert "Missing or unreliable data" in result["rejection_reasons"]
        # The five model inputs were intact, so the model still ran.
        assert result["gg_probability"] is not None
