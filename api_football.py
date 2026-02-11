"""
API-Football Data Fetcher.

Primary data source for:
- Fixtures (live / upcoming)
- Team statistics
- Home/away goals scored
- Home/away goals conceded
- League average goals
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import date
from config import API_FOOTBALL_KEY, API_FOOTBALL_HOST, ALLOWED_LEAGUES

BASE_URL = f"https://{API_FOOTBALL_HOST}"


# ----------------------------
# Season handling (CRITICAL)
# ----------------------------
def get_season_from_date(fixture_date: date) -> int:
    """
    API-Football seasons use the START year of the season.

    Examples:
    - Jan 2025  -> season 2024
    - May 2025  -> season 2024
    - Sep 2025  -> season 2025
    """
    return fixture_date.year if fixture_date.month >= 8 else fixture_date.year - 1


# ----------------------------
# Core request helper
# ----------------------------
def _make_request(endpoint: str, params: dict) -> Optional[dict]:
    """
    Make authenticated request to API-Football.
    Returns None if request fails.
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


# ----------------------------
# Fixtures
# ----------------------------
def get_fixtures(fixture_date: date) -> List[Dict[str, Any]]:
    """
    Fetch fixtures for a given date, filtered by allowed leagues.

    NOTE:
    - On the free plan, this works reliably for TODAY / UPCOMING dates
    - Past dates may return empty results
    """
    fixtures: List[Dict[str, Any]] = []
    date_str = fixture_date.strftime("%Y-%m-%d")
    season = get_season_from_date(fixture_date)

    for league_id, league_name in ALLOWED_LEAGUES.items():
        data = _make_request(
            "fixtures",
            {
                "date": date_str,
                "league": league_id,
                "season": season,
            },
        )

        if not data or "response" not in data:
            continue

        for fixture in data["response"]:
            fixtures.append({
                "fixture_id": fixture["fixture"]["id"],
                "league_id": league_id,
                "league_name": league_name,
                "season": season,
                "home_team_id": fixture["teams"]["home"]["id"],
                "home_team_name": fixture["teams"]["home"]["name"],
                "away_team_id": fixture["teams"]["away"]["id"],
                "away_team_name": fixture["teams"]["away"]["name"],
                "datetime": fixture["fixture"]["date"],
                "status": fixture["fixture"]["status"]["short"],
            })

    return fixtures


# ----------------------------
# Team statistics
# ----------------------------
def get_team_stats(team_id: int, league_id: int, season: int) -> Optional[Dict[str, Any]]:
    """
    Fetch team statistics for a specific league and season.
    Season MUST be explicitly provided.
    """
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
        goals = response.get("goals", {})
        goals_for = goals.get("for", {})
        goals_against = goals.get("against", {})

        home_scored = float(goals_for.get("average", {}).get("home") or 0)
        away_scored = float(goals_for.get("average", {}).get("away") or 0)
        home_conceded = float(goals_against.get("average", {}).get("home") or 0)
        away_conceded = float(goals_against.get("average", {}).get("away") or 0)

        clean_sheets = response.get("clean_sheet", {})
        fixtures_played = response.get("fixtures", {}).get("played", {})

        home_played = fixtures_played.get("home", 0) or 0
        away_played = fixtures_played.get("away", 0) or 0

        home_cs_pct = (clean_sheets.get("home", 0) / home_played) if home_played else 0
        away_cs_pct = (clean_sheets.get("away", 0) / away_played) if away_played else 0

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
            "matches_played": fixtures_played.get("total", 0),
        }

    except (KeyError, TypeError, ValueError) as e:
        print(f"Error parsing team stats: {e}")
        return None


# ----------------------------
# League average goals
# ----------------------------
def get_league_avg_goals(league_id: int, season: int) -> Optional[float]:
    """
    Calculate league average goals per team per match.
    """
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
            total_goals += team["all"]["goals"]["for"]
            total_matches += team["all"]["played"]

        return (total_goals / total_matches) if total_matches else None

    except (KeyError, TypeError, IndexError) as e:
        print(f"Error calculating league average: {e}")
        return None