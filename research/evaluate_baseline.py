"""
RESEARCH TOOLING - Epic 2B.3. NOT production code. NOT imported by the pipeline.

Representative offline evaluation of POISSON_V1 against real historical data.

Runs with ZERO network requests by replaying the Epic 2A payload cache
(`research/.cache`) through the production ESPN parser and the Epic 2B.2 dataset
builder, then scoring it with the Epic 2B.3 harness. The scope is the four
boundary seasons across the five production leagues - the same scope Epic 2B.2
audited, so the dataset underneath these numbers is one already proven intact.

    python research/evaluate_baseline.py
    python research/evaluate_baseline.py --calibration --breakdown evidence

What this is: a measurement of how well the CURRENT model's probabilities match
reality, produced under strict point-in-time rules.

What this is NOT: a betting result, a profitability claim, or a mandate to
change the model. No odds are loaded, no thresholds are read, and no parameter
is tuned. Reading a poor Brier score here and going on to fit a better model is
Epic 2C's decision, made deliberately - not a side effect of this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import espn  # noqa: E402
from domain.evaluation import MetricSummary  # noqa: E402
from evaluation_harness import evaluate, get_model  # noqa: E402
from historical_dataset import build_dataset  # noqa: E402

CACHE_DIR = REPO_ROOT / "research" / ".cache"

LEAGUES = ["eng.1", "ger.1", "ita.1", "esp.1", "fra.1"]
# 2018/19 normal, 2019/20 COVID-extended, 2020/21 the contamination boundary,
# 2023/24 a recent normal season.
SEASONS = [2018, 2019, 2020, 2023]


def cache_path(url: str, params: Dict[str, Any]) -> Path:
    """Identical keying to the Epic 2A audit, so its cache is readable here."""
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


class CacheOnlyFetch:
    """
    A `fetch` for the 2B.2 builder that reads the 2A cache and never the network.

    A cache miss returns None - the provider-failure signal - rather than an
    empty season. The distinction is the whole point of the Epic 1B.2 error
    semantics: "we have no payload for this" must never be scored as "this
    league played no matches".
    """

    def __init__(self) -> None:
        self.hits = 0
        self.misses: List[str] = []

    def __call__(self, league: str, season: int):
        url = f"{espn.ESPN_BASE_URL}/{league}/scoreboard"
        matches = []
        rejected: Dict[str, int] = {}
        seen = set()

        for window in espn._season_discovery_windows(season):
            path = cache_path(url, {"dates": window, "limit": 1000})
            if not path.exists():
                self.misses.append(f"{league} {season} {window}")
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
            # The 2A cache stores an envelope: {payload, error, retrieved_at,
            # url, params}. A cached ERROR is a cached failure, not an empty
            # season, so it propagates as None rather than as zero matches.
            if entry.get("error"):
                self.misses.append(f"{league} {season} {window} (cached error)")
                return None
            payload = entry.get("payload") or {}
            self.hits += 1

            # Same parser as production: this audit measures the real path, not
            # a research reimplementation of it.
            readout = espn.parse_scoreboard_history(payload, league, season)
            for reason, count in readout.rejected.items():
                rejected[reason] = rejected.get(reason, 0) + count
            for match in readout.matches:
                if match.event_id in seen:
                    continue
                seen.add(match.event_id)
                matches.append(match)

        return espn.HistoricalReadout(
            league=league,
            season=season,
            matches=matches,
            rejected=rejected,
        )


def _fmt(value: Optional[float], places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def print_summary(summary: MetricSummary, label: str = "") -> None:
    heading = f"{summary.model_id} {summary.model_version}"
    if label:
        heading += f"  [{label}]"
    print(f"\n{heading}")
    print(f"  targets         {summary.targets}")
    print(f"  scored          {summary.scored}")
    print(f"  coverage        {_fmt(summary.coverage)}")
    print(f"  Brier           {_fmt(summary.brier)}")
    print(f"  log loss        {_fmt(summary.log_loss)}")
    print(f"  mean predicted  {_fmt(summary.mean_predicted)}")
    print(f"  observed BTTS   {_fmt(summary.observed_rate)}")
    if summary.unevaluable:
        for reason, count in summary.unevaluable.items():
            print(f"    {reason:<24} {count}")


def print_calibration(summary: MetricSummary) -> None:
    print("\n  calibration:")
    print(f"    {'bin':<16}{'n':>7}{'predicted':>12}{'observed':>11}{'gap':>9}")
    for bucket in summary.calibration:
        if bucket.count == 0:
            print(f"    {bucket.label:<16}{0:>7}{'-':>12}{'-':>11}{'-':>9}")
            continue
        print(
            f"    {bucket.label:<16}{bucket.count:>7}"
            f"{_fmt(bucket.mean_predicted, 3):>12}"
            f"{_fmt(bucket.observed_rate, 3):>11}"
            f"{_fmt(bucket.gap, 3):>9}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Offline POISSON_V1 evaluation (Epic 2B.3)")
    parser.add_argument("--leagues", default=",".join(LEAGUES))
    parser.add_argument("--seasons", default=",".join(str(s) for s in SEASONS))
    parser.add_argument("--calibration", action="store_true")
    parser.add_argument("--breakdown", choices=["competition", "season", "evidence"])
    args = parser.parse_args(argv)

    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
    seasons = [int(x) for x in args.seasons.split(",") if x.strip()]

    print("Offline evaluation - Epic 2B.3")
    print(f"cache:   {CACHE_DIR}")
    print(f"leagues: {', '.join(leagues)}")
    print(f"seasons: {', '.join(str(s) for s in seasons)}")

    fetch = CacheOnlyFetch()
    report = build_dataset(leagues, seasons, fetch=fetch)

    if fetch.misses:
        print(f"\ncache misses ({len(fetch.misses)}) - those seasons are absent, not empty:")
        for miss in fetch.misses[:10]:
            print(f"  {miss}")

    dataset = [match for build in report.builds for match in build.matches]
    print(f"\ncache reads: {fetch.hits}   network requests: 0")
    built = [b for b in report.builds if not b.failed]
    print(f"dataset:     {len(dataset)} records from {len(built)} league-seasons")

    if not dataset:
        print("no data - nothing to evaluate", file=sys.stderr)
        return 2

    for model_id in ("POISSON_V1", "REFERENCE_BASE_RATE"):
        run = evaluate(dataset, get_model(model_id))
        print_summary(run.summary)
        if args.calibration:
            print_calibration(run.summary)
        if args.breakdown:
            for name, summary in run.breakdown(args.breakdown).items():
                print_summary(summary, label=f"{args.breakdown}={name}")

    print("\nProbability quality only. No odds, no thresholds, no betting claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
