"""
RESEARCH PROBE - Epic 2C. Temporary diagnostic, zero network.

Answers one blocking question before any code is written: does the Epic 2A
payload cache contain the PREVIOUS season for every season Epic 2C wants to
evaluate? A previous-season team prior that cannot be sourced is a STOP
condition, not something to approximate.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import espn  # noqa: E402

CACHE_DIR = REPO_ROOT / "research" / ".cache"


def cache_path(url: str, params: dict) -> Path:
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def season_available(league: str, season: int) -> bool:
    url = f"{espn.ESPN_BASE_URL}/{league}/scoreboard"
    for window in espn._season_discovery_windows(season):
        path = cache_path(url, {"dates": window, "limit": 1000})
        if not path.exists():
            return False
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("error"):
            return False
    return True


def main() -> int:
    leagues = ["eng.1", "ger.1", "ita.1", "esp.1", "fra.1", "eng.2", "ger.2"]
    years = list(range(2012, 2026))

    print(f"cache dir:   {CACHE_DIR}")
    print(f"cache files: {len(list(CACHE_DIR.glob('*.json')))}")
    print(f"discovery windows per season: {espn._season_discovery_windows(2019)}")
    print()
    print("league   " + "".join(f"{y % 100:>5}" for y in years))
    for league in leagues:
        row = "".join("    Y" if season_available(league, y) else "    ." for y in years)
        print(f"{league:8} {row}")

    print()
    print("Epic 2B.3 target seasons were 2018, 2019, 2020, 2023.")
    print("For a previous-season prior each needs season-1 present:")
    for target in (2018, 2019, 2020, 2023):
        for league in ("eng.1", "ger.1", "ita.1", "esp.1", "fra.1"):
            prev = season_available(league, target - 1)
            cur = season_available(league, target)
            print(
                f"  {league} target={target} cur={'Y' if cur else '.'} "
                f"prev({target - 1})={'Y' if prev else '.'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
