"""
GG-006 — both entry points must filter identically (Epic 1B.3, Task 12).

The defect: `main.py` passed `total_goals_avg` (goals scored PLUS conceded) into
the parameter `analyze_all.py` fed with the team's goals-SCORED rate. Same
fixture, same ESPN response, two different verdicts depending on which script
was run.

These tests drive both entry points from ONE mocked ESPN response and require
the same filter conclusion. They are deliberately behavioural: they assert on
what each script outputs, not on internal calls, so a future refactor that keeps
the outputs aligned is free to change the wiring.
"""

from typing import Any, Dict, Optional

import pytest

import analyze_all
import espn
import main
import shared.odds
from config import MIN_AVG_GOALS
from domain import build_filter_stats, evaluate_filters

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
}


def stats_payload(
    games: int,
    home_games: int,
    away_games: int,
    home_for: int,
    home_against: int,
    away_for: int,
    away_against: int,
) -> Dict[str, Any]:
    """A complete ESPN team record built from explicit goal counts."""
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


# The fixture that exposed GG-006.
#
# Home team: 5 goals scored, 30 conceded in 20 matches (10 home, 10 away).
#   home scoring rate      = 3 / 10 = 0.30   -> FAILS MIN_AVG_GOALS
#   total_goals_avg        = 35 / 20 = 1.75  -> PASSES MIN_AVG_GOALS
#
# A hopeless attack that leaks goals. The old main.py saw 1.75 and approved it;
# analyze_all.py saw 0.30 and rejected it. Both must now reject.
LEAKY_HOME = stats_payload(
    games=20, home_games=10, away_games=10,
    home_for=3, home_against=18, away_for=2, away_against=12,
)
SOLID_AWAY = stats_payload(
    games=20, home_games=10, away_games=10,
    home_for=18, home_against=8, away_for=14, away_against=10,
)


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
    """Attractive odds on both sides, so a wrongly-passed fixture would be recommended."""
    monkeypatch.setattr(shared.odds, "find_odds_for_match", lambda *a, **k: 1.80)
    monkeypatch.setattr(main, "get_btts_odds", lambda **k: 1.80)


def run_both(league_avg: float = 1.35):
    """Drive both entry points over the same fixture and mocked provider."""
    home = espn.get_team_stats(FIXTURE["home_team_id"], "eng.1")
    away = espn.get_team_stats(FIXTURE["away_team_id"], "eng.1")
    # Every payload used here is complete, so the provider returns a record.
    # Asserted rather than cast so a broken payload fails here with a clear
    # message instead of surfacing deeper in the pipeline.
    assert home is not None and away is not None

    main_result = main.process_fixture(FIXTURE, league_avg_goals=league_avg)

    analyze_rows = analyze_all.analyze_gg_match(FIXTURE, home, away, league_avg=league_avg)
    return main_result, analyze_rows


class TestGG006GoalsAverageSemantics:
    """The exact disagreement Epic 0 recorded as D4/GG-006."""

    def test_the_two_readings_genuinely_disagree(self):
        """
        Establishes the test fixture is meaningful before asserting on it. If
        these two ever stopped straddling the threshold, the tests below would
        pass vacuously.
        """
        scoring_rate = 3 / 10          # what the filter must see
        combined = (5 + 30) / 20       # what main.py used to send

        assert scoring_rate < MIN_AVG_GOALS
        assert combined > MIN_AVG_GOALS

    def test_both_entry_points_reject_the_leaky_team(self, espn_stats, generous_odds):
        espn_stats({"359": LEAKY_HOME, "360": SOLID_AWAY})
        main_result, analyze_rows = run_both()

        assert main_result["passes_filters"] is False
        assert all(row["filter_status"] != "PASSED" for row in analyze_rows)

    def test_main_no_longer_uses_the_combined_average(self, espn_stats, generous_odds):
        """
        The regression that would reintroduce GG-006. With `total_goals_avg`
        (1.75) the goals filter passed; with the scoring rate (0.30) it fails.
        """
        espn_stats({"359": LEAKY_HOME, "360": SOLID_AWAY})
        main_result, _ = run_both()

        assert main_result["decision"] == "NO BET"
        assert main_result["passes_filters"] is False

    def test_both_entry_points_report_the_same_reasons(self, espn_stats, generous_odds):
        espn_stats({"359": LEAKY_HOME, "360": SOLID_AWAY})
        main_result, analyze_rows = run_both()

        # analyze_all reports filter reasons per market row; main aggregates them
        # into rejection_reasons alongside decision reasons. The filter-derived
        # subset must be identical.
        analyze_reasons = set(analyze_rows[0]["filter_reasons"])
        assert analyze_reasons
        assert analyze_reasons.issubset(set(main_result["rejection_reasons"]))


class TestIdenticalFilterConclusion:
    """Same ESPN response in, same filter conclusion out - whichever script runs."""

    @pytest.mark.parametrize(
        "home_payload, away_payload",
        [
            (LEAKY_HOME, SOLID_AWAY),
            (SOLID_AWAY, LEAKY_HOME),
            (SOLID_AWAY, SOLID_AWAY),
        ],
    )
    def test_filter_verdict_matches(self, espn_stats, generous_odds, home_payload, away_payload):
        espn_stats({"359": home_payload, "360": away_payload})
        main_result, analyze_rows = run_both()

        main_passed = main_result["passes_filters"]
        analyze_passed = analyze_rows[0]["filter_status"] == "PASSED"
        assert main_passed == analyze_passed

    def test_neither_entry_point_recommends_without_clean_sheet_data(
        self, espn_stats, generous_odds
    ):
        """
        ESPN cannot supply clean-sheet rates, so with a COMPLETE response both
        entry points must still refuse to recommend - despite generous odds and
        a healthy probability. This is GG-002 closed end-to-end.
        """
        espn_stats({"359": SOLID_AWAY, "360": SOLID_AWAY})
        main_result, analyze_rows = run_both()

        assert main_result["decision"] == "NO BET"
        assert main_result["filter_outcome"] == "UNEVALUATED"
        assert all(row["filter_status"] == "FILTER_DATA_UNAVAILABLE" for row in analyze_rows)
        assert all(
            row["system_recommendation"] == "RECOMMEND_NO_PLAY" for row in analyze_rows
        )

    def test_probability_is_still_produced_by_both(self, espn_stats, generous_odds):
        """
        TASK 18. Unavailable FILTER data must not suppress the POISSON_V1
        probability - the five model inputs were all present, so the number is
        real and both entry points report it.
        """
        espn_stats({"359": SOLID_AWAY, "360": SOLID_AWAY})
        main_result, analyze_rows = run_both()

        assert main_result["gg_probability"] is not None
        assert all(row["model_probability"] is not None for row in analyze_rows)

        # analyze_all rounds for display (4dp); main keeps full precision. That
        # is presentation, not a modelling difference, so the tolerance matches
        # the rounding rather than pinning one representation over the other.
        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")
        assert gg_yes["model_probability"] == pytest.approx(
            main_result["gg_probability"], abs=1e-4
        )



class TestSharedBoundaryIsTheOnlyInterpreter:
    """
    Structural guard. Both entry points must obtain filter inputs from
    `build_filter_stats`; if either starts assembling its own, the mapping can
    drift apart again exactly as it did before.
    """

    def test_neither_entry_point_imports_apply_filters_directly(self):
        import inspect

        for module in (main, analyze_all):
            source = inspect.getsource(module)
            assert "from filters import" not in source, (
                f"{module.__name__} must route through domain.evaluate_filters, "
                "not call the raw filter API"
            )

    def test_build_filter_stats_is_the_single_mapping(self):
        """
        The mapping itself, asserted once. If someone swaps a key here, the
        entry-point tests above start failing together rather than silently
        diverging.
        """
        home = {"home_goals_scored": 1.7, "total_goals_avg": 99.0, "home_clean_sheet_pct": 0.1}
        away = {"away_goals_scored": 1.3, "total_goals_avg": 99.0, "away_clean_sheet_pct": 0.2}

        built = build_filter_stats(home, away)

        assert built.home_avg_goals_scored == 1.7
        assert built.away_avg_goals_scored == 1.3
        assert 99.0 not in (built.home_avg_goals_scored, built.away_avg_goals_scored)
        assert evaluate_filters(built).passed is True
