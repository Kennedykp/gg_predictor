"""
ESPN API Data Fetcher.

Provides free access to:
- Fixtures (via scoreboard)
- Team statistics (via team endpoints)
- League averages (via standings)
- Completed match history (via team schedule) - Epic 1B.4

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
from domain.match_records import DerivedHistory, MatchRecord, Venue, derive_history
from domain.poisson_inputs import (
    LeagueBaseline,
    VenueGoalAverages,
    derive_league_baseline,
    derive_venue_averages,
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
        # GG-002 RESOLVED (Epic 1B.3). These were hardcoded to 0, which asserted
        # "this team has never kept a clean sheet" for every team in every
        # league. The clean-sheet filter fires on `> 0.40`, so a constant 0 could
        # not fire, ever - the filter existed but was unreachable.
        #
        # None is the correct value because the statistic is genuinely
        # UNAVAILABLE from this endpoint, and provably so rather than by
        # omission. The standings record supplies season AGGREGATES
        # (pointsAgainst, gamesPlayed); a clean sheet is a per-match event.
        # Conceding 5 across 5 matches is consistent with 0 clean sheets
        # (1,1,1,1,1) and with 4 (5,0,0,0,0). The aggregate does not determine
        # the answer, so no exact derivation exists from what we fetch here.
        #
        # Consequence, accepted deliberately: fixtures now report
        # FILTER_DATA_UNAVAILABLE and are not recommended. That is the intended
        # outcome. A filter that cannot be evaluated must not be treated as
        # passed, and the previous behaviour was not "filter passing" but
        # "filter disabled while appearing to pass".
        #
        # The exact derivation is implemented in domain/match_records.py and
        # needs per-match results. See docs/EPIC_1B3_FILTER_WIRING.md.
        "home_clean_sheet_pct": None,
        "away_clean_sheet_pct": None,

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


# ---------------------------------------------------------------------------
# Match-level history (Epic 1B.4)
#
# ENDPOINT, verified live 2026-08-08:
#   GET {ESPN_BASE_URL}/{league}/teams/{team_id}/schedule[?season=YYYY]
#
# What the live investigation established (docs/EPIC_1B4_MATCH_HISTORY.md):
#
#   COMPETITION PURITY - the league-scoped path returns ONLY that league's
#   matches. Real Madrid's esp.1 schedule holds 38 esp.1 events; its UCL ties
#   are on the uefa.champions path, not mixed in. Every event still carries
#   `league.slug`, and this adapter checks it anyway rather than trusting the
#   path: a silent upstream change that started mixing in cup matches would
#   otherwise contaminate a league statistic invisibly.
#
#   SEASON - unlike the team STATISTICS endpoint (which ignores `season=` and
#   always answers with current-season aggregates, GG-024), this endpoint DOES
#   honour it. `?season=2023` returns the 2023-24 fixtures and echoes
#   `requestedSeason.year = 2023`. That does NOT make historical backtesting
#   safe; see TASK 26 and the doc.
#
#   RESULTS-ONLY - in every league sampled, including mid-season ones, the
#   response contained exclusively completed events. Future fixtures were never
#   present. That is an OBSERVATION, not a guarantee, so the completion policy
#   below is enforced regardless.
# ---------------------------------------------------------------------------

# ESPN status names that mean "this match produced a real, final score".
#
# `status.type.completed` and `state == "post"` are the primary signals, but
# neither is sufficient alone: an ABANDONED match also reports state `post`, and
# its partial score is not a result. The allow-list is therefore intersected
# with those flags. Anything unrecognised is EXCLUDED (TASK 7: when uncertain,
# exclude) - a new status name will suppress a match, never fabricate one.
_COMPLETED_STATUS_NAMES = frozenset({
    "STATUS_FULL_TIME",       # observed live on every completed soccer event
    "STATUS_FINAL",           # ESPN's generic terminal status
    "STATUS_FINAL_AET",       # after extra time
    "STATUS_FINAL_PEN",       # after penalties
})


def _is_completed_event(status_type: Dict[str, Any]) -> bool:
    """
    True only for a match with a trustworthy final score.

    All three must agree: the structured `completed` flag, `state == post`, and
    a recognised terminal status name. Abandoned and suspended matches fail the
    name check even though they can present as post/completed.
    """
    return (
        bool(status_type.get("completed"))
        and status_type.get("state") == FixtureState.POST.value
        and status_type.get("name") in _COMPLETED_STATUS_NAMES
    )


def _parse_score(competitor: Dict[str, Any]) -> Optional[int]:
    """
    Goals for one competitor, or None if the payload does not clearly state it.

    ESPN nests the score as `{"value": 2.0, "displayValue": "2"}` on the
    schedule endpoint but uses a bare string on some others, so both are
    accepted. Everything else - missing, null, non-numeric, negative, or
    fractional - returns None, which drops the match.

    A missing score is NEVER read as 0. That would invent a clean sheet and a
    BTTS "no" out of an absent field, which is the exact class of bug Epic 1B.1
    exists to prevent.
    """
    score = competitor.get("score")
    raw: Any = score.get("value") if isinstance(score, dict) else score

    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            raw = float(raw)
        except ValueError:
            return None
    if not isinstance(raw, (int, float)):
        return None

    goals = int(raw)
    if goals != raw or goals < 0:
        # 1.5 goals, or -1, means we are misreading the field. Refuse it.
        return None
    return goals


def parse_schedule_events(
    payload: Dict[str, Any],
    team_id: str,
    league_code: str,
) -> List[MatchRecord]:
    """
    Convert an ESPN team-schedule payload into MatchRecords for `team_id`.

    ALL ESPN JSON knowledge lives here. `MatchRecord` is provider-independent -
    the domain layer never sees a `competitors` array - so a second provider can
    be added later by writing another adapter, not by teaching the domain about
    ESPN.

    An event is SKIPPED unless every one of these can be established:
      - the match is completed with a recognised terminal status
      - both competitors carry a team id, one of which is `team_id`
      - the perspective team's `homeAway` is exactly "home" or "away"
      - a parseable, non-negative, integral score for BOTH sides
      - a parseable kickoff timestamp

    Skipping is silent by design: a schedule legitimately contains matches this
    pipeline cannot use, and a warning per event would drown the run. What is
    never done is substituting a default for any of the above.
    """
    records: List[MatchRecord] = []

    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]

        status_type = ((competition.get("status") or {}).get("type")) or {}
        if not _is_completed_event(status_type):
            continue

        competitors = competition.get("competitors") or []
        if len(competitors) != 2:
            # Not a two-sided football match as far as this parser is concerned.
            continue

        ours = None
        theirs = None
        for competitor in competitors:
            # `competitor.id` is the team id on this endpoint; `team.id` is the
            # same value and is read as a fallback in case the shape shifts.
            competitor_id = competitor.get("id") or (competitor.get("team") or {}).get("id")
            if competitor_id is None:
                continue
            if str(competitor_id) == str(team_id):
                ours = competitor
            else:
                theirs = competitor

        if ours is None or theirs is None:
            continue

        # ------------------------------------------------------------------
        # Venue perspective (TASK 6). The single most dangerous mapping in this
        # Epic: reversing it silently swaps every clean sheet for a shutout
        # suffered. Read from ESPN's own homeAway label on OUR competitor, never
        # inferred from array order, and never defaulted.
        # ------------------------------------------------------------------
        home_away = ours.get("homeAway")
        if home_away == "home":
            venue = Venue.HOME
        elif home_away == "away":
            venue = Venue.AWAY
        else:
            continue

        goals_for = _parse_score(ours)
        goals_against = _parse_score(theirs)
        if goals_for is None or goals_against is None:
            continue

        kickoff = parse_kickoff(event.get("date") or competition.get("date"))
        if kickoff is None:
            # Without a kickoff the record can never satisfy the cutoff, so it
            # would be dead weight at best and a leak at worst.
            continue

        event_id = event.get("id") or competition.get("id")
        opponent_id = theirs.get("id") or (theirs.get("team") or {}).get("id")

        records.append(
            MatchRecord(
                venue=venue,
                goals_for=goals_for,
                goals_against=goals_against,
                completed=True,
                kickoff=kickoff,
                event_id=str(event_id) if event_id is not None else None,
                # The event's own league, not the requested one. If ESPN ever
                # mixes competitions, the mismatch is preserved here so the
                # domain filter can drop it, instead of being papered over.
                competition=((event.get("league") or {}).get("slug")) or None,
                team_id=str(team_id),
                opponent_id=str(opponent_id) if opponent_id is not None else None,
            )
        )

    return records


# Per-run schedule cache (TASK 21).
#
# One analysis run asks for the same team's schedule repeatedly - main.py and
# analyze_all.py each resolve both sides of every fixture, and a league's
# fixtures share teams across dates. Keyed by every parameter that changes the
# response: league, team and season.
#
# Deliberately a plain dict: process-lifetime, in-memory, no eviction, no Redis,
# no disk. It caches RAW RECORDS ONLY - never a derived statistic - so the
# target-kickoff cutoff still runs per fixture and cannot be bypassed by a cache
# hit. A failure is not cached, so one transient outage does not poison the rest
# of the run.
_schedule_cache: Dict[tuple, List[MatchRecord]] = {}


def clear_schedule_cache() -> None:
    """Drop the per-run cache. For tests, and for any long-lived caller."""
    _schedule_cache.clear()


def get_team_match_records(
    team_id: str,
    league_code: str,
    season: Optional[int] = None,
) -> Optional[List[MatchRecord]]:
    """
    Completed match records for one team, from ESPN's team-schedule endpoint.

    Returns:
        list  - the completed matches ESPN reported (possibly EMPTY, which is a
                real answer: in August a team may genuinely have played none)
        None  - the request FAILED. A different fact, and the caller must not
                treat it as "no matches" (TASK 19).

    That distinction is the reason for Optional. Collapsing the two would make an
    ESPN outage indistinguishable from a promoted side's blank record, and both
    would silently become a 0% clean-sheet rate that passes the filter.
    """
    if season is None:
        season = resolve_season(league_code)

    cache_key = (league_code, str(team_id), season)
    if cache_key in _schedule_cache:
        return _schedule_cache[cache_key]

    url = f"{ESPN_BASE_URL}/{league_code}/teams/{team_id}/schedule"
    # Same _fetch transport as every other ESPN call here: HTTPS, timeout,
    # bounded retry on transient failure, status validation, malformed-JSON
    # handling. No second HTTP stack (TASK 20).
    result = _fetch(url, params={"season": season})

    if not result.ok:
        if result.error == ESPNError.EMPTY_RESPONSE:
            # HTTP 200 with `{}`. This endpoint returns a populated object even
            # for a team with no fixtures, so an empty body is a provider
            # problem, not evidence of zero matches.
            print(f"ESPN schedule empty for {league_code}/{team_id} season={season}")
        return None

    records = parse_schedule_events(result.data or {}, team_id, league_code)
    _schedule_cache[cache_key] = records
    return records


# ESPN's scoreboard silently truncates at 100 events (verified live: a full-season
# date range returned exactly 100 with HTTP 200 and no error field, cutting off
# mid-November). A truncated league baseline is not a smaller sample - it is a
# WRONG one, biased toward whichever part of the season ESPN chose to return.
#
# 1000 comfortably exceeds a 380-fixture league season while staying a bound
# rather than an invitation to unbounded growth.
_SCOREBOARD_LIMIT = 1000


def _season_date_range(season: int) -> str:
    """
    ESPN `dates=` range covering one football season.

    A "2025" season means 2025/26, so the window runs July 2025 to June 2026 -
    wide enough for early qualifiers and a late final, and narrow enough that it
    cannot reach into an adjacent season. The season boundary is what keeps
    2025/26 results out of a 2026/27 baseline (TASK 22).
    """
    return f"{season}0701-{season + 1}0630"


def parse_scoreboard_events(
    payload: Dict[str, Any],
    league_code: str,
) -> List[MatchRecord]:
    """
    Convert an ESPN league-scoreboard payload into one MatchRecord per fixture.

    Records are built from the HOME side's perspective, always. This is not an
    arbitrary choice: it is what makes each fixture countable exactly once. The
    league baseline sums `goals_for + goals_against`, which is the fixture's
    total goals regardless of which side is speaking, so one consistent
    perspective per fixture gives an exact goal total and an exact fixture count.

    Rejection rules are identical to the team-schedule parser - completed status,
    two competitors, both scores parseable, kickoff present - because a match
    that is not trustworthy evidence for a team is not trustworthy evidence for a
    league either.
    """
    records: List[MatchRecord] = []

    # The feed's own declaration of which competition this response covers, used
    # only when an event does not carry its own league slug.
    leagues = payload.get("leagues") or []
    payload_slug = (leagues[0].get("slug") if leagues else None) or None

    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]

        status_type = ((competition.get("status") or {}).get("type")) or {}
        if not _is_completed_event(status_type):
            continue

        competitors = competition.get("competitors") or []
        if len(competitors) != 2:
            continue

        home = None
        away = None
        for competitor in competitors:
            if competitor.get("homeAway") == "home":
                home = competitor
            elif competitor.get("homeAway") == "away":
                away = competitor

        # Both sides must be explicitly labelled. Inferring the missing one from
        # array order would invent a venue the feed never stated.
        if home is None or away is None:
            continue

        home_goals = _parse_score(home)
        away_goals = _parse_score(away)
        if home_goals is None or away_goals is None:
            continue

        kickoff = parse_kickoff(event.get("date") or competition.get("date"))
        if kickoff is None:
            continue

        event_id = event.get("id") or competition.get("id")
        home_id = home.get("id") or (home.get("team") or {}).get("id")
        away_id = away.get("id") or (away.get("team") or {}).get("id")

        records.append(
            MatchRecord(
                venue=Venue.HOME,
                goals_for=home_goals,
                goals_against=away_goals,
                completed=True,
                kickoff=kickoff,
                event_id=str(event_id) if event_id is not None else None,
                competition=((event.get("league") or {}).get("slug")) or payload_slug,
                team_id=str(home_id) if home_id is not None else None,
                opponent_id=str(away_id) if away_id is not None else None,
            )
        )

    return records


# Per-run league cache, same contract as `_schedule_cache`: raw records only, so
# the per-fixture cutoff still runs on every read and a cache hit can never
# bypass it. Keyed by league and season, the two parameters that change the
# response. One entry serves every fixture in that league for the whole run.
_league_cache: Dict[tuple, List[MatchRecord]] = {}


def clear_league_cache() -> None:
    """Drop the per-run league cache. For tests, and for long-lived callers."""
    _league_cache.clear()


def get_league_match_records(
    league_code: str,
    season: Optional[int] = None,
) -> Optional[List[MatchRecord]]:
    """
    Every completed fixture in one league season, from ESPN's scoreboard.

    ONE request per league per run, not one per team. Assembling this from the
    20 team schedules would take 20 requests and then need deduplication, since
    each fixture appears in two of them; the scoreboard returns each fixture once
    and was verified live to reproduce the league goal total exactly (eng.1
    2025-26: 380 fixtures, 1045 goals, matching the standings-derived figure).

    Returns None on provider failure - never an empty list, which would be
    indistinguishable from a league that has genuinely not started (TASK 15).
    """
    if season is None:
        season = resolve_season(league_code)

    cache_key = (league_code, season)
    if cache_key in _league_cache:
        return _league_cache[cache_key]

    url = f"{ESPN_BASE_URL}/{league_code}/scoreboard"
    result = _fetch(
        url,
        params={"dates": _season_date_range(season), "limit": _SCOREBOARD_LIMIT},
    )

    if not result.ok:
        return None

    payload = result.data or {}
    records = parse_scoreboard_events(payload, league_code)

    # Truncation guard. If ESPN returned exactly the cap, the response was
    # probably cut short and the baseline would be computed from an arbitrary
    # slice of the season. Refusing is correct: a silently partial league average
    # divides every lambda in the run by a wrong number, and unlike a failed
    # request it would leave no trace.
    if len(payload.get("events") or []) >= _SCOREBOARD_LIMIT:
        print(
            f"ESPN scoreboard for {league_code} season={season} hit the "
            f"{_SCOREBOARD_LIMIT}-event limit; refusing a possibly truncated "
            "league baseline"
        )
        return None

    _league_cache[cache_key] = records
    return records


def get_league_baseline(
    league_code: str,
    target_kickoff: datetime,
    exclude_event_id: Optional[str] = None,
    season: Optional[int] = None,
) -> Optional[LeagueBaseline]:
    """
    Point-in-time league goals per team per match, as of one kickoff.

    Composes fetch -> parse -> `domain.derive_league_baseline`, which takes the
    cutoff as a REQUIRED keyword argument. There is no path through this module
    that yields a league average over a whole finished season when a mid-season
    fixture is being predicted.

    Returns None when the provider failed, so the caller can distinguish that
    from a league with no completed fixtures yet.
    """
    records = get_league_match_records(league_code, season=season)
    if records is None:
        return None

    return derive_league_baseline(
        records,
        target_kickoff=target_kickoff,
        competition=league_code,
        exclude_event_id=exclude_event_id,
    )


def get_team_venue_averages(
    team_id: str,
    league_code: str,
    venue: str,
    target_kickoff: datetime,
    exclude_event_id: Optional[str] = None,
    season: Optional[int] = None,
) -> Optional[VenueGoalAverages]:
    """
    Point-in-time goals for/against per match for one team at one venue.

    Reuses the same cached team-schedule records as `get_team_history`, so
    turning on point-in-time model inputs costs no additional requests - the
    filter statistics and the model inputs are two derivations over one fetch.

    Returns None on provider failure, never zeroed averages.
    """
    records = get_team_match_records(team_id, league_code, season=season)
    if records is None:
        return None

    return derive_venue_averages(
        records,
        target_kickoff=target_kickoff,
        venue=venue,
        competition=league_code,
        exclude_event_id=exclude_event_id,
    )


def get_team_history(
    team_id: str,
    league_code: str,
    venue: str,
    target_kickoff: datetime,
    exclude_event_id: Optional[str] = None,
    season: Optional[int] = None,
) -> Optional[DerivedHistory]:
    """
    Derived clean-sheet and BTTS rates for one team, at one venue, as of one
    kickoff.

    The only history function the pipeline should call. It composes fetch ->
    parse -> `domain.derive_history`, and `derive_history` takes the cutoff as a
    REQUIRED keyword argument, so there is no path through this module that can
    produce a statistic without applying it.

    `competition=league_code` is passed deliberately: GG.md scopes the model to
    league matches and explicitly excludes friendlies and early cup rounds, so a
    cup result must not inflate a league clean-sheet rate even if the feed offers
    one.

    Returns None when the provider failed - never a zero rate.
    """
    records = get_team_match_records(team_id, league_code, season=season)
    if records is None:
        return None

    return derive_history(
        records,
        target_kickoff=target_kickoff,
        venue=venue,
        competition=league_code,
        exclude_event_id=exclude_event_id,
    )
