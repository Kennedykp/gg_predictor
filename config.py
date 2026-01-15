"""
Configuration module for GG Prediction System.

Contains API keys, league whitelist, and thresholds.
All values are constants that must not be modified.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# API Keys
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = "v3.football.api-sports.io"
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# League Whitelist - Phase 1 Only
# API-Football league IDs for allowed leagues
ALLOWED_LEAGUES = {
    39: "English Premier League",
    78: "Bundesliga",
    135: "Serie A",
    140: "La Liga",
    61: "Ligue 1",
}

# Phase 2 leagues (DO NOT USE until Phase 1 proves stable)
PHASE_2_LEAGUES = {
    40: "EFL Championship",
    79: "Bundesliga 2",
}

# Current season (2024/25 season - free plan only allows 2022-2024)
# CURRENT_SEASON = 2024

# Value & Decision Thresholds (DO NOT MODIFY)
EDGE_THRESHOLD = 0.05  # 5% minimum edge required
MIN_ODDS = 1.60  # Minimum odds to consider

# Hard Filter Thresholds (DO NOT MODIFY)
MIN_AVG_GOALS = 1.0  # Minimum average goals per team
MAX_CLEAN_SHEET_PCT = 0.40  # Maximum clean sheet percentage (40%)
