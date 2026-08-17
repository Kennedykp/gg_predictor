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

from espn import (
    get_fixtures,
    get_league_avg_goals,
    get_league_baseline,
    get_team_history,
    get_team_stats,
    get_team_venue_averages,
)
from poisson import calculate_gg_probability
from shared.odds import analyze_market, clear_cache
from shared.match_history import (
    build_fixture_filter_stats,
    build_fixture_poisson_inputs,
)
from config import ALLOWED_LEAGUES
# `validate_poisson_inputs` / `LeagueStats` / `TeamStats` validated the
# CURRENT-SEASON aggregate dicts that used to feed the model. The point-in-time
# contract returned by `build_fixture_poisson_inputs` carries its own
# completeness check, so the guarantee is enforced on the data actually modelled.
from domain import evaluate_filters


def _gate_recommendation_on_filters(
    analysis: Dict[str, Any],
    filter_status: str,
) -> Dict[str, Any]:
    """
    Withhold the recommendation unless the hard filters explicitly PASSED.

    EPIC 2F-P0-1. `analyze_market()` derives `system_recommendation` from edge
    and odds ALONE. This file then attached `filter_status`/`filter_reasons`
    beside that verdict without ever revising it, so a row could state the very
    reasons it must be rejected and still publish RECOMMEND_PLAY. A fixture
    whose sides both average 0.30 goals - an unambiguous MIN_AVG_GOALS failure -
    was published as STRONG_VALUE / RECOMMEND_PLAY, and `print_summary` then
    collected it into the headline "RECOMMENDED PLAYS" list.

    Only an explicit "PASSED" permits a play. "FILTERED" (a statistic breached a
    threshold) and "FILTER_DATA_UNAVAILABLE" (no statistic existed to compare
    against one) are both refusals. This is exactly what main.py has always done
    by passing `filter_result.allows_recommendation` INTO `make_decision`; the
    same rule is now applied at this file's equivalent step, so the two entry
    points can no longer disagree about what may be published.

    Nothing about the model is touched. `model_probability`, `lambda_home`,
    `lambda_away`, `odds`, `implied_probability` and `edge` are left exactly as
    computed. `classification` is also deliberately left alone: it describes the
    PRICE - "was this market generous?" - not the bet, and overwriting it would
    destroy the evidence that a filtered fixture happened to be mispriced. Only
    the publishable recommendation changes.
    """
    if filter_status != "PASSED":
        analysis["system_recommendation"] = "RECOMMEND_NO_PLAY"
    return analysis






def analyze_gg_match(
    fixture: Dict[str, Any],
    home_stats: Optional[Dict[str, Any]],
    away_stats: Optional[Dict[str, Any]],
    league_avg: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Analyze a single fixture for GG markets (YES and NO).

    Returns list of market analysis dicts.

    `home_stats`/`away_stats` are Optional because the provider returns None on
    failure and the branch below relies on that: annotating them as required
    dicts claimed a guarantee the ESPN layer does not make, so the type said
    "cannot be None" while the first statement of the body checked for exactly
    that.

    `league_avg` is **vestigial and no longer used for modelling.** Since Epic
    1B.5 the baseline comes from `model_inputs.league_avg_goals`, derived from
    matches before this fixture's kickoff; the caller's value is a current-season
    figure. It is kept only so existing callers keep working, and is Optional
    because `get_league_avg_goals()` returns None when unavailable (GG-003).
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
    
    # POINT-IN-TIME MODEL INPUTS (Epic 1B.5, LEAK-001).
    #
    # Identical call to main.py's, through the same shared boundary, so the two
    # entry points cannot derive different inputs for the same fixture - the
    # structural fix GG-006 established for filters, now applied to the model.
    #
    # All five inputs come from matches that kicked off STRICTLY BEFORE this
    # fixture. The previous source, ESPN's current-season aggregates, described
    # the season as it stands today, so scoring an already-played fixture used
    # its own result as evidence for itself.
    #
    # GG-001's guarantee is preserved and strengthened: incomplete inputs are
    # refused rather than modelled. That matters most here, because this file
    # publishes `gg_no_prob = 1 - gg_yes_prob`, so a fabricated 0.0 becomes a
    # 100%-confident GG_NO recommendation.
    model_inputs = build_fixture_poisson_inputs(
        fixture,
        get_team_venue_averages,
        get_league_baseline,
    )

    if not model_inputs.is_complete:
        # No fallback to current-season aggregates. That fallback would fire
        # exactly when history is thin - early season, promoted sides - which is
        # where the leak was most valuable and least visible.
        reason = "Point-in-time model inputs unavailable: " + ", ".join(
            model_inputs.missing
        )
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
                "filter_reasons": [reason],
                "model_input_samples": {
                    "home": model_inputs.home_sample,
                    "away": model_inputs.away_sample,
                    "league": model_inputs.league_sample,
                },
            })
        return results

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
    
    # Apply filters through the single shared boundary (GG-006 RESOLVED).
    #
    # This file's interpretation - each team's goals SCORED at the venue it is
    # playing at - was the correct one and is now the only one. main.py used to
    # pass `total_goals_avg` (scored + conceded) into the same parameter, so the
    # two entry points returned different verdicts for the same fixture. Both
    # now call `build_filter_stats`, so that is structurally impossible.
    #
    # `evaluate_filters` refuses to compare an absent statistic against a
    # threshold; it reports UNEVALUATED instead of inventing a value.
    #
    # Epic 1B.4 supplies the clean-sheet feed. `build_fixture_filter_stats`
    # derives each team's rate from completed ESPN league matches kicking off
    # strictly before this fixture - home team at home, away team away - and
    # main.py calls the identical function with the identical arguments.
    filter_result = evaluate_filters(
        build_fixture_filter_stats(fixture, home_stats, away_stats, get_team_history)
    )


    passes_filters = filter_result.passed
    filter_reasons = filter_result.reasons

    # Three states, not two. "FILTER_DATA_UNAVAILABLE" is distinct from
    # "FILTERED": one means a statistic breached a threshold, the other means no
    # statistic was available to compare. Both block a play, but conflating them
    # in the output is what hid GG-002 for so long.
    if filter_result.was_evaluated:
        filter_status = "PASSED" if passes_filters else "FILTERED"
    else:
        filter_status = "FILTER_DATA_UNAVAILABLE"

    
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
    results.append(_gate_recommendation_on_filters(gg_yes_analysis, filter_status))

    
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
    results.append(_gate_recommendation_on_filters(gg_no_analysis, filter_status))

    
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
        # GG-003 (Epic 1B.2): no `or 1.35`. Note the old expression was doubly
        # wrong - `or` also replaced a genuine 0.0 average.
        if league_id not in league_avg_cache:
            league_avg_cache[league_id] = get_league_avg_goals(league_id)
        
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
