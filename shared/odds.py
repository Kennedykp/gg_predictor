"""
Shared Odds Module - Authoritative Odds Fetcher & Classification Layer.

Provides single authoritative implementation for:
- Authenticated Odds API requests with quota header inspection
- Run-level league odds caching (eliminates redundant per-fixture requests)
- Deterministic team name normalization and explicit alias resolution
- Bookmaker and BTTS market traversal (first-available bookmaker rule)
- Edge calculation, value classification, and recommendation derivation

Used ONLY to compute implied probability and value.
Odds NEVER modify Poisson probability or model calculations.
"""

import os
import sys
from typing import Any, Dict, Optional
import requests

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from config import ODDS_API_KEY
except ImportError:
    ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONSTANTS & CONFIGURATION
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

# Explicit canonical alias mapping for common ESPN <-> The Odds API differences
TEAM_ALIASES: Dict[str, str] = {
    # Premier League
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "wolverhampton wanderers": "wolverhampton wanderers",
    "nottm forest": "nottingham forest",
    "notts forest": "nottingham forest",
    "nottingham forest": "nottingham forest",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "tottenham hotspur": "tottenham hotspur",
    "brighton": "brighton and hove albion",
    "brighton & hove albion": "brighton and hove albion",
    "brighton and hove albion": "brighton and hove albion",
    "west ham": "west ham united",
    "west ham united": "west ham united",
    "newcastle": "newcastle united",
    "newcastle united": "newcastle united",
    "man united": "manchester united",
    "man utd": "manchester united",
    "manchester united": "manchester united",
    "man city": "manchester city",
    "manchester city": "manchester city",
    "bournemouth": "afc bournemouth",
    "afc bournemouth": "afc bournemouth",
    "leicester": "leicester city",
    "leicester city": "leicester city",
    "ipswich": "ipswich town",
    "ipswich town": "ipswich town",
    # La Liga
    "athletic bilbao": "athletic club",
    "athletic club": "athletic club",
    "atletico madrid": "atletico madrid",
    "atlético madrid": "atletico madrid",
    "atlético de madrid": "atletico madrid",
    "atletico de madrid": "atletico madrid",
    "betis": "real betis",
    "real betis": "real betis",
    "real betis balompie": "real betis",
    "espanyol": "rcd espanyol",
    "rcd espanyol": "rcd espanyol",
    "mallorca": "rcd mallorca",
    "rcd mallorca": "rcd mallorca",
    "rayo": "rayo vallecano",
    "rayo vallecano": "rayo vallecano",
    "alaves": "deportivo alaves",
    "deportivo alaves": "deportivo alaves",
    "deportivo alavés": "deportivo alaves",
    "celta vigo": "rc celta",
    "celta de vigo": "rc celta",
    "rc celta": "rc celta",
    # Serie A
    "inter": "internazionale",
    "inter milan": "internazionale",
    "internazionale": "internazionale",
    "ac milan": "milan",
    "milan": "milan",
    "as roma": "roma",
    "roma": "roma",
    "verona": "hellas verona",
    "hellas verona": "hellas verona",
    # Bundesliga
    "dortmund": "borussia dortmund",
    "borussia dortmund": "borussia dortmund",
    "monchengladbach": "borussia monchengladbach",
    "mönchengladbach": "borussia monchengladbach",
    "borussia mönchengladbach": "borussia monchengladbach",
    "leverkusen": "bayer leverkusen",
    "bayer leverkusen": "bayer leverkusen",
    "frankfurt": "eintracht frankfurt",
    "eintracht frankfurt": "eintracht frankfurt",
    "union berlin": "union berlin",
    "1. fc union berlin": "union berlin",
    "1 fc union berlin": "union berlin",
    "schalke": "schalke 04",
    "schalke 04": "schalke 04",
    "fc schalke 04": "schalke 04",
    "stuttgart": "vfb stuttgart",
    "vfb stuttgart": "vfb stuttgart",
    "mainz": "mainz 05",
    "mainz 05": "mainz 05",
    "1. fsv mainz 05": "mainz 05",
    "1 fsv mainz 05": "mainz 05",
    # Ligue 1
    "psg": "paris saint germain",
    "paris saint-germain": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "marseille": "marseille",
    "olympique de marseille": "marseille",
    "olympique marseille": "marseille",
    "lyon": "olympique lyonnais",
    "olympique lyonnais": "olympique lyonnais",
    "rennes": "stade rennais",
    "stade rennais": "stade rennais",
    "stade rennais fc": "stade rennais",
    "monaco": "as monaco",
    "as monaco": "as monaco",
    "nice": "ogc nice",
    "ogc nice": "ogc nice",
    "saint-etienne": "as saint etienne",
    "as saint-etienne": "as saint etienne",
    "as saint etienne": "as saint etienne",
}

# Run-level in-memory cache and quota tracking
_odds_cache: Dict[str, Dict[str, Any]] = {}
_last_quota: Dict[str, Optional[int]] = {
    "requests_used": None,
    "requests_remaining": None,
}


def clear_cache() -> None:
    """Clear the in-memory odds cache and quota metrics for a fresh process/run."""
    global _odds_cache, _last_quota
    _odds_cache = {}
    _last_quota = {
        "requests_used": None,
        "requests_remaining": None,
    }


def get_last_quota_info() -> Dict[str, Optional[int]]:
    """Return a copy of the last inspected quota headers."""
    return dict(_last_quota)


def normalize_team_name(name: str) -> str:
    """Normalize team name for deterministic matching: lowercase, strip punctuation, map aliases."""
    if not name:
        return ""
    cleaned = name.strip().lower()
    cleaned = cleaned.replace("'", "").replace(".", "").replace("-", " ")
    cleaned = " ".join(cleaned.split())
    return TEAM_ALIASES.get(cleaned, cleaned)


def _teams_match(cand: str, query: str) -> bool:
    """
    Deterministic team name comparison between normalized names.
    Returns True on exact match or safe, unambiguous substring containment (len >= 4).
    """
    if not cand or not query:
        return False
    if cand == query:
        return True
    if len(cand) >= 4 and len(query) >= 4:
        if cand in query or query in cand:
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API & CACHING FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_request(endpoint: str, params: dict) -> Optional[Any]:
    """
    Make authenticated request to The Odds API.
    Inspects quota headers (x-requests-used, x-requests-remaining).
    Returns None if request fails or no API key is configured.
    """
    if not ODDS_API_KEY:
        return None

    request_params = dict(params)
    request_params["apiKey"] = ODDS_API_KEY

    try:
        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=request_params,
            timeout=30,
        )
        used = response.headers.get("x-requests-used")
        remaining = response.headers.get("x-requests-remaining")
        if used is not None and used.isdigit():
            _last_quota["requests_used"] = int(used)
        if remaining is not None and remaining.isdigit():
            _last_quota["requests_remaining"] = int(remaining)

        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if hasattr(e, "response") and e.response is not None:
            used = e.response.headers.get("x-requests-used")
            remaining = e.response.headers.get("x-requests-remaining")
            if used is not None and used.isdigit():
                _last_quota["requests_used"] = int(used)
            if remaining is not None and remaining.isdigit():
                _last_quota["requests_remaining"] = int(remaining)

        # Sanitize: NEVER print response.url or query params which contain the API key
        print(f"Odds API request failed for '{endpoint}' (status: {status_code})")
        return None


def fetch_league_odds(league_code: str) -> Dict[str, Dict[str, Optional[float]]]:
    """
    Fetch all BTTS odds for a league once per process/run.
    Results are cached in memory for the duration of the run.

    Returns dict mapping fixture keys to odds:
    {
        "home vs away": {
            "btts_yes": 1.85,
            "btts_no": 1.95
        }
    }
    """
    global _odds_cache

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

    if not data or not isinstance(data, list):
        return {}

    odds_map: Dict[str, Dict[str, Optional[float]]] = {}

    for game in data:
        raw_home = game.get("home_team", "")
        raw_away = game.get("away_team", "")
        norm_home = normalize_team_name(raw_home)
        norm_away = normalize_team_name(raw_away)
        key = f"{norm_home} vs {norm_away}"

        game_odds: Dict[str, Optional[float]] = {"btts_yes": None, "btts_no": None}

        # Deterministic bookmaker selection: first bookmaker in the payload
        # offering valid market odds
        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") == "btts":
                    for outcome in market.get("outcomes", []):
                        name = outcome.get("name", "").lower()
                        price = outcome.get("price")
                        if price is not None and isinstance(price, (int, float)) and price > 0:
                            if name == "yes" and game_odds["btts_yes"] is None:
                                game_odds["btts_yes"] = float(price)
                            elif name == "no" and game_odds["btts_no"] is None:
                                game_odds["btts_no"] = float(price)
            if game_odds["btts_yes"] is not None or game_odds["btts_no"] is not None:
                break

        if game_odds["btts_yes"] is not None or game_odds["btts_no"] is not None:
            odds_map[key] = game_odds
            raw_key = f"{raw_home.lower().strip()} vs {raw_away.lower().strip()}"
            if raw_key not in odds_map:
                odds_map[raw_key] = game_odds

    _odds_cache[league_code] = odds_map
    return odds_map


def find_odds_for_match(
    home_team: str,
    away_team: str,
    league_code: str,
    market: str = "btts_yes",
) -> Optional[float]:
    """
    Find odds for a specific match from cached league odds.

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

    norm_home = normalize_team_name(home_team)
    norm_away = normalize_team_name(away_team)

    # 1. Exact normalized key lookup (O(1))
    key = f"{norm_home} vs {norm_away}"
    if key in league_odds:
        return league_odds[key].get(market)

    # 2. Iterate keys with alias / containment matching
    for fixture_key, odds in league_odds.items():
        parts = fixture_key.split(" vs ")
        if len(parts) != 2:
            continue
        cand_home, cand_away = parts
        if _teams_match(cand_home, norm_home) and _teams_match(cand_away, norm_away):
            return odds.get(market)

    return None


def get_btts_odds(
    home_team: str,
    away_team: str,
    league_id: str,
) -> Optional[float]:
    """
    Fetch BTTS Yes odds for a match from cached league odds.

    Args:
        home_team: Home team name
        away_team: Away team name
        league_id: ESPN league code (e.g. 'eng.1')

    Returns:
        BTTS Yes odds as float, or None if not available
    """
    return find_odds_for_match(home_team, away_team, league_id, market="btts_yes")


def get_upcoming_odds(league_id: str) -> Dict[str, float]:
    """
    Fetch all upcoming BTTS Yes odds for a league from cached league odds.

    Returns dict mapping "home_team vs away_team" to odds.
    """
    league_odds = fetch_league_odds(league_id)
    return {
        key: odds["btts_yes"]
        for key, odds in league_odds.items()
        if odds.get("btts_yes") is not None
    }


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
        odds_key = "btts_yes"
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
