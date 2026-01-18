"""
Sofascore Data Fetcher.

Web scraping approach to get free football data from Sofascore.

Provides:
- Fixtures (today's matches)
- Team statistics
- League standings
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import date
from config import ALLOWED_LEAGUES


# Sofascore API endpoints (undocumented public API)
BASE_URL = "https://api.sofascore.com/api/v1"

# League ID mapping for Sofascore
SOFASCORE_LEAGUES = {
    # Original GG.md leagues
    "English Premier League": 17,
    "Bundesliga": 35,
    "Serie A": 23,
    "La Liga": 8,
    "Ligue 1": 34,
    # Additional leagues
    "Danish Superliga": 50,
    "Scottish Premiership": 36,
}

# Headers to mimic browser request
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
}


def _make_request(endpoint: str, params: dict = None) -> Optional[dict]:
    """
    Make request to Sofascore API.
    """
    try:
        url = f"{BASE_URL}/{endpoint}"
        response = requests.get(url, headers=HEADERS, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Sofascore request failed: {e}")
        return None


def get_fixtures(fixture_date: date) -> List[Dict[str, Any]]:
    """
    Fetch fixtures for a given date, filtered by allowed leagues.
    
    Uses Sofascore's scheduled-events endpoint.
    """
    fixtures: List[Dict[str, Any]] = []
    date_str = fixture_date.strftime("%Y-%m-%d")

    # Fetch events for the date
    data = _make_request(f"sport/football/scheduled-events/{date_str}")

    if not data or "events" not in data:
        return fixtures

    # Build reverse mapping from Sofascore IDs to our league names
    sofascore_to_name = {v: k for k, v in SOFASCORE_LEAGUES.items()}
    allowed_sofascore_ids = set(SOFASCORE_LEAGUES.values())

    for event in data["events"]:
        tournament = event.get("tournament", {})
        tournament_id = tournament.get("uniqueTournament", {}).get("id")

        # Filter by allowed leagues
        if tournament_id not in allowed_sofascore_ids:
            continue

        home_team = event.get("homeTeam", {})
        away_team = event.get("awayTeam", {})

        league_name = sofascore_to_name.get(tournament_id, tournament.get("name", "Unknown"))

        # Get season ID from the event
        season = event.get("season", {})
        season_id = season.get("id")

        fixtures.append({
            "fixture_id": event.get("id"),
            "league_id": tournament_id,
            "league_name": league_name,
            "season_id": season_id,
            "home_team_id": home_team.get("id"),
            "home_team_name": home_team.get("name", "Unknown"),
            "away_team_id": away_team.get("id"),
            "away_team_name": away_team.get("name", "Unknown"),
            "datetime": event.get("startTimestamp"),
            "status": event.get("status", {}).get("type", "notstarted"),
        })

    return fixtures


def get_team_stats(team_id: int, season_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch team statistics for a specific season.
    
    Uses team statistics endpoint.
    """
    # Get team statistics
    data = _make_request(f"team/{team_id}/unique-tournament-season/{season_id}/statistics/overall")

    if not data or "statistics" not in data:
        # Try alternate endpoint
        data = _make_request(f"team/{team_id}/statistics/season/{season_id}")
        if not data or "statistics" not in data:
            return None

    stats = data["statistics"]

    # Parse statistics
    matches_played = stats.get("matches", 0)
    goals_scored = stats.get("goalsScored", 0)
    goals_conceded = stats.get("goalsConceded", 0)

    # Home/away breakdown (if available)
    home_stats = data.get("homeStatistics", stats)
    away_stats = data.get("awayStatistics", stats)

    home_matches = home_stats.get("matches", matches_played // 2) or 1
    away_matches = away_stats.get("matches", matches_played // 2) or 1

    home_goals_scored = home_stats.get("goalsScored", goals_scored // 2)
    away_goals_scored = away_stats.get("goalsScored", goals_scored // 2)
    home_goals_conceded = home_stats.get("goalsConceded", goals_conceded // 2)
    away_goals_conceded = away_stats.get("goalsConceded", goals_conceded // 2)

    # Clean sheets
    clean_sheets = stats.get("cleanSheets", 0)
    home_clean_sheets = home_stats.get("cleanSheets", clean_sheets // 2)
    away_clean_sheets = away_stats.get("cleanSheets", clean_sheets // 2)

    # Calculate averages
    home_scored_avg = home_goals_scored / home_matches if home_matches > 0 else 0
    away_scored_avg = away_goals_scored / away_matches if away_matches > 0 else 0
    home_conceded_avg = home_goals_conceded / home_matches if home_matches > 0 else 0
    away_conceded_avg = away_goals_conceded / away_matches if away_matches > 0 else 0

    home_cs_pct = home_clean_sheets / home_matches if home_matches > 0 else 0
    away_cs_pct = away_clean_sheets / away_matches if away_matches > 0 else 0

    total_goals_avg = goals_scored / matches_played if matches_played > 0 else 0

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
        "matches_played": matches_played,
    }


def get_league_avg_goals(league_id: int, season_id: int) -> Optional[float]:
    """
    Calculate league average goals per team per match.
    
    Uses standings endpoint.
    """
    data = _make_request(f"unique-tournament/{league_id}/season/{season_id}/standings/total")

    if not data or "standings" not in data:
        return None

    try:
        standings = data["standings"]
        if not standings:
            return None

        rows = standings[0].get("rows", [])
        total_goals = 0
        total_matches = 0

        for row in rows:
            goals_for = row.get("scoresFor", 0)
            played = row.get("matches", 0)
            total_goals += goals_for
            total_matches += played

        if total_matches == 0:
            return None

        return total_goals / total_matches

    except (KeyError, TypeError, IndexError) as e:
        print(f"Error calculating league average: {e}")
        return None
