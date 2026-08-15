"""
Epic 2C experiment: parameter search, then ONE final holdout run.

ZERO NETWORK. Replays payloads already cached by the Epic 2A audit through the
production parser (`espn.parse_scoreboard_history`), exactly as
`research/audit_historical_dataset.py` does. If the cache is cold this script
reports what is missing and stops rather than reaching for the network, because
a partial dataset would silently change which fixtures land in the sparse
buckets - the very thing being measured.

VALIDATION PROTOCOL (Part 15)
-----------------------------
The repository had no train/validation/test split, so this Epic establishes one
and writes it down BEFORE any final number is looked at.

    development  2018, 2019       parameter search runs here, repeatedly
    validation   2020             confirms the choice generalises, inspected once
    final test   2023             run ONCE, after parameters are frozen

WHY 2023 AND NOT 2020. Epic 2A's cold-start research and Epic 2B.3's baseline
both examined 2018-2020 in detail; calling any of them untouched would be false.
2023 was present in the 2B.3 corpus as a target season, so its HEADLINE Brier
has been seen - but no Epic has inspected 2023 by evidence bucket, by promotion
status, or under any shrinkage parameter. It is the least contaminated partition
available, and that limitation is stated in the report rather than glossed.

    Honest description: "held out for this Epic's parameter selection", not
    "never before observed".

The search never reads 2023. `--stage final` refuses to run unless the frozen
configuration is passed explicitly, so a parameter cannot be adjusted after
seeing a final-test score without that being visible in the command line.

ROLLING ORIGIN. Every prediction is already point-in-time: `replay` walks each
target fixture and rebuilds inputs from matches with kickoff strictly before it.
A season's evaluation is therefore itself a rolling-origin backtest, and no
additional resampling is needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import espn  # noqa: E402
from domain.comparison import (  # noqa: E402
    calibration_on_intersection,
    compare,
    evidence_bucket_table,
    extreme_probability_stats,
    fixture_key,
    intersect,
)
from domain.historical import HistoricalMatch  # noqa: E402
from domain.match_records import Venue  # noqa: E402
from domain.team_strength import (  # noqa: E402
    EstimatorConfig,
    method_of_moments_prior_strength,
)
from evaluation_harness import (  # noqa: E402
    PoissonV1Adapter,
    PoissonV1ShrunkAdapter,
    replay,
)

CACHE_DIR = REPO_ROOT / "research" / ".cache"

#: Top flights only. Second tiers are loaded as CONTEXT (for promoted-club
#: analysis) but never used as target fixtures: GG.md's model is specified for
#: top-division football and mixing tiers would confound the comparison.
TARGET_LEAGUES = ["eng.1", "ger.1", "ita.1", "esp.1", "fra.1"]
CONTEXT_LEAGUES = ["eng.2", "ger.2"]

DEVELOPMENT_SEASONS = [2018, 2019]
VALIDATION_SEASONS = [2020]
FINAL_TEST_SEASONS = [2023]


# ---------------------------------------------------------------------------
# Cache-backed dataset construction
# ---------------------------------------------------------------------------


def cache_path(url: str, params: Dict[str, object]) -> Path:
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def load_season(league: str, season: int) -> Tuple[List[HistoricalMatch], List[str]]:
    """
    Parse one league-season from cache through the PRODUCTION parser.

    Using `espn.parse_scoreboard_history` rather than a bespoke reader means the
    season-integrity rules from Epic 2B.1 (WRONG_SEASON rejection, phase
    classification, completed-match filtering) apply identically here. A private
    parser would let this experiment evaluate fixtures production would reject.
    """
    matches: List[HistoricalMatch] = []
    missing: List[str] = []
    for window in espn._season_discovery_windows(season):
        url = f"{espn.ESPN_BASE_URL}/{league}/scoreboard"
        # `limit` and the {"payload": ...} envelope must match how the Epic 2A
        # audit wrote the cache exactly, or every lookup silently misses and the
        # experiment reports a cold cache it in fact has.
        params = {"dates": window, "limit": 1000}
        path = cache_path(url, params)
        if not path.exists():
            missing.append(f"{league}:{season}:{window}")
            continue
        entry = json.loads(path.read_text())
        payload = entry.get("payload")
        if payload is None:
            missing.append(f"{league}:{season}:{window}:empty")
            continue
        readout = espn.parse_scoreboard_history(payload, league, season)
        matches.extend(readout.matches)

    unique = {match.event_id: match for match in matches}
    return list(unique.values()), missing


def build_dataset(seasons: Sequence[int]) -> Tuple[List[HistoricalMatch], List[str]]:
    """
    Load target seasons plus the season BEFORE each, which the priors require.

    The previous season is not optional: without it every team is NEW_TO_LEAGUE
    and the two-level prior degenerates to a one-level one. Loading it here, and
    reporting anything missing, is what makes the Part 3 claim checkable.
    """
    needed = sorted({s for season in seasons for s in (season - 1, season)})
    dataset: List[HistoricalMatch] = []
    missing: List[str] = []
    for league in TARGET_LEAGUES + CONTEXT_LEAGUES:
        for season in needed:
            matches, gaps = load_season(league, season)
            dataset.extend(matches)
            missing.extend(gaps)
    return dataset, missing


# ---------------------------------------------------------------------------
# Shared evidence counts (Part 9)
# ---------------------------------------------------------------------------


def evidence_counts(
    dataset: Sequence[HistoricalMatch],
    targets: Sequence[HistoricalMatch],
) -> Dict[Tuple[str, int, str], int]:
    """
    Prior CURRENT-SEASON venue matches per target, counted ONCE for both arms.

    Defined as min(home venue matches, away venue matches): the binding
    constraint on a BTTS estimate is the side with less evidence, since the
    probability is a product over both. Reporting the mean would let a mature
    home side mask a debutant away side - precisely the case GG-028 describes.

    Computed from the dataset itself rather than from either model's reported
    sample, because the two arms count different things and a shared bucketing
    key must not favour either.
    """
    by_season: Dict[Tuple[str, int], List[HistoricalMatch]] = {}
    for match in dataset:
        by_season.setdefault((match.competition, match.season), []).append(match)
    for matches in by_season.values():
        matches.sort(key=lambda m: m.kickoff)

    counts: Dict[Tuple[str, int, str], int] = {}
    for target in targets:
        pool = by_season.get((target.competition, target.season), [])
        home = away = 0
        for match in pool:
            if match.kickoff >= target.kickoff:
                break  # sorted: nothing later can qualify
            if match.event_id == target.event_id:
                continue
            if match.home_team_id == target.home_team_id:
                home += 1
            if match.away_team_id == target.away_team_id:
                away += 1
        counts[fixture_key(target)] = min(home, away)
    return counts


def promoted_targets(
    dataset: Sequence[HistoricalMatch],
    targets: Sequence[HistoricalMatch],
) -> set:
    """
    Target fixtures involving a club with no PREVIOUS season in this competition.

    Identified from observed participation, which is the only reliable signal
    available: ESPN's payloads carry no promotion field, and inferring one from
    league relationships would be guesswork. A club absent from last season's
    fixtures in this competition is new to it - which covers promotion and also
    a first appearance in the corpus, so the analysis is labelled NEW_TO_LEAGUE
    rather than "promoted".
    """
    participants: Dict[Tuple[str, int], set] = {}
    for match in dataset:
        key = (match.competition, match.season)
        participants.setdefault(key, set()).update({match.home_team_id, match.away_team_id})

    flagged = set()
    for target in targets:
        previous = participants.get((target.competition, target.season - 1), set())
        if not previous:
            continue  # cannot judge without a previous season; excluded, not assumed
        if target.home_team_id not in previous or target.away_team_id not in previous:
            flagged.add(fixture_key(target))
    return flagged


# ---------------------------------------------------------------------------
# Moment-based reference point for k (Part 7)
# ---------------------------------------------------------------------------


def moment_estimates(dataset: Sequence[HistoricalMatch], seasons: Sequence[int]) -> Dict[str, float]:
    """
    Estimate k from between-team variance on DEVELOPMENT seasons only.

    An independent anchor for the grid: a search optimum near this estimate is
    evidence the parameter is real rather than an artefact of the objective.
    """
    out: Dict[str, float] = {}
    for venue in (Venue.HOME, Venue.AWAY):
        for_rates: List[float] = []
        against_rates: List[float] = []
        sizes: List[int] = []
        by_season: Dict[Tuple[str, int], List[HistoricalMatch]] = {}
        for match in dataset:
            if match.season not in seasons or match.competition not in TARGET_LEAGUES:
                continue
            by_season.setdefault((match.competition, match.season), []).append(match)

        for matches in by_season.values():
            teams: Dict[str, List[int]] = {}
            for match in matches:
                if match.home_goals is None or match.away_goals is None:
                    continue
                if venue is Venue.HOME:
                    team, scored, conceded = match.home_team_id, match.home_goals, match.away_goals
                else:
                    team, scored, conceded = match.away_team_id, match.away_goals, match.home_goals
                bucket = teams.setdefault(team, [0, 0, 0])
                bucket[0] += scored
                bucket[1] += conceded
                bucket[2] += 1
            for scored, conceded, played in teams.values():
                if played < 5:
                    continue
                for_rates.append(scored / played)
                against_rates.append(conceded / played)
                sizes.append(played)

        label = "home" if venue is Venue.HOME else "away"
        for name, rates in (("for", for_rates), ("against", against_rates)):
            estimate = method_of_moments_prior_strength(rates, sizes)
            if estimate is not None:
                out[f"{label}_{name}"] = estimate
    return out


# ---------------------------------------------------------------------------
# One evaluation of one configuration
# ---------------------------------------------------------------------------


@dataclass
class Result:
    config: EstimatorConfig
    label: str
    intersection: int
    baseline_brier: Optional[float]
    shrunk_brier: Optional[float]
    baseline_log_loss: Optional[float]
    shrunk_log_loss: Optional[float]
    baseline_coverage: int
    shrunk_coverage: int
    sparse_baseline_brier: Optional[float]
    sparse_shrunk_brier: Optional[float]
    sparse_n: int
    shrunk_certain: int
    baseline_certain: int


def run_configuration(
    dataset: Sequence[HistoricalMatch],
    seasons: Sequence[int],
    config: EstimatorConfig,
    label: str,
    *,
    baseline_cache: Optional[List] = None,
) -> Tuple[Result, List, List, Dict]:
    """
    Evaluate one configuration against the raw baseline on identical fixtures.

    The baseline is replayed once and reused across configurations: it does not
    depend on the estimator's parameters, and recomputing it would only add a way
    for the two arms to diverge.
    """
    targets = [
        match
        for match in dataset
        if match.season in seasons and match.competition in TARGET_LEAGUES
    ]

    baseline = (
        baseline_cache
        if baseline_cache is not None
        else replay(dataset, PoissonV1Adapter(), targets=targets)
    )
    shrunk = replay(dataset, PoissonV1ShrunkAdapter(config), targets=targets)

    comparison = compare(baseline, shrunk)
    counts = evidence_counts(dataset, targets)
    rows = evidence_bucket_table(baseline, shrunk, evidence_of=counts)

    sparse = [row for row in rows if row.bucket in ("0", "1-2")]
    sparse_n = sum(row.n for row in sparse)

    def weighted(attr: str, arm: str) -> Optional[float]:
        total = 0.0
        seen = 0
        for row in sparse:
            summary = getattr(row, arm)
            value = getattr(summary, attr)
            if value is None:
                continue
            total += value * summary.scored
            seen += summary.scored
        return total / seen if seen else None

    left_paired, right_paired = intersect(baseline, shrunk)
    result = Result(
        config=config,
        label=label,
        intersection=comparison.intersection_size,
        baseline_brier=comparison.left.summary.brier,
        shrunk_brier=comparison.right.summary.brier,
        baseline_log_loss=comparison.left.summary.log_loss,
        shrunk_log_loss=comparison.right.summary.log_loss,
        baseline_coverage=comparison.left.raw_scored,
        shrunk_coverage=comparison.right.raw_scored,
        sparse_baseline_brier=weighted("brier", "baseline"),
        sparse_shrunk_brier=weighted("brier", "shrunk"),
        sparse_n=sparse_n,
        baseline_certain=extreme_probability_stats(left_paired).certain,
        shrunk_certain=extreme_probability_stats(right_paired).certain,
    )
    return result, baseline, shrunk, counts


# ---------------------------------------------------------------------------
# Parameter grid (Part 7)
# ---------------------------------------------------------------------------


def build_grid() -> List[Tuple[str, EstimatorConfig]]:
    """
    A documented, deliberately coarse grid.

    RANGES AND WHY. Team k is in matches against a ~19-match venue season, so
    values above ~12 would leave a full season's evidence outweighed by the prior
    and are not plausible; 0 is included as the null arm. League k is in
    team-games against ~760 per season, so its scale is an order of magnitude
    larger. Promotion factors bracket 1.0 in both directions rather than assuming
    the direction Epic 2A observed.

    Coarse ON PURPOSE. A fine grid over four parameters on two development
    seasons would select noise; the objective here is to establish whether
    shrinkage helps and roughly how much, not to find a third decimal place.
    """
    grid: List[Tuple[str, EstimatorConfig]] = [("null_k0", EstimatorConfig())]

    # The upper values (16-40) were added AFTER a first pass found the optimum
    # sitting on the old boundary at k=12. A boundary optimum is not an optimum,
    # it is an unfinished search, and reporting one as a chosen parameter would
    # misrepresent the evidence. k=40 shrinks a full 19-match venue season to
    # roughly a third of its own weight, so if the curve is still improving there
    # the honest conclusion is about the signal in venue splits, not about k.
    for k_team in (2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 20.0, 30.0, 40.0):

        grid.append(
            (
                f"team_k{k_team:g}",
                EstimatorConfig(
                    k_goals_for=k_team,
                    k_goals_against=k_team,
                    k_prev_season=k_team,
                    k_league=40.0,
                ),
            )
        )

    # Does defence warrant stronger shrinkage than attack (Epic 2A's reliability
    # asymmetry)? Asked, not assumed.
    for k_for, k_against in ((4.0, 6.0), (4.0, 8.0), (6.0, 10.0)):
        grid.append(
            (
                f"split_for{k_for:g}_against{k_against:g}",
                EstimatorConfig(
                    k_goals_for=k_for,
                    k_goals_against=k_against,
                    k_prev_season=k_for,
                    k_league=40.0,
                ),
            )
        )

    # League prior strength, holding the best-guess team k fixed.
    for k_league in (0.0, 20.0, 40.0, 80.0, 160.0):
        grid.append(
            (
                f"league_k{k_league:g}",
                EstimatorConfig(
                    k_goals_for=6.0,
                    k_goals_against=6.0,
                    k_prev_season=6.0,
                    k_league=k_league,
                ),
            )
        )

    # Promotion handling (Part 3/12), including the neutral control.
    for attack, defence in ((1.0, 1.0), (0.85, 1.1), (0.72, 1.15), (0.605, 1.2)):
        grid.append(
            (
                f"promo_a{attack:g}_d{defence:g}",
                EstimatorConfig(
                    k_goals_for=6.0,
                    k_goals_against=6.0,
                    k_prev_season=6.0,
                    k_league=40.0,
                    new_team_attack_factor=attack,
                    new_team_defence_factor=defence,
                ),
            )
        )

    return grid


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def fmt(value: Optional[float], places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def ece(summary) -> Optional[float]:
    """
    Expected calibration error from the Epic 2B.3 calibration bins.

    `MetricSummary.calibration` is a list of bins, not a scalar, so the scalar is
    derived here rather than added to the shared domain type: ECE is a reporting
    convenience for this Epic and baking a particular definition of it into the
    domain layer would quietly commit every future Epic to that choice.

    Weighted by bin population so a 3-fixture bin cannot outvote a 500-fixture
    one, and bins with no predictions are skipped rather than counted as perfect.
    """
    total = 0.0
    seen = 0
    for bin_ in summary.calibration:
        if not bin_.count or bin_.mean_predicted is None or bin_.observed_rate is None:
            continue
        total += abs(bin_.mean_predicted - bin_.observed_rate) * bin_.count
        seen += bin_.count
    return total / seen if seen else None



def print_search(results: Sequence[Result]) -> None:
    print()
    print("PARAMETER SEARCH - development seasons only")
    print(
        f"{'config':<26} {'kF':>5} {'kA':>5} {'kL':>6} {'pA':>5} {'pD':>5} "
        f"{'inter':>6} {'Brier':>7} {'dBrier':>8} {'LogL':>7} {'dLogL':>8} "
        f"{'sparseB':>8} {'dSparse':>8} {'cov':>6} {'0/1':>5}"
    )
    for result in results:
        config = result.config
        d_brier = (
            None
            if result.shrunk_brier is None or result.baseline_brier is None
            else result.shrunk_brier - result.baseline_brier
        )
        d_log = (
            None
            if result.shrunk_log_loss is None or result.baseline_log_loss is None
            else result.shrunk_log_loss - result.baseline_log_loss
        )
        d_sparse = (
            None
            if result.sparse_shrunk_brier is None or result.sparse_baseline_brier is None
            else result.sparse_shrunk_brier - result.sparse_baseline_brier
        )
        print(
            f"{result.label:<26} {config.k_goals_for:>5.1f} {config.k_goals_against:>5.1f} "
            f"{config.k_league:>6.1f} {config.new_team_attack_factor:>5.2f} "
            f"{config.new_team_defence_factor:>5.2f} {result.intersection:>6d} "
            f"{fmt(result.shrunk_brier):>7} {fmt(d_brier, 4):>8} "
            f"{fmt(result.shrunk_log_loss):>7} {fmt(d_log, 4):>8} "
            f"{fmt(result.sparse_shrunk_brier):>8} {fmt(d_sparse, 4):>8} "
            f"{result.shrunk_coverage:>6d} {result.shrunk_certain:>5d}"
        )
    if results:
        first = results[0]
        print(
            f"\nbaseline on same intersection: Brier {fmt(first.baseline_brier)}  "
            f"logloss {fmt(first.baseline_log_loss)}  "
            f"sparse Brier {fmt(first.sparse_baseline_brier)}  "
            f"coverage {first.baseline_coverage}  exact 0/1 {first.baseline_certain}"
        )


def print_buckets(rows, title: str) -> None:
    print()
    print(title)
    print(
        f"{'bucket':<8} {'N':>6} {'baseBrier':>10} {'shrBrier':>10} {'baseLogL':>10} "
        f"{'shrLogL':>10} {'baseCal':>8} {'shrCal':>8} {'baseCov':>8} {'shrCov':>8}"
    )
    for row in rows:
        print(
            f"{row.bucket:<8} {row.n:>6d} {fmt(row.baseline.brier):>10} "
            f"{fmt(row.shrunk.brier):>10} {fmt(row.baseline.log_loss):>10} "
            f"{fmt(row.shrunk.log_loss):>10} {fmt(ece(row.baseline), 3):>8} "
            f"{fmt(ece(row.shrunk), 3):>8} {row.baseline.scored:>8d} "
            f"{row.shrunk.scored:>8d}"

        )


def print_extremes(baseline, shrunk) -> None:
    left, right = intersect(baseline, shrunk)
    print()
    print("EXTREME PROBABILITIES (identical intersection)")
    print(f"{'metric':<22} {'baseline':>10} {'shrunk':>10}")
    for name, attr in (
        ("scored", "scored"),
        ("p <= 0.05", "at_or_below_05"),
        ("p >= 0.95", "at_or_above_95"),
        ("p == 0 exactly", "exactly_zero"),
        ("p == 1 exactly", "exactly_one"),
    ):
        lhs = getattr(extreme_probability_stats(left), attr)
        rhs = getattr(extreme_probability_stats(right), attr)
        print(f"{name:<22} {lhs:>10d} {rhs:>10d}")


def print_calibration(baseline, shrunk) -> None:
    # Intersect FIRST, then bin each arm: `calibration_on_intersection` takes an
    # already-paired set precisely so the caller cannot accidentally calibrate two
    # different fixture populations against each other (Part 8).
    left_paired, right_paired = intersect(baseline, shrunk)
    base_bins = calibration_on_intersection(left_paired)
    shrunk_bins = calibration_on_intersection(right_paired)

    print()
    print("CALIBRATION (identical intersection)")
    print(
        f"{'bin':<12} {'baseN':>6} {'basePred':>9} {'baseObs':>9} "
        f"{'shrN':>6} {'shrPred':>9} {'shrObs':>9}"
    )
    for left, right in zip(base_bins, shrunk_bins, strict=False):
        # Bin populations are printed for BOTH arms: the fixture set is identical
        # but shrinkage moves probabilities between bins, so a shared N column
        # would misstate how much evidence sits behind each arm's row.
        label = f"{left.lower:.1f}-{left.upper:.1f}"
        print(
            f"{label:<12} {left.count:>6d} {fmt(left.mean_predicted, 3):>9} "
            f"{fmt(left.observed_rate, 3):>9} {right.count:>6d} "
            f"{fmt(right.mean_predicted, 3):>9} {fmt(right.observed_rate, 3):>9}"
        )



def print_promoted(dataset, targets, baseline, shrunk) -> None:
    from domain.comparison import summarise

    flagged = promoted_targets(dataset, targets)
    left, right = intersect(baseline, shrunk)
    left_promo = [r for r in left if fixture_key(r) in flagged]
    right_promo = [r for r in right if fixture_key(r) in flagged]

    print()
    print("NEW-TO-LEAGUE (promoted) FIXTURES - identical intersection")
    print(f"flagged target fixtures: {len(flagged)}  in intersection: {len(left_promo)}")
    if not left_promo:
        print("  none in intersection - nothing to report")
        return
    # model_id/version are carried on the records themselves; taking them from the
    # first record keeps the summary labelled with the model that actually produced
    # it instead of a hardcoded string that could drift from reality.
    base = summarise(
        left_promo,
        model_id=left_promo[0].model_id,
        model_version=left_promo[0].model_version,
    )
    shr = summarise(
        right_promo,
        model_id=right_promo[0].model_id,
        model_version=right_promo[0].model_version,
    )
    print(f"{'arm':<10} {'scored':>7} {'Brier':>8} {'logloss':>9} {'calErr':>8}")
    for name, summary in (("baseline", base), ("shrunk", shr)):

        print(
            f"{name:<10} {summary.scored:>7d} {fmt(summary.brier):>8} "
            f"{fmt(summary.log_loss):>9} {fmt(ece(summary), 3):>8}"
        )



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_config(text: str) -> EstimatorConfig:
    """Parse `kF=6,kA=6,kP=6,kL=40,pA=1.0,pD=1.0` from the command line."""
    mapping = {
        "kF": "k_goals_for",
        "kA": "k_goals_against",
        "kP": "k_prev_season",
        "kL": "k_league",
        "pA": "new_team_attack_factor",
        "pD": "new_team_defence_factor",
    }
    values: Dict[str, float] = {}
    for part in text.split(","):
        key, _, raw = part.partition("=")
        key = key.strip()
        if key not in mapping:
            raise SystemExit(f"unknown config key {key!r}; expected one of {sorted(mapping)}")
        values[mapping[key]] = float(raw)
    return EstimatorConfig(**values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=["search", "validation", "final"],
        default="search",
        help="search=development seasons; validation=confirm; final=frozen holdout ONCE",
    )
    parser.add_argument(
        "--config",
        help="frozen configuration, REQUIRED for validation/final (e.g. kF=6,kA=6,kP=6,kL=40)",
    )
    parser.add_argument("--limit", type=int, help="evaluate only the first N grid entries")
    args = parser.parse_args()

    if args.stage == "search":
        seasons = DEVELOPMENT_SEASONS
    elif args.stage == "validation":
        seasons = VALIDATION_SEASONS
    else:
        seasons = FINAL_TEST_SEASONS

    if args.stage in ("validation", "final") and not args.config:
        # The guard that makes Part 15 enforceable rather than aspirational: a
        # holdout number cannot be produced without naming the frozen parameters
        # on the command line, so post-hoc tuning leaves evidence.
        raise SystemExit(
            f"--stage {args.stage} requires --config. Parameters must be frozen on the "
            "development seasons BEFORE the holdout is inspected (Part 15)."
        )

    print(f"stage: {args.stage}   target seasons: {seasons}")
    dataset, missing = build_dataset(seasons)
    if missing:
        print(f"\nSTOP: {len(missing)} cached windows missing, e.g. {missing[:4]}")
        print("A partial dataset would change which fixtures are sparse. Refusing to guess.")
        return 1

    targets = [m for m in dataset if m.season in seasons and m.competition in TARGET_LEAGUES]
    print(f"dataset matches: {len(dataset)}   target fixtures: {len(targets)}")

    if args.stage == "search":
        moments = moment_estimates(dataset, DEVELOPMENT_SEASONS)
        print("\nmethod-of-moments k estimates (independent anchor for the grid):")
        for name, value in sorted(moments.items()):
            print(f"  {name:<14} k ~= {value:.2f}")

        grid = build_grid()
        if args.limit:
            grid = grid[: args.limit]
        results: List[Result] = []
        baseline_cache: Optional[List] = None
        for label, config in grid:
            result, baseline_cache, _, _ = run_configuration(
                dataset, seasons, config, label, baseline_cache=baseline_cache
            )
            results.append(result)
            print(
                f"  {label:<26} sparse Brier {fmt(result.sparse_shrunk_brier)} "
                f"overall {fmt(result.shrunk_brier)}"
            )
        print_search(results)
        return 0

    config = parse_config(args.config)
    print(f"frozen config: {config}")
    result, baseline, shrunk, counts = run_configuration(dataset, seasons, config, "frozen")

    print()
    print("COVERAGE (raw, before any intersection)")
    print(f"  target fixtures        {len(targets)}")
    print(f"  POISSON_V1_RAW scored  {result.baseline_coverage}")
    print(f"  POISSON_V1_SHRUNK_V1   {result.shrunk_coverage}")
    print(f"  intersection           {result.intersection}")

    print()
    print("HEADLINE (identical intersection)")
    print(f"{'arm':<24} {'Brier':>8} {'logloss':>9}")
    print(f"{'POISSON_V1_RAW':<24} {fmt(result.baseline_brier):>8} {fmt(result.baseline_log_loss):>9}")
    print(f"{'POISSON_V1_SHRUNK_V1':<24} {fmt(result.shrunk_brier):>8} {fmt(result.shrunk_log_loss):>9}")

    print_buckets(
        evidence_bucket_table(baseline, shrunk, evidence_of=counts),
        "EVIDENCE BUCKETS (prior current-season venue matches, min of both sides)",
    )
    print_extremes(baseline, shrunk)
    print_calibration(baseline, shrunk)
    print_promoted(dataset, targets, baseline, shrunk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
