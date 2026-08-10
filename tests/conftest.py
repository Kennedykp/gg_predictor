"""
Shared test harness: offline guarantee, cache isolation, ESPN payload builders.

Three jobs, all of which exist because Epic 1B.5 moved the model's inputs onto a
second ESPN endpoint:

1. NETWORK IS BLOCKED. Enforced at the socket layer rather than by trusting each
   test to monkeypatch the right seam. `espn` now has two transports in play -
   `_make_request` for aggregates and `_fetch` for schedule/scoreboard - and a
   test that stubs only the first would silently start making real HTTP calls.
   That test would still pass, on live data, at whatever speed the network
   allowed, and would fail in CI or on a plane for reasons unrelated to the code.
   Blocking sockets converts that class of mistake from "mysteriously flaky" into
   "fails immediately, with the offending call named".

2. CACHES ARE CLEARED between tests. The per-run schedule and league caches are
   module-level dicts, so without this a payload installed by one test would be
   served to the next one, and the resulting pass/fail would depend on test
   ORDER. Deterministic tests cannot depend on ordering.

3. PAYLOAD BUILDERS. One place that knows the shape of an ESPN event, so a
   change in the feed's structure is a one-line fix here instead of a hunt
   through every test file.
"""

import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

import espn


# ---------------------------------------------------------------------------
# 1. Offline guarantee
# ---------------------------------------------------------------------------
class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a real socket."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """
    Fail loudly on any real network access.

    Autouse and unconditional: a test that needs ESPN data must stub the
    provider seam, and one that forgets should say so in its own failure rather
    than quietly depending on the live internet.
    """

    def guard(*args, **kwargs):
        raise NetworkAccessAttempted(
            "A test attempted a real network connection. Stub `espn._fetch` or "
            "`espn._make_request` (see the builders in tests/conftest.py)."
        )

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


# ---------------------------------------------------------------------------
# 2. Cache isolation
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_espn_caches():
    """Empty the per-run caches before and after every test."""
    espn.clear_schedule_cache()
    espn.clear_league_cache()
    yield
    espn.clear_schedule_cache()
    espn.clear_league_cache()


# ---------------------------------------------------------------------------
# 3. ESPN payload builders
# ---------------------------------------------------------------------------
def utc(year: int, month: int, day: int, hour: int = 15, minute: int = 0) -> datetime:
    """A timezone-aware UTC datetime. Naive datetimes are a bug, never a default."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


#: Sentinel for "build an event with NO season block at all".
#: Distinct from None, which means "fill in the season implied by the request".
#: Real ESPN events always carry season metadata (0 of 53,934 in the Epic 2A
#: corpus lacked it), so a payload without it is a malformed-provider scenario
#: and must be requested explicitly rather than produced by accident.
OMIT = "__omit__"


def espn_event(
    event_id: str,
    kickoff: datetime,
    home_id: str,
    away_id: str,
    home_goals: Optional[int],
    away_goals: Optional[int],
    league: str = "eng.1",
    status_name: str = "STATUS_FULL_TIME",
    state: str = "post",
    completed: bool = True,
    season_year: Any = None,
    season_label: Any = None,
) -> Dict[str, Any]:
    """
    One ESPN event, shaped as the live feed shapes it.

    Scores are nested under `{"value": ...}` because that is what the schedule
    endpoint actually returns; passing None omits the score entirely, which is
    how a genuinely missing score is simulated.

    SEASON METADATA (Epic 2B.1). `season_year=None` leaves the season block off
    HERE and lets the feed stub fill it in from the request, which is what the
    real API does - a response to `?season=2019` contains events labelled 2019.
    That keeps every existing test describing coherent data without restating
    the season in each one. Pass `season_year` explicitly to build a
    wrong-season event, or `season_year=OMIT` to build one with no season
    metadata at all; both must be REJECTED by the provider.
    """

    def competitor(team_id: str, goals: Optional[int], home_away: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "id": team_id,
            "homeAway": home_away,
            "team": {"id": team_id, "displayName": f"Team {team_id}"},
        }
        if goals is not None:
            entry["score"] = {"value": float(goals), "displayValue": str(goals)}
        return entry

    event: Dict[str, Any] = {
        "id": event_id,
        "date": kickoff.strftime("%Y-%m-%dT%H:%MZ"),
        "league": {"slug": league},
        "uid": f"s:600~l:700~e:{event_id}",
        "competitions": [
            {
                "id": event_id,
                "status": {
                    "type": {
                        "name": status_name,
                        "state": state,
                        "completed": completed,
                    }
                },
                "competitors": [
                    competitor(home_id, home_goals, "home"),
                    competitor(away_id, away_goals, "away"),
                ],
            }
        ],
    }

    if season_year is not OMIT and season_year is not None:
        season: Dict[str, Any] = {"year": season_year}
        if season_label is not None and season_label is not OMIT:
            season["slug"] = season_label
        event["season"] = season
    elif season_label is not None and season_label is not OMIT:
        # A label with no year: metadata that exists but cannot identify a
        # season, which must fail closed rather than be half-trusted.
        event["season"] = {"slug": season_label}

    return event


def _stamp_season(event: Dict[str, Any], season_year: int, league: str) -> Dict[str, Any]:
    """
    Fill in the season block the real feed would have carried, if absent.

    This mirrors ESPN rather than convenience-patching the tests: a response to
    a request for season S labels its events S. An event that already states a
    season is left EXACTLY as the test built it, so wrong-season and
    missing-season fixtures survive the stub untouched.
    """
    if "season" in event:
        return event
    stamped = dict(event)
    stamped["season"] = {
        "year": season_year,
        "slug": f"{season_year}-{str(season_year + 1)[-2:]}-{league.replace('.', '-')}",
    }
    return stamped


def schedule_payload(
    events: List[Dict[str, Any]],
    league: str = "eng.1",
    season: int = 2025,
) -> Dict[str, Any]:
    """
    An ESPN team-schedule response.

    `season` names the season the request asked for, and events that do not
    state their own are labelled with it - exactly as the live endpoint behaves.
    Note that live payloads also carry a top-level `season` describing the
    CURRENT season regardless of what was requested (measured: a 2019 request
    answers with `season.year = 2026`), which is why nothing reads it.
    """
    return {
        "events": [_stamp_season(e, season, league) for e in events],
        "requestedSeason": {"year": season},
        "season": {"year": 2026},
    }


def scoreboard_payload(
    events: List[Dict[str, Any]],
    league: str = "eng.1",
    season: int = 2025,
) -> Dict[str, Any]:
    """
    An ESPN league-scoreboard response.

    Carries `leagues[0].id` as well as the slug because scoreboard events have
    no per-event league object; the id is what the per-event `uid` check
    compares against.
    """
    return {
        "events": [_stamp_season(e, season, league) for e in events],
        "leagues": [{"slug": league, "id": "700", "season": {"year": 2026}}],
    }


@pytest.fixture
def espn_feed(monkeypatch):
    """
    Install a fake `espn._fetch` serving schedule and scoreboard payloads.

    Routes on the URL exactly as the real endpoints are distinguished, so a
    caller that requests the wrong one gets a failure rather than the other
    one's data.

    Usage:
        espn_feed(team_events={"359": [...]}, league_events=[...])

    Any team without an entry yields an empty schedule - a real answer meaning
    "no completed matches" - while `fail=True` simulates provider failure, which
    is a different fact and must stay distinguishable.
    """

    def _install(
        team_events: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        league_events: Optional[List[Dict[str, Any]]] = None,
        fail: bool = False,
    ):
        team_events = team_events or {}

        def fake_fetch(url: str, params: Optional[dict] = None):
            if fail:
                return espn.FetchResult(
                    error=espn.ESPNError.SERVER_ERROR, detail="stubbed failure"
                )
            # Which season did the caller ask for? The schedule endpoint says so
            # directly; the scoreboard says so through its discovery window.
            # Answering with a season the caller did not request is what the
            # real feed does NOT do, so the stub does not either.
            requested = (params or {}).get("season")
            if requested is None:
                dates = str((params or {}).get("dates") or "")
                requested = int(dates[:4]) if dates[:4].isdigit() else 2025
            requested = int(requested)

            if "/schedule" in url:
                for team_id, events in team_events.items():
                    if f"/teams/{team_id}/" in url:
                        return espn.FetchResult(
                            data=schedule_payload(events, season=requested)
                        )
                return espn.FetchResult(data=schedule_payload([], season=requested))
            if "/scoreboard" in url:
                return espn.FetchResult(
                    data=scoreboard_payload(league_events or [], season=requested)
                )
            return espn.FetchResult(
                error=espn.ESPNError.HTTP_ERROR, detail=f"unstubbed URL: {url}"
            )

        monkeypatch.setattr(espn, "_fetch", fake_fetch)
        espn.clear_schedule_cache()
        espn.clear_league_cache()

    return _install
