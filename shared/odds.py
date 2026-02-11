"""
Shared Odds Module - Classification Layer.

Fetches odds and provides classification + recommendations.
Does NOT modify any prediction logic.

Classification Labels:
- STRONG_VALUE → edge ≥ 0.10
- VALUE → 0.05 ≤ edge < 0.10
- FAIR_NO_EDGE → −0.05 < edge < 0.05
- OVERPRICED → edge ≤ −0.05
- NO_ODDS → odds unavailable

System Recommendation:
- RECOMMEND_PLAY if edge ≥ 0.05 AND odds ≥ 1.60
- Otherwise RECOMMEND_NO_PLAY
"""

import os
import sys
import requests
from typing import Optional, Dict, Any, List
from datetime import date

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from config import ODDS_API_KEY
except ImportError:
    ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONSTANTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BASE_URL = "https://api.the-odds-api.com/v4"

# Classification thresholds
STRONG_VALUE_EDGE = 0.10
VALUE_EDGE = 0.05
FAIR_EDGE_LOW = -0.05
MIN_ODDS_FOR_PLAY = 1.60

# Sport key mapping for football leagues (ESPN codes -> The Odds API)
SPORT_KEYS = {
    "eng.1": "soccer_epl",
    "eng.2": "soccer_efl_champ",
    "ger.1": "soccer_germany_bundesliga",
    "ger.2": "soccer_germany_bundesliga2",
    "ita.1": "soccer_italy_serie_a",
    "ita.2": "soccer_italy_serie_b",
    "esp.1": "soccer_spain_la_liga",
    "esp.2": "soccer_spain_segunda_division",
    "fra.1": "soccer_france_ligue_one",
    "fra.2": "soccer_france_ligue_two",
    "ned.1": "soccer_netherlands_eredivisie",
    "por.1": "soccer_portugal_primeira_liga",
    "bel.1": "soccer_belgium_first_div",
    "tur.1": "soccer_turkey_super_league",
    "sco.1": "soccer_spl",
    "gre.1": "soccer_greece_super_league",
    "aut.1": "soccer_austria_bundesliga",
    "sui.1": "soccer_switzerland_superleague",
    "den.1": "soccer_denmark_superliga",
    "nor.1": "soccer_norway_eliteserien",
    "swe.1": "soccer_sweden_allsvenskan",
    "pol.1": "soccer_poland_ekstraklasa",
    "bra.1": "soccer_brazil_campeonato",
    "arg.1": "soccer_argentina_primera_division",
    "mex.1": "soccer_mexico_ligamx",
    "usa.1": "soccer_usa_mls",
    "jpn.1": "soccer_japan_j_league",
    "aus.1": "soccer_australia_aleague",
}

# Odds cache (per run)
_odds_cache: Dict[str, Dict[str, Any]] = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_request(endpoint: str, params: dict) -> Optional[Any]:
    """Make authenticated request to The Odds API."""
    if not ODDS_API_KEY:
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


def fetch_league_odds(league_code: str) -> Dict[str, Dict[str, float]]:
    """
    Fetch all BTTS odds for a league.
    
    Returns dict mapping fixture keys to odds:
    {
        "home vs away": {
            "btts_yes": 1.85,
            "btts_no": 1.95
        }
    }
    """
    global _odds_cache
    
    # Return cached if available
    if league_code in _odds_cache:
        return _odds_cache[league_code]
    
    sport_key = SPORT_KEYS.get(league_code)
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
        key = f"{home_team.lower()} vs {away_team.lower()}"

        game_odds = {"btts_yes": None, "btts_no": None}

        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") == "btts":
                    for outcome in market.get("outcomes", []):
                        name = outcome.get("name", "").lower()
                        if name == "yes" and game_odds["btts_yes"] is None:
                            game_odds["btts_yes"] = outcome.get("price")
                        elif name == "no" and game_odds["btts_no"] is None:
                            game_odds["btts_no"] = outcome.get("price")
            # Take first bookmaker only
            if game_odds["btts_yes"] or game_odds["btts_no"]:
                break

        if game_odds["btts_yes"] or game_odds["btts_no"]:
            odds_map[key] = game_odds

    # Cache for this run
    _odds_cache[league_code] = odds_map
    return odds_map


def find_odds_for_match(
    home_team: str,
    away_team: str,
    league_code: str,
    market: str = "btts_yes"
) -> Optional[float]:
    """
    Find odds for a specific match.
    
    Args:
        home_team: Home team name
        away_team: Away team name
        league_code: ESPN league code
        market: "btts_yes" or "btts_no"
    
    Returns:
        Odds as float, or None if not found
    """
    league_odds = fetch_league_odds(league_code)
    if not league_odds:
        return None

    home_lower = home_team.lower()
    away_lower = away_team.lower()

    # Try exact match first
    key = f"{home_lower} vs {away_lower}"
    if key in league_odds:
        return league_odds[key].get(market)

    # Fuzzy match
    for fixture_key, odds in league_odds.items():
        parts = fixture_key.split(" vs ")
        if len(parts) != 2:
            continue
        
        api_home, api_away = parts
        
        # Check if team names overlap
        if (home_lower in api_home or api_home in home_lower) and \
           (away_lower in api_away or api_away in away_lower):
            return odds.get(market)

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLASSIFICATION FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def calculate_edge(model_probability: float, odds: Optional[float]) -> Optional[float]:
    """
    Calculate edge.
    
    edge = model_probability - implied_probability
    implied_probability = 1 / odds
    """
    if odds is None or odds <= 0:
        return None
    
    implied_probability = 1 / odds
    return model_probability - implied_probability


def classify_edge(edge: Optional[float]) -> str:
    """
    Classify edge into categories.
    
    - STRONG_VALUE → edge ≥ 0.10
    - VALUE → 0.05 ≤ edge < 0.10
    - FAIR_NO_EDGE → −0.05 < edge < 0.05
    - OVERPRICED → edge ≤ −0.05
    - NO_ODDS → odds unavailable
    """
    if edge is None:
        return "NO_ODDS"
    
    if edge >= STRONG_VALUE_EDGE:
        return "STRONG_VALUE"
    elif edge >= VALUE_EDGE:
        return "VALUE"
    elif edge > FAIR_EDGE_LOW:
        return "FAIR_NO_EDGE"
    else:
        return "OVERPRICED"


def get_recommendation(edge: Optional[float], odds: Optional[float]) -> str:
    """
    Get system recommendation.
    
    RECOMMEND_PLAY if edge ≥ 0.05 AND odds ≥ 1.60
    Otherwise RECOMMEND_NO_PLAY
    """
    if edge is None or odds is None:
        return "RECOMMEND_NO_PLAY"
    
    if edge >= VALUE_EDGE and odds >= MIN_ODDS_FOR_PLAY:
        return "RECOMMEND_PLAY"
    
    return "RECOMMEND_NO_PLAY"


def analyze_market(
    market: str,
    model_probability: float,
    home_team: str,
    away_team: str,
    league_code: str,
) -> Dict[str, Any]:
    """
    Analyze a single market for a match.
    
    Args:
        market: "GG_YES", "GG_NO", "R3_YES", "R3_NO"
        model_probability: Probability from the model
        home_team: Home team name
        away_team: Away team name
        league_code: ESPN league code
    
    Returns:
        Analysis dict with all required fields
    """
    # Map market to odds key
    if market in ("GG_YES", "R3_YES"):
        odds_key = "btts_yes"  # Note: R3 odds rarely available, using BTTS as proxy
    else:
        odds_key = "btts_no"
    
    # Fetch odds
    odds = find_odds_for_match(home_team, away_team, league_code, odds_key)
    
    # Calculate edge
    edge = calculate_edge(model_probability, odds)
    
    # Calculate implied probability
    implied_probability = (1 / odds) if odds and odds > 0 else None
    
    # Classify
    classification = classify_edge(edge)
    
    # Recommendation
    recommendation = get_recommendation(edge, odds)
    
    return {
        "market": market,
        "model_probability": round(model_probability, 4),
        "odds": round(odds, 2) if odds else None,
        "implied_probability": round(implied_probability, 4) if implied_probability else None,
        "edge": round(edge, 4) if edge else None,
        "classification": classification,
        "system_recommendation": recommendation,
    }


def clear_cache():
    """Clear the odds cache."""
    global _odds_cache
    _odds_cache = {}
