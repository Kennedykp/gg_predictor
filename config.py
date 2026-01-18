"""
Configuration module for GG Prediction System.

Contains API keys, league whitelist, and thresholds.
All values are constants that must not be modified.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ESPN API (Free, No Key Required)
ESPN_BASE_URL = "http://site.api.espn.com/apis/site/v2/sports/soccer"

# Legacy APIs (optional/deprecated)
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# League Whitelist - ESPN League Codes
# Top 5 European leagues
ALLOWED_LEAGUES = {
    "eng.1": "English Premier League",
    "ger.1": "Bundesliga",
    "ita.1": "Serie A",
    "esp.1": "La Liga",
    "fra.1": "Ligue 1",
}

# Phase 2 leagues
PHASE_2_LEAGUES = {
    "eng.2": "EFL Championship",
    "ger.2": "Bundesliga 2",
}

# Value & Decision Thresholds (DO NOT MODIFY)
EDGE_THRESHOLD = 0.05  # 5% minimum edge required
MIN_ODDS = 1.60  # Minimum odds to consider

# Hard Filter Thresholds (DO NOT MODIFY)
MIN_AVG_GOALS = 1.0  # Minimum average goals per team
MAX_CLEAN_SHEET_PCT = 0.40  # Maximum clean sheet percentage (40%)
