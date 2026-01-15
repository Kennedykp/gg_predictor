"""
API-Football Data Fetcher.

Primary data source for:
- Fixtures
- Team statistics
- Home/away goals scored
- Home/away goals conceded
- League average goals
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import date
from config import API_FOOTBALL_KEY, API_FOOTBALL_HOST, ALLOWED_LEAGUES, CURRENT_SEASON


BASE_URL = f"https://{API_FOOTBALL_HOST}"


def _make_request(endpoint: str, params: dict) -> Optional[dict]:
    """
    Make authenticated request to API-Football.

    Returns None if request fails or no API key configured.
    """
    if not API_FOOTBALL_KEY:
        print("Warning: API_FOOTBALL_KEY not configured")
        return None

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }

    try:
        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"API request failed: {e}")
        return None


def get_fixtures(fixture_date: date, league_ids: List[int] = None) -> List[Dict[str, Any]]:
    """
    Fetch fixtures for a given date, filtered by allowed leagues.

    Args:
        fixture_date: Date to fetch fixtures for
        league_ids: Optional list of league IDs to filter (defaults to ALLOWED_LEAGUES)

    Returns:
        List of fixture dictionaries
    """
    if league_ids is None:
        league_ids = list(ALLOWED_LEAGUES.keys())

    fixtures = []
    date_str = fixture_date.strftime("%Y-%m-%d")

    for league_id in league_ids:
        if league_id not in ALLOWED_LEAGUES:
            continue

        data = _make_request(
            "fixtures",
            {
                "date": date_str,
                "league": league_id,
                "season": CURRENT_SEASON,
            },
        )

        if data and "response" in data:
            for fixture in data["response"]:
                fixtures.append({
                    "fixture_id": fixture["fixture"]["id"],
                    "league_id": league_id,
                    "league_name": ALLOWED_LEAGUES[league_id],
                    "home_team_id": fixture["teams"]["home"]["id"],
                    "home_team_name": fixture["teams"]["home"]["name"],
                    "away_team_id": fixture["teams"]["away"]["id"],
                    "away_team_name": fixture["teams"]["away"]["name"],
                    "datetime": fixture["fixture"]["date"],
                    "status": fixture["fixture"]["status"]["short"],
                })

    return fixtures


def get_team_stats(team_id: int, league_id: int, season: int = None) -> Optional[Dict[str, Any]]:
    """
    Fetch team statistics for a specific league and season.

    Returns home/away goals scored/conceded and clean sheet data.
    """
    if season is None:
        season = CURRENT_SEASON

    data = _make_request(
        "teams/statistics",
        {
            "team": team_id,
            "league": league_id,
            "season": season,
        },
    )

    if not data or "response" not in data:
        return None

    response = data["response"]

    try:
        # Extract goals data
        goals = response.get("goals", {})
        goals_for = goals.get("for", {})
        goals_against = goals.get("against", {})

        # Home/away averages
        home_scored = goals_for.get("average", {}).get("home")
        away_scored = goals_for.get("average", {}).get("away")
        home_conceded = goals_against.get("average", {}).get("home")
        away_conceded = goals_against.get("average", {}).get("away")

        # Convert to float
        home_scored = float(home_scored) if home_scored else None
        away_scored = float(away_scored) if away_scored else None
        home_conceded = float(home_conceded) if home_conceded else None
        away_conceded = float(away_conceded) if away_conceded else None

        # Clean sheets
        clean_sheets = response.get("clean_sheet", {})
        cs_home = clean_sheets.get("home", 0) or 0
        cs_away = clean_sheets.get("away", 0) or 0
        cs_total = clean_sheets.get("total", 0) or 0

        # Fixtures played
        fixtures = response.get("fixtures", {})
        played = fixtures.get("played", {})
        home_played = played.get("home", 0) or 0
        away_played = played.get("away", 0) or 0
        total_played = played.get("total", 0) or 0

        # Calculate clean sheet percentage
        home_cs_pct = cs_home / home_played if home_played > 0 else 0
        away_cs_pct = cs_away / away_played if away_played > 0 else 0

        return {
            "team_id": team_id,
            "league_id": league_id,
            "season": season,
            "home_goals_scored": home_scored,
            "away_goals_scored": away_scored,
            "home_goals_conceded": home_conceded,
            "away_goals_conceded": away_conceded,
            "home_clean_sheet_pct": home_cs_pct,
            "away_clean_sheet_pct": away_cs_pct,
            "total_goals_avg": (
                (float(goals_for.get("average", {}).get("total", 0) or 0))
            ),
            "matches_played": total_played,
        }
    except (KeyError, TypeError, ValueError) as e:
        print(f"Error parsing team stats: {e}")
        return None


def get_league_avg_goals(league_id: int, season: int = None) -> Optional[float]:
    """
    Calculate league average goals per team per match.

    This is computed by averaging team statistics across the league.
    """
    if season is None:
        season = CURRENT_SEASON

    # Get league standings to get list of teams
    data = _make_request(
        "standings",
        {
            "league": league_id,
            "season": season,
        },
    )

    if not data or "response" not in data or not data["response"]:
        return None

    try:
        standings = data["response"][0]["league"]["standings"][0]
        total_goals = 0
        total_matches = 0

        for team in standings:
            goals_for = team["all"]["goals"]["for"]
            played = team["all"]["played"]
            total_goals += goals_for
            total_matches += played

        if total_matches == 0:
            return None

        # Average goals per team per match
        # Total goals / total matches gives total goals per match
        # Divide by 2 to get per-team average
        avg_goals = total_goals / total_matches

        return avg_goals
    except (KeyError, TypeError, IndexError) as e:
        print(f"Error calculating league average: {e}")
        return None
