"""
RESEARCH TOOLING - Epic 2B.2. NOT production code. NOT imported by the pipeline.

Bounded historical coverage audit of the NEW dataset path.

Replays payloads already cached on disk by the Epic 2A audit
(`research/.cache`) through the production-consistent parser
(`espn.parse_scoreboard_history`), so the audit costs ZERO network requests when
the cache is warm. With `--live` it fetches the small number of league-seasons
that are not cached.

What this reports, per league-season:

    raw            events ESPN actually returned
    accepted       records the dataset path admits (season + competition +
                   result all validated)
    rejected       events refused with a reason, broken down by reason
    eligible       accepted records a league model may learn from
    teams          distinct team ids in the accepted records
    first/last     kickoff span of the accepted records
    expected       double round-robin count for that league-season, as a sanity
                   check only - a mismatch is reported, never "fixed"

The point of the audit is to PROVE the integrity layer held while the data
moved through it: the four boundary seasons (2018/19, 2019/20, 2020/21 and one
normal recent season) must survive intact, including the July 2020 COVID
extension, and the 2020/21 discovery window must not admit any 2019/20 events.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import espn  # noqa: E402

CACHE_DIR = REPO_ROOT / "research" / ".cache"
TIMEOUT_SECONDS = 30
DELAY_SECONDS = 1.0
MAX_LIVE_REQUESTS = 40

# The targeted audit scope. Four boundary seasons per league:
#   previous season      2018/19 - normal season, pre-COVID
#   COVID season         2019/20 - extends past June 30, 2020
#   next season          2020/21 - must not absorb the tail of 2019/20
#   a recent normal      2023/24 - sanity check that nothing broke recently
FOCUS_SEASONS = [2018, 2019, 2020, 2023]

# Same format table as the 2A audit: a sanity check only, never a correction.
TEAMS_BY_LEAGUE_SEASON: Dict[str, List[Tuple[int, Optional[int], int]]] = {
    "eng.1": [(1995, None, 20)],
    "esp.1": [(1997, None, 20)],
    "ita.1": [(2004, None, 20)],
    "ger.1": [(1992, None, 18)],
    "fra.1": [(2002, 2022, 20), (2023, None, 18)],
}


def expected_matches(league: str, season: int) -> Optional[int]:
    for start, end, teams in TEAMS_BY_LEAGUE_SEASON.get(league, []):
        if season >= start and (end is None or season <= end):
            return teams * (teams - 1)
    return None


def cache_path(url: str, params: Dict[str, Any]) -> Path:
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{digest}.json"


def league_season_from_params(url: str, params: Dict[str, Any]) -> Tuple[str, int]:
    league = url.rstrip("/").split("/")[-1]
    dates = params.get("dates", "")
    season = int(dates[:4])
    return league, season


def load_cached(league: str, window: str) -> Optional[dict]:
    url = f"{espn.ESPN_BASE_URL}/{league}/scoreboard"
    params = {"dates": window, "limit": 1000}
    path = cache_path(url, params)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text())
    except ValueError:
        return None
    return entry.get("payload")


def live_fetch(league: str, window: str) -> Optional[dict]:
    """Bounded live fallback. Only used with --live, never by CI."""
    url = f"{espn.ESPN_BASE_URL}/{league}/scoreboard"
    params = {"dates": window, "limit": 1000}

    try:
        result = espn._fetch(url, params)
    except Exception as exc:  # pragma: no cover - transport boundary
        print(f"    [live] {league} {window}: {exc}")
        return None
    if not result.ok:
        return None
    payload = result.data
    if payload is None:
        return None

    path = cache_path(url, params)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "payload": payload,
                "error": None,
                "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "url": url,
                "params": params,
            }
        )
    )
    return payload


def _blank_row(league: str, season: int, source: str) -> Dict[str, Any]:
    return {
        "league": league,
        "season": season,
        "source": source,
        "raw": 0,
        "accepted": 0,
        "eligible": 0,
        "rejected": {},
        "teams": 0,
        "first": None,
        "last": None,
        "windows": 0,
        "expected": expected_matches(league, season),
    }


def audit_league_season(league: str, season: int, live: bool) -> Dict[str, Any]:
    """
    Replay EVERY discovery window production would query, then merge.

    This mirrors `espn.get_league_history` deliberately, including the
    de-duplication on event id at the window seam. An audit that read only the
    primary July-June window would report the pre-2B.1 truncation as though it
    were the current behaviour - eng.1 2019/20 would show 314 rather than the
    full 380, and the July 2020 restart would look like data loss instead of the
    thing the fix recovers.
    """
    windows = espn._season_discovery_windows(season)
    matches: List[Any] = []
    rejected: Dict[str, int] = {}
    seen_event_ids: set = set()
    raw = 0
    windows_read = 0
    source = "cache"

    for window in windows:
        payload = load_cached(league, window)
        if payload is None:
            if not live:
                continue
            source = "live"
            time.sleep(DELAY_SECONDS)
            payload = live_fetch(league, window)
        if payload is None:
            continue

        windows_read += 1
        raw += len(payload.get("events") or [])
        readout = espn.parse_scoreboard_history(payload, league, season)
        for reason, count in readout.rejected.items():
            rejected[reason] = rejected.get(reason, 0) + count
        for match in readout.matches:
            if match.event_id in seen_event_ids:
                continue
            seen_event_ids.add(match.event_id)
            matches.append(match)

    if windows_read == 0:
        return _blank_row(league, season, "MISSING" if not live else "FAILED")

    team_ids: set = set()
    for match in matches:
        team_ids.add(match.home_team_id)
        team_ids.add(match.away_team_id)
    first = min((m.kickoff for m in matches), default=None)
    last = max((m.kickoff for m in matches), default=None)

    return {
        "league": league,
        "season": season,
        "source": f"{source}/{windows_read}w",
        "raw": raw,
        "accepted": len(matches),
        "eligible": sum(
            1 for m in matches if m.eligibility.verdict.value == "ELIGIBLE" and m.has_result
        ),
        "rejected": dict(collections.Counter(rejected)),
        "teams": len(team_ids),
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "windows": windows_read,
        "expected": expected_matches(league, season),
    }



def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded historical dataset coverage audit")
    parser.add_argument("--leagues", default="eng.1,ger.1,ita.1,esp.1,fra.1")
    parser.add_argument("--seasons", default=",".join(str(s) for s in FOCUS_SEASONS))
    parser.add_argument("--live", action="store_true", help="fetch league-seasons missing from cache")
    args = parser.parse_args()

    leagues = [code.strip() for code in args.leagues.split(",") if code.strip()]
    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]
    live_requests = 0

    print("=" * 108)
    print("EPIC 2B.2 - HISTORICAL DATASET COVERAGE AUDIT (read-only)")
    print(f"leagues={leagues} seasons={seasons} live={'yes' if args.live else 'no (cache only)'}")
    print("=" * 108)
    header = (f"{'League':<7} {'Season':<7} {'Src':<7} {'Raw':>5} {'Acc':>5} "
              f"{'Elig':>5} {'Teams':>5} {'Exp':>4} {'First kickoff':<21} {'Last kickoff':<21} Rejected")
    print(header)
    print("-" * len(header))

    rows: List[Dict[str, Any]] = []
    for league in leagues:
        for season in seasons:
            row = audit_league_season(league, season, args.live)
            if str(row.get("source", "")).startswith("live"):
                live_requests += int(row.get("windows") or 1)
            rows.append(row)
            if live_requests >= MAX_LIVE_REQUESTS:
                print(f"    [abort] live request cap ({MAX_LIVE_REQUESTS}) reached")
                break
        if live_requests >= MAX_LIVE_REQUESTS:
            break


    print_rows(rows)

    # Two passes, because they answer different questions.
    #
    # CONFIRMATIONS are the things the integrity layer was built to do, observed
    # actually happening. WRONG_SEASON rejections belong here and NOT in the
    # anomaly list: the second discovery window is the following season's
    # window, so every event in it that ESPN attributes elsewhere SHOULD be
    # refused. A run with zero such rejections would be the surprise.
    print()
    print("CONFIRMATIONS (the guard doing its job)")
    print("-" * 108)
    for row in rows:
        league, season = row["league"], row["season"]
        if str(row["source"]).startswith(("MISSING", "FAILED")):
            continue
        wrong = row["rejected"].get("WRONG_SEASON", 0)
        if wrong:
            print(
                f"  {league} {season}: {wrong} event(s) from the discovery windows refused "
                f"as belonging to another season; {row['accepted']} admitted"
            )
        if season == 2019 and (row["last"] or "") >= "2020-07-01":
            print(
                f"  {league} 2019/20: COVID tail PRESERVED - last kickoff {row['last']} "
                "(ESPN attributes it to 2019/20; a June 30 cutoff would have dropped it)"
            )

    print()
    print("ANOMALIES (needing human judgement)")
    print("-" * 108)
    problems: List[str] = []
    for row in rows:
        league, season = row["league"], row["season"]
        accepted, raw, exp = row["accepted"], row["raw"], row.get("expected")

        if str(row["source"]).startswith(("MISSING", "FAILED")):
            problems.append(f"{league} {season}: {row['source']}")
            continue

        if raw == 0:
            problems.append(f"{league} {season}: raw=0 - ESPN returned an empty window")
            continue

        # Any refusal that is NOT a wrong-season one. These are the reasons that
        # would indicate a payload problem rather than the window seam.
        unexpected = {k: v for k, v in row["rejected"].items() if k != "WRONG_SEASON"}
        if unexpected:
            problems.append(f"{league} {season}: non-season rejections {unexpected}")

        if exp and 0 < accepted < exp:
            problems.append(
                f"{league} {season}: accepted {accepted} < expected {exp} "
                "(verify against real-world history before treating as a defect)"
            )
        if exp and accepted > exp:
            problems.append(
                f"{league} {season}: accepted {accepted} EXCEEDS expected {exp} "
                f"({accepted - exp} extra; {row['accepted'] - row['eligible']} record(s) are "
                "postseason/without-result and excluded from the model view)"
            )
        if exp and row["eligible"] < exp:
            problems.append(
                f"{league} {season}: {row['eligible']} model-eligible vs {exp} expected "
                "- REPORTED, never padded"
            )

    if problems:
        for problem in problems:
            print("  " + problem)
    else:
        print("  none")


    print(f"\nnetwork requests: {live_requests} (all others served from cache)")
    return 0


def print_rows(rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        print(
            f"{row['league']:<7} {row['season']:<7} {str(row['source']):<7} "
            f"{row['raw']:>5} {row['accepted']:>5} {row['eligible']:>5} "
            f"{row['teams']:>5} {str(row.get('expected') or '?'):>4} "
            f"{str(row['first'] or ''):<21} {str(row['last'] or ''):<21} "
            f"{json.dumps(row['rejected']) if row['rejected'] else ''}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
