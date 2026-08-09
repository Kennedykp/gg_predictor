"""
RESEARCH TOOLING - Epic 2A. NOT production code. NOT imported by the pipeline.

Second pass over the coverage audit. `audit_espn_history.py` finds WHERE the
counts disagree with the league format; this explains WHY, because the two have
opposite consequences for cold-start research:

  - a season genuinely missing matches is a data-depth limit
  - a season whose matches were merely filed under a neighbouring date window is
    a RETRIEVAL bug in our own code, and the data is fine

Guessing between those would be the worst outcome, so every anomaly class here is
resolved against the actual event list: kickoff months, status names, per-team
match counts, and league slugs.

Reuses the same on-disk cache as the coverage audit, so re-running costs nothing.
Read-only. GET only.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import ESPN_BASE_URL  # noqa: E402
from research.audit_espn_history import (  # noqa: E402
    CACHE_DIR,
    SCOREBOARD_LIMIT,
    BoundedFetcher,
    expected_matches,
)


def fetch_season(fetcher: BoundedFetcher, league: str, season: int) -> List[Dict[str, Any]]:
    """Raw event list for one league-season, via the production date window."""
    url = f"{ESPN_BASE_URL}/{league}/scoreboard"
    params = {"dates": f"{season}0701-{season + 1}0630", "limit": SCOREBOARD_LIMIT}
    payload, error = fetcher.get(url, params)
    if payload is None:
        print(f"  !! {league} {season}: {error}")
        return []
    return payload.get("events") or []


def month_histogram(events: List[Dict[str, Any]]) -> Dict[str, int]:
    hist: collections.Counter = collections.Counter()
    for event in events:
        date = event.get("date") or ""
        hist[date[:7]] += 1
    return dict(sorted(hist.items()))


def status_histogram(events: List[Dict[str, Any]]) -> Dict[str, int]:
    hist: collections.Counter = collections.Counter()
    for event in events:
        competition = (event.get("competitions") or [{}])[0]
        status_type = (competition.get("status") or {}).get("type") or {}
        hist[str(status_type.get("name"))] += 1
    return dict(hist)


def team_match_counts(events: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: collections.Counter = collections.Counter()
    for event in events:
        competition = (event.get("competitions") or [{}])[0]
        for competitor in competition.get("competitors") or []:
            team = competitor.get("team") or {}
            name = team.get("displayName") or str(competitor.get("id"))
            counts[name] += 1
    return dict(counts)


def describe(fetcher: BoundedFetcher, league: str, season: int) -> None:
    events = fetch_season(fetcher, league, season)
    exp = expected_matches(league, season)
    print(f"\n{'=' * 90}")
    print(f"{league} season={season}  raw_events={len(events)}  expected={exp}")
    print(f"{'=' * 90}")

    print("  kickoff months:")
    for month, count in month_histogram(events).items():
        print(f"    {month}: {count}")

    print(f"  status names: {status_histogram(events)}")

    slugs = collections.Counter((e.get("league") or {}).get("slug") for e in events)
    print(f"  league slugs: {dict(slugs)}")

    seasons = collections.Counter((e.get("season") or {}).get("year") for e in events)
    print(f"  per-event season.year: {dict(seasons)}")

    types = collections.Counter((e.get("season") or {}).get("slug") for e in events)
    print(f"  per-event season.slug: {dict(types)}")

    counts = team_match_counts(events)
    by_count: collections.Counter = collections.Counter(counts.values())
    print(f"  teams={len(counts)}  matches-per-team distribution: {dict(sorted(by_count.items()))}")
    outliers = {t: c for t, c in sorted(counts.items(), key=lambda kv: kv[1])
                if c != (2 * (len(counts) - 1))}
    if outliers and len(outliers) <= 12:
        print(f"  teams NOT playing the full round robin: {outliers}")
    elif outliers:
        print(f"  {len(outliers)} teams off the expected round-robin count "
              f"(showing 12): {dict(list(outliers.items())[:12])}")

    # Non-completed events, named individually - this is where abandoned and
    # never-played fixtures show up.
    unfinished = []
    for event in events:
        competition = (event.get("competitions") or [{}])[0]
        status_type = (competition.get("status") or {}).get("type") or {}
        if not status_type.get("completed"):
            names = [((c.get("team") or {}).get("abbreviation") or "?")
                     for c in (competition.get("competitors") or [])]
            unfinished.append((event.get("date"), status_type.get("name"), "v".join(names)))
    if unfinished:
        print(f"  NOT COMPLETED ({len(unfinished)}):")
        for date, name, teams in unfinished[:15]:
            print(f"    {date}  {name}  {teams}")


def earliest_season_probe(fetcher: BoundedFetcher, league: str,
                          candidates: List[int]) -> None:
    """Walk backwards to find where match-level retrieval stops being reliable."""
    print(f"\n{'=' * 90}")
    print(f"EARLIEST-SEASON PROBE: {league}")
    print(f"{'=' * 90}")
    print(f"  {'season':<8}{'raw':>6}{'exp':>6}{'teams':>7}{'scored':>8}  months")
    for season in candidates:
        events = fetch_season(fetcher, league, season)
        exp = expected_matches(league, season)
        teams = set()
        scored = 0
        for event in events:
            competition = (event.get("competitions") or [{}])[0]
            competitors = competition.get("competitors") or []
            for competitor in competitors:
                cid = competitor.get("id") or (competitor.get("team") or {}).get("id")
                if cid:
                    teams.add(str(cid))
            have = 0
            for competitor in competitors:
                score = competitor.get("score")
                raw = score.get("value") if isinstance(score, dict) else score
                if raw is not None and str(raw).strip() != "":
                    have += 1
            if have == 2:
                scored += 1
        months = month_histogram(events)
        span = f"{min(months)}..{max(months)}" if months else "-"
        print(f"  {season:<8}{len(events):>6}{(exp or 0):>6}{len(teams):>7}{scored:>8}  {span}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Epic 2A anomaly investigation")
    parser.add_argument("--detail", default="",
                        help="comma separated league:season pairs, e.g. eng.1:2019,eng.2:2021")
    parser.add_argument("--earliest", default="",
                        help="comma separated leagues for the earliest-season probe")
    parser.add_argument("--earliest-from", type=int, default=2001)
    parser.add_argument("--earliest-to", type=int, default=2014)
    args = parser.parse_args()

    fetcher = BoundedFetcher(CACHE_DIR)

    for pair in [p for p in args.detail.split(",") if p.strip()]:
        league, _, season = pair.partition(":")
        describe(fetcher, league.strip(), int(season))

    for league in [code for code in args.earliest.split(",") if code.strip()]:
        seasons = list(range(args.earliest_from, args.earliest_to + 1))
        earliest_season_probe(fetcher, league.strip(), seasons)

    print(f"\nnetwork requests made: {fetcher.requests_made} (cache hits: {fetcher.cache_hits})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
