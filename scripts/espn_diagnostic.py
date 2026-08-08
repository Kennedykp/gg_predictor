#!/usr/bin/env python3
"""
ESPN live endpoint diagnostic (Epic 1B.2, TASK 16).

*** THIS SCRIPT MAKES REAL, OUTBOUND NETWORK CALLS TO ESPN. ***

It is NOT a test, it is NOT run by CI, and it MUST be run manually:

  - It lives in scripts/, which is outside pytest's `testpaths = ["tests"]`
    (pyproject.toml), so pytest never collects it.
  - There is deliberately no scripts/__init__.py, so it is not an importable
    package and cannot be pulled in as a side effect.
  - All work happens inside main(), guarded by `if __name__ == "__main__"`, so
    importing this module performs no I/O whatsoever.

Never add it to a CI job. A test that depends on a third-party endpoint being
up is not a test - it fails for reasons that have nothing to do with this
repository. That is precisely why the offline unit tests in tests/unit/ stub
the transport, and why this lives here instead.

Usage
-----
    ./.venv/bin/python scripts/espn_diagnostic.py                # eng.1
    ./.venv/bin/python scripts/espn_diagnostic.py ita.1
    ./.venv/bin/python scripts/espn_diagnostic.py eng.1 --date 20250816

What it reports, per endpoint: the URL, the HTTP status, the response size in
bytes, whether the data the production code actually needs was present, and a
one-line verdict.

It exists to answer, in one command and from live data, the questions Epic 1B.2
had to answer by hand:

  1. GG-003 - why did the league average silently fall back to a hardcoded
     1.35? Both standings paths are probed side by side, same league, same
     season, so the difference between the broken `/apis/site/v2/.../standings`
     and the working `/apis/v2/.../standings` is self-evident rather than
     asserted.
  2. Does the scoreboard return events for a date, and in what states?
  3. GG-004 - does ESPN really supply homeGamesPlayed / awayGamesPlayed, or
     was halving `matches_played` unavoidable? (It was not.)
  4. What does espn.get_league_avg_goals() return right now - a real number,
     or an honest UNAVAILABLE?

Safety
------
No credentials are involved (ESPN's public endpoints require none) and none are
read or printed. Raw response bodies are flattened to one line and hard-capped
at 200 characters, so a 68KB standings table never lands in your terminal or a
pasted bug report.

This script reimplements NOTHING. It imports espn.py and config.py and calls
the real functions, so what it prints is what production sees.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

# ---------------------------------------------------------------------------
# scripts/ is deliberately not a package, and running this file directly puts
# scripts/ - not the repository root - on sys.path. Add the root so the REAL
# production modules are the ones exercised below.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402  - must follow the sys.path bootstrap above
import espn  # noqa: E402  - must follow the sys.path bootstrap above

DEFAULT_LEAGUE = "eng.1"
BODY_LIMIT = 200          # hard cap on any raw body ever printed
MAX_EVENTS_LISTED = 8     # per-event detail is capped; the tally covers the rest
WIDTH = 78

# What GG-003 silently substituted whenever the standings call failed. Printed
# only for comparison against the live figure - it is not used as a fallback.
LEGACY_HARDCODED_AVG = 1.35


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _heading(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _snippet(body: str) -> str:
    """
    One flattened, hard-capped line of a raw body.

    Slice before flattening: the working standings response is ~68KB and there
    is no reason to normalise whitespace across all of it just to show 200
    characters.
    """
    flat = " ".join(body[: BODY_LIMIT * 4].split())
    if len(body) <= BODY_LIMIT:
        return flat or "<empty>"
    return f"{flat[:BODY_LIMIT]}... [truncated, {len(body)} chars total]"


def _path_of(url: str) -> str:
    """Path only - the part that differs between the two standings hosts."""
    return urlsplit(url).path


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    """One raw HTTP observation. Deliberately uninterpreted."""

    url: str
    status: int | None = None
    n_bytes: int = 0
    payload: Any = None
    body: str = ""
    transport_error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.transport_error)


def probe(url: str, params: dict | None = None) -> Probe:
    """
    A single GET, bypassing espn._fetch on purpose.

    Production deliberately hides status codes and byte counts behind
    Optional[dict] - that abstraction is correct for the pipeline and useless
    for a diagnostic, because "HTTP 200 with a 2-byte body" and "HTTP 404" both
    arrive as None. Here we want the raw facts. The production timeout is
    reused so the observation is representative.
    """
    try:
        response = requests.get(url, params=params, timeout=config.ESPN_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:BODY_LIMIT]}"
        return Probe(url=url, transport_error=detail)

    try:
        payload = response.json()
    except ValueError:
        payload = None

    return Probe(
        url=response.url,
        status=response.status_code,
        n_bytes=len(response.content),
        payload=payload,
        body=response.text,
    )


def report(label: str, p: Probe, found: bool, detail: str, verdict: str) -> None:
    """The compact per-endpoint block: url, status, bytes, found, body, verdict."""
    print(f"[{label}]")
    print(f"  url     : {p.url}")
    if p.failed:
        print("  status  : n/a - request never completed")
        print(f"  error   : {p.transport_error}")
    else:
        print(f"  status  : {p.status}")
        print(f"  bytes   : {p.n_bytes}")
        print(f"  found   : {'YES' if found else 'NO'} - {detail}")
        print(f"  body    : {_snippet(p.body)}")
    print(f"  verdict : {verdict}")
    print()


# ---------------------------------------------------------------------------
# 1. Standings - the GG-003 comparison
# ---------------------------------------------------------------------------
def _standings_entries(payload: Any) -> list:
    """
    Entries read from exactly where espn.get_league_avg_goals looks for them.

    Same traversal, same tolerance for a malformed shape - if this returns
    nothing, that function returns None.
    """
    if not isinstance(payload, dict) or not payload:
        return []
    children = payload.get("children") or []
    if not children:
        return []
    try:
        entries = children[0]["standings"]["entries"]
    except (KeyError, IndexError, TypeError):
        return []
    return entries or []


def check_standings(league: str, season: int) -> tuple[list[tuple[str, str]], dict | None]:
    """
    Probe BOTH standings paths and print them side by side.

    Same league, same season, same query string: the ONLY variable is the path.
    That is the whole point - the old code was not asking the wrong question,
    it was asking the right question at the wrong address, and getting an
    HTTP 200 for it.
    """
    _heading(f"1. STANDINGS - GG-003 root cause (league={league}, season={season})")

    broken_url = f"{config.ESPN_BASE_URL}/{league}/standings"
    working_url = f"{config.ESPN_STANDINGS_BASE_URL}/{league}/standings"

    broken = probe(broken_url, {"season": season})
    working = probe(working_url, {"season": season})

    broken_entries = _standings_entries(broken.payload)
    working_entries = _standings_entries(working.payload)

    if broken.failed:
        broken_verdict = "UNREACHABLE - see error above"
    elif broken.status == 200 and not broken_entries:
        broken_verdict = (
            "BROKEN - HTTP 200 with no standings data; nothing raises, so the "
            "old code fell through to the hardcoded 1.35"
        )
    elif broken_entries:
        broken_verdict = f"UNEXPECTED - this path now returns {len(broken_entries)} entries"
    else:
        broken_verdict = f"BROKEN - HTTP {broken.status}, no standings data"

    report(
        "standings via ESPN_BASE_URL (the OLD, BROKEN path)",
        broken,
        bool(broken_entries),
        f"{len(broken_entries)} standings entries; 'children' key "
        f"{'present' if isinstance(broken.payload, dict) and 'children' in broken.payload else 'ABSENT'}",
        broken_verdict,
    )

    if working.failed:
        working_verdict = "UNREACHABLE - see error above"
    elif working_entries:
        working_verdict = f"OK - real standings table, {len(working_entries)} teams"
    else:
        working_verdict = f"UNEXPECTED - HTTP {working.status} but no entries found"

    report(
        "standings via ESPN_STANDINGS_BASE_URL (the CURRENT, WORKING path)",
        working,
        bool(working_entries),
        f"{len(working_entries)} standings entries; 'children' key "
        f"{'present' if isinstance(working.payload, dict) and 'children' in working.payload else 'ABSENT'}",
        working_verdict,
    )

    print("  SIDE BY SIDE")
    print(f"    {'path':<46} {'status':>6} {'bytes':>8} {'entries':>8}")
    for p, entries in ((broken, broken_entries), (working, working_entries)):
        status = "n/a" if p.failed else str(p.status)
        print(
            f"    {_clip(_path_of(p.url), 46):<46} {status:>6} "
            f"{p.n_bytes:>8} {len(entries):>8}"
        )
    same_status = (not broken.failed and not working.failed and broken.status == working.status)
    if same_status:
        print(
            f"\n    Both paths answer HTTP {broken.status}. Only the second carries data.\n"
            "    A status-code-only check cannot tell these apart - which is exactly\n"
            "    how GG-003 survived: success by status, nothing by content."
        )
    print()

    team_ref = None
    if working_entries:
        try:
            team = working_entries[0]["team"]
            team_ref = {"id": str(team["id"]), "name": team.get("displayName", "?")}
        except (KeyError, IndexError, TypeError):
            team_ref = None

    return (
        [
            ("standings /apis/site/v2", broken_verdict),
            ("standings /apis/v2", working_verdict),
        ],
        team_ref,
    )


# ---------------------------------------------------------------------------
# 2. Scoreboard
# ---------------------------------------------------------------------------
def check_scoreboard(league: str, on: date) -> tuple[list[tuple[str, str]], dict | None]:
    """Probe the scoreboard for one date; report event count and status states."""
    date_str = on.strftime("%Y%m%d")
    _heading(f"2. SCOREBOARD - fixtures (league={league}, dates={date_str})")

    p = probe(f"{config.ESPN_BASE_URL}/{league}/scoreboard", {"dates": date_str})

    payload = p.payload if isinstance(p.payload, dict) else {}
    has_events_key = "events" in payload
    events = payload.get("events") or []

    states: Counter = Counter()
    names: Counter = Counter()
    predictable = 0
    rows: list[str] = []
    team_ref = None

    for event in events:
        status_type = (event.get("status") or {}).get("type") or {}
        state = status_type.get("state") or espn.FixtureState.UNKNOWN.value
        name = status_type.get("name", "STATUS_UNKNOWN")
        states[state] += 1
        names[name] += 1

        # `_NOT_PLAYABLE` is espn.py's own list of "will not be played as
        # scheduled" names. Reused rather than retyped so this cannot drift
        # from production. is_predictable() is the real production predicate.
        fixture = {"state": state, "is_postponed": name in espn._NOT_PLAYABLE}
        ok = espn.is_predictable(fixture)
        predictable += int(ok)

        competitions = event.get("competitions") or [{}]
        competitors = competitions[0].get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home_name = (home.get("team") or {}).get("displayName", "?")
        away_name = (away.get("team") or {}).get("displayName", "?")

        if team_ref is None and (home.get("team") or {}).get("id"):
            team_ref = {"id": str(home["team"]["id"]), "name": home_name}

        kickoff = espn.parse_kickoff(event.get("date"))
        when = kickoff.strftime("%H:%MZ") if kickoff else "  ?  "
        rows.append(
            f"    {when}  {_clip(home_name, 20):<20} v {_clip(away_name, 20):<20} "
            f"state={state:<7} {_clip(name, 20):<20} predictable={'Y' if ok else 'N'}"
        )

    if p.failed:
        verdict = "UNREACHABLE - see error above"
    elif not has_events_key:
        verdict = f"BROKEN - HTTP {p.status} but no 'events' key in the payload"
    elif not events:
        verdict = "EMPTY - endpoint healthy, genuinely no fixtures on this date (not a failure)"
    else:
        tally = ", ".join(f"{s}={c}" for s, c in sorted(states.items()))
        verdict = f"OK - {len(events)} events ({tally}); {predictable} predictable pre-match"

    report(
        "scoreboard",
        p,
        bool(events),
        f"'events' key {'present' if has_events_key else 'ABSENT'}, {len(events)} events",
        verdict,
    )

    if rows:
        print(f"  EVENTS (showing up to {MAX_EVENTS_LISTED} of {len(rows)})")
        for row in rows[:MAX_EVENTS_LISTED]:
            print(row)
        print()
        print(f"  state tally  : {dict(sorted(states.items()))}")
        print(f"  status names : {dict(sorted(names.items()))}")
        print(f"  predictable  : {predictable}/{len(events)} (espn.is_predictable)")
        print()

    return [("scoreboard", verdict)], team_ref


# ---------------------------------------------------------------------------
# 3. Team endpoint - the GG-004 evidence
# ---------------------------------------------------------------------------
def _total_stats(payload: Any) -> tuple[list, list[str]]:
    """The `type == "total"` record item's stats, exactly as espn.py reads them."""
    if not isinstance(payload, dict):
        return [], []
    items = ((payload.get("team") or {}).get("record") or {}).get("items") or []
    types = [str(i.get("type")) for i in items]
    if not items:
        return [], types
    overall = next((r for r in items if r.get("type") == "total"), items[0])
    return overall.get("stats") or [], types


def _stat(stats: list, name: str) -> tuple[bool, Any]:
    """(present, value) - the distinction GG-001 was about."""
    for s in stats:
        if s.get("name") == name:
            return True, s.get("value")
    return False, None


def check_team(league: str, team_ref: dict | None) -> list[tuple[str, str]]:
    """Probe one team endpoint and report the home/away games-played split."""
    _heading("3. TEAM ENDPOINT - GG-004 (homeGamesPlayed / awayGamesPlayed)")

    if not team_ref:
        print("  SKIPPED - no team id could be discovered from standings or scoreboard.\n")
        return [("team endpoint", "SKIPPED - no team id available")]

    team_id, team_name = team_ref["id"], team_ref["name"]
    print(f"  probing team: {team_name} (id={team_id})\n")

    p = probe(f"{config.ESPN_BASE_URL}/{league}/teams/{team_id}")
    stats, item_types = _total_stats(p.payload)

    home_present, home_value = _stat(stats, "homeGamesPlayed")
    away_present, away_value = _stat(stats, "awayGamesPlayed")
    total_present, total_value = _stat(stats, "gamesPlayed")
    both = home_present and away_present

    if p.failed:
        verdict = "UNREACHABLE - see error above"
    elif both and (home_value or away_value):
        verdict = (
            f"OK - ESPN supplies the real split (home={home_value}, away={away_value}); "
            "halving matches_played was never necessary"
        )
    elif both:
        # Keys present, both zero. That is the GG-004 evidence only in part: it
        # proves ESPN exposes the fields, but a preseason table has no split to
        # observe yet, so claiming the fix is vindicated here would overstate it.
        verdict = (
            f"PRESENT but ZERO (home={home_value}, away={away_value}) - fields exist; "
            "no games played yet, so no split to verify. Re-run mid-season."
        )
    elif stats:
        verdict = "DEGRADED - record stats present but home/away games-played ABSENT"
    else:
        verdict = f"BROKEN - HTTP {p.status} with no usable record stats"

    report(
        "team",
        p,
        both,
        f"record item types={item_types}, {len(stats)} stats in 'total'",
        verdict,
    )

    print("  REQUIRED STATS")
    for label, present, value in (
        ("gamesPlayed", total_present, total_value),
        ("homeGamesPlayed", home_present, home_value),
        ("awayGamesPlayed", away_present, away_value),
    ):
        state = f"PRESENT  value={value}" if present else "ABSENT   value=None"
        print(f"    {label:<16}: {state}")

    if home_present and away_present and total_present:
        summed = (home_value or 0) + (away_value or 0)
        agree = "consistent" if summed == total_value else "MISMATCH"
        print(f"    {'home+away':<16}: {summed} vs gamesPlayed={total_value} ({agree})")
        if total_value:
            print(
                f"    {'legacy guess':<16}: matches_played/2 = {total_value / 2} "
                f"- what GG-004 fabricated instead of {home_value}/{away_value}"
            )
    if not stats:
        print("    (no stats returned - nothing to inspect)")
    elif not both:
        available = [str(s.get("name")) for s in stats][:10]
        print(f"    available names (first 10): {available}")
    print()

    # The real production consumer, on the same team.
    print("  espn.get_team_stats() - the production path, same team")
    team_stats = espn.get_team_stats(team_id, league)
    if team_stats is None:
        print("    -> None (UNAVAILABLE: no usable record for this team)")
    else:
        for key in (
            "matches_played",
            "home_matches",
            "away_matches",
            "home_goals_scored",
            "away_goals_scored",
            "home_goals_conceded",
            "away_goals_conceded",
            "total_goals_avg",
        ):
            value = team_stats.get(key)
            shown = "UNAVAILABLE (None)" if value is None else value
            print(f"    {key:<21}: {shown}")
    print()

    return [("team endpoint", verdict)]


# ---------------------------------------------------------------------------
# 4. The production league average
# ---------------------------------------------------------------------------
def check_league_avg(league: str, season: int) -> list[tuple[str, str]]:
    """Call the real espn.get_league_avg_goals() and print what it returns."""
    _heading(f"4. espn.get_league_avg_goals({league!r}) - the production call")

    avg = espn.get_league_avg_goals(league)

    if avg is None:
        print("    result  : UNAVAILABLE")
        print("    meaning : the function returned None. It did NOT substitute a")
        print(f"              plausible constant (the old code returned {LEGACY_HARDCODED_AVG}).")
        print("              Downstream must treat this fixture as unpredictable.")
        verdict = "UNAVAILABLE (None) - correct behaviour on failure, no fabricated value"
    else:
        print(f"    result  : {avg:.4f} goals per team per match")
        print(f"    units   : total goals / total team-games (season={season})")
        print(f"    per-fixture equivalent : {avg * 2:.4f} goals per match")
        print(
            f"    vs legacy hardcoded {LEGACY_HARDCODED_AVG}: "
            f"delta {avg - LEGACY_HARDCODED_AVG:+.4f} "
            f"({abs(avg - LEGACY_HARDCODED_AVG) / avg * 100:.2f}% off)"
        )
        verdict = f"OK - {avg:.4f} goals per team per match"
    print()
    return [("get_league_avg_goals()", verdict)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="espn_diagnostic.py",
        description="Live ESPN endpoint diagnostic (manual only - makes real network calls).",
    )
    parser.add_argument(
        "league",
        nargs="?",
        default=DEFAULT_LEAGUE,
        help=f"ESPN league code, e.g. eng.1 ita.1 ger.1 (default: {DEFAULT_LEAGUE})",
    )
    parser.add_argument(
        "--date",
        dest="on",
        default=None,
        metavar="YYYYMMDD",
        help="scoreboard date to probe (default: today, UTC)",
    )
    args = parser.parse_args(argv)

    if args.on:
        try:
            on = datetime.strptime(args.on, "%Y%m%d").date()
        except ValueError:
            parser.error(f"--date must be YYYYMMDD, got {args.on!r}")
    else:
        on = datetime.now(timezone.utc).date()

    league = args.league
    season = espn.resolve_season(league)

    print("=" * WIDTH)
    print("ESPN LIVE DIAGNOSTIC - Epic 1B.2 TASK 16")
    print("=" * WIDTH)
    print("  MANUAL SCRIPT: this makes REAL network calls to ESPN and is excluded")
    print("  from pytest (scripts/ is outside testpaths) and from CI.")
    print()
    print(f"  run at (UTC)   : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  league         : {league}"
          f"{'' if league in config.ALLOWED_LEAGUES else '   [NOT in config.ALLOWED_LEAGUES]'}")
    print(f"  season (resolved): {season}   via espn.resolve_season()")
    print(f"  scoreboard date: {on.strftime('%Y%m%d')}")
    print(f"  timeout        : {config.ESPN_TIMEOUT_SECONDS}s, "
          f"retries={config.ESPN_MAX_RETRIES}, backoff={config.ESPN_BACKOFF_SECONDS}s")
    print("  auth           : none required, none sent, none printed")

    summary: list[tuple[str, str]] = []

    standings_lines, standings_team = check_standings(league, season)
    summary += standings_lines

    scoreboard_lines, scoreboard_team = check_scoreboard(league, on)
    summary += scoreboard_lines

    # Prefer a team from the standings table (always a league member); fall back
    # to a team seen on the scoreboard.
    summary += check_team(league, standings_team or scoreboard_team)
    summary += check_league_avg(league, season)

    _heading("SUMMARY")
    for label, verdict in summary:
        print(f"  {label:<24}: {_clip(verdict, WIDTH - 28)}")
    print()
    print("  Reminder: manual diagnostic. Not a test, not collected by pytest,")
    print("  not run in CI. Re-run it when ESPN behaviour is in question.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
