"""
Season-integrity coverage audit (Epic 2B.1, Phase 14).

Answers one question per league-season: does event-level season identity return
the season ESPN actually played, and does the old July-June window return
something different?

Runs OFFLINE against the Epic 2A cache in `research/.cache` by default, so it
can be re-run without touching the network. That matters because the interesting
seasons are historical and settled - refetching them proves nothing and costs
requests.

WHY THIS SCRIPT IMPORTS THE PRODUCTION CLASSIFIER
-------------------------------------------------
It calls `domain.season_identity.classify_event_season`, the same chokepoint
production uses. An audit with its own private definition of season membership
would be measuring itself: Epic 2A's audit did exactly that, reimplemented the
July-June window locally, and inherited the defect it was meant to detect. There
is one definition of season membership in this repository and this file uses it.

    python3 research/verify_season_integrity.py
    python3 research/verify_season_integrity.py --leagues eng.1,ita.1 --seasons 2019,2020

NOT part of production execution: nothing under `research/` is imported by
`main.py` or `analyze_all.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.season_identity import (  # noqa: E402
    SeasonVerdict,
    classify_event_season,
)
from espn import extract_season_identity  # noqa: E402

CACHE_DIR = REPO_ROOT / "research" / ".cache"
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

PRODUCTION_LEAGUES = ("eng.1", "ger.1", "ita.1", "esp.1", "fra.1")
FOCUS_SEASONS = (2018, 2019, 2020, 2022)

# Seasons whose real length is not 380/306: history, not corruption.
KNOWN_REAL_ANOMALIES = {
    ("fra.1", 2019): "abandoned on 2020-04-28 (COVID); Ligue 1 was ended early by decree",
    ("fra.1", 2018): "38 matchdays plus playoff fixtures in the same competition",
}


def cache_path(league: str, window_season: int, limit: int = 1000) -> Path:
    """Reproduce Epic 2A's cache key so its captured payloads can be reused."""
    url = f"{BASE}/{league}/scoreboard"
    params = {"dates": f"{window_season}0701-{window_season + 1}0630", "limit": limit}
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def load_window(league: str, window_season: int) -> Optional[Dict[str, Any]]:
    path = cache_path(league, window_season)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("payload") or {}
    except ValueError:
        return None


def in_july_june_window(event: Dict[str, Any], season: int) -> bool:
    """The pre-2B.1 rule, kept here only so the comparison can be measured."""
    raw = event.get("date")
    if not isinstance(raw, str):
        return False
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (
        datetime(season, 7, 1, tzinfo=timezone.utc)
        <= moment
        < datetime(season + 1, 7, 1, tzinfo=timezone.utc)
    )


def completed(event: Dict[str, Any]) -> bool:
    comp = (event.get("competitions") or [{}])[0]
    status_type = (comp.get("status") or {}).get("type") or {}
    return bool(status_type.get("completed"))


def audit(league: str, season: int) -> Optional[Dict[str, Any]]:
    """
    Compare old and new semantics for one league-season.

    Discovery spans the season's own window and the following one, matching
    production; validation is the production classifier.
    """
    windows: List[Tuple[int, Dict[str, Any]]] = []
    for window_season in (season, season + 1):
        payload = load_window(league, window_season)
        if payload is None:
            return None
        windows.append((window_season, payload))

    accepted: Dict[str, Dict[str, Any]] = {}
    verdicts: Counter = Counter()
    old_rule: Dict[str, Dict[str, Any]] = {}
    phases: Counter = Counter()

    for window_season, payload in windows:
        payload_slug = ((payload.get("leagues") or [{}])[0]).get("slug")
        for event in payload.get("events") or []:
            event_id = str(event.get("id") or "")

            identity = extract_season_identity(event, payload_competition=payload_slug)
            verdict = classify_event_season(
                identity, expected_competition=league, requested_season=season
            )
            verdicts[verdict.value] += 1
            if verdict is SeasonVerdict.ACCEPTED:
                accepted[event_id] = event
                if identity.phase:
                    phases[identity.phase] += 1

            # The old rule only ever saw the season's own window.
            if window_season == season and in_july_june_window(event, season):
                old_rule[event_id] = event

    truncated = set(accepted) - set(old_rule)
    contamination = set(old_rule) - set(accepted)

    return {
        "league": league,
        "season": season,
        "new_total": len(accepted),
        "new_completed": sum(1 for e in accepted.values() if completed(e)),
        "old_total": len(old_rule),
        "truncated_by_old_rule": len(truncated),
        "admitted_wrongly_by_old_rule": len(contamination),
        "verdicts": dict(verdicts),
        "phases": dict(phases.most_common(4)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default=",".join(PRODUCTION_LEAGUES))
    parser.add_argument("--seasons", default=",".join(str(s) for s in FOCUS_SEASONS))
    args = parser.parse_args()

    leagues = [s.strip() for s in args.leagues.split(",") if s.strip()]
    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]

    print("Season-integrity coverage audit (offline, Epic 2A cache)")
    print(f"cache: {CACHE_DIR}")
    print()
    header = f"{'league':8} {'season':7} {'new':>5} {'done':>5} {'old':>5} {'lost':>5} {'bogus':>6}"
    print(header)
    print("-" * len(header))

    missing: List[str] = []
    rows: List[Dict[str, Any]] = []
    for league in leagues:
        for season in seasons:
            result = audit(league, season)
            if result is None:
                missing.append(f"{league} {season}")
                continue
            rows.append(result)
            print(
                f"{league:8} {season:<7} {result['new_total']:>5} "
                f"{result['new_completed']:>5} {result['old_total']:>5} "
                f"{result['truncated_by_old_rule']:>5} "
                f"{result['admitted_wrongly_by_old_rule']:>6}"
            )
        print()

    print("new   = events accepted by event-level season identity")
    print("done  = of those, completed (a MatchRecord is only built for these)")
    print("old   = events the July-June window would have returned")
    print("lost  = real fixtures the old rule dropped")
    print("bogus = other-season fixtures the old rule admitted")
    print()

    damaged = [r for r in rows if r["truncated_by_old_rule"] or r["admitted_wrongly_by_old_rule"]]
    print(f"league-seasons audited:        {len(rows)}")
    print(f"league-seasons the old rule got wrong: {len(damaged)}")
    print(f"fixtures the old rule lost:    {sum(r['truncated_by_old_rule'] for r in rows)}")
    print(f"fixtures the old rule invented: {sum(r['admitted_wrongly_by_old_rule'] for r in rows)}")

    unverifiable = sum(r["verdicts"].get("UNVERIFIABLE", 0) for r in rows)
    print(f"events refused as unverifiable: {unverifiable}")

    print()
    print("Genuine historical anomalies (NOT data bugs, deliberately preserved):")
    for row in rows:
        note = KNOWN_REAL_ANOMALIES.get((row["league"], row["season"]))
        if note:
            print(
                f"  {row['league']} {row['season']}: "
                f"{row['new_completed']} completed of {row['new_total']} - {note}"
            )

    if missing:
        print()
        print(f"not in cache ({len(missing)}): {', '.join(missing[:12])}")
        print("re-run research/audit_espn_history.py to populate, or pass --seasons")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
