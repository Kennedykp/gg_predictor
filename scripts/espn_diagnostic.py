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
from typing import Any, Dict
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
from domain.match_records import (  # noqa: E402  - as above
    Venue,
    derive_history,
)

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
        home: Dict[str, Any] = next(
            (c for c in competitors if c.get("homeAway") == "home"), {}
        )
        away: Dict[str, Any] = next(
            (c for c in competitors if c.get("homeAway") == "away"), {}
        )

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
# 5. Filter statistics (Epic 1B.3, TASK 21)
# ---------------------------------------------------------------------------
def check_filter_stats(league: str, team_ref: dict | None) -> list[tuple[str, str]]:
    """
    Report which GG hard-filter statistics ESPN can actually supply.

    This answers TASK 4 against the live API rather than from documentation:
    for each statistic, is it DIRECT (ESPN states it), DERIVED (computed exactly
    from genuine ESPN data) or UNAVAILABLE (cannot be obtained honestly)?

    One successful response proves a field exists for ONE team in ONE league at
    ONE moment - not that it is universally available. The sample size is printed
    alongside each value so a rate computed from two matches is not mistaken for
    a settled one.
    """
    _heading("5. GG filter statistics - what can ESPN actually supply?")

    if not team_ref:
        print("    SKIPPED: no team reference available from earlier sections.")
        print()
        return [("filter statistics", "SKIPPED - no team available")]

    team_id = str(team_ref.get("id", ""))
    team_name = team_ref.get("displayName") or team_ref.get("name") or f"team {team_id}"

    stats = espn.get_team_stats(team_id, league)
    if stats is None:
        print(f"    {team_name}: provider returned None - no usable record.")
        print()
        return [("filter statistics", "UNAVAILABLE - provider returned None")]

    matches = stats.get("matches_played")
    home_matches = stats.get("home_matches")
    away_matches = stats.get("away_matches")

    print(f"    team: {team_name}  (id={team_id}, league={league})")
    print(f"    sample: {matches} matches played "
          f"({home_matches} home, {away_matches} away)")
    print()
    print(f"    {'STATISTIC':<26} {'SOURCE':<12} {'VALUE':<12} SAMPLE")
    print(f"    {'-' * 26} {'-' * 12} {'-' * 12} {'-' * 12}")

    def _row(label: str, source: str, value: Any, sample: Any) -> None:
        shown = "UNAVAILABLE" if value is None else (
            f"{value:.4f}" if isinstance(value, float) else str(value)
        )
        sample_shown = "-" if sample is None else f"{sample} matches"
        print(f"    {label:<26} {source:<12} {shown:<12} {sample_shown}")

    # Goals scored/conceded: ESPN gives season TOTALS and the match counts, so
    # the per-match rate is an exact division - genuinely DERIVED.
    _row("home_avg_goals_scored", "DERIVED", stats.get("home_goals_scored"), home_matches)
    _row("away_avg_goals_scored", "DERIVED", stats.get("away_goals_scored"), away_matches)
    _row("home_goals_conceded", "DERIVED", stats.get("home_goals_conceded"), home_matches)
    _row("away_goals_conceded", "DERIVED", stats.get("away_goals_conceded"), away_matches)

    # Clean sheets: the standings record carries aggregate goals-against only.
    # GA=5 over 5 matches is consistent with 0 clean sheets or with 4, so no
    # exact derivation exists from this endpoint.
    _row("home_clean_sheet_pct", "UNAVAILABLE", stats.get("home_clean_sheet_pct"), home_matches)
    _row("away_clean_sheet_pct", "UNAVAILABLE", stats.get("away_clean_sheet_pct"), away_matches)

    # BTTS history needs per-match scorelines, which this endpoint never returns.
    _row("both_scored_pct", "UNAVAILABLE", None, None)

    # Games played IS stated by ESPN, so it is DIRECT rather than derived.
    _row("matches_played", "DIRECT", matches, None)

    print()
    print("    DIRECT      = ESPN states the statistic itself")
    print("    DERIVED     = computed exactly from genuine ESPN totals + match counts")
    print("    UNAVAILABLE = cannot be obtained honestly from this endpoint")
    print()
    print("    Clean-sheet and BTTS rates need MATCH-LEVEL results. The standings")
    print("    record is aggregate-only: goals-against does not determine how many")
    print("    matches ended with zero conceded. Approximating it would be exactly")
    print("    the fabrication Epic 1B.3 removed, so both are reported UNAVAILABLE")
    print("    and block a recommendation rather than silently passing the filter.")
    print()

    unavailable = sum(
        1 for v in (stats.get("home_clean_sheet_pct"), stats.get("away_clean_sheet_pct")) if v is None
    )
    verdict = (
        f"goals rates DERIVED ok; {unavailable + 1} filter stats UNAVAILABLE "
        "(clean sheets, BTTS history)"
    )
    return [("filter statistics", verdict)]


# ---------------------------------------------------------------------------
# 6. Team schedule - the Epic 1B.4 match-level feed
# ---------------------------------------------------------------------------
def check_schedule(league: str, team_ref: dict | None) -> list[tuple[str, str]]:
    """
    The schedule endpoint and the statistics derived from it (Epic 1B.4).

    Prints the COUNTS that make a derived percentage auditable - events
    returned, how many survived each filter, and the resulting rates with their
    denominators. A clean-sheet figure with no visible n is not something anyone
    can check. Payloads are never dumped (TASK 24).
    """
    _heading("6. TEAM SCHEDULE  /{league}/teams/{id}/schedule   [Epic 1B.4]")

    if team_ref is None:
        print("  SKIPPED: no team reference available from the earlier checks.")
        return [("schedule", "SKIPPED - no team id")]

    team_id = str(team_ref.get("id"))
    team_name = team_ref.get("displayName") or team_ref.get("name") or f"team {team_id}"
    season = espn.resolve_season(league)
    url = f"{config.ESPN_BASE_URL}/{league}/teams/{team_id}/schedule"

    p = probe(url, {"season": season})
    if p.failed or not isinstance(p.payload, dict):
        report("schedule", p, False, "no JSON object returned", "FAILED")
        return [("schedule", "FAILED - no usable response")]

    events = p.payload.get("events") or []
    report(
        "schedule",
        p,
        bool(events),
        f"{len(events)} events for {team_name}",
        "OK" if events else "EMPTY - no events returned",
    )

    # Status census. An unrecognised status shows up here as a number rather
    # than disappearing silently into the excluded bucket.
    statuses: Counter[str] = Counter()
    for event in events:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        type_block = ((competitions[0].get("status") or {}).get("type")) or {}
        statuses[type_block.get("name", "?")] += 1

    print("  status census:")
    for name, count in statuses.most_common():
        print(f"    {name:<30}: {count}")

    # Competition census (TASK 3). More than one slug here means the endpoint is
    # NOT competition-pure and the adapter's filter is what protects the number.
    competitions_seen: Counter[str] = Counter()
    for event in events:
        competitions_seen[((event.get("league") or {}).get("slug")) or "UNKNOWN"] += 1

    print(f"  competitions present: {len(competitions_seen)}")
    for slug, count in competitions_seen.most_common():
        flag = "" if slug == league else "   <-- NOT the requested league"
        print(f"    {slug:<30}: {count}{flag}")

    records = espn.parse_schedule_events(p.payload, team_id, league)
    in_league = [r for r in records if r.competition == league]
    home_records = [r for r in in_league if r.venue == Venue.HOME]
    away_records = [r for r in in_league if r.venue == Venue.AWAY]

    print(f"  parsed MatchRecords : {len(records)}")
    print(f"    same competition  : {len(in_league)}")
    print(f"    home              : {len(home_records)}")
    print(f"    away              : {len(away_records)}")

    # Cutoff = now, so every figure below is point-in-time correct as of this
    # moment - the same rule the pipeline applies per fixture.
    cutoff = datetime.now(timezone.utc)
    before = [r for r in in_league if r.kickoff and r.kickoff < cutoff]
    print(f"  cutoff (now, UTC)   : {cutoff.isoformat(timespec='seconds')}")
    print(f"    before cutoff     : {len(before)}")

    home_history = derive_history(
        records, target_kickoff=cutoff, venue=Venue.HOME, competition=league
    )
    away_history = derive_history(
        records, target_kickoff=cutoff, venue=Venue.AWAY, competition=league
    )

    print()
    print("  DERIVED  (None = UNAVAILABLE, which is not the same as 0.0)")
    for label, history in (("HOME", home_history), ("AWAY", away_history)):
        clean_sheet = history.clean_sheet_pct
        btts = history.both_teams_scored_pct
        print(
            f"    {label} clean-sheet % : "
            f"{'unavailable' if clean_sheet is None else format(clean_sheet, '.3f')}"
            f"   (n={history.sample_size})"
        )
        print(
            f"    {label} BTTS %        : "
            f"{'unavailable' if btts is None else format(btts, '.3f')}"
            f"   (n={history.sample_size})"
        )

    if home_history.sample_size == 0 and away_history.sample_size == 0:
        print()
        print("  NOTE: zero eligible matches. Early in a season this is the")
        print("  EXPECTED state, not a fault. The clean-sheet filter stays")
        print("  unavailable and blocks a recommendation rather than reading 0.")

    mixed = len(competitions_seen) > 1
    return [
        ("schedule events", str(len(events))),
        ("schedule records", f"{len(records)} parsed / {len(in_league)} in-league"),
        ("competition purity", "MIXED - filter required" if mixed else f"PURE ({league})"),
        (
            "derived home CS%",
            "unavailable" if home_history.clean_sheet_pct is None
            else f"{home_history.clean_sheet_pct:.3f} (n={home_history.sample_size})",
        ),
        (
            "derived away CS%",
            "unavailable" if away_history.clean_sheet_pct is None
            else f"{away_history.clean_sheet_pct:.3f} (n={away_history.sample_size})",
        ),
    ]


def check_schedule_season(league: str, team_ref: dict | None) -> list[tuple[str, str]]:
    """
    Does the schedule endpoint honour `?season=` (TASK 4, GG-024)?

    Epic 1B.2 established that the team STATISTICS endpoint ignores it. This
    asks the same question of the schedule endpoint and answers it by COMPARING
    RETURNED EVENT IDS - a 200 response proves the parameter was accepted, not
    that it did anything.
    """
    _heading("7. SCHEDULE season= BEHAVIOUR   [Epic 1B.4 / GG-024]")

    if team_ref is None:
        print("  SKIPPED: no team reference available.")
        return [("schedule season=", "SKIPPED")]

    team_id = str(team_ref.get("id"))
    current = espn.resolve_season(league)
    previous = current - 1
    url = f"{config.ESPN_BASE_URL}/{league}/teams/{team_id}/schedule"

    current_probe = probe(url, {"season": current})
    previous_probe = probe(url, {"season": previous})

    if not isinstance(current_probe.payload, dict) or not isinstance(
        previous_probe.payload, dict
    ):
        print("  One or both requests returned no JSON object.")
        return [("schedule season=", "FAILED")]

    current_ids = {e.get("id") for e in (current_probe.payload.get("events") or [])}
    previous_ids = {e.get("id") for e in (previous_probe.payload.get("events") or [])}
    overlap = current_ids & previous_ids

    print(f"  season={current} : {len(current_ids)} events")
    print(f"  season={previous} : {len(previous_ids)} events")
    print(f"  shared event ids : {len(overlap)}")

    if current_ids and current_ids == previous_ids:
        verdict = "IGNORED - identical event ids for both seasons"
    elif not previous_ids:
        verdict = f"NO DATA returned for season={previous}"
    elif overlap:
        verdict = f"PARTIAL - {len(overlap)} shared event ids"
    else:
        verdict = "HONOURED - disjoint event sets"

    print(f"  VERDICT          : {verdict}")
    print()
    print("  Even a HONOURED result does NOT make historical backtesting safe.")
    print("  The team STATISTICS endpoint is still current-season-only, so")
    print("  POISSON_V1's inputs would remain present-day values applied to a")
    print("  past fixture. GG-024 and LEAK-001 both stay OPEN. See")
    print("  docs/EPIC_1B4_MATCH_HISTORY.md.")

    return [("schedule season=", verdict)]


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
    team_ref = standings_team or scoreboard_team
    summary += check_team(league, team_ref)
    summary += check_league_avg(league, season)
    summary += check_filter_stats(league, team_ref)
    summary += check_schedule(league, team_ref)
    summary += check_schedule_season(league, team_ref)

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
