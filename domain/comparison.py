"""
Fair comparison between two models over IDENTICAL target fixtures (Epic 2C, Part 8).

    model A predictions          model B predictions
            |                            |
            +----------> intersect <-----+
                            |
                  the SAME target fixtures
                            |
              metrics_A  <--+-->  metrics_B

WHY THIS MODULE EXISTS
----------------------
The Epic 2B.3 baseline reported POISSON_V1 Brier 0.2657 over 6,955 scored
fixtures and a naive reference Brier 0.2479 over 7,025. Those two numbers were
NEVER comparable: the reference scored 70 fixtures the model refused, and the
refused ones are exactly the sparse-evidence fixtures where behaviour differs
most. Comparing them would attribute a coverage difference to a skill
difference - and would do so in the direction that flatters whichever model
declines the hardest fixtures.

Epic 2C's whole claim is that shrinkage helps sparse evidence. Shrinkage also
INCREASES coverage, because a rate that was previously unavailable now has a
prior-based estimate. Those two effects push the aggregate Brier in opposite
directions: the newly covered fixtures are the hard ones, so a shrunk model can
be better on every fixture both models score and still post a worse headline
average. Only an intersection comparison can separate them.

Therefore: no function in this module will score two models over different
fixture sets. `compare` intersects first and raises if asked to do otherwise.

This module contains NO probability mathematics and NO model. It re-uses the
Epic 2B.3 metric functions verbatim (`brier_score`, `log_loss`,
`calibration_table`, `summarise`) so that a number reported here is the same
number the harness reports - a second metric implementation is precisely how two
"identical" evaluations come to disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from domain.evaluation import (
    CalibrationBin,
    MetricSummary,
    PredictionRecord,
    calibration_table,
    summarise,
)

__all__ = [
    "FixtureKey",
    "FixtureIdentity",
    "fixture_key",
    "scored_keys",
    "intersect",
    "ExtremeProbabilityStats",
    "extreme_probability_stats",
    "ArmMetrics",
    "Comparison",
    "compare",
    "EvidenceBucketRow",
    "evidence_bucket_table",
    "evidence_bucket",
    "EVIDENCE_BUCKET_ORDER",
]


#: (competition, season, event_id). Event ids are unique per provider, but the
#: competition and season travel with the key so that a mismatch between two
#: runs over different corpora cannot silently produce a plausible-looking
#: intersection.
FixtureKey = Tuple[str, int, str]


class FixtureIdentity(Protocol):
    """
    Anything that identifies a fixture: a `PredictionRecord` or a
    `HistoricalMatch`.

    Structural rather than nominal on purpose. Evidence counts and promotion
    flags are derived from dataset rows, while metrics are computed from
    prediction records, and the two MUST agree on fixture identity or the Part 9
    bucket lookup silently misses every key. A second key builder for dataset
    rows would be the obvious way to make that happen, so there is only one.
    """

    @property
    def competition(self) -> str: ...

    @property
    def season(self) -> int: ...

    @property
    def event_id(self) -> str: ...


def fixture_key(record: FixtureIdentity) -> FixtureKey:
    """The identity of the fixture a prediction is about."""
    return (record.competition, record.season, record.event_id)


def scored_keys(records: Iterable[PredictionRecord]) -> set[FixtureKey]:
    """
    Keys of fixtures this model actually SCORED.

    Unevaluable records are excluded: a refusal is not a prediction, and letting
    one into an intersection would compare a probability against a blank.
    """
    return {fixture_key(r) for r in records if r.is_scored}


def intersect(
    left: Sequence[PredictionRecord],
    right: Sequence[PredictionRecord],
) -> Tuple[List[PredictionRecord], List[PredictionRecord]]:
    """
    Restrict both prediction sets to the fixtures BOTH models scored.

    Returns two lists in the SAME fixture order, so row i of each describes the
    same match. Order is derived from a sort on the key rather than from either
    input's ordering, so the pairing cannot depend on which model was replayed
    first.

    Duplicate keys within one model's records are rejected. A duplicate would
    make the pairing ambiguous and would double-count one fixture in a mean -
    silently, and only in one arm.
    """
    shared = scored_keys(left) & scored_keys(right)

    def indexed(records: Sequence[PredictionRecord]) -> Dict[FixtureKey, PredictionRecord]:
        out: Dict[FixtureKey, PredictionRecord] = {}
        for record in records:
            if not record.is_scored:
                continue
            key = fixture_key(record)
            if key not in shared:
                continue
            if key in out:
                raise ValueError(
                    f"duplicate scored prediction for {key!r} in model "
                    f"{record.model_id!r}; an intersection cannot be built from "
                    "ambiguous records"
                )
            out[key] = record
        return out

    left_index = indexed(left)
    right_index = indexed(right)
    order = sorted(shared)
    return (
        [left_index[key] for key in order],
        [right_index[key] for key in order],
    )


# ---------------------------------------------------------------------------
# Extreme probability diagnostics (Part 10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtremeProbabilityStats:
    """
    How often a model claims near-certainty, and how often it claims certainty.

    `exactly_zero` and `exactly_one` are counted SEPARATELY from the <=0.05 and
    >=0.95 tails because they are a different kind of statement. 0.03 is an
    aggressive estimate; exactly 0.0 asserts that the outcome is impossible, and
    is unfalsifiable-by-evidence in a way no finite sample supports. GG-028 is
    the mechanism that produces it.

    No clipping is applied anywhere in this module. Clipping would make these
    counters read zero while the underlying estimate stayed just as wrong, and
    would improve log loss without improving a single estimate.
    """

    scored: int
    at_or_below_05: int
    at_or_above_95: int
    exactly_zero: int
    exactly_one: int

    @property
    def extreme(self) -> int:
        return self.at_or_below_05 + self.at_or_above_95

    @property
    def extreme_rate(self) -> Optional[float]:
        if self.scored == 0:
            return None
        return self.extreme / self.scored

    @property
    def certain(self) -> int:
        """Predictions asserting impossibility or inevitability."""
        return self.exactly_zero + self.exactly_one


def extreme_probability_stats(
    records: Iterable[PredictionRecord],
) -> ExtremeProbabilityStats:
    """Count extreme and exactly-certain probabilities among scored records."""
    scored = at_or_below = at_or_above = zeros = ones = 0
    for record in records:
        if not record.is_scored or record.probability is None:
            continue
        scored += 1
        probability = record.probability
        if probability <= 0.05:
            at_or_below += 1
        if probability >= 0.95:
            at_or_above += 1
        # Exact comparison is intended: the question is whether the model
        # emitted the value 0.0, not whether it emitted something small.
        if probability == 0.0:
            zeros += 1
        if probability == 1.0:
            ones += 1
    return ExtremeProbabilityStats(
        scored=scored,
        at_or_below_05=at_or_below,
        at_or_above_95=at_or_above,
        exactly_zero=zeros,
        exactly_one=ones,
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmMetrics:
    """One model's numbers, and the fixture count they were computed over."""

    model_id: str
    model_version: str
    raw_targets: int
    raw_scored: int
    summary: MetricSummary
    extremes: ExtremeProbabilityStats

    @property
    def raw_coverage(self) -> Optional[float]:
        """Coverage BEFORE intersection - reported separately, never compared."""
        if self.raw_targets == 0:
            return None
        return self.raw_scored / self.raw_targets

    @property
    def calibration(self) -> List[CalibrationBin]:
        return list(self.summary.calibration)


@dataclass(frozen=True)
class Comparison:
    """
    Two models on the SAME fixtures, plus each model's raw coverage.

    The three quantities Part 8 requires are distinct fields, so a report cannot
    accidentally present an intersection metric as a coverage claim or vice
    versa: `left.raw_coverage` / `right.raw_coverage` describe availability, and
    `left.summary` / `right.summary` describe quality on `intersection_size`
    identical fixtures.
    """

    left: ArmMetrics
    right: ArmMetrics
    intersection_size: int
    left_only: int
    right_only: int

    @property
    def brier_delta(self) -> Optional[float]:
        """right - left. Negative means `right` is better (lower is better)."""
        if self.left.summary.brier is None or self.right.summary.brier is None:
            return None
        return self.right.summary.brier - self.left.summary.brier

    @property
    def log_loss_delta(self) -> Optional[float]:
        if self.left.summary.log_loss is None or self.right.summary.log_loss is None:
            return None
        return self.right.summary.log_loss - self.left.summary.log_loss


def compare(
    left: Sequence[PredictionRecord],
    right: Sequence[PredictionRecord],
    *,
    calibration_bins: int = 10,
) -> Comparison:
    """
    Compare two models fairly.

    Intersection is computed FIRST and both summaries are built from it, so
    there is no code path through this function that scores the two arms over
    different fixtures. That is a structural guarantee rather than a convention:
    a caller cannot opt out of it by passing a flag, because no flag exists.
    """
    left_records = list(left)
    right_records = list(right)
    left_paired, right_paired = intersect(left_records, right_records)

    left_scored = scored_keys(left_records)
    right_scored = scored_keys(right_records)

    def arm(
        records: Sequence[PredictionRecord],
        paired: Sequence[PredictionRecord],
        all_records: Sequence[PredictionRecord],
    ) -> ArmMetrics:
        model_id = records[0].model_id if records else "UNKNOWN"
        model_version = records[0].model_version if records else "UNKNOWN"
        summary = summarise(
            paired,
            model_id=model_id,
            model_version=model_version,
            bin_count=calibration_bins,
        )
        return ArmMetrics(
            model_id=model_id,
            model_version=model_version,
            raw_targets=len(all_records),
            raw_scored=sum(1 for r in all_records if r.is_scored),
            summary=summary,
            extremes=extreme_probability_stats(paired),
        )

    return Comparison(
        left=arm(left_records, left_paired, left_records),
        right=arm(right_records, right_paired, right_records),
        intersection_size=len(left_paired),
        left_only=len(left_scored - right_scored),
        right_only=len(right_scored - left_scored),
    )


# ---------------------------------------------------------------------------
# Evidence buckets (Part 9)
# ---------------------------------------------------------------------------

#: Reported in this order regardless of dictionary insertion.
EVIDENCE_BUCKET_ORDER: Tuple[str, ...] = ("0", "1-2", "3-5", "6-9", "10+")


def evidence_bucket(matches: int) -> str:
    """
    Bucket a count of prior relevant venue matches. Boundaries as Part 9 states.

    Deliberately the same boundaries as the harness's own `_evidence_bucket`;
    duplicated as a public function here because the bucket must be computable
    from ONE arm's sample count and then applied to BOTH arms (see
    `evidence_bucket_table`), which the harness's per-run breakdown cannot do.
    """
    if matches < 0:
        raise ValueError(f"matches must be >= 0; got {matches!r}")
    if matches == 0:
        return "0"
    if matches <= 2:
        return "1-2"
    if matches <= 5:
        return "3-5"
    if matches <= 9:
        return "6-9"
    return "10+"


@dataclass(frozen=True)
class EvidenceBucketRow:
    """One row of the Part 9 table: both arms, one evidence level, same N."""

    bucket: str
    n: int
    baseline: MetricSummary
    shrunk: MetricSummary
    baseline_extremes: ExtremeProbabilityStats
    shrunk_extremes: ExtremeProbabilityStats

    @property
    def brier_delta(self) -> Optional[float]:
        if self.baseline.brier is None or self.shrunk.brier is None:
            return None
        return self.shrunk.brier - self.baseline.brier

    @property
    def log_loss_delta(self) -> Optional[float]:
        if self.baseline.log_loss is None or self.shrunk.log_loss is None:
            return None
        return self.shrunk.log_loss - self.baseline.log_loss


def evidence_bucket_table(
    baseline: Sequence[PredictionRecord],
    shrunk: Sequence[PredictionRecord],
    *,
    evidence_of: Dict[FixtureKey, int],
    calibration_bins: int = 10,
) -> List[EvidenceBucketRow]:
    """
    The Part 9 table: performance by evidence level, on identical fixtures.

    `evidence_of` maps a fixture to its evidence count and is supplied by the
    CALLER, deliberately. The two arms do not agree on what their own
    `home_sample` means - the raw baseline counts every prior venue match in the
    competition, while the shrunk estimator counts current-season venue matches,
    which is the sparsity the Epic is about. Bucketing each arm by its own field
    would put the same fixture in different rows for each model and quietly
    destroy the comparison. One externally supplied count per fixture is applied
    to both arms, so a row always contains the same matches on both sides.

    Buckets with no fixtures are omitted rather than emitted with N=0, so a row
    in the output always carries evidence.
    """
    baseline_paired, shrunk_paired = intersect(baseline, shrunk)

    missing = [
        fixture_key(record)
        for record in baseline_paired
        if fixture_key(record) not in evidence_of
    ]
    if missing:
        # Defaulting to zero would silently pile unknown-evidence fixtures into
        # the "0" bucket - the exact bucket whose behaviour this Epic claims to
        # have fixed.
        raise KeyError(
            f"{len(missing)} intersection fixtures have no evidence count; "
            f"first missing: {missing[0]!r}"
        )

    grouped: Dict[str, Tuple[List[PredictionRecord], List[PredictionRecord]]] = {}
    for base_record, shrunk_record in zip(baseline_paired, shrunk_paired, strict=False):
        bucket = evidence_bucket(evidence_of[fixture_key(base_record)])
        left_list, right_list = grouped.setdefault(bucket, ([], []))
        left_list.append(base_record)
        right_list.append(shrunk_record)

    rows: List[EvidenceBucketRow] = []
    for bucket in EVIDENCE_BUCKET_ORDER:
        if bucket not in grouped:
            continue
        base_records, shrunk_records = grouped[bucket]
        rows.append(
            EvidenceBucketRow(
                bucket=bucket,
                n=len(base_records),
                baseline=summarise(
                    base_records,
                    model_id=base_records[0].model_id,
                    model_version=base_records[0].model_version,
                    bin_count=calibration_bins,
                ),
                shrunk=summarise(
                    shrunk_records,
                    model_id=shrunk_records[0].model_id,
                    model_version=shrunk_records[0].model_version,
                    bin_count=calibration_bins,
                ),
                baseline_extremes=extreme_probability_stats(base_records),
                shrunk_extremes=extreme_probability_stats(shrunk_records),
            )
        )
    return rows


def calibration_on_intersection(
    records: Sequence[PredictionRecord],
    *,
    bins: int = 10,
) -> List[CalibrationBin]:
    """
    Calibration for an already-intersected record set (Part 11).

    Thin by design: it exists so a caller reaches the Epic 2B.3 calibration
    machinery rather than writing a second binning rule whose edge convention
    might differ.
    """
    return calibration_table(records, bin_count=bins)
