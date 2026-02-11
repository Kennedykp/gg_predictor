"""
Sportmonks API Data Fetcher.

Primary data source for:
- Fixtures (live / upcoming)
- Team statistics
- Home/away goals scored
- Home/away goals conceded
- League average goals

API Documentation: https://docs.sportmonks.com/football
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import date
from config import SPORTMONKS_API_KEY, SPORTMONKS_BASE_URL, ALLOWED_LEAGUES


def _make_request(endpoint: str, params: dict = None) -> Optional[dict]:
    """
    Make authenticated request to Sportmonks API.
    
    Sportmonks uses api_token as a query parameter.
    """
    if not SPORTMONKS_API_KEY:
        print("Warning: SPORTMONKS_API_KEY not configured")
        return None

    if params is None:
        params = {}
    
    params["api_token"] = SPORTMONKS_API_KEY

    try:
        url = f"{SPORTMONKS_BASE_URL}/{endpoint}"
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"API request failed: {e}")
        return None


def get_season_from_date(fixture_date: date) -> int:
    """
    Sportmonks seasons use the START year of the season.
    
    Examples:
    - Jan 2026 -> season 2025
    - Sep 2025 -> season 2025
    """
    return fixture_date.year if fixture_date.month >= 8 else fixture_date.year - 1


def get_current_season_id(league_id: int) -> Optional[int]:
    """
    Get the current season ID for a league.
    
    Sportmonks uses season IDs, not years.
    """
    data = _make_request(f"leagues/{league_id}", {"include": "currentSeason"})
    
    if not data or "data" not in data:
        return None
    
    try:
        current_season = data["data"].get("current_season_id")
        return current_season
    except (KeyError, TypeError):
        return None


def get_fixtures(fixture_date: date) -> List[Dict[str, Any]]:
    """
    Fetch fixtures for a given date, filtered by allowed leagues.
    
    Uses the /fixtures/date/{date} endpoint.
    """
    fixtures: List[Dict[str, Any]] = []
    date_str = fixture_date.strftime("%Y-%m-%d")

    # Fetch all fixtures for the date
    data = _make_request(
        f"fixtures/date/{date_str}",
        {"include": "participants"}
    )

    if not data or "data" not in data:
        return fixtures

    for fixture in data["data"]:
        league_id = fixture.get("league_id")
        
        # Filter by allowed leagues
        if league_id not in ALLOWED_LEAGUES:
            continue

        # Extract team info from participants
        participants = fixture.get("participants", [])
        home_team = None
        away_team = None
        
        for team in participants:
            if team.get("meta", {}).get("location") == "home":
                home_team = team
            elif team.get("meta", {}).get("location") == "away":
                away_team = team

        if not home_team or not away_team:
            continue

        fixtures.append({
            "fixture_id": fixture["id"],
            "league_id": league_id,
            "league_name": ALLOWED_LEAGUES[league_id],
            "season_id": fixture.get("season_id"),
            "home_team_id": home_team["id"],
            "home_team_name": home_team["name"],
            "away_team_id": away_team["id"],
            "away_team_name": away_team["name"],
            "datetime": fixture.get("starting_at"),
            "status": fixture.get("state", {}).get("short_name", "NS"),
        })

    return fixtures


def get_team_stats(team_id: int, season_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch team statistics for a specific season.
    
    Uses the /teams/{id} endpoint with statistics include.
    """
    data = _make_request(
        f"teams/{team_id}",
        {
            "include": "statistics",
            "filters": f"teamStatisticSeasons:{season_id}"
        }
    )

    if not data or "data" not in data:
        return None

    team_data = data["data"]
    statistics = team_data.get("statistics", [])

    if not statistics:
        return None

    # Initialize counters
    home_goals_scored = 0
    home_goals_conceded = 0
    away_goals_scored = 0
    away_goals_conceded = 0
    home_matches = 0
    away_matches = 0
    home_clean_sheets = 0
    away_clean_sheets = 0

    # Parse statistics
    for stat in statistics:
        detail = stat.get("details", {})
        stat_type = stat.get("type", {}).get("code", "")
        location = detail.get("location", "all")
        value = detail.get("value", 0) or 0

        if stat_type == "goals":
            if location == "home":
                home_goals_scored = value
            elif location == "away":
                away_goals_scored = value
        elif stat_type == "goals_conceded":
            if location == "home":
                home_goals_conceded = value
            elif location == "away":
                away_goals_conceded = value
        elif stat_type == "matches":
            if location == "home":
                home_matches = value
            elif location == "away":
                away_matches = value
        elif stat_type == "clean_sheets":
            if location == "home":
                home_clean_sheets = value
            elif location == "away":
                away_clean_sheets = value

    # Calculate averages
    home_scored_avg = home_goals_scored / home_matches if home_matches > 0 else 0
    away_scored_avg = away_goals_scored / away_matches if away_matches > 0 else 0
    home_conceded_avg = home_goals_conceded / home_matches if home_matches > 0 else 0
    away_conceded_avg = away_goals_conceded / away_matches if away_matches > 0 else 0
    
    home_cs_pct = home_clean_sheets / home_matches if home_matches > 0 else 0
    away_cs_pct = away_clean_sheets / away_matches if away_matches > 0 else 0

    total_matches = home_matches + away_matches
    total_goals = home_goals_scored + away_goals_scored
    total_goals_avg = total_goals / total_matches if total_matches > 0 else 0

    return {
        "team_id": team_id,
        "season_id": season_id,
        "home_goals_scored": home_scored_avg,
        "away_goals_scored": away_scored_avg,
        "home_goals_conceded": home_conceded_avg,
        "away_goals_conceded": away_conceded_avg,
        "home_clean_sheet_pct": home_cs_pct,
        "away_clean_sheet_pct": away_cs_pct,
        "total_goals_avg": total_goals_avg,
        "matches_played": total_matches,
    }


def get_league_avg_goals(league_id: int, season_id: int) -> Optional[float]:
    """
    Calculate league average goals per team per match.
    
    Uses standings to iterate through teams and calculate average.
    """
    data = _make_request(
        f"standings/seasons/{season_id}",
        {"filters": f"leagueId:{league_id}"}
    )

    if not data or "data" not in data or not data["data"]:
        return None

    try:
        total_goals = 0
        total_matches = 0

        for standing in data["data"]:
            goals_for = standing.get("goals_scored", 0) or 0
            played = standing.get("played", 0) or 0
            total_goals += goals_for
            total_matches += played

        if total_matches == 0:
            return None

        # Average goals per team per match
        return total_goals / total_matches

    except (KeyError, TypeError) as e:
        print(f"Error calculating league average: {e}")
        return None
