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
from espn import get_fixtures, get_team_stats, get_league_avg_goals
from odds_api import get_btts_odds
from poisson import calculate_gg_probability
from filters import apply_filters
from decision import make_decision
from output import print_results, write_csv, write_json
from domain import LeagueStats, TeamStats, validate_poisson_inputs


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

    # Validate the five required POISSON_V1 inputs BEFORE calling the model.
    # GG-001 (Epic 1B.1): this guard existed before but could never fire, because
    # the provider converted every absent statistic to 0. Now that absence is
    # represented as None, incomplete data is refused instead of being modelled.
    #
    # Nothing is substituted here - no zero, no league average, no other team's
    # figures. An unavailable input means no prediction for this fixture.
    # CALCULATED since Epic 1B.2 (GG-003 resolved). get_league_avg_goals() no
    # longer substitutes 1.35 - it computes the figure from real ESPN standings
    # or returns None - so a value arriving here is genuinely measured, and an
    # unavailable one arrives as None and stops the prediction below.
    validation = validate_poisson_inputs(
        league=(
            LeagueStats.calculated(fixture["league_id"], league_avg_goals)
            if league_avg_goals is not None
            else LeagueStats.unavailable(fixture["league_id"])
        ),
        home_team=TeamStats.from_provider_dict(home_stats),
        away_team=TeamStats.from_provider_dict(away_stats),
    )

    if not validation.is_complete:
        result["rejection_reasons"].append(validation.reason())
        return result

    model_inputs = validation.inputs
    assert model_inputs is not None  # guaranteed by is_complete

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

    # Apply hard filters.
    # Filter inputs can now be None (GG-001), and comparing None against a
    # threshold raises TypeError. When an input is unavailable the filter cannot
    # be evaluated, so the fixture is rejected as unreliable data - the same
    # outcome apply_filters() produces for has_reliable_data=False, reached
    # without inventing a number to compare. Thresholds are untouched.
    filter_inputs = (
        home_stats.get("total_goals_avg"),
        away_stats.get("total_goals_avg"),
        home_stats.get("home_clean_sheet_pct"),
        away_stats.get("away_clean_sheet_pct"),
    )

    if any(value is None for value in filter_inputs):
        passes_filters, filter_reasons = False, ["Missing or unreliable data"]
    else:
        passes_filters, filter_reasons = apply_filters(
            home_avg_goals=filter_inputs[0],
            away_avg_goals=filter_inputs[1],
            home_clean_sheet_pct=filter_inputs[2],
            away_clean_sheet_pct=filter_inputs[3],
            is_knockout_first_leg=False,
            is_heavy_favorite_mismatch=False,
            has_reliable_data=True,
        )

    result["passes_filters"] = passes_filters
    if not passes_filters:
        result["rejection_reasons"].extend(filter_reasons)

    # Fetch odds (optional)
    # Note: get_btts_odds might need update for ESPN IDs or just use names (it uses names)
    odds = get_btts_odds(
        home_team=fixture["home_team_name"],
        away_team=fixture["away_team_name"],
        league_id=fixture["league_id"], # passing code (e.g. eng.1) might break int expectation in odds_api? check.
    )
    result["odds"] = odds

    # Make decision
    decision_result = make_decision(
        gg_probability=result["gg_probability"],
        odds=odds,
        passes_filters=passes_filters,
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
