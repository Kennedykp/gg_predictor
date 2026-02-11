"""
The Odds API Fetcher (Optional).

Used ONLY to compute implied probability and value.
Odds are NOT used for prediction.
"""

import requests
from typing import Optional, Dict, Any
from config import ODDS_API_KEY


BASE_URL = "https://api.the-odds-api.com/v4"


def _make_request(endpoint: str, params: dict) -> Optional[dict]:
    """
    Make authenticated request to The Odds API.

    Returns None if request fails or no API key configured.
    """
    if not ODDS_API_KEY:
        # Odds are optional, silently return None
        return None

    params["apiKey"] = ODDS_API_KEY

    try:
        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Odds API request failed: {e}")
        return None


# Sport key mapping for football leagues (Mapped from ESPN codes)
SPORT_KEYS = {
    "eng.1": "soccer_epl",  # English Premier League
    "ger.1": "soccer_germany_bundesliga",  # Bundesliga
    "ita.1": "soccer_italy_serie_a",  # Serie A
    "esp.1": "soccer_spain_la_liga",  # La Liga
    "fra.1": "soccer_france_ligue_one",  # Ligue 1
}


def get_btts_odds(
    home_team: str,
    away_team: str,
    league_id: str,
) -> Optional[float]:
    """
    Fetch BTTS (Both Teams To Score) Yes odds for a match.

    Args:
        home_team: Home team name
        away_team: Away team name
        league_id: ESPN league code (e.g. 'eng.1')

    Returns:
        BTTS Yes odds as float, or None if not available
    """
    sport_key = SPORT_KEYS.get(league_id)
    if not sport_key:
        return None

    data = _make_request(
        f"sports/{sport_key}/odds",
        {
            "regions": "eu",
            "markets": "btts",
            "oddsFormat": "decimal",
        },
    )

    if not data:
        return None

    # Search for matching game
    for game in data:
        game_home = game.get("home_team", "").lower()
        game_away = game.get("away_team", "").lower()

        # Fuzzy match team names
        if (
            home_team.lower() in game_home or game_home in home_team.lower()
        ) and (
            away_team.lower() in game_away or game_away in away_team.lower()
        ):
            # Find BTTS market
            for bookmaker in game.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") == "btts":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name", "").lower() == "yes":
                                return outcome.get("price")

    return None


def get_upcoming_odds(league_id: str) -> Dict[str, Any]:
    """
    Fetch all upcoming BTTS odds for a league.

    Returns dict mapping "home_team vs away_team" to odds.
    """
    sport_key = SPORT_KEYS.get(league_id)
    if not sport_key:
        return {}

    data = _make_request(
        f"sports/{sport_key}/odds",
        {
            "regions": "eu",
            "markets": "btts",
            "oddsFormat": "decimal",
        },
    )

    if not data:
        return {}

    odds_map = {}

    for game in data:
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        key = f"{home_team} vs {away_team}"

        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") == "btts":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name", "").lower() == "yes":
                            # Take first bookmaker's odds
                            if key not in odds_map:
                                odds_map[key] = outcome.get("price")

    return odds_map
