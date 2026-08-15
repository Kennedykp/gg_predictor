"""
Epic 2C collapse diagnostic: is the Brier gain skill, or degeneracy?

WHY THIS EXISTS. The Part 7 parameter search improves monotonically in k with no
interior optimum - Brier keeps falling out to the edge of the grid (k=40) and
beyond. That pattern is exactly what a search would produce if shrinkage were
not sharpening the estimate but flattening it: as k grows, every team's rate is
pulled to the league mean, every lambda converges, and POISSON_V1 emits nearly
the same probability for every fixture. A constant predictor at the base rate p
scores Brier p(1-p), which on this corpus is competitive with the model.

So "lower Brier" alone cannot distinguish:

    (a) better-estimated team strengths, from
    (b) a model that has stopped discriminating between fixtures.

This script measures the standard deviation and range of the emitted
probabilities as k grows, alongside the constant-predictor Brier. Collapsing sd
with falling Brier is evidence for (b), and reporting the Brier gain as a
success without this check would be misleading.

ZERO NETWORK: reuses the experiment's cache-backed loader.
"""

from __future__ import annotations

import statistics as st
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.team_strength import EstimatorConfig  # noqa: E402
from evaluation_harness import (  # noqa: E402
    PoissonV1Adapter,
    PoissonV1ShrunkAdapter,
    replay,
)
from research.epic2c_experiment import (  # noqa: E402
    DEVELOPMENT_SEASONS,
    TARGET_LEAGUES,
    build_dataset,
)

#: Deliberately extends far past any defensible value (1000 is absurd on purpose):
#: the point is to show where the curve ENDS UP, so the degenerate limit is
#: visible rather than inferred.
K_SWEEP: Sequence[float] = (2.0, 8.0, 16.0, 40.0, 100.0, 1000.0)


def spread(records: Sequence) -> Tuple[float, float, float, float]:
    """Mean, population sd, min and max of emitted probabilities."""
    values: List[float] = [r.probability for r in records if r.probability is not None]
    return st.mean(values), st.pstdev(values), min(values), max(values)


def auc(records: Sequence) -> Optional[float]:
    """
    Rank-discrimination (ROC AUC) of the emitted probabilities.

    THE DECISIVE STATISTIC for this diagnostic. AUC depends only on the ORDERING
    of predictions, so any monotone flattening - dividing every deviation from the
    mean by ten, say - leaves it completely unchanged. That makes it able to
    answer the question Brier cannot:

        Brier falling + AUC holding   -> the ordering survived; the gain is in
                                         magnitudes, i.e. real calibration.
        Brier falling + AUC ->  0.5   -> the ordering was destroyed; the "gain"
                                         is degeneracy toward a constant.

    Computed by the rank-sum (Mann-Whitney) identity rather than by sweeping
    thresholds, with ties given average ranks so that a model emitting one
    repeated value scores exactly 0.5 instead of an accidental 1.0.
    """
    pairs = [
        (r.probability, 1 if r.outcome.name == "YES" else 0)
        for r in records
        if r.probability is not None and r.outcome is not None
    ]
    positives = sum(flag for _, flag in pairs)
    negatives = len(pairs) - positives
    if not positives or not negatives:
        return None

    pairs.sort(key=lambda item: item[0])
    ranks: List[float] = [0.0] * len(pairs)
    index = 0
    while index < len(pairs):
        stop = index
        while stop + 1 < len(pairs) and pairs[stop + 1][0] == pairs[index][0]:
            stop += 1
        average = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[position] = average
        index = stop + 1

    positive_rank_sum = sum(
        rank for rank, (_, flag) in zip(ranks, pairs, strict=True) if flag
    )
    return (positive_rank_sum - positives * (positives + 1) / 2.0) / (
        positives * negatives
    )



def main() -> int:
    dataset, missing = build_dataset(DEVELOPMENT_SEASONS)
    if missing:
        print(f"STOP: {len(missing)} cached windows missing, e.g. {missing[:3]}")
        return 1

    targets = [
        m
        for m in dataset
        if m.season in DEVELOPMENT_SEASONS and m.competition in TARGET_LEAGUES
    ]

    print("PROBABILITY SPREAD vs PRIOR STRENGTH (development seasons)")
    print("Collapsing sd alongside falling Brier = degeneracy, not skill.")
    print()
    print(f"{'arm':<16} {'mean':>8} {'sd':>8} {'min':>8} {'max':>8} {'AUC':>8}")

    baseline = replay(dataset, PoissonV1Adapter(), targets=targets)
    mean, sd, low, high = spread(baseline)
    base_auc = auc(baseline)
    print(
        f"{'baseline raw':<16} {mean:>8.4f} {sd:>8.4f} {low:>8.3f} {high:>8.3f} "
        f"{base_auc:>8.4f}"
    )

    for k in K_SWEEP:
        config = EstimatorConfig(
            k_goals_for=k,
            k_goals_against=k,
            k_prev_season=k,
            k_league=40.0,
        )
        records = replay(dataset, PoissonV1ShrunkAdapter(config), targets=targets)
        mean, sd, low, high = spread(records)
        value = auc(records)
        print(
            f"{'k=' + f'{k:g}':<16} {mean:>8.4f} {sd:>8.4f} {low:>8.3f} {high:>8.3f} "
            f"{value:>8.4f}"
        )


    observed = [
        1 if (r.outcome is not None and r.outcome.name == "YES") else 0
        for r in baseline
        if r.outcome is not None
    ]
    rate = sum(observed) / len(observed)
    print()
    print(f"observed BTTS base rate      {rate:.4f}")
    print(f"constant-predictor Brier     {rate * (1 - rate):.4f}")
    print("A configuration whose Brier approaches this while sd approaches 0 has")
    print("stopped discriminating between fixtures; it has not learned anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
