#!/usr/bin/env python3
"""
Run-3 (Unanswered Goals) Market - Main Entry Point.

Primary focus: R3-NO (3 unanswered goals does NOT happen)
Secondary focus: R3-YES (rare)

Workflow:
1. Fetch ALL football fixtures worldwide for the given date
2. Compute lambdas (from shared data)
3. Apply Run-3 probability logic
4. Apply filters
5. Assign decision (R3-NO / R3-YES / SKIP)
6. Output: Terminal summary + JSON file

Usage:
    python main_run3.py              # Run for today
    python main_run3.py 2026-01-26   # Run for specific date
"""

import sys
import os
import json
from datetime import date, datetime
from typing import List, Dict, Any, Optional

# Add parent directory to path to import shared modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from run3_probability import calculate_run3_probability
from run3_filters import apply_run3_filters
from run3_decision import make_run3_decision


# ESPN API Base URL
ESPN_BASE_URL = "http://site.api.espn.com/apis/site/v2/sports/soccer"

# ALL worldwide leagues for Run-3 (no filtering)
ALL_LEAGUES = {
    # Top 5 European Leagues
    "eng.1": "English Premier League",
    "ger.1": "Bundesliga",
    "ita.1": "Serie A",
    "esp.1": "La Liga",
    "fra.1": "Ligue 1",
    # Other Major European Leagues
    "ned.1": "Eredivisie",
    "por.1": "Primeira Liga",
    "bel.1": "Belgian Pro League",
    "tur.1": "Super Lig",
    "sco.1": "Scottish Premiership",
    "rus.1": "Russian Premier League",
    "ukr.1": "Ukrainian Premier League",
    "gre.1": "Super League Greece",
    "aut.1": "Austrian Bundesliga",
    "sui.1": "Swiss Super League",
    "den.1": "Danish Superliga",
    "nor.1": "Eliteserien",
    "swe.1": "Allsvenskan",
    "pol.1": "Ekstraklasa",
    "cze.1": "Czech First League",
    # Second Divisions
    "eng.2": "EFL Championship",
    "ger.2": "2. Bundesliga",
    "ita.2": "Serie B",
    "esp.2": "LaLiga2",
    "fra.2": "Ligue 2",
    # South American Leagues
    "bra.1": "Brasileirao",
    "arg.1": "Liga Profesional Argentina",
    "col.1": "Categoria Primera A",
    "chi.1": "Primera Division Chile",
    "mex.1": "Liga MX",
    # Other Regions
    "usa.1": "MLS",
    "jpn.1": "J1 League",
    "aus.1": "A-League",
    "chn.1": "Chinese Super League",
    "kor.1": "K League 1",
    "sau.1": "Saudi Pro League",
}


def _make_request(url: str, params: dict = None) -> Optional[dict]:
    """Make request to ESPN API."""
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def get_all_fixtures(fixture_date: date) -> List[Dict[str, Any]]:
    """
    Fetch ALL football fixtures worldwide for a given date.
    """
    fixtures: List[Dict[str, Any]] = []
    date_str = fixture_date.strftime("%Y%m%d")

    for league_code, league_name in ALL_LEAGUES.items():
        url = f"{ESPN_BASE_URL}/{league_code}/scoreboard"
        data = _make_request(url, {"dates": date_str})

        if not data or "events" not in data:
            continue

        for event in data["events"]:
            status = event.get("status", {}).get("type", {}).get("name", "STATUS_UNKNOWN")
            
            competitions = event.get("competitions", [])
            if not competitions:
                continue
                
            competitors = competitions[0].get("competitors", [])
            home_team = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_team = next((c for c in competitors if c.get("homeAway") == "away"), None)

            if not home_team or not away_team:
                continue

            fixtures.append({
                "fixture_id": event.get("id"),
                "league_id": league_code,
                "league_name": league_name,
                "home_team_id": home_team["team"]["id"],
                "home_team_name": home_team["team"]["displayName"],
                "away_team_id": away_team["team"]["id"],
                "away_team_name": away_team["team"]["displayName"],
                "datetime": event.get("date"),
                "status": status,
            })

    return fixtures


def get_team_stats(team_id: str, league_code: str) -> Optional[Dict[str, Any]]:
    """
    Fetch team statistics from ESPN.
    """
    url = f"{ESPN_BASE_URL}/{league_code}/teams/{team_id}"
    data = _make_request(url)

    if not data or "team" not in data:
        return None

    team = data["team"]
    record_items = team.get("record", {}).get("items", [])
    
    if not record_items:
        return None

    overall = next((r for r in record_items if r.get("type") == "total"), record_items[0])
    stats_list = overall.get("stats", [])
    
    def get_stat(name):
        return next((s.get("value", 0) for s in stats_list if s.get("name") == name), 0)

    matches_played = get_stat("gamesPlayed")
    if matches_played == 0:
        return None

    goals_scored = get_stat("pointsFor")
    goals_conceded = get_stat("pointsAgainst")
    home_matches = get_stat("homeGamesPlayed") or matches_played / 2
    away_matches = get_stat("awayGamesPlayed") or matches_played / 2
    home_goals_scored = get_stat("homePointsFor")
    home_goals_conceded = get_stat("homePointsAgainst")
    away_goals_scored = get_stat("awayPointsFor")
    away_goals_conceded = get_stat("awayPointsAgainst")

    return {
        "team_id": team_id,
        "league_id": league_code,
        "home_goals_scored": home_goals_scored / home_matches if home_matches else 0,
        "away_goals_scored": away_goals_scored / away_matches if away_matches else 0,
        "home_goals_conceded": home_goals_conceded / home_matches if home_matches else 0,
        "away_goals_conceded": away_goals_conceded / away_matches if away_matches else 0,
        "matches_played": matches_played,
    }


def get_league_avg_goals(league_code: str) -> float:
    """
    Calculate league average goals per team per match.
    """
    url = f"{ESPN_BASE_URL}/{league_code}/standings"
    data = _make_request(url)
    
    if not data or "children" not in data:
        return 1.35  # Fallback

    try:
        standings = data["children"][0]["standings"]["entries"]
        total_goals = 0
        total_matches = 0
        
        for entry in standings:
            stats = entry.get("stats", [])
            gf = next((s["value"] for s in stats if s["name"] == "pointsFor"), 0)
            gp = next((s["value"] for s in stats if s["name"] == "gamesPlayed"), 0)
            total_goals += gf
            total_matches += gp
            
        if total_matches == 0:
            return 1.35
            
        return total_goals / total_matches

    except (KeyError, IndexError, TypeError):
        return 1.35


def calculate_lambdas(
    home_stats: Dict[str, Any],
    away_stats: Dict[str, Any],
    league_avg: float,
) -> tuple:
    """
    Calculate lambda_home and lambda_away using the same formula as GG.
    """
    if not home_stats or not away_stats:
        return None, None

    home_goals_scored_home = home_stats.get("home_goals_scored", 0)
    home_goals_conceded_home = home_stats.get("home_goals_conceded", 0)
    away_goals_scored_away = away_stats.get("away_goals_scored", 0)
    away_goals_conceded_away = away_stats.get("away_goals_conceded", 0)

    if league_avg == 0:
        return None, None

    lambda_home = (home_goals_scored_home * away_goals_conceded_away) / league_avg
    lambda_away = (away_goals_scored_away * home_goals_conceded_home) / league_avg

    return lambda_home, lambda_away


def process_fixture(fixture: Dict[str, Any], league_avg: float) -> Dict[str, Any]:
    """
    Process a single fixture for Run-3 analysis.
    """
    result = {
        "fixture_id": fixture["fixture_id"],
        "league": fixture["league_name"],
        "home_team": fixture["home_team_name"],
        "away_team": fixture["away_team_name"],
        "datetime": fixture["datetime"],
        "lambda_home": None,
        "lambda_away": None,
        "p_home": None,
        "p_away": None,
        "P_R3_YES": None,
        "P_R3_NO": None,
        "passes_filters": False,
        "decision": "SKIP",
        "rejection_reasons": [],
    }

    # Fetch team statistics
    home_stats = get_team_stats(fixture["home_team_id"], fixture["league_id"])
    away_stats = get_team_stats(fixture["away_team_id"], fixture["league_id"])

    if home_stats is None or away_stats is None:
        result["rejection_reasons"].append("Missing team stats")
        return result

    # Calculate lambdas
    lambda_home, lambda_away = calculate_lambdas(home_stats, away_stats, league_avg)

    if lambda_home is None or lambda_away is None:
        result["rejection_reasons"].append("Could not calculate lambdas")
        return result

    result["lambda_home"] = round(lambda_home, 3)
    result["lambda_away"] = round(lambda_away, 3)

    # Calculate Run-3 probabilities
    prob_result = calculate_run3_probability(lambda_home, lambda_away)

    if prob_result is None:
        result["rejection_reasons"].append("Probability calculation failed")
        return result

    result["p_home"] = round(prob_result["p_home"], 3)
    result["p_away"] = round(prob_result["p_away"], 3)
    result["P_R3_YES"] = round(prob_result["P_R3_YES"], 3)
    result["P_R3_NO"] = round(prob_result["P_R3_NO"], 3)

    # Apply filters
    passes_filters, filter_reasons = apply_run3_filters(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        p_home=prob_result["p_home"],
        p_away=prob_result["p_away"],
        has_reliable_data=True,
    )

    result["passes_filters"] = passes_filters
    if not passes_filters:
        result["rejection_reasons"].extend(filter_reasons)

    # Make decision
    decision_result = make_run3_decision(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        p_home=prob_result["p_home"],
        p_away=prob_result["p_away"],
        P_R3_YES=prob_result["P_R3_YES"],
        P_R3_NO=prob_result["P_R3_NO"],
        passes_filters=passes_filters,
        odds=None,  # Odds not fetched automatically
    )

    result["decision"] = decision_result["decision"]
    if decision_result.get("reasons"):
        result["rejection_reasons"].extend(decision_result["reasons"])

    return result


def run_run3_workflow(target_date: date) -> List[Dict[str, Any]]:
    """
    Run the Run-3 analysis workflow.
    """
    print(f"\n{'='*60}")
    print(f"RUN-3 (UNANSWERED GOALS) ANALYSIS")
    print(f"Date: {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*60}")
    print()
    print("Fetching ALL worldwide fixtures...")
    
    fixtures = get_all_fixtures(target_date)
    
    if not fixtures:
        print("No fixtures found.")
        return []
    
    print(f"Found {len(fixtures)} fixtures across {len(ALL_LEAGUES)} leagues")
    print()

    results = []
    league_avg_cache = {}

    for i, fixture in enumerate(fixtures):
        league_id = fixture["league_id"]

        # Cache league average
        if league_id not in league_avg_cache:
            league_avg_cache[league_id] = get_league_avg_goals(league_id)

        league_avg = league_avg_cache[league_id]
        result = process_fixture(fixture, league_avg)
        results.append(result)

        # Progress indicator
        if (i + 1) % 10 == 0:
            print(f"  Processed {i + 1}/{len(fixtures)} fixtures...")

    return results


def print_results(results: List[Dict[str, Any]]):
    """
    Print results to terminal.
    """
    print()
    print(f"{'='*60}")
    print("RESULTS SUMMARY")
    print(f"{'='*60}")
    
    r3_no = [r for r in results if r["decision"] == "R3-NO"]
    r3_yes = [r for r in results if r["decision"] == "R3-YES"]
    skipped = [r for r in results if r["decision"] == "SKIP"]

    print(f"\nTotal fixtures analyzed: {len(results)}")
    print(f"  R3-NO flags:  {len(r3_no)}")
    print(f"  R3-YES flags: {len(r3_yes)}")
    print(f"  SKIPPED:      {len(skipped)}")
    print()

    if r3_no:
        print("-" * 60)
        print("R3-NO SELECTIONS (Primary Market):")
        print("-" * 60)
        for r in r3_no:
            print(f"  {r['home_team']} vs {r['away_team']}")
            print(f"    League: {r['league']}")
            print(f"    λH={r['lambda_home']:.2f}, λA={r['lambda_away']:.2f}")
            print(f"    P(R3-NO)={r['P_R3_NO']:.2f}")
            print()

    if r3_yes:
        print("-" * 60)
        print("R3-YES SELECTIONS (Secondary Market - Rare):")
        print("-" * 60)
        for r in r3_yes:
            print(f"  {r['home_team']} vs {r['away_team']}")
            print(f"    League: {r['league']}")
            print(f"    λH={r['lambda_home']:.2f}, λA={r['lambda_away']:.2f}")
            print(f"    P(R3-YES)={r['P_R3_YES']:.2f}")
            print()

    if not r3_no and not r3_yes:
        print("\nNo selections for today. This is a valid outcome.")


def write_json(results: List[Dict[str, Any]], filename: str):
    """
    Write results to JSON file.
    """
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults written to: {filename}")


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        try:
            target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {sys.argv[1]}")
            print("Usage: python main_run3.py [YYYY-MM-DD]")
            sys.exit(1)
    else:
        target_date = date.today()

    results = run_run3_workflow(target_date)
    print_results(results)

    # Write JSON output
    date_str = target_date.strftime("%Y-%m-%d")
    output_dir = os.path.dirname(os.path.abspath(__file__))
    write_json(results, os.path.join(output_dir, f"run3_output_{date_str}.json"))


if __name__ == "__main__":
    main()
