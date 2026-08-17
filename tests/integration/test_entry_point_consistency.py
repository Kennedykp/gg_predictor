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
from conftest import espn_event, utc

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
    # Epic 1B.5: model inputs are derived from matches strictly before kickoff,
    # so the target kickoff is now a required part of the fixture contract.
    "kickoff_utc": utc(2026, 2, 8, 15, 0),
}

# Point-in-time history feeding the five POISSON_V1 inputs. Both entry points
# read the SAME feed here - that shared source is the whole point of GG-006, so
# any divergence in the verdict is attributable to wiring rather than to data.
HOME_HISTORY = [espn_event(f"eh{i}", utc(2025, 9, i + 1), "359", "900", 2, 1) for i in range(5)]
AWAY_HISTORY = [espn_event(f"ea{i}", utc(2025, 9, i + 1), "901", "360", 1, 2) for i in range(5)]


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
    games=20,
    home_games=10,
    away_games=10,
    home_for=3,
    home_against=18,
    away_for=2,
    away_against=12,
)
SOLID_AWAY = stats_payload(
    games=20,
    home_games=10,
    away_games=10,
    home_for=18,
    home_against=8,
    away_for=14,
    away_against=10,
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


@pytest.fixture(autouse=True)
def _point_in_time_history(espn_feed):
    """
    A complete match history for both sides, shared by both entry points.

    Autouse because since Epic 1B.5 the model reads this rather than the
    aggregate payload. Without it every fixture would be refused for missing
    model inputs, and the filter-agreement assertions below - which are the
    actual subject of GG-006 - would never be reached.
    """
    espn_feed(
        team_events={"359": HOME_HISTORY, "360": AWAY_HISTORY},
        league_events=HOME_HISTORY + AWAY_HISTORY,
    )


@pytest.fixture
def generous_odds(monkeypatch):
    """
    Attractive odds on both sides, so a wrongly-passed fixture would be recommended.

    EPIC 2F-P0-1. This priced the market at 1.80, which does not honour the
    docstring above. These fixtures model P(GG) = 0.5423, and 1.80 implies
    0.5556, so the edge was NEGATIVE (-0.0133) and every assertion of
    "RECOMMEND_NO_PLAY" below was satisfied by the PRICE rather than by the
    filters. The recommendation was ungated for a year and the suite stayed
    green. Break-even for this fixture is 1 / (0.5423 - 0.05) = 2.0313, so 2.50
    is unambiguously above it: the edge alone (+0.1423) now WANTS to recommend,
    and only the filter gate stops it. See TestEpic2FRecommendationGating, which
    asserts that premise directly so this can never silently regress.
    """

    monkeypatch.setattr(shared.odds, "find_odds_for_match", lambda *a, **k: 2.50)
    monkeypatch.setattr(main, "get_btts_odds", lambda **k: 2.50)



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
        scoring_rate = 3 / 10  # what the filter must see
        combined = (5 + 30) / 20  # what main.py used to send

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
        self, espn_stats, generous_odds, monkeypatch
    ):
        """
        GG-002 end-to-end: an unavailable clean-sheet rate blocks BOTH scripts.

        Epic 1B.4 changed where this rate comes from. It used to be structurally
        impossible - ESPN's aggregates cannot express it, so it was permanently
        unavailable and this test needed no setup. It is now derived from match
        history, so unavailability has to be CAUSED rather than assumed.

        Only the clean-sheet lookup is failed here, not the whole feed. Failing
        the feed outright would also starve the model, the fixture would be
        rejected before filters ran, and the test would pass without ever
        reaching the assertion it exists to make.
        """
        espn_stats({"359": SOLID_AWAY, "360": SOLID_AWAY})
        for module in (main, analyze_all):
            monkeypatch.setattr(module, "get_team_history", lambda **kwargs: None)
        main_result, analyze_rows = run_both()

        assert main_result["decision"] == "NO BET"
        assert main_result["filter_outcome"] == "UNEVALUATED"
        assert all(row["filter_status"] == "FILTER_DATA_UNAVAILABLE" for row in analyze_rows)
        assert all(row["system_recommendation"] == "RECOMMEND_NO_PLAY" for row in analyze_rows)

    def test_both_entry_points_agree_once_clean_sheet_data_exists(self, espn_stats, generous_odds):
        """
        The other side of the same coin, and the Epic 1B.4 payoff.

        With a real history the rate is genuinely measured - 0% here, since both
        sides conceded in every match - so the clean-sheet filter can finally be
        EVALUATED rather than skipped. Both entry points must reach that state
        together; if only one of them consumed the derived history, this fails.
        """
        espn_stats({"359": SOLID_AWAY, "360": SOLID_AWAY})
        main_result, analyze_rows = run_both()

        assert main_result["filter_outcome"] != "UNEVALUATED"
        assert not main_result.get("filter_data_unavailable")
        assert all(row["filter_status"] != "FILTER_DATA_UNAVAILABLE" for row in analyze_rows)

        # And the verdict itself still agrees across the two scripts.
        assert main_result["passes_filters"] == (analyze_rows[0]["filter_status"] == "PASSED")

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
        assert gg_yes["model_probability"] == pytest.approx(main_result["gg_probability"], abs=1e-4)


class TestEpic2FRecommendationGating:
    """
    EPIC 2F-P0-1 — a filter verdict must GATE the recommendation, not annotate it.

    `analyze_market()` derives `system_recommendation` from edge and odds alone.
    `analyze_all.py` attached `filter_status`/`filter_reasons` to the row without
    revising that verdict, so a fixture could publish the reasons it must be
    rejected AND `RECOMMEND_PLAY` in the same breath, and `print_summary` listed
    it under "RECOMMENDED PLAYS".

    Every test here prices the market at 2.50, above this fixture's 2.0313
    break-even, so the edge genuinely wants to recommend and only the gate stops
    it. `test_the_edge_alone_would_recommend` pins that premise, which is what
    the previous 1.80 fixture lacked.
    """

    def test_the_edge_alone_would_recommend(self, espn_stats, generous_odds):
        """
        Anti-vacuity guard, and the reason the original test never caught this.

        A PASSING fixture at 2.50 must reach RECOMMEND_PLAY. If a future change
        makes these odds unattractive, or moves the probability, this fails
        loudly instead of letting the tests below pass for the wrong reason.
        """
        espn_stats({"359": SOLID_AWAY, "360": SOLID_AWAY})
        _, analyze_rows = run_both()

        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")
        assert gg_yes["filter_status"] == "PASSED"
        assert gg_yes["edge"] > 0.05
        assert gg_yes["system_recommendation"] == "RECOMMEND_PLAY"

    def test_failed_filters_cannot_recommend_despite_high_edge(self, espn_stats, generous_odds):
        """A high edge on a fixture that FAILED the hard filters must not be played."""
        espn_stats({"359": LEAKY_HOME, "360": SOLID_AWAY})
        _, analyze_rows = run_both()

        assert all(row["filter_status"] == "FILTERED" for row in analyze_rows)
        assert all(row["system_recommendation"] == "RECOMMEND_NO_PLAY" for row in analyze_rows)

        # The bet is refused, but the measurements survive: the edge is still
        # reported, so the refusal is auditable rather than hidden.
        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")
        assert gg_yes["edge"] > 0.05
        assert gg_yes["model_probability"] is not None
        assert gg_yes["filter_reasons"]

    def test_unavailable_filter_data_cannot_recommend_despite_high_edge(
        self, espn_stats, generous_odds, monkeypatch
    ):
        """
        Absent data is not permission. Only the clean-sheet lookup is failed, so
        the model still has all five inputs and the row still carries a real
        edge - otherwise this would pass merely because nothing was computed.
        """
        espn_stats({"359": SOLID_AWAY, "360": SOLID_AWAY})
        for module in (main, analyze_all):
            monkeypatch.setattr(module, "get_team_history", lambda **kwargs: None)
        _, analyze_rows = run_both()

        assert all(row["filter_status"] == "FILTER_DATA_UNAVAILABLE" for row in analyze_rows)
        assert all(row["system_recommendation"] == "RECOMMEND_NO_PLAY" for row in analyze_rows)

        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")
        assert gg_yes["edge"] > 0.05
        assert gg_yes["model_probability"] is not None

    def test_gate_never_recommends_against_a_non_passing_status(self, espn_stats, generous_odds):
        """
        The invariant, stated once over every row rather than per scenario:
        RECOMMEND_PLAY implies filter_status == "PASSED".
        """
        for home_payload, away_payload in (
            (LEAKY_HOME, SOLID_AWAY),
            (SOLID_AWAY, LEAKY_HOME),
            (LEAKY_HOME, LEAKY_HOME),
            (SOLID_AWAY, SOLID_AWAY),
        ):
            espn_stats({"359": home_payload, "360": away_payload})
            _, analyze_rows = run_both()

            for row in analyze_rows:
                if row["system_recommendation"] == "RECOMMEND_PLAY":
                    assert row["filter_status"] == "PASSED", (
                        f"{row['market']} recommended a play with "
                        f"filter_status={row['filter_status']}"
                    )

    def test_gate_leaves_every_measurement_untouched(self, espn_stats, generous_odds):
        """
        The fix must change the RECOMMENDATION and nothing else. Probability,
        lambdas, odds, implied probability, edge and classification are all
        evidence and must survive the refusal intact - `classification` included,
        because it describes the PRICE, not the bet.
        """
        espn_stats({"359": LEAKY_HOME, "360": SOLID_AWAY})
        _, analyze_rows = run_both()

        gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")
        assert gg_yes["filter_status"] == "FILTERED"
        assert gg_yes["system_recommendation"] == "RECOMMEND_NO_PLAY"

        # A genuinely mispriced but unbettable market still reads as mispriced.
        assert gg_yes["classification"] == "STRONG_VALUE"
        assert gg_yes["odds"] == 2.50
        assert gg_yes["implied_probability"] == pytest.approx(0.40, abs=1e-4)
        assert gg_yes["model_probability"] is not None
        assert gg_yes["lambda_home"] is not None
        assert gg_yes["lambda_away"] is not None
        assert gg_yes["edge"] == pytest.approx(
            gg_yes["model_probability"] - gg_yes["implied_probability"], abs=1e-3
        )

    def test_both_entry_points_now_agree_on_what_may_be_published(
        self, espn_stats, generous_odds
    ):
        """
        The GG-006 guarantee extended to the publication step. main.py gates on
        `allows_recommendation`; analyze_all.py now applies the same rule, so a
        FLAG in one and a RECOMMEND_PLAY in the other can no longer disagree.
        """
        for home_payload, away_payload in (
            (LEAKY_HOME, SOLID_AWAY),
            (SOLID_AWAY, SOLID_AWAY),
        ):
            espn_stats({"359": home_payload, "360": away_payload})
            main_result, analyze_rows = run_both()

            main_recommends = main_result["decision"] != "NO BET"
            gg_yes = next(r for r in analyze_rows if r["market"] == "GG_YES")
            analyze_recommends = gg_yes["system_recommendation"] == "RECOMMEND_PLAY"

            assert main_recommends == analyze_recommends


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
