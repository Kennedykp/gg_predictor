"""
End-to-end proof that missing data can no longer produce a bet.

The original failure chain (Epic 1B.1, GG-001), which these tests closed:

    ESPN omits a statistic
      -> provider returns 0
      -> POISSON_V1 accepts 0 as real
      -> lambda_home = 0, so P(GG_YES) = 0.0
      -> analyze_all.py: P(GG_NO) = 1 - 0.0 = 1.0
      -> a 100%-confident GG_NO, priced against real odds
      -> STRONG_VALUE / RECOMMEND_PLAY on a statistic that never arrived

WHAT CHANGED IN EPIC 1B.5, AND WHY THESE TESTS MOVED
----------------------------------------------------
The invariant is unchanged and still enforced:

    if an input the pipeline actually uses is unavailable,
    no recommendation is produced.

What changed is WHICH input feeds WHICH consumer. The model's five POISSON_V1
inputs used to come from ESPN's current-season aggregate endpoint. They now come
from completed matches that kicked off strictly before the fixture, because the
aggregates describe the season as it stands TODAY - so scoring an already-played
fixture used that fixture's own result as evidence for itself (LEAK-001).

The consequence for these tests is precise and worth stating plainly:

    deleting `awayPointsAgainst` from the aggregate payload no longer blocks
    the model, because the model no longer reads it.

That is not a weakened guarantee, it is a relocated one. So each test below now
targets the source its consumer genuinely depends on:

    MODEL   <- point-in-time match history   (absent -> no probability at all)
    FILTERS <- aggregates + derived history  (absent -> UNEVALUATED, no bet)

Tests that assert the aggregate path still blocks the MODEL would now be
asserting a coupling that no longer exists, and would pass or fail for reasons
unrelated to safety.

No network: sockets are blocked in conftest, and both ESPN transports
(`_make_request` for aggregates, `_fetch` for schedule/scoreboard) are stubbed.
"""

from typing import Any, Dict, List, Optional

import pytest
from conftest import espn_event, utc

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
    # Epic 1B.5: the target kickoff is load-bearing. Every model input is derived
    # from matches strictly before it, so a fixture without one is unpriceable.
    # `espn.get_fixtures` populates this in production; this dict predated it.
    "kickoff_utc": utc(2026, 2, 8, 15, 0),
}

# The point-in-time history behind the five POISSON_V1 inputs.
# Home side wins 2-1 at home five times; away side wins 2-1 away five times.
# Asymmetric per venue, so a reversed perspective would change the numbers
# rather than quietly cancel out.
HOME_HISTORY = [espn_event(f"h{i}", utc(2025, 9, i + 1), "359", "900", 2, 1) for i in range(5)]
AWAY_HISTORY = [espn_event(f"a{i}", utc(2025, 9, i + 1), "901", "360", 1, 2) for i in range(5)]
LEAGUE_HISTORY = HOME_HISTORY + AWAY_HISTORY


def payload(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"team": {"record": {"items": [{"type": "total", "stats": stats}]}}}


def without(*names: str) -> List[Dict[str, Any]]:
    return [s for s in FULL_STATS if s["name"] not in names]


@pytest.fixture
def espn_stats(monkeypatch):
    """Serve a per-team payload from the mocked ESPN aggregate endpoint."""

    def _install(by_team: Dict[str, Optional[Dict[str, Any]]]):
        def fake_request(url: str, params: Optional[dict] = None):
            for team_id, response in by_team.items():
                if f"/teams/{team_id}" in url:
                    return response
            return None

        monkeypatch.setattr(espn, "_make_request", fake_request)

    return _install


@pytest.fixture(autouse=True)
def _point_in_time_history(espn_feed):
    """
    A complete, valid match history for both sides and for the league.

    Autouse because this is what the MODEL now runs on. Without it every fixture
    here would report "inputs unavailable", and the aggregate-focused tests would
    pass for the wrong reason - they would stop exercising the thing they exist
    to protect. Individual tests override it where the point is its absence.
    """
    espn_feed(
        team_events={"359": HOME_HISTORY, "360": AWAY_HISTORY},
        league_events=LEAGUE_HISTORY,
    )


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


def run_analyze_all(league_avg: Optional[float] = 1.35) -> List[Dict[str, Any]]:
    return analyze_all.analyze_gg_match(
        FIXTURE,
        espn.get_team_stats("359", "eng.1"),
        espn.get_team_stats("360", "eng.1"),
        league_avg=league_avg,
    )


class TestModelInputsUnavailable:
    """
    The MODEL's own source is the match history. When it is absent the fixture
    is unpriceable, and POISSON_V1 must not be called at all.
    """

    def test_no_prior_matches_produces_no_probability(self, espn_stats, generous_odds, espn_feed):
        """
        An empty history is the August case: the season has not started, so
        there is genuinely nothing to model. The old code would have fallen back
        to current-season aggregates, which is precisely the leak.
        """
        espn_feed(team_events={}, league_events=[])
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["gg_probability"] is None
        assert result["decision"] == "NO BET"
        assert any(
            "Point-in-time model inputs unavailable" in r for r in result["rejection_reasons"]
        )

    def test_provider_failure_produces_no_probability(self, espn_stats, generous_odds, espn_feed):
        """
        A failed fetch is not a zero. It must not be silently modelled as one,
        and it must not fall back to the leaky aggregate path.
        """
        espn_feed(fail=True)
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["gg_probability"] is None
        assert result["decision"] == "NO BET"

    def test_unavailable_history_is_never_recommended(self, espn_stats, generous_odds, espn_feed):
        """The odds here are generous enough to tempt a fabricated probability."""
        espn_feed(team_events={}, league_events=[])
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        rows = run_analyze_all()

        assert all(r["model_probability"] is None for r in rows)
        assert not any(r["system_recommendation"] == "RECOMMEND_PLAY" for r in rows)

    def test_no_hundred_percent_gg_no_from_missing_data(self, espn_stats, generous_odds, espn_feed):
        """
        The specific pre-fix danger: `P(GG_NO) = 1 - 0.0` published as certainty.

        This is the assertion that most directly encodes GG-001, so it is kept
        pointed at whichever source can currently starve the model - now the
        history rather than the aggregates.
        """
        espn_feed(team_events={}, league_events=[])
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        gg_no = next(r for r in run_analyze_all() if r["market"] == "GG_NO")

        assert gg_no["model_probability"] != 1.0
        assert gg_no["model_probability"] is None
        assert gg_no["classification"] != "STRONG_VALUE"


class TestFilterInputsUnavailable:
    """
    The FILTERS still read the aggregate endpoint. An absent filter input must
    leave the filter UNEVALUATED and block the bet - while the model, which no
    longer depends on that endpoint, legitimately still produces a probability.

    This is the MODEL AVAILABLE / FILTER DATA UNAVAILABLE split.
    """

    def test_missing_aggregate_blocks_recommendation_not_probability(
        self, espn_stats, generous_odds
    ):
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        # The model ran: its own inputs came from match history and were complete.
        assert result["gg_probability"] is not None

        # The recommendation did not.
        assert result["passes_filters"] is False
        assert result["filter_outcome"] == "UNEVALUATED"
        assert result["decision"] == "NO BET"
        assert "home_avg_goals_scored" in result["filter_data_unavailable"]

    def test_rejection_reason_names_the_absent_input(self, espn_stats, generous_odds):
        """Naming the field is what made this class of bug findable at all."""
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert any("home_avg_goals_scored" in r for r in result["rejection_reasons"])

    def test_missing_aggregate_is_never_recommended(self, espn_stats, generous_odds):
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})

        rows = run_analyze_all()

        assert all(r["filter_status"] == "FILTER_DATA_UNAVAILABLE" for r in rows)
        assert not any(r["system_recommendation"] == "RECOMMEND_PLAY" for r in rows)

    def test_clean_sheet_unavailability_blocks_recommendation_but_not_probability(
        self, espn_stats, generous_odds, monkeypatch
    ):
        """
        GG-002's live consequence, now driven by the real feed.

        The clean-sheet history and the model's inputs are separate provider
        calls, so one can fail alone. Before Epic 1B.3 the rates arrived as a
        hardcoded 0, the filter silently passed, and the fixture could be
        recommended on a statistic nobody had ever measured.
        """
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})
        monkeypatch.setattr(main, "get_team_history", lambda **kwargs: None)

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["gg_probability"] is not None
        assert 0.0 <= result["gg_probability"] <= 1.0
        assert result["lambda_home"] is not None

        assert result["passes_filters"] is False
        assert result["filter_outcome"] == "UNEVALUATED"
        assert result["decision"] == "NO BET"
        assert set(result["filter_data_unavailable"]) == {
            "home_clean_sheet_pct",
            "away_clean_sheet_pct",
        }

    def test_unavailable_team_record_yields_no_bet(self, espn_stats, generous_odds):
        """An absent aggregate response entirely, rather than a missing field."""
        espn_stats({"359": None, "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["decision"] == "NO BET"
        assert "Missing or unreliable team stats" in result["rejection_reasons"]

    def test_no_typeerror_when_filter_inputs_are_unavailable(self, espn_stats, generous_odds):
        """
        A filter input can be None, and `None < 1.0` raises TypeError. The
        fixture must be rejected cleanly rather than crashing the run.
        """
        espn_stats({"359": payload(without("homePointsFor")), "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["passes_filters"] is False
        assert result["decision"] == "NO BET"


class TestCompleteDataStillFlows:
    """The fix must not block valid fixtures. Real data must still be priced."""

    def test_complete_data_still_produces_a_probability(self, espn_stats, generous_odds):
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        results = run_analyze_all()

        assert len(results) == 2
        for row in results:
            assert row["model_probability"] is not None
            assert 0.0 <= row["model_probability"] <= 1.0
            assert row["filter_status"] != "MISSING_DATA"

        yes = next(r for r in results if r["market"] == "GG_YES")
        no = next(r for r in results if r["market"] == "GG_NO")
        assert yes["model_probability"] + no["model_probability"] == pytest.approx(1.0, abs=1e-4)

    def test_complete_data_still_produces_a_prediction(self, espn_stats, generous_odds):
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        result = main.process_fixture(FIXTURE, league_avg_goals=1.35)

        assert result["gg_probability"] is not None
        assert result["lambda_home"] is not None
        assert result["lambda_away"] is not None
        assert 0.0 <= result["gg_probability"] <= 1.0

    def test_genuine_zero_scoring_is_still_modelled(self, espn_stats, generous_odds, espn_feed):
        """
        A goalless team is real data - it must be predicted, not refused.

        The zero now has to come from actual matches, so this home side genuinely
        failed to score in all five of its home games. That drives lambda_home to
        0 and P(GG_YES) to 0.0 - a MEASURED zero, which must still be published.
        The contrast that matters is with an ABSENT statistic, which yields None
        and no prediction at all.
        """
        goalless_home = [
            espn_event(f"z{i}", utc(2025, 9, i + 1), "359", "900", 0, 1) for i in range(5)
        ]
        espn_feed(
            team_events={"359": goalless_home, "360": AWAY_HISTORY},
            league_events=goalless_home + AWAY_HISTORY,
        )
        espn_stats({"359": payload(FULL_STATS), "360": payload(FULL_STATS)})

        results = run_analyze_all()

        assert all(r["filter_status"] != "MISSING_DATA" for r in results)
        gg_yes = next(r for r in results if r["market"] == "GG_YES")
        assert gg_yes["model_probability"] == 0.0
