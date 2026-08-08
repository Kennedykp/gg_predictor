#!/usr/bin/env python3
"""
Unified Analysis Output - All Matches with Odds Classification.

This script outputs ALL matches with classification labels.
SKIP is replaced by proper classification.

Usage:
    python analyze_all.py              # Run for today
    python analyze_all.py 2026-02-08   # Run for specific date
"""

import sys
import os
import json
from datetime import date, datetime
from typing import List, Dict, Any, Optional

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from espn import get_fixtures, get_team_stats, get_league_avg_goals
from poisson import calculate_gg_probability
from filters import apply_filters
from shared.odds import analyze_market, clear_cache
from config import ALLOWED_LEAGUES
from domain import LeagueStats, TeamStats, validate_poisson_inputs


def analyze_gg_match(
    fixture: Dict[str, Any],
    home_stats: Dict[str, Any],
    away_stats: Dict[str, Any],
    league_avg: float,
) -> List[Dict[str, Any]]:
    """
    Analyze a single fixture for GG markets (YES and NO).
    
    Returns list of market analysis dicts.
    """
    results = []
    
    # Calculate GG probability
    if not home_stats or not away_stats:
        # Missing data - output both markets with null probabilities
        for market in ["GG_YES", "GG_NO"]:
            results.append({
                "fixture_id": fixture["fixture_id"],
                "league": fixture["league_name"],
                "home_team": fixture["home_team_name"],
                "away_team": fixture["away_team_name"],
                "datetime": fixture.get("datetime"),
                "market": market,
                "model_probability": None,
                "odds": None,
                "implied_probability": None,
                "edge": None,
                "classification": "NO_ODDS",
                "system_recommendation": "RECOMMEND_NO_PLAY",
                "filter_status": "MISSING_DATA",
                "filter_reasons": ["Missing team statistics"],
            })
        return results
    
    # Validate the five required POISSON_V1 inputs BEFORE the model call.
    # GG-001 (Epic 1B.1). This path was the most dangerous consequence of the
    # old behaviour: a missing statistic became 0, P(GG_YES) collapsed to 0.0,
    # and `gg_no_prob = 1 - 0.0` published a 100%-confident GG_NO - which could
    # classify as STRONG_VALUE and RECOMMEND_PLAY on data that never arrived.
    validation = validate_poisson_inputs(
        league=LeagueStats.unattributed(fixture["league_id"], league_avg),
        home_team=TeamStats.from_provider_dict(home_stats),
        away_team=TeamStats.from_provider_dict(away_stats),
    )

    if not validation.is_complete:
        for market in ["GG_YES", "GG_NO"]:
            results.append({
                "fixture_id": fixture["fixture_id"],
                "league": fixture["league_name"],
                "home_team": fixture["home_team_name"],
                "away_team": fixture["away_team_name"],
                "datetime": fixture.get("datetime"),
                "market": market,
                "model_probability": None,
                "odds": None,
                "implied_probability": None,
                "edge": None,
                "classification": "NO_ODDS",
                "system_recommendation": "RECOMMEND_NO_PLAY",
                "filter_status": "MISSING_DATA",
                "filter_reasons": [validation.reason()],
            })
        return results

    model_inputs = validation.inputs
    assert model_inputs is not None  # guaranteed by is_complete

    prob_result = calculate_gg_probability(
        league_avg_goals=model_inputs.league_avg_goals,
        home_goals_scored_home=model_inputs.home_goals_scored_home,
        home_goals_conceded_home=model_inputs.home_goals_conceded_home,
        away_goals_scored_away=model_inputs.away_goals_scored_away,
        away_goals_conceded_away=model_inputs.away_goals_conceded_away,
    )
    
    if not prob_result:
        for market in ["GG_YES", "GG_NO"]:
            results.append({
                "fixture_id": fixture["fixture_id"],
                "league": fixture["league_name"],
                "home_team": fixture["home_team_name"],
                "away_team": fixture["away_team_name"],
                "datetime": fixture.get("datetime"),
                "market": market,
                "model_probability": None,
                "odds": None,
                "implied_probability": None,
                "edge": None,
                "classification": "NO_ODDS",
                "system_recommendation": "RECOMMEND_NO_PLAY",
                "filter_status": "CALCULATION_FAILED",
                "filter_reasons": ["Probability calculation failed"],
            })
        return results
    
    gg_yes_prob = prob_result["gg_probability"]
    gg_no_prob = 1 - gg_yes_prob
    
    # Apply filters.
    # NOTE (GG-006, out of scope): this passes single-side scoring rates into
    # parameters named `*_avg_goals`, whereas main.py passes `total_goals_avg`.
    # The two entry points therefore filter differently. Left exactly as-is;
    # only the None-safety guard below is new.
    #
    # Filter inputs can now be None (GG-001), and comparing None to a threshold
    # raises TypeError. Unavailable input means the filter cannot be evaluated,
    # so the fixture is rejected as unreliable rather than compared against an
    # invented number. Thresholds are untouched.
    filter_inputs = (
        home_stats.get("home_goals_scored"),
        away_stats.get("away_goals_scored"),
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
        )
    
    filter_status = "PASSED" if passes_filters else "FILTERED"
    
    # Analyze GG YES
    gg_yes_analysis = analyze_market(
        market="GG_YES",
        model_probability=gg_yes_prob,
        home_team=fixture["home_team_name"],
        away_team=fixture["away_team_name"],
        league_code=fixture["league_id"],
    )
    gg_yes_analysis.update({
        "fixture_id": fixture["fixture_id"],
        "league": fixture["league_name"],
        "home_team": fixture["home_team_name"],
        "away_team": fixture["away_team_name"],
        "datetime": fixture.get("datetime"),
        "lambda_home": prob_result.get("lambda_home"),
        "lambda_away": prob_result.get("lambda_away"),
        "filter_status": filter_status,
        "filter_reasons": filter_reasons if not passes_filters else [],
    })
    results.append(gg_yes_analysis)
    
    # Analyze GG NO
    gg_no_analysis = analyze_market(
        market="GG_NO",
        model_probability=gg_no_prob,
        home_team=fixture["home_team_name"],
        away_team=fixture["away_team_name"],
        league_code=fixture["league_id"],
    )
    gg_no_analysis.update({
        "fixture_id": fixture["fixture_id"],
        "league": fixture["league_name"],
        "home_team": fixture["home_team_name"],
        "away_team": fixture["away_team_name"],
        "datetime": fixture.get("datetime"),
        "lambda_home": prob_result.get("lambda_home"),
        "lambda_away": prob_result.get("lambda_away"),
        "filter_status": filter_status,
        "filter_reasons": filter_reasons if not passes_filters else [],
    })
    results.append(gg_no_analysis)
    
    return results


def run_unified_analysis(target_date: date) -> List[Dict[str, Any]]:
    """
    Run unified analysis for all matches.
    
    Outputs ALL matches - nothing is hidden.
    """
    print(f"\n{'='*60}")
    print(f"UNIFIED ODDS ANALYSIS")
    print(f"Date: {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*60}")
    print()
    
    # Clear odds cache for fresh run
    clear_cache()
    
    print("Fetching fixtures...")
    fixtures = get_fixtures(target_date)
    
    print(f"Found {len(fixtures)} fixtures")
    print()
    
    if not fixtures:
        return []
    
    all_results = []
    league_avg_cache = {}
    
    for i, fixture in enumerate(fixtures):
        league_id = fixture["league_id"]
        
        # Cache league average
        if league_id not in league_avg_cache:
            league_avg_cache[league_id] = get_league_avg_goals(league_id) or 1.35
        
        league_avg = league_avg_cache[league_id]
        
        # Fetch team stats
        home_stats = get_team_stats(fixture["home_team_id"], league_id)
        away_stats = get_team_stats(fixture["away_team_id"], league_id)
        
        # Analyze GG markets
        match_results = analyze_gg_match(fixture, home_stats, away_stats, league_avg)
        all_results.extend(match_results)
        
        # Progress
        if (i + 1) % 5 == 0:
            print(f"  Processed {i + 1}/{len(fixtures)} fixtures...")
    
    return all_results


def print_summary(results: List[Dict[str, Any]]):
    """Print summary to terminal."""
    print()
    print(f"{'='*60}")
    print("ANALYSIS SUMMARY")
    print(f"{'='*60}")
    
    # Count by classification
    classifications = {}
    recommendations = {"RECOMMEND_PLAY": [], "RECOMMEND_NO_PLAY": 0}
    
    for r in results:
        cls = r.get("classification", "NO_ODDS")
        classifications[cls] = classifications.get(cls, 0) + 1
        
        if r.get("system_recommendation") == "RECOMMEND_PLAY":
            recommendations["RECOMMEND_PLAY"].append(r)
        else:
            recommendations["RECOMMEND_NO_PLAY"] += 1
    
    print(f"\nTotal market analyses: {len(results)}")
    print(f"\nBy Classification:")
    for cls in ["STRONG_VALUE", "VALUE", "FAIR_NO_EDGE", "OVERPRICED", "NO_ODDS"]:
        count = classifications.get(cls, 0)
        print(f"  {cls}: {count}")
    
    print(f"\nRecommendations:")
    print(f"  RECOMMEND_PLAY: {len(recommendations['RECOMMEND_PLAY'])}")
    print(f"  RECOMMEND_NO_PLAY: {recommendations['RECOMMEND_NO_PLAY']}")
    
    # Show RECOMMEND_PLAY matches
    if recommendations["RECOMMEND_PLAY"]:
        print()
        print("-" * 60)
        print("RECOMMENDED PLAYS:")
        print("-" * 60)
        for r in recommendations["RECOMMEND_PLAY"]:
            print(f"  {r['home_team']} vs {r['away_team']}")
            print(f"    Market: {r['market']}")
            print(f"    Model Prob: {r['model_probability']:.2%}")
            print(f"    Odds: {r['odds']}")
            print(f"    Edge: {r['edge']:.2%}" if r['edge'] else "    Edge: N/A")
            print(f"    Classification: {r['classification']}")
            print()


def write_output(results: List[Dict[str, Any]], target_date: date):
    """Write results to JSON file."""
    filename = f"analysis_output_{target_date.strftime('%Y-%m-%d')}.json"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    
    with open(filepath, "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nResults written to: {filename}")


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        try:
            target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {sys.argv[1]}")
            print("Usage: python analyze_all.py [YYYY-MM-DD]")
            sys.exit(1)
    else:
        target_date = date.today()
    
    results = run_unified_analysis(target_date)
    print_summary(results)
    write_output(results, target_date)


if __name__ == "__main__":
    main()
