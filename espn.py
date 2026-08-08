"""
ESPN API Data Fetcher.

Provides free access to:
- Fixtures (via scoreboard)
- Team statistics (via team endpoints)
- League averages (via standings/team stats)

No API key required.
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import date
from config import ESPN_BASE_URL, ALLOWED_LEAGUES


def _make_request(url: str, params: dict = None) -> Optional[dict]:
    """Make request to ESPN API."""
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"ESPN request failed: {e}")
        return None


def get_fixtures(fixture_date: date) -> List[Dict[str, Any]]:
    """
    Fetch fixtures for a given date across allowed leagues.
    
    ESPN uses dates=YYYYMMDD param.
    """
    fixtures: List[Dict[str, Any]] = []
    date_str = fixture_date.strftime("%Y%m%d")

    for league_code, league_name in ALLOWED_LEAGUES.items():
        url = f"{ESPN_BASE_URL}/{league_code}/scoreboard"
        data = _make_request(url, {"dates": date_str})

        if not data or "events" not in data:
            continue

        for event in data["events"]:
            # Status check
            status = event.get("status", {}).get("type", {}).get("name", "STATUS_UNKNOWN")
            
            # Competitors
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            competitors = competitions[0].get("competitors", [])
            home_team = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_team = next((c for c in competitors if c.get("homeAway") == "away"), None)

            if not home_team or not away_team:
                continue

            fixtures.append({
                "fixture_id": event.get("id"),
                "league_id": league_code,
                "league_name": league_name,
                "home_team_id": home_team["team"]["id"],
                "home_team_name": home_team["team"]["displayName"],
                "away_team_id": away_team["team"]["id"],
                "away_team_name": away_team["team"]["displayName"],
                "datetime": event.get("date"),
                "status": status,
            })

    return fixtures


def get_team_stats(team_id: str, league_code: str) -> Optional[Dict[str, Any]]:
    """
    Fetch team statistics.
    
    ESPN provides 'record' in team endpoint.
    Stats mapping:
    - pointsFor = Goals Scored (GF)
    - pointsAgainst = Goals Conceded (GA)
    - gamesPlayed = Matches Played
    """
    url = f"{ESPN_BASE_URL}/{league_code}/teams/{team_id}"
    data = _make_request(url)

    if not data or "team" not in data:
        return None

    team = data["team"]
    record_items = team.get("record", {}).get("items", [])
    
    if not record_items:
        return None

    overall = next((r for r in record_items if r.get("type") == "total"), record_items[0])
    stats_list = overall.get("stats", [])

    def get_stat(name):
        """
        Return the statistic ESPN supplied, or None if it did not supply one.

        GG-001 (Epic 1B.1). This previously ended `..., 0)` in two places, so an
        absent statistic and a genuine zero both arrived as 0 and the model could
        not tell them apart. Three cases are now distinct:

            entry absent from stats_list  -> None  (never received)
            entry present, no "value" key -> None  (no number received)
            entry present, value 0        -> 0     (genuinely zero: real data)
        """
        for stat in stats_list:
            if stat.get("name") == name:
                return stat.get("value")
        return None

    matches_played = get_stat("gamesPlayed")
    # Divisor for every rate below. Unavailable or zero means no usable record.
    # Pre-fix this read `== 0` and worked only because absent became 0.
    if matches_played is None or matches_played == 0:
        return None

    goals_scored = get_stat("pointsFor")
    goals_conceded = get_stat("pointsAgainst")

    home_matches = get_stat("homeGamesPlayed")
    away_matches = get_stat("awayGamesPlayed")
    home_goals_scored = get_stat("homePointsFor")
    home_goals_conceded = get_stat("homePointsAgainst")
    away_goals_scored = get_stat("awayPointsFor")
    away_goals_conceded = get_stat("awayPointsAgainst")

    # LEGACY (GG-004) — an assumed even home/away split when ESPN omits the
    # counts. This IS fabricated data, but correcting it changes every rate and
    # is explicitly out of scope for Epic 1B.1. Behaviour preserved exactly:
    # `not x` covers both the pre-fix 0 and the new None.
    if not home_matches: home_matches = matches_played / 2
    if not away_matches: away_matches = matches_played / 2

    def rate(total, matches):
        """Per-match rate, or None when the underlying total was never supplied."""
        if total is None or not matches:
            return None
        return total / matches

    # Unavailable if either component is missing: a total built from a value we
    # never received would look like a real average.
    if goals_scored is None or goals_conceded is None:
        total_goals_avg = None
    else:
        total_goals_avg = (goals_scored + goals_conceded) / matches_played

    return {
        "team_id": team_id,
        "league_id": league_code,
        "home_goals_scored": rate(home_goals_scored, home_matches),
        "away_goals_scored": rate(away_goals_scored, away_matches),
        "home_goals_conceded": rate(home_goals_conceded, home_matches),
        "away_goals_conceded": rate(away_goals_conceded, away_matches),
        # LEGACY (GG-002) — ESPN supplies no clean-sheet data at all, so these
        # are hardcoded. Left as 0 deliberately: the contract in domain/stats.py
        # can represent them as unavailable, but switching them here would make
        # every fixture fail the filter and change production output, which is
        # GG-002's job, not this sub-epic's.
        "home_clean_sheet_pct": 0,
        "away_clean_sheet_pct": 0,
        "total_goals_avg": total_goals_avg,
        "matches_played": matches_played,
    }


def get_league_avg_goals(league_code: str, season_id: int = None) -> Optional[float]:
    """
    Calculate league average goals per team per match.
    """
    url = f"{ESPN_BASE_URL}/{league_code}/standings"
    data = _make_request(url)
    
    if not data or "children" not in data:
        return 1.35  # Fallback

    try:
        standings = data["children"][0]["standings"]["entries"]
        total_goals = 0
        total_matches = 0
        
        for entry in standings:
            stats = entry.get("stats", [])
            gf = next((s["value"] for s in stats if s["name"] == "pointsFor"), 0)
            gp = next((s["value"] for s in stats if s["name"] == "gamesPlayed"), 0)
            total_goals += gf
            total_matches += gp
            
        if total_matches == 0:
            return 1.35
            
        return total_goals / total_matches

    except (KeyError, IndexError, TypeError):
        return 1.35
