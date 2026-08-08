"""
ESPN API Data Fetcher.

Provides free access to:
- Fixtures (via scoreboard)
- Team statistics (via team endpoints)
- League averages (via standings)

No API key required.

Epic 1B.2 corrected the transport and the two fabricated inputs (GG-003 league
average, GG-004 home/away counts). The governing rule, inherited from Epic
1B.1: a value this module cannot obtain is returned as None. It is never
replaced with a plausible-looking constant.
"""

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import requests

from config import (
    ALLOWED_LEAGUES,
    CALENDAR_YEAR_LEAGUES,
    ESPN_BACKOFF_SECONDS,
    ESPN_BASE_URL,
    ESPN_MAX_RETRIES,
    ESPN_STANDINGS_BASE_URL,
    ESPN_TIMEOUT_SECONDS,
    EUROPEAN_SEASON_ROLLOVER_MONTH,
)


# ---------------------------------------------------------------------------
# Error semantics (Epic 1B.2, TASK 14)
#
# A failed request and a genuinely empty result are different facts. Both used
# to arrive as None/[], so "ESPN is down" and "no matches today" were the same
# observation. These let a caller - and the diagnostic script - tell them apart
# without a large exception hierarchy.
# ---------------------------------------------------------------------------
class ESPNError(str, Enum):
    TIMEOUT = "TIMEOUT"                  # transient: retried
    CONNECTION = "CONNECTION"            # transient: retried
    SERVER_ERROR = "SERVER_ERROR"        # 5xx, transient: retried
    HTTP_ERROR = "HTTP_ERROR"            # 4xx, permanent: not retried
    MALFORMED_JSON = "MALFORMED_JSON"    # 200 with a body that will not parse
    EMPTY_RESPONSE = "EMPTY_RESPONSE"    # 200 with no usable payload (the `{}` case)


# 5xx/timeout/connection are worth another attempt; a 404 never becomes a 200.
_TRANSIENT = frozenset({ESPNError.TIMEOUT, ESPNError.CONNECTION, ESPNError.SERVER_ERROR})


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one ESPN call: either data, or a reason there is none."""

    data: Optional[dict] = None
    error: Optional[ESPNError] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None


def _fetch(url: str, params: Optional[dict] = None) -> FetchResult:
    """
    One ESPN GET, with explicit timeout and bounded retry on transient failure.

    Deliberately bounded (config.ESPN_MAX_RETRIES): retrying forever turns an
    outage into a hang, and hammering a free endpoint is its own failure mode.
    A permanent failure (4xx, malformed body) is returned immediately rather
    than retried - and, critically, is never converted into statistics.
    """
    attempts = ESPN_MAX_RETRIES + 1
    result = FetchResult(error=ESPNError.CONNECTION, detail="no attempt made")

    for attempt in range(attempts):
        try:
            response = requests.get(url, params=params, timeout=ESPN_TIMEOUT_SECONDS)
            status = response.status_code

            if status >= 500:
                result = FetchResult(error=ESPNError.SERVER_ERROR, detail=f"HTTP {status}")
            elif status >= 400:
                # Permanent. Return at once; another attempt cannot help.
                return FetchResult(error=ESPNError.HTTP_ERROR, detail=f"HTTP {status}")
            else:
                try:
                    data = response.json()
                except ValueError as exc:
                    # HTTP 200 carrying an unparseable body. Permanent.
                    return FetchResult(error=ESPNError.MALFORMED_JSON, detail=str(exc))

                if not isinstance(data, dict) or not data:
                    # The GG-003 signature: HTTP 200 with `{}`. Success by status
                    # code, nothing by content. Named so it cannot pass silently.
                    return FetchResult(error=ESPNError.EMPTY_RESPONSE, detail="empty JSON object")
                return FetchResult(data=data)

        except requests.Timeout as exc:
            result = FetchResult(error=ESPNError.TIMEOUT, detail=str(exc))
        except requests.ConnectionError as exc:
            result = FetchResult(error=ESPNError.CONNECTION, detail=str(exc))
        except requests.RequestException as exc:
            return FetchResult(error=ESPNError.CONNECTION, detail=str(exc))

        if result.error not in _TRANSIENT or attempt == attempts - 1:
            break
        time.sleep(ESPN_BACKOFF_SECONDS * (2**attempt))

    return result


def _make_request(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """
    Thin Optional[dict] wrapper over _fetch.

    Retained deliberately as the single seam every test monkeypatches. Keeping
    the signature stable means the transport rewrite above did not force a
    rewrite of the existing offline tests.
    """
    result = _fetch(url, params)
    if not result.ok:
        print(f"ESPN request failed [{result.error}]: {url} - {result.detail}")
        return None
    return result.data


# ---------------------------------------------------------------------------
# Season resolution (Epic 1B.2, TASK 10)
# ---------------------------------------------------------------------------
def resolve_season(league_code: str, today: Optional[date] = None) -> int:
    """
    ESPN identifies a season by the calendar year it STARTS in.

    Verified live: `.../eng.1/scoreboard?dates=20250816` reports
    `season.year = 2025` with `startDate 2025-06-01`, `endDate 2026-06-01` -
    i.e. the 2025-26 EPL season is `2025`. So the 2026-27 season is `2026`.

    Two conventions, because assuming one would be wrong half the time:
      - European (Aug-May): season id is the earlier year, rolling over in July.
      - Calendar-year (Brazil, MLS, Nordics): season id is simply the year.
    """
    today = today or date.today()
    if league_code in CALENDAR_YEAR_LEAGUES:
        return today.year
    if today.month >= EUROPEAN_SEASON_ROLLOVER_MONTH:
        return today.year
    return today.year - 1


# ---------------------------------------------------------------------------
# Fixture status (Epic 1B.2, TASK 11)
# ---------------------------------------------------------------------------
class FixtureState(str, Enum):
    """ESPN's `status.type.state`, plus an explicit unknown."""

    PRE = "pre"        # scheduled
    IN = "in"          # in progress
    POST = "post"      # finished/abandoned
    UNKNOWN = "unknown"


# Names ESPN uses for matches that will not be played as scheduled. `state`
# alone is insufficient: a postponed match still reports state `pre`.
_NOT_PLAYABLE = frozenset({"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_ABANDONED"})


def is_predictable(fixture: Dict[str, Any]) -> bool:
    """
    True only for a match that has not started and is still expected to happen.

    A pre-match model must not be handed a finished match: its statistics
    already contain that result, so the 'prediction' would be of a known
    outcome (GG-013).
    """
    return fixture.get("state") == FixtureState.PRE.value and not fixture.get("is_postponed", False)


def parse_kickoff(raw: Optional[str]) -> Optional[datetime]:
    """
    ESPN timestamps are UTC with a trailing `Z` (e.g. `2025-08-16T11:30Z`).

    Returned timezone-aware. A naive datetime here would silently compare
    against local time - the machine running this is UTC+1, so a 23:30Z kickoff
    would land on the wrong matchday (GG-014).
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


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
            status_type = event.get("status", {}).get("type", {}) or {}
            status = status_type.get("name", "STATUS_UNKNOWN")
            state = status_type.get("state") or FixtureState.UNKNOWN.value

            # Competitors
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            competitors = competitions[0].get("competitors", [])
            home_team = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_team = next((c for c in competitors if c.get("homeAway") == "away"), None)

            if not home_team or not away_team:
                continue

            raw_datetime = event.get("date")
            fixtures.append({
                "fixture_id": event.get("id"),
                "league_id": league_code,
                "league_name": league_name,
                "home_team_id": home_team["team"]["id"],
                "home_team_name": home_team["team"]["displayName"],
                "away_team_id": away_team["team"]["id"],
                "away_team_name": away_team["team"]["displayName"],
                "datetime": raw_datetime,
                "status": status,
                # --- Epic 1B.2 additions. Existing keys above are unchanged so
                # every current consumer keeps working.
                "state": state,
                "is_completed": bool(status_type.get("completed", False)),
                "is_postponed": status in _NOT_PLAYABLE,
                "kickoff_utc": parse_kickoff(raw_datetime),
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

    # GG-004 RESOLVED (Epic 1B.2). This previously read:
    #     if not home_matches: home_matches = matches_played / 2
    # which invented an even split. Schedules are genuinely uneven - live-verified
    # Aalesund 9 home vs 6 away - so halving distorted every rate fed to lambda,
    # and did so invisibly. ESPN does supply homeGamesPlayed/awayGamesPlayed
    # (confirmed live on the team endpoint), so the real counts are used and
    # absence is now absence. `rate()` below returns None when the divisor is
    # missing or zero, so an unplayed split yields no rate rather than a
    # fabricated one.

    def rate(total, matches):
        """
        Per-match rate, or None when it cannot be computed.

        `matches` of 0 means no games played, so the rate is UNDEFINED, not zero.
        Reporting 0.0 would assert 'this team scores zero per home match', which
        is a different and much stronger claim than 'no home matches yet'.
        """
        if total is None or matches is None or matches == 0:
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
        # Epic 1B.2: real counts, exposed so callers can see the split they got.
        "home_matches": home_matches,
        "away_matches": away_matches,
    }


def get_league_avg_goals(league_code: str, season_id: Optional[int] = None) -> Optional[float]:
    """
    League average goals PER TEAM PER MATCH, or None if it cannot be obtained.

    UNITS (Epic 1B.2, TASK 4) - this is the denominator of both lambda values in
    POISSON_V1, so the units must match the quantities divided by it:

        total goals scored by all teams / total team-games

    A standings table counts each fixture twice (once per team), so summing
    `gamesPlayed` gives TEAM-GAMES, not fixtures. The result is therefore goals
    per team per match (EPL 2025-26: 1045/760 = 1.3750), which is exactly half
    the goals-per-fixture figure (2.7500). Dividing by the per-fixture number
    would halve every lambda. `poisson.py:26` and GG.md:110 ("per team") agree.

    GG-003 RESOLVED. Two defects, both fixed here:
      1. Wrong path - `/apis/site/v2/.../standings` answers HTTP 200 with `{}`.
         The working path is `/apis/v2/.../standings`.
      2. On any failure this returned the hardcoded 1.35. It now returns None.
         Note the real EPL figure is 1.3750, so the old constant was not merely
         unsourced, it was wrong - and being close enough to look right is what
         made it survive.
    """
    season = season_id if season_id is not None else resolve_season(league_code)
    url = f"{ESPN_STANDINGS_BASE_URL}/{league_code}/standings"
    data = _make_request(url, {"season": season})

    if not data or "children" not in data:
        return None

    try:
        children = data["children"]
        if not children:
            return None
        standings = children[0]["standings"]["entries"]
    except (KeyError, IndexError, TypeError):
        return None

    total_goals = 0.0
    total_matches = 0.0
    total_conceded = 0.0

    for entry in standings:
        stats = entry.get("stats", [])
        gf = next((s.get("value") for s in stats if s.get("name") == "pointsFor"), None)
        ga = next((s.get("value") for s in stats if s.get("name") == "pointsAgainst"), None)
        gp = next((s.get("value") for s in stats if s.get("name") == "gamesPlayed"), None)
        # A partial table would understate the average. Refuse rather than
        # publish a number computed from some of the league.
        if gf is None or gp is None:
            return None
        total_goals += gf
        total_matches += gp
        if ga is not None:
            total_conceded += ga

    # Preseason: the table exists but nothing has been played. Genuinely
    # unavailable, not zero - and dividing here would raise.
    if total_matches == 0:
        return None

    # Integrity check: league-wide goals scored must equal goals conceded, since
    # every goal is both. A mismatch means a truncated or inconsistent table, so
    # the figure is not trustworthy as a model denominator. Verified live: EPL
    # 2025-26 gives 1045 == 1045.
    if total_conceded and abs(total_goals - total_conceded) > 0.5:
        return None

    return total_goals / total_matches
