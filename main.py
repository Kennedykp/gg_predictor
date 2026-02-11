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

    # Validate required stats exist
    required_home = [
        home_stats.get("home_goals_scored"),
        home_stats.get("home_goals_conceded"),
    ]
    required_away = [
        away_stats.get("away_goals_scored"),
        away_stats.get("away_goals_conceded"),
    ]

    if None in required_home or None in required_away:
        result["rejection_reasons"].append("Missing goal statistics")
        return result

    # Calculate GG probability using Poisson model
    poisson_result = calculate_gg_probability(
        league_avg_goals=league_avg_goals,
        home_goals_scored_home=home_stats["home_goals_scored"],
        home_goals_conceded_home=home_stats["home_goals_conceded"],
        away_goals_scored_away=away_stats["away_goals_scored"],
        away_goals_conceded_away=away_stats["away_goals_conceded"],
    )

    if poisson_result is None:
        result["rejection_reasons"].append("Failed to calculate probability")
        return result

    result["lambda_home"] = poisson_result["lambda_home"]
    result["lambda_away"] = poisson_result["lambda_away"]
    result["gg_probability"] = poisson_result["gg_probability"]

    # Apply hard filters
    passes_filters, filter_reasons = apply_filters(
        home_avg_goals=home_stats.get("total_goals_avg", 0),
        away_avg_goals=away_stats.get("total_goals_avg", 0),
        home_clean_sheet_pct=home_stats.get("home_clean_sheet_pct", 0),
        away_clean_sheet_pct=away_stats.get("away_clean_sheet_pct", 0),
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
        if league_id not in league_avg_cache:
            league_avg = get_league_avg_goals(league_id)
            if league_avg is None:
                league_avg = 1.35  # Fallback
            league_avg_cache[league_id] = league_avg

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
