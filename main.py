#!/usr/bin/env python3
"""
GG (BTTS) Football Prediction System - Main Entry Point.

Daily Workflow (NO UI):
1. Fetch today's fixtures for allowed leagues
2. For each fixture:
   - Fetch team statistics
   - Compute GG probability
   - Apply hard filters
   - Optionally fetch odds and calculate edge
   - Make decision
3. Output results (terminal + CSV + JSON)
4. Accept zero-bet days gracefully

Usage:
    python main.py              # Run for today
    python main.py 2025-01-20   # Run for specific date
"""

import sys
from datetime import date, datetime
from typing import List, Dict, Any

from config import ALLOWED_LEAGUES
from espn import (
    get_fixtures,
    get_league_avg_goals,
    get_league_baseline,
    get_team_history,
    get_team_stats,
    get_team_venue_averages,
)


from odds_api import get_btts_odds
from poisson import calculate_gg_probability
from decision import make_decision
from output import print_results, write_csv, write_json
# `validate_poisson_inputs` / `LeagueStats` / `TeamStats` are no longer imported
# here: they validated the CURRENT-SEASON aggregate dicts that used to feed the
# model. `build_fixture_poisson_inputs` returns a contract that carries its own
# completeness check, so the same guarantee is enforced one layer earlier and on
# point-in-time data. The legacy contract remains in `domain.validation` for the
# aggregate path and its tests.
from domain import evaluate_filters

from shared.match_history import (
    build_fixture_filter_stats,
    build_fixture_poisson_inputs,
)





def process_fixture(fixture: Dict[str, Any], league_avg_goals: float) -> Dict[str, Any]:
    """
    Process a single fixture and return prediction result.
    """
    result = {
        "fixture_id": fixture["fixture_id"],
        "datetime": fixture["datetime"],
        "league_id": fixture["league_id"],
        "league_name": fixture["league_name"],
        "home_team": fixture["home_team_name"],
        "away_team": fixture["away_team_name"],
        "home_team_id": fixture["home_team_id"],
        "away_team_id": fixture["away_team_id"],
        "lambda_home": None,
        "lambda_away": None,
        "gg_probability": None,
        "odds": None,
        "implied_probability": None,
        "edge": None,
        "passes_filters": False,
        "decision": "NO BET",
        "rejection_reasons": [],
    }

    # Fetch team statistics using league_code (league_id in fixture)
    home_stats = get_team_stats(fixture["home_team_id"], fixture["league_id"])
    away_stats = get_team_stats(fixture["away_team_id"], fixture["league_id"])

    # Check for missing data
    if home_stats is None or away_stats is None:
        result["rejection_reasons"].append("Missing or unreliable team stats")
        return result

    # ------------------------------------------------------------------
    # POINT-IN-TIME MODEL INPUTS (Epic 1B.5, LEAK-001).
    #
    # All five POISSON_V1 inputs are now derived from completed matches that
    # kicked off STRICTLY BEFORE this fixture, replacing ESPN's current-season
    # aggregates. Those aggregates describe the season as it stands TODAY, so
    # scoring a fixture from 1 December with them fed the model results from
    # January, February and March - the future, presented as evidence.
    #
    # The five quantities are semantically unchanged; only their provenance
    # moved. `league_avg_goals` is still goals per TEAM per match, each team is
    # still described by its own venue, and `poisson.py` is untouched.
    #
    # `league_avg_goals` (from standings) is still fetched above and passed in,
    # but is now used only as a diagnostic comparison; it is NOT what the model
    # consumes. See TASK 26 in docs/EPIC_1B5_POINT_IN_TIME_INPUTS.md.
    # ------------------------------------------------------------------
    model_inputs = build_fixture_poisson_inputs(
        fixture,
        get_team_venue_averages,
        get_league_baseline,
    )

    if not model_inputs.is_complete:
        # No fallback to the current-season aggregates. Reaching for them here
        # would fire exactly when history is thin - early season, promoted
        # sides, obscure leagues - and silently restore the leak in the cases
        # least likely to be checked. Unavailable is the honest answer.
        result["rejection_reasons"].append(
            "Point-in-time model inputs unavailable: "
            + ", ".join(model_inputs.missing)
        )
        result["model_input_samples"] = {
            "home": model_inputs.home_sample,
            "away": model_inputs.away_sample,
            "league": model_inputs.league_sample,
        }
        return result

    # Sample sizes travel with the prediction (TASK 17). A lambda built from one
    # match and one built from nineteen are not equally trustworthy, and the
    # float alone cannot say which it is. No minimum is imposed here - that is a
    # calibration decision for a later Epic, not a threshold to introduce quietly.
    result["model_input_samples"] = {
        "home": model_inputs.home_sample,
        "away": model_inputs.away_sample,
        "league": model_inputs.league_sample,
    }

    # Calculate GG probability using Poisson model
    poisson_result = calculate_gg_probability(
        league_avg_goals=model_inputs.league_avg_goals,
        home_goals_scored_home=model_inputs.home_goals_scored_home,
        home_goals_conceded_home=model_inputs.home_goals_conceded_home,
        away_goals_scored_away=model_inputs.away_goals_scored_away,
        away_goals_conceded_away=model_inputs.away_goals_conceded_away,
    )


    if poisson_result is None:
        result["rejection_reasons"].append("Failed to calculate probability")
        return result

    result["lambda_home"] = poisson_result["lambda_home"]
    result["lambda_away"] = poisson_result["lambda_away"]
    result["gg_probability"] = poisson_result["gg_probability"]

    # Apply hard filters through the single shared boundary (GG-006).
    #
    # This previously passed `total_goals_avg` - goals SCORED PLUS CONCEDED -
    # into a parameter documented as the team's goals-scored average, while
    # analyze_all.py passed the scoring rate. Same fixture, two verdicts. A side
    # scoring 5 and conceding 30 in 20 games has a scoring rate of 0.25 (a clear
    # fail) but a combined average of 1.75 (a comfortable pass), so the filter
    # was passing exactly the teams it exists to exclude.
    #
    # `build_filter_stats` is now the only place either entry point decides what
    # a filter input means. Thresholds are untouched.
    #
    # Epic 1B.4 supplies the clean-sheet feed. `build_fixture_filter_stats`
    # derives each team's rate from completed ESPN league matches kicking off
    # strictly before this fixture - home team at home, away team away - and
    # analyze_all.py calls the identical function, so the two cannot diverge.
    filter_result = evaluate_filters(
        build_fixture_filter_stats(fixture, home_stats, away_stats, get_team_history)
    )


    result["passes_filters"] = filter_result.passed
    result["filter_outcome"] = filter_result.outcome.value
    if filter_result.unavailable_fields:
        result["filter_data_unavailable"] = list(filter_result.unavailable_fields)
    if not filter_result.passed:
        result["rejection_reasons"].extend(filter_result.reasons)


    # Fetch odds (optional)
    # Note: get_btts_odds might need update for ESPN IDs or just use names (it uses names)
    odds = get_btts_odds(
        home_team=fixture["home_team_name"],
        away_team=fixture["away_team_name"],
        league_id=fixture["league_id"], # passing code (e.g. eng.1) might break int expectation in odds_api? check.
    )
    result["odds"] = odds

    # Make decision
    # TASK 18. The probability above is already recorded on `result` and stays
    # there regardless of what happens here: POISSON_V1 had all five of its
    # inputs, so the number is real and is reported. What an unevaluated filter
    # blocks is the RECOMMENDATION, not the calculation.
    #
    # `allows_recommendation` is True only for an explicit PASS, so both FAILED
    # and UNEVALUATED reach make_decision as False and yield NO BET.
    decision_result = make_decision(
        gg_probability=result["gg_probability"],
        odds=odds,
        passes_filters=filter_result.allows_recommendation,
    )


    result["implied_probability"] = decision_result.get("implied_probability")
    result["edge"] = decision_result.get("edge")
    result["decision"] = decision_result["decision"]

    # Add decision reasons if not already captured
    for reason in decision_result.get("reasons", []):
        if reason not in result["rejection_reasons"]:
            result["rejection_reasons"].append(reason)

    return result


def run_daily_workflow(target_date: date) -> List[Dict[str, Any]]:
    """
    Run the daily GG prediction workflow.
    """
    print(f"\nFetching fixtures for {target_date.strftime('%Y-%m-%d')}...")
    print(f"Allowed leagues: {', '.join(ALLOWED_LEAGUES.values())}")
    print()

    # Fetch today's fixtures
    fixtures = get_fixtures(target_date)

    if not fixtures:
        print("No fixtures found for today in allowed leagues.")
        return []

    print(f"Found {len(fixtures)} fixtures")
    print()

    results = []

    # Cache league average goals
    league_avg_cache = {}

    for fixture in fixtures:
        league_id = fixture["league_id"] # string code now

        # Get league average goals (cached)
        # GG-003 (Epic 1B.2): no fallback. An unobtainable league average is
        # cached as None and every fixture in that league is then refused by
        # process_fixture, because POISSON_V1 divides both lambdas by it.
        if league_id not in league_avg_cache:
            league_avg_cache[league_id] = get_league_avg_goals(league_id)

        league_avg_goals = league_avg_cache[league_id]

        # Process fixture
        result = process_fixture(fixture, league_avg_goals)
        results.append(result)

    return results


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        try:
            target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {sys.argv[1]}")
            print("Usage: python main.py [YYYY-MM-DD]")
            sys.exit(1)
    else:
        target_date = date.today()

    results = run_daily_workflow(target_date)
    print_results(results)

    date_str = target_date.strftime("%Y-%m-%d")
    write_csv(results, f"output_{date_str}.csv")
    write_json(results, f"output_{date_str}.json")


if __name__ == "__main__":
    main()
