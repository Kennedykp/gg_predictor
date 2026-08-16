"""
Discrimination metrics: can a model ORDER fixtures, not merely score them well?

WHY THIS MODULE EXISTS
----------------------
Epic 2C established that Brier score is an unsafe objective for this model
(GG-029). Because shrinkage flattens predictions toward the base rate, and
because the BTTS base rate is near 0.52, a *constant* predictor scores Brier
0.2496 while raw POISSON_V1 scores 0.2615. Minimising Brier therefore drives the
prior strength k toward infinity - the search has no interior optimum, and the
"best" model by that objective is the one that has stopped saying anything.

The missing measurement was discrimination. AUC depends ONLY on the ordering of
predictions, so it is invariant to exactly the monotone flattening that Brier
rewards: shrinking every prediction toward the mean cannot change AUC at all.
That invariance is the point. A candidate that improves Brier while leaving AUC
unchanged has improved its calibration, not its forecasting, and Epic 2D must be
able to tell those two apart mechanically rather than by argument.

This module contains NO model and NO probability construction. It consumes the
`PredictionRecord`s the Epic 2B.3 harness already produces and reuses that
module's scored/unscored semantics, so a refusal is never silently ranked as
though it were a prediction.

TIES ARE THE SUBTLE PART
------------------------
Ties are not an edge case here, they are the expected state. Epic 2C measured
predictions collapsing into a narrow band (sd 0.047 at k=1000), and any two
fixtures whose inputs round to the same rate produce byte-identical
probabilities. A naive "count pairs where p_pos > p_neg" would silently discard
every tied pair and report an optimistic AUC; a constant predictor would score
0.0 rather than the correct 0.5. This module therefore uses midranks, which
credit a tied pair exactly 0.5 - the mathematically correct treatment, and the
one that makes "predicts nothing" register as 0.5 instead of looking like a
catastrophic or a perfect model depending on which way the comparison is
written.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from domain.evaluation import (
    BttsOutcome,
    PredictionRecord,
    outcome_to_y,
)

__all__ = [
    "roc_auc",
    "auc_from_labelled",
    "DiscriminationSummary",
    "summarise_discrimination",
    "PredictionSpread",
    "prediction_spread",
    "constant_predictor_brier",
    "BootstrapInterval",
    "paired_auc_delta",
    "paired_brier_delta",
    "auc_by_group",

    "DEFAULT_BOOTSTRAP_ITERATIONS",
    "DEFAULT_BOOTSTRAP_SEED",
]


#: Enough resamples for a stable 95% interval on a few thousand fixtures without
#: making the experiment slow to re-run. Fixed rather than tunable per call site
#: so two reported intervals are always built the same way.
DEFAULT_BOOTSTRAP_ITERATIONS = 2000

#: Bootstrap resampling is random, and an unseeded interval would change every
#: time the report was regenerated - which would make "the difference is inside
#: the interval" an unreproducible claim.
DEFAULT_BOOTSTRAP_SEED = 20260815


# ---------------------------------------------------------------------------
# ROC AUC
# ---------------------------------------------------------------------------


def auc_from_labelled(pairs: Sequence[Tuple[float, int]]) -> Optional[float]:
    """
    ROC AUC from (probability, label) pairs, with correct tie handling.

    Computed as the Mann-Whitney U statistic over midranks:

        AUC = (R_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

    where `R_pos` is the sum of the midranks of the positive cases. This is
    algebraically identical to "the probability that a randomly chosen positive
    is ranked above a randomly chosen negative, counting ties as half", but it
    is O(n log n) rather than O(n^2) - which matters because the paired
    bootstrap below evaluates this thousands of times.

    Returns None when AUC is UNDEFINED rather than substituting a value: with no
    positives or no negatives there are no pairs to rank, and returning 0.5
    would report "no discriminatory power" for what is really "no measurement".
    Epic 2C's GG-028 is a standing reminder of what a plausible-looking
    substituted number costs.
    """
    if not pairs:
        return None

    positives = sum(1 for _, label in pairs if label == 1)
    negatives = len(pairs) - positives
    if positives == 0 or negatives == 0:
        return None

    ordered = sorted(pairs, key=lambda item: item[0])

    # Midranks: every member of a tied group receives the average of the ranks
    # that group spans, so a tied pair contributes exactly 0.5.
    rank_sum_positive = 0.0
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        # Ranks are 1-based and inclusive of both endpoints.
        midrank = (index + 1 + end + 1) / 2.0
        for position in range(index, end + 1):
            if ordered[position][1] == 1:
                rank_sum_positive += midrank
        index = end + 1

    u_statistic = rank_sum_positive - positives * (positives + 1) / 2.0
    return u_statistic / (positives * negatives)


def _labelled(records: Iterable[PredictionRecord]) -> List[Tuple[float, int]]:
    """(probability, y) for scored records only. Refusals are not predictions."""
    pairs: List[Tuple[float, int]] = []
    for record in records:
        if not record.is_scored or record.probability is None:
            continue
        if record.outcome is BttsOutcome.UNKNOWN:
            continue
        pairs.append((record.probability, outcome_to_y(record.outcome)))
    return pairs


def roc_auc(records: Iterable[PredictionRecord]) -> Optional[float]:
    """ROC AUC over scored predictions. None when undefined (see above)."""
    return auc_from_labelled(_labelled(records))


# ---------------------------------------------------------------------------
# Prediction spread - the diagnostic that exposes flattening
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictionSpread:
    """
    How much a model actually varies its answer.

    Reported alongside every score because it is the direct evidence for
    "flattening": a model whose sd collapses toward zero has stopped
    distinguishing fixtures, and `distinct` counts how many different answers it
    is capable of giving at all. Brier improves under exactly this degradation,
    so the two numbers must travel together.
    """

    n: int
    mean: Optional[float]
    sd: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    distinct: int

    @property
    def span(self) -> Optional[float]:
        if self.minimum is None or self.maximum is None:
            return None
        return self.maximum - self.minimum


def prediction_spread(records: Iterable[PredictionRecord]) -> PredictionSpread:
    """Distribution of the probabilities a model emitted."""
    values = [
        record.probability
        for record in records
        if record.is_scored and record.probability is not None
    ]
    if not values:
        return PredictionSpread(
            n=0, mean=None, sd=None, minimum=None, maximum=None, distinct=0
        )
    mean = sum(values) / len(values)
    if len(values) > 1:
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        sd: Optional[float] = math.sqrt(variance)
    else:
        sd = None
    return PredictionSpread(
        n=len(values),
        mean=mean,
        sd=sd,
        minimum=min(values),
        maximum=max(values),
        distinct=len({round(value, 12) for value in values}),
    )


def constant_predictor_brier(records: Iterable[PredictionRecord]) -> Optional[float]:
    """
    Brier score of always predicting the observed base rate of THIS set.

    The reference every candidate must clear before any Brier improvement can be
    called skill. Note this is a deliberately generous benchmark - it uses the
    realised base rate of the very fixtures being scored, which a real forecaster
    could not know in advance - so beating it is meaningful and losing to it,
    as raw POISSON_V1 does, is damning.

    For a constant prediction p over labels y, the mean squared error reduces to
    p^2 - 2*p*rate + rate, evaluated here directly at p = rate.
    """
    labelled = _labelled(records)
    if not labelled:
        return None
    rate = sum(label for _, label in labelled) / len(labelled)
    return sum((rate - label) ** 2 for _, label in labelled) / len(labelled)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscriminationSummary:
    """AUC plus the context needed to interpret it honestly."""

    model_id: str
    model_version: str
    scored: int
    positives: int
    negatives: int
    auc: Optional[float]
    spread: PredictionSpread
    constant_brier: Optional[float]

    @property
    def prevalence(self) -> Optional[float]:
        total = self.positives + self.negatives
        if total == 0:
            return None
        return self.positives / total


def summarise_discrimination(
    records: Sequence[PredictionRecord],
    *,
    model_id: Optional[str] = None,
    model_version: Optional[str] = None,
) -> DiscriminationSummary:
    """Discrimination summary for one model over one fixture set."""
    labelled = _labelled(records)
    positives = sum(1 for _, label in labelled if label == 1)
    resolved_id = model_id or (records[0].model_id if records else "UNKNOWN")
    resolved_version = model_version or (
        records[0].model_version if records else "UNKNOWN"
    )
    return DiscriminationSummary(
        model_id=resolved_id,
        model_version=resolved_version,
        scored=len(labelled),
        positives=positives,
        negatives=len(labelled) - positives,
        auc=auc_from_labelled(labelled),
        spread=prediction_spread(records),
        constant_brier=constant_predictor_brier(records),
    )


# ---------------------------------------------------------------------------
# Paired bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapInterval:
    """
    A difference and its uncertainty.

    `contains_zero` is the whole reason this exists. Epic 2D must not report
    "AUC 0.538 beats 0.535" as an improvement when resampling the same fixtures
    would routinely reverse the sign.
    """

    point: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    iterations: int
    seed: int
    resamples_used: int

    @property
    def contains_zero(self) -> bool:
        """True when the data cannot distinguish the two models."""
        if self.lower is None or self.upper is None:
            return True
        return self.lower <= 0.0 <= self.upper

    @property
    def verdict(self) -> str:
        """One of: UNDETERMINED, INDISTINGUISHABLE, IMPROVEMENT, DEGRADATION."""
        if self.point is None or self.lower is None or self.upper is None:
            return "UNDETERMINED"
        if self.contains_zero:
            return "INDISTINGUISHABLE"
        return "IMPROVEMENT" if self.point > 0 else "DEGRADATION"


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile of a sorted-on-demand sample."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _paired_delta(
    left: Sequence[Tuple[float, int]],
    right: Sequence[Tuple[float, int]],
    statistic: str,
    *,
    iterations: int,
    seed: int,
    confidence: float,
) -> BootstrapInterval:
    """
    Bootstrap the difference `right - left` by resampling FIXTURES, not values.

    Paired deliberately: both arms are evaluated on the SAME resampled fixture
    indices in every iteration. Resampling the two models independently would
    add variance that has nothing to do with the difference between them and
    would widen every interval toward "indistinguishable" - the two arms are
    highly correlated because they see identical matches, and a paired design is
    what preserves that correlation.
    """
    if len(left) != len(right):
        raise ValueError(
            f"paired bootstrap needs equal-length aligned arms; "
            f"got {len(left)} and {len(right)}"
        )

    def compute(
        sample_left: Sequence[Tuple[float, int]],
        sample_right: Sequence[Tuple[float, int]],
    ) -> Optional[float]:
        if statistic == "auc":
            left_value = auc_from_labelled(sample_left)
            right_value = auc_from_labelled(sample_right)
        elif statistic == "brier":
            left_value = (
                sum((p - y) ** 2 for p, y in sample_left) / len(sample_left)
                if sample_left
                else None
            )
            right_value = (
                sum((p - y) ** 2 for p, y in sample_right) / len(sample_right)
                if sample_right
                else None
            )
        else:  # pragma: no cover - guarded by the public wrappers
            raise ValueError(f"unknown statistic {statistic!r}")
        if left_value is None or right_value is None:
            return None
        return right_value - left_value

    point = compute(left, right)
    if point is None or not left:
        return BootstrapInterval(
            point=point,
            lower=None,
            upper=None,
            iterations=iterations,
            seed=seed,
            resamples_used=0,
        )

    rng = random.Random(seed)
    size = len(left)
    deltas: List[float] = []
    for _ in range(iterations):
        indices = [rng.randrange(size) for _ in range(size)]
        resampled_left = [left[index] for index in indices]
        resampled_right = [right[index] for index in indices]
        delta = compute(resampled_left, resampled_right)
        # A resample can be degenerate (all one class), leaving AUC undefined.
        # Those are skipped and counted, not silently replaced with 0.5.
        if delta is not None:
            deltas.append(delta)

    if not deltas:
        return BootstrapInterval(
            point=point,
            lower=None,
            upper=None,
            iterations=iterations,
            seed=seed,
            resamples_used=0,
        )

    tail = (1.0 - confidence) / 2.0
    return BootstrapInterval(
        point=point,
        lower=_percentile(deltas, tail),
        upper=_percentile(deltas, 1.0 - tail),
        iterations=iterations,
        seed=seed,
        resamples_used=len(deltas),
    )


def _aligned(
    left: Sequence[PredictionRecord],
    right: Sequence[PredictionRecord],
) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
    """
    Turn two already-intersected record lists into aligned labelled arms.

    Requires the callers to have intersected first (`domain.comparison.intersect`)
    and verifies the alignment by fixture identity rather than trusting order.
    A silent misalignment here would compare model A's prediction for one match
    against model B's label for another, and would still produce a plausible
    number.
    """
    if len(left) != len(right):
        raise ValueError(
            f"arms must be intersected and aligned before comparison; "
            f"got {len(left)} and {len(right)}"
        )
    left_pairs: List[Tuple[float, int]] = []
    right_pairs: List[Tuple[float, int]] = []
    for left_record, right_record in zip(left, right, strict=True):
        if (
            left_record.competition != right_record.competition
            or left_record.season != right_record.season
            or left_record.event_id != right_record.event_id
        ):
            raise ValueError(
                "arms are misaligned: "
                f"{left_record.competition}/{left_record.season}/{left_record.event_id}"
                " vs "
                f"{right_record.competition}/{right_record.season}/{right_record.event_id}"
            )
        if not left_record.is_scored or not right_record.is_scored:
            continue
        if left_record.probability is None or right_record.probability is None:
            continue
        if left_record.outcome is BttsOutcome.UNKNOWN:
            continue
        label = outcome_to_y(left_record.outcome)
        left_pairs.append((left_record.probability, label))
        right_pairs.append((right_record.probability, label))
    return left_pairs, right_pairs


def paired_auc_delta(
    left: Sequence[PredictionRecord],
    right: Sequence[PredictionRecord],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> BootstrapInterval:
    """
    Paired bootstrap interval for AUC(right) - AUC(left). Positive favours right.
    """
    left_pairs, right_pairs = _aligned(left, right)
    return _paired_delta(
        left_pairs,
        right_pairs,
        "auc",
        iterations=iterations,
        seed=seed,
        confidence=confidence,
    )


def paired_brier_delta(
    left: Sequence[PredictionRecord],
    right: Sequence[PredictionRecord],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> BootstrapInterval:
    """
    Paired bootstrap interval for Brier(right) - Brier(left).

    NOTE THE SIGN CONVENTION DIFFERENCE: lower Brier is better, so a NEGATIVE
    point estimate favours `right`. `verdict` is therefore not meaningful for
    this statistic and callers should read `point`/`lower`/`upper` directly. The
    asymmetry is left explicit rather than hidden behind a flipped sign, because
    silently negating one metric is how a report ends up claiming the opposite of
    what it measured.
    """
    left_pairs, right_pairs = _aligned(left, right)
    return _paired_delta(
        left_pairs,
        right_pairs,
        "brier",
        iterations=iterations,
        seed=seed,
        confidence=confidence,
    )


def auc_by_group(
    records: Sequence[PredictionRecord],
    *,
    group_of: Dict[Tuple[str, int, str], str],
) -> Dict[str, Optional[float]]:
    """
    AUC within each group (league, season, evidence bucket).

    Grouping is supplied by the caller for the same reason Epic 2C's evidence
    buckets were: the two arms must be split by ONE externally supplied key, or
    the same fixture lands in different groups for different models and the
    comparison quietly stops being like-for-like.
    """
    grouped: Dict[str, List[Tuple[float, int]]] = {}
    for record in records:
        if not record.is_scored or record.probability is None:
            continue
        if record.outcome is BttsOutcome.UNKNOWN:
            continue
        key = (record.competition, record.season, record.event_id)
        if key not in group_of:
            raise KeyError(f"no group for fixture {key!r}")
        grouped.setdefault(group_of[key], []).append(
            (record.probability, outcome_to_y(record.outcome))
        )
    return {name: auc_from_labelled(pairs) for name, pairs in grouped.items()}
