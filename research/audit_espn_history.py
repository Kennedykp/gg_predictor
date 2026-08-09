"""
RESEARCH TOOLING - Epic 2A. NOT production code. NOT imported by the pipeline.

Audits how far back ESPN's match-level history actually goes, per league, so the
cold-start research in docs/EPIC_2A_COLD_START_RESEARCH.md rests on measured
coverage rather than on the assumption that a season parameter "works" because
the server answered 200.

SAFETY CONTRACT
---------------
- READ-ONLY. Only HTTP GET. Nothing here writes to any ESPN endpoint.
- Not imported by production code; it imports FROM the repo, never the reverse.
- Bounded: one request per (league, season), a fixed inter-request delay, and a
  hard cap on total requests.
- Cached to disk. A re-run of the same audit costs ZERO requests, so iterating on
  the analysis does not repeatedly hit a free public endpoint.

WHY IT DOES NOT USE espn.get_league_match_records()
---------------------------------------------------
That function is the production path and answers a production question: "give me
usable records, or None". This audit needs the opposite - the things production
correctly discards. A season that returns 380 events of which 60 lack scores is
reported by production as 320 clean records; here it must surface as a coverage
defect. So this reads the raw payload and applies the SAME parsers
(`espn.parse_scoreboard_events`) alongside its own raw counts, which also makes
the two directly comparable: raw events vs what the real adapter accepts.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from config import ALLOWED_LEAGUES, ESPN_BASE_URL, PHASE_2_LEAGUES  # noqa: E402
from espn import parse_scoreboard_events  # noqa: E402

CACHE_DIR = REPO_ROOT / "research" / ".cache"
REQUEST_DELAY_SECONDS = 1.0
MAX_REQUESTS = 400
TIMEOUT_SECONDS = 30

# NO User-Agent override. Measured 2026-08-09 against
# `eng.1/scoreboard?dates=20250816`:
#
#     requests default UA                  -> HTTP 200, 53764 bytes
#     "gg-predictor-research/1.0 (...)"    -> HTTP 403, 445 bytes
#     "curl/8.7.1"                         -> HTTP 200, 53764 bytes
#     a Chrome-like UA string              -> HTTP 403, 445 bytes
#
# So ESPN edge-filters on User-Agent, and a browser-like string is rejected
# harder than the library default. Production (`espn._fetch`) sends the plain
# `requests` default and is accepted, so this audit sends exactly the same thing.
# Spoofing a browser would be both dishonest about what this client is and, per
# the measurement above, counter-productive. This is also a live finding worth
# recording: a future dependency bump that changes the default UA could turn
# every production ESPN call into a 403.

# ESPN's scoreboard truncates silently; production caps at 1000 for the same
# reason. Kept identical so a truncation here means a truncation there.
SCOREBOARD_LIMIT = 1000

# Known league formats, used ONLY as a sanity check to flag anomalies for human
# review - never to correct or filter the data. Formats change (Serie A and La
# Liga were 18-team competitions historically; Ligue 1 cut to 18 teams in
# 2023-24; the Bundesliga has been 18 teams throughout), so an expectation that
# does not match is a prompt to investigate, not proof of a defect.
# (first_season, last_season_inclusive_or_None, team_count). Annotated explicitly:
# without it mypy joins the None-ended and int-ended tuple variants to `object`.
TEAMS_BY_LEAGUE_SEASON: Dict[str, List[Tuple[int, Optional[int], int]]] = {
    "eng.1": [(1995, None, 20)],
    "esp.1": [(1997, None, 20)],
    "ita.1": [(2004, None, 20)],
    "ger.1": [(1992, None, 18)],
    "fra.1": [(2002, 2022, 20), (2023, None, 18)],
    "eng.2": [(1995, None, 24)],
    "ger.2": [(1992, None, 18)],
}


def expected_matches(league: str, season: int) -> Optional[int]:
    """Double round robin match count, or None when the format is unknown."""
    for start, end, teams in TEAMS_BY_LEAGUE_SEASON.get(league, []):
        if season >= start and (end is None or season <= end):
            return teams * (teams - 1)
    return None


@dataclass
class SeasonAudit:
    """Everything measured about one (league, season) payload."""

    league: str
    season: int
    http_ok: bool = False
    error: Optional[str] = None
    raw_events: int = 0
    declared_season_years: Dict[Any, int] = field(default_factory=dict)
    payload_season_year: Optional[int] = None
    event_league_slugs: Dict[Any, int] = field(default_factory=dict)
    duplicate_event_ids: int = 0
    completed_events: int = 0
    missing_scores: int = 0
    missing_kickoff: int = 0
    distinct_team_ids: int = 0
    distinct_teams_named: int = 0
    valid_match_records: int = 0
    total_goals: int = 0
    first_kickoff: Optional[str] = None
    last_kickoff: Optional[str] = None
    truncated: bool = False
    status_names: Dict[Any, int] = field(default_factory=dict)

    @property
    def expected(self) -> Optional[int]:
        return expected_matches(self.league, self.season)

    @property
    def coverage(self) -> Optional[float]:
        exp = self.expected
        if not exp:
            return None
        return self.valid_match_records / exp

    @property
    def goals_per_team_per_match(self) -> Optional[float]:
        if self.valid_match_records == 0:
            return None
        return self.total_goals / (2 * self.valid_match_records)

    @property
    def verdict(self) -> str:
        """Human-facing judgement. Deliberately conservative."""
        if not self.http_ok:
            return "UNUSABLE (request failed)"
        if self.raw_events == 0:
            return "NO DATA"
        if self.truncated:
            return "UNUSABLE (truncated)"
        if self.duplicate_event_ids:
            return "SUSPECT (duplicate ids)"
        cov = self.coverage
        if cov is None:
            return "UNKNOWN FORMAT (manual review)"
        if cov >= 0.999:
            return "COMPLETE"
        if cov >= 0.97:
            return "NEAR-COMPLETE"
        if cov >= 0.5:
            return "PARTIAL"
        return "UNUSABLE (sparse)"


class BoundedFetcher:
    """GET with a hard request cap, a fixed delay, and a disk cache."""

    def __init__(self, cache_dir: Path, max_requests: int = MAX_REQUESTS,
                 delay: float = REQUEST_DELAY_SECONDS, refresh: bool = False) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.delay = delay
        self.refresh = refresh
        self.requests_made = 0
        self.cache_hits = 0
        self.session = requests.Session()

    def _cache_path(self, url: str, params: Dict[str, Any]) -> Path:
        key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def get(self, url: str, params: Dict[str, Any]) -> Tuple[Optional[dict], Optional[str]]:
        path = self._cache_path(url, params)
        if path.exists() and not self.refresh:
            self.cache_hits += 1
            try:
                cached = json.loads(path.read_text())
            except ValueError:
                path.unlink()
            else:
                return cached.get("payload"), cached.get("error")

        if self.requests_made >= self.max_requests:
            return None, f"request cap {self.max_requests} reached"

        time.sleep(self.delay)
        self.requests_made += 1
        try:
            response = self.session.get(url, params=params, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            return None, f"transport: {exc}"

        if response.status_code != 200:
            error = f"HTTP {response.status_code}"
            path.write_text(json.dumps({"payload": None, "error": error}))
            return None, error
        try:
            payload = response.json()
        except ValueError as exc:
            return None, f"malformed JSON: {exc}"

        # Retrieval provenance, so a snapshot can later be dated (Phase 23).
        path.write_text(json.dumps({
            "payload": payload,
            "error": None,
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "url": url,
            "params": params,
        }))
        return payload, None


def audit_season(fetcher: BoundedFetcher, league: str, season: int) -> SeasonAudit:
    """Measure one league-season of ESPN scoreboard data."""
    audit = SeasonAudit(league=league, season=season)
    url = f"{ESPN_BASE_URL}/{league}/scoreboard"
    # Same window production uses: July..June, so it cannot bleed into a
    # neighbouring season.
    params = {"dates": f"{season}0701-{season + 1}0630", "limit": SCOREBOARD_LIMIT}

    payload, error = fetcher.get(url, params)
    if payload is None:
        audit.error = error
        return audit

    audit.http_ok = True
    events = payload.get("events") or []
    audit.raw_events = len(events)
    audit.truncated = len(events) >= SCOREBOARD_LIMIT

    leagues_block = payload.get("leagues") or []
    if leagues_block:
        audit.payload_season_year = (leagues_block[0].get("season") or {}).get("year")

    slugs: collections.Counter = collections.Counter()
    season_years: collections.Counter = collections.Counter()
    status_names: collections.Counter = collections.Counter()
    seen_ids: set = set()
    team_ids: set = set()
    team_names: set = set()
    kickoffs: List[str] = []

    for event in events:
        slugs[(event.get("league") or {}).get("slug")] += 1
        season_years[(event.get("season") or {}).get("year")] += 1

        event_id = event.get("id")
        if event_id is not None:
            if event_id in seen_ids:
                audit.duplicate_event_ids += 1
            seen_ids.add(event_id)

        competition = (event.get("competitions") or [{}])[0]
        status_type = (competition.get("status") or {}).get("type") or {}
        status_names[status_type.get("name")] += 1
        if status_type.get("completed"):
            audit.completed_events += 1

        date = event.get("date") or competition.get("date")
        if date:
            kickoffs.append(date)
        else:
            audit.missing_kickoff += 1

        scored = 0
        for competitor in competition.get("competitors") or []:
            cid = competitor.get("id") or (competitor.get("team") or {}).get("id")
            if cid is not None:
                team_ids.add(str(cid))
            team = competitor.get("team") or {}
            name = team.get("displayName") or competitor.get("displayName")
            if name:
                team_names.add(name)
            score = competitor.get("score")
            raw = score.get("value") if isinstance(score, dict) else score
            if raw is not None and str(raw).strip() != "":
                scored += 1
        if status_type.get("completed") and scored < 2:
            audit.missing_scores += 1

    audit.event_league_slugs = dict(slugs)
    audit.declared_season_years = dict(season_years)
    audit.status_names = dict(status_names)
    audit.distinct_team_ids = len(team_ids)
    audit.distinct_teams_named = len(team_names)
    if kickoffs:
        audit.first_kickoff = min(kickoffs)
        audit.last_kickoff = max(kickoffs)

    # The production adapter's own verdict, for an apples-to-apples comparison.
    records = parse_scoreboard_events(payload, league)
    audit.valid_match_records = len(records)
    audit.total_goals = sum((r.goals_for or 0) + (r.goals_against or 0) for r in records)

    return audit


def audit_team_continuity(fetcher: BoundedFetcher, league: str,
                          seasons: List[int]) -> Dict[str, Any]:
    """
    Track team id -> name across seasons for one league.

    Identity must survive a season boundary by ID, because names are not stable
    ("Spurs" / "Tottenham Hotspur") and a name-keyed prior would silently reset a
    club's history the year ESPN adjusts its display string.
    """
    by_id: Dict[str, set] = collections.defaultdict(set)
    seasons_by_id: Dict[str, set] = collections.defaultdict(set)
    name_to_ids: Dict[str, set] = collections.defaultdict(set)

    for season in seasons:
        url = f"{ESPN_BASE_URL}/{league}/scoreboard"
        params = {"dates": f"{season}0701-{season + 1}0630", "limit": SCOREBOARD_LIMIT}
        payload, _ = fetcher.get(url, params)
        if payload is None:
            continue
        for event in payload.get("events") or []:
            competition = (event.get("competitions") or [{}])[0]
            for competitor in competition.get("competitors") or []:
                cid = competitor.get("id") or (competitor.get("team") or {}).get("id")
                if cid is None:
                    continue
                team = competitor.get("team") or {}
                name = team.get("displayName") or "?"
                by_id[str(cid)].add(name)
                seasons_by_id[str(cid)].add(season)
                name_to_ids[name].add(str(cid))

    return {
        "league": league,
        "seasons": seasons,
        "distinct_ids": len(by_id),
        "ids_with_multiple_names": {k: sorted(v) for k, v in by_id.items() if len(v) > 1},
        "names_with_multiple_ids": {k: sorted(v) for k, v in name_to_ids.items() if len(v) > 1},
        "id_season_span": {k: sorted(v) for k, v in seasons_by_id.items()},
    }


def print_coverage_table(audits: List[SeasonAudit]) -> None:
    header = (f"{'League':<8} {'Season':<7} {'Exp':>5} {'Raw':>5} {'Compl':>6} "
              f"{'Valid':>6} {'Cov':>7} {'Teams':>6} {'G/T/M':>6}  Verdict")
    print(header)
    print("-" * len(header))
    for a in audits:
        exp = a.expected
        cov = a.coverage
        gpm = a.goals_per_team_per_match
        print(f"{a.league:<8} {a.season:<7} "
              f"{(exp if exp else '?'):>5} {a.raw_events:>5} {a.completed_events:>6} "
              f"{a.valid_match_records:>6} "
              f"{(f'{cov:.1%}' if cov is not None else '?'):>7} "
              f"{a.distinct_team_ids:>6} "
              f"{(f'{gpm:.3f}' if gpm else '?'):>6}  {a.verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Epic 2A read-only ESPN coverage audit")
    parser.add_argument("--leagues", default="", help="comma separated; default = configured")
    parser.add_argument("--from-season", type=int, default=2014)
    parser.add_argument("--to-season", type=int, default=2025)
    parser.add_argument("--refresh", action="store_true", help="ignore cache and refetch")
    parser.add_argument("--continuity", action="store_true", help="run team-id continuity audit")
    parser.add_argument("--json-out", default="", help="write raw audit results to this path")
    args = parser.parse_args()

    if args.leagues:
        leagues = [code.strip() for code in args.leagues.split(",") if code.strip()]
    else:
        leagues = list(ALLOWED_LEAGUES) + list(PHASE_2_LEAGUES)

    seasons = list(range(args.from_season, args.to_season + 1))
    fetcher = BoundedFetcher(CACHE_DIR, refresh=args.refresh)

    print("=" * 100)
    print("EPIC 2A - ESPN HISTORICAL COVERAGE AUDIT (read-only research tooling)")
    print(f"leagues={leagues}")
    print(f"seasons={seasons[0]}..{seasons[-1]}  ({len(leagues) * len(seasons)} league-seasons)")
    print("=" * 100)

    audits: List[SeasonAudit] = []
    for league in leagues:
        for season in seasons:
            audits.append(audit_season(fetcher, league, season))
        print(f"[done] {league}")

    print()
    print_coverage_table(audits)

    print()
    print("ANOMALIES")
    print("-" * 100)
    any_anomaly = False
    for a in audits:
        problems = []
        if a.error:
            problems.append(f"error={a.error}")
        if a.truncated:
            problems.append("TRUNCATED at limit")
        if a.duplicate_event_ids:
            problems.append(f"{a.duplicate_event_ids} duplicate event ids")
        if a.missing_scores:
            problems.append(f"{a.missing_scores} completed events missing a score")
        if a.missing_kickoff:
            problems.append(f"{a.missing_kickoff} events missing kickoff")
        foreign = {s: n for s, n in a.event_league_slugs.items() if s and s != a.league}
        if foreign:
            problems.append(f"foreign competitions: {foreign}")
        exp = a.expected
        if exp and a.raw_events > exp:
            problems.append(f"raw events {a.raw_events} EXCEEDS expected {exp}")
        if exp and 0 < a.valid_match_records < exp:
            problems.append(f"valid {a.valid_match_records} < expected {exp}")
        if problems:
            any_anomaly = True
            print(f"  {a.league} {a.season}: " + "; ".join(problems))
    if not any_anomaly:
        print("  none")

    if args.continuity:
        print()
        print("TEAM ID CONTINUITY")
        print("-" * 100)
        for league in leagues:
            result = audit_team_continuity(fetcher, league, seasons)
            print(f"\n{league}: {result['distinct_ids']} distinct team ids across "
                  f"{len(seasons)} seasons")
            renamed = result["ids_with_multiple_names"]
            print(f"  ids whose display name changed: {len(renamed)}")
            for tid, names in list(renamed.items())[:10]:
                print(f"    id={tid}: {names}")
            collisions = result["names_with_multiple_ids"]
            print(f"  names mapping to >1 id: {len(collisions)}")
            for name, ids in list(collisions.items())[:10]:
                print(f"    {name!r}: {ids}")
            spans = result["id_season_span"]
            ever_present = [t for t, s in spans.items() if len(s) == len(seasons)]
            print(f"  ids present in ALL {len(seasons)} seasons: {len(ever_present)}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([a.__dict__ for a in audits], indent=2, default=str))
        print(f"\nwrote {out}")

    print()
    print(f"network requests made: {fetcher.requests_made}  (cache hits: {fetcher.cache_hits})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
