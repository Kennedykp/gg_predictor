"""
The Odds API Fetcher.

Authoritative implementation lives in shared.odds (with league caching,
quota inspection, and alias normalization).
This module re-exports the public interface for full backward compatibility.
Used ONLY to compute implied probability and value; NEVER for Poisson prediction.
"""

from shared.odds import (
    BASE_URL,
    SPORT_KEYS,
    TEAM_ALIASES,
    _make_request,
    clear_cache,
    fetch_event_odds,
    fetch_league_events,
    fetch_league_odds,
    find_event_id_for_match,
    find_odds_for_match,
    get_btts_odds,
    get_last_quota_info,
    get_upcoming_odds,
    normalize_team_name,
)

__all__ = [
    "BASE_URL",
    "SPORT_KEYS",
    "TEAM_ALIASES",
    "_make_request",
    "clear_cache",
    "fetch_event_odds",
    "fetch_league_events",
    "fetch_league_odds",
    "find_event_id_for_match",
    "find_odds_for_match",
    "get_btts_odds",
    "get_last_quota_info",
    "get_upcoming_odds",
    "normalize_team_name",
]
