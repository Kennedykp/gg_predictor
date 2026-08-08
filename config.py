"""
Configuration module for GG Prediction System.

Contains API keys, league whitelist, and thresholds.
All values are constants that must not be modified.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ESPN API (Free, No Key Required)
#
# HTTPS since Epic 1B.2 (GG-020). ESPN traffic is unauthenticated, but it was
# plaintext and therefore MITM-modifiable, and a tampered response feeds the
# model directly. Verified working over TLS for every endpoint used here.
ESPN_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Standings live on a DIFFERENT host path (note: no `/site`).
#
# GG-003 root cause. The old code called `{ESPN_BASE_URL}/{league}/standings`,
# i.e. `/apis/site/v2/.../standings`, which answers HTTP 200 with a 2-byte body
# `{}`. Because the status is 200 nothing raised, so every call silently fell
# through to the hardcoded 1.35. The working path is `/apis/v2/...`, verified
# live returning ~68KB of real standings.
ESPN_STANDINGS_BASE_URL = "https://site.api.espn.com/apis/v2/sports/soccer"

# --- ESPN transport (Epic 1B.2, GG-012) -----------------------------------
# Bounded. A retry storm against a free endpoint is its own failure mode, and
# an unbounded one turns a permanent outage into a hang.
ESPN_TIMEOUT_SECONDS = 15
ESPN_MAX_RETRIES = 2          # total attempts = 1 + 2
ESPN_BACKOFF_SECONDS = 0.5    # doubled per retry

# Leagues played inside a single calendar year (Brazil, Argentina, MLS, the
# Nordics, Japan). ESPN identifies a season by the year it STARTS in, so for
# these the season id is simply the current year, whereas European leagues
# spanning Aug-May are identified by the earlier year (2025-26 -> 2025).
# Listed explicitly rather than inferred: guessing per-competition calendar
# conventions is exactly the kind of silent wrongness this Epic is removing.
CALENDAR_YEAR_LEAGUES = {
    "bra.1", "arg.1", "usa.1", "mex.1", "jpn.1", "kor.1",
    "nor.1", "swe.1", "den.1", "fin.1", "isl.1", "irl.1", "chn.1",
}

# Month at/after which European seasons roll over to the new season id.
# July: ESPN's 2025-26 EPL season block starts 2025-06-01, so by July the new
# season is already addressable.
EUROPEAN_SEASON_ROLLOVER_MONTH = 7


# Legacy APIs (optional/deprecated)
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY", "")
SPORTMONKS_BASE_URL = os.getenv("SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3/football")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = os.getenv("API_FOOTBALL_HOST", "v3.football.api-sports.io")
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
