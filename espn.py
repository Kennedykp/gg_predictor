"""
ESPN API Data Fetcher.

Provides free access to:
- Fixtures (via scoreboard)
- Team statistics (via team endpoints)
- League averages (via standings/team stats)

No API key required.
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import date
from config import ESPN_BASE_URL, ALLOWED_LEAGUES


def _make_request(url: str, params: dict = None) -> Optional[dict]:
    """Make request to ESPN API."""
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"ESPN request failed: {e}")
        return None


def get_fixtures(fixture_date: date) -> List[Dict[str, Any]]:
    """
    Fetch fixtures for a given date across allowed leagues.
    
    ESPN uses dates=YYYYMMDD param.
    """
    fixtures: List[Dict[str, Any]] = []
    date_str = fixture_date.strftime("%Y%m%d")

    for league_code, league_name in ALLOWED_LEAGUES.items():
        url = f"{ESPN_BASE_URL}/{league_code}/scoreboard"
        data = _make_request(url, {"dates": date_str})

        if not data or "events" not in data:
            continue

        for event in data["events"]:
            # Status check
            status = event.get("status", {}).get("type", {}).get("name", "STATUS_UNKNOWN")
            
            # Competitors
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
    Fetch team statistics.
    
    ESPN provides 'record' in team endpoint.
    Stats mapping:
    - pointsFor = Goals Scored (GF)
    - pointsAgainst = Goals Conceded (GA)
    - gamesPlayed = Matches Played
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
    
    home_matches = get_stat("homeGamesPlayed")
    away_matches = get_stat("awayGamesPlayed")
    home_goals_scored = get_stat("homePointsFor")
    home_goals_conceded = get_stat("homePointsAgainst")
    away_goals_scored = get_stat("awayPointsFor")
    away_goals_conceded = get_stat("awayPointsAgainst")
    
    if home_matches == 0: home_matches = matches_played / 2
    if away_matches == 0: away_matches = matches_played / 2

    return {
        "team_id": team_id,
        "league_id": league_code,
        "home_goals_scored": home_goals_scored / home_matches if home_matches else 0,
        "away_goals_scored": away_goals_scored / away_matches if away_matches else 0,
        "home_goals_conceded": home_goals_conceded / home_matches if home_matches else 0,
        "away_goals_conceded": away_goals_conceded / away_matches if away_matches else 0,
        "home_clean_sheet_pct": 0,
        "away_clean_sheet_pct": 0,
        "total_goals_avg": (goals_scored + goals_conceded) / matches_played,
        "matches_played": matches_played,
    }


def get_league_avg_goals(league_code: str, season_id: int = None) -> Optional[float]:
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
