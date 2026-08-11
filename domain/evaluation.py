"""
Evaluation contracts and probability metrics (Epic 2B.3).

This module is the REFEREE, not a model. It answers one question:

    given a probability that was produced using only pre-kickoff information,
    how good was it once the real result became known?

Three separations are load-bearing, and each exists because collapsing it would
produce a number that looks fine and means nothing:

    OUTCOME vs UNKNOWN      a fixture with no usable result is UNEVALUABLE.
                            It never becomes BTTS=0. A missing result scored as
                            a negative outcome silently rewards models that
                            predict low probabilities.

    QUALITY vs COVERAGE     "how good are the predictions it made" and "how
                            often could it predict at all" are different
                            questions. POISSON_V1 cannot produce a probability
                            early in a season, and averaging that away would
                            hide the single most important fact about it.

    MODEL vs REFERENCE      a naive base rate is not a competitor. It is the
                            yardstick that makes a Brier score interpretable.

NO ODDS. Nothing here imports odds, prices, edges or thresholds, and nothing
may. Probability quality is a football question; betting value is blocked by
LEAK-001 and is not measured in this Epic. See `tests/regression/
test_evaluation_leakage.py`, which enforces that as an import-level guard.

This module performs NO model mathematics. It never calls a model; it receives
probabilities and grades them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "LOG_LOSS_EPSILON",
    "BttsOutcome",
    "UnevaluableReason",
    "PredictionRecord",
    "CalibrationBin",
    "MetricSummary",
    "btts_outcome",
    "validate_probability",
    "brier_score",
    "log_loss",
    "calibration_table",
    "summarise",
    "prediction_sort_key",
    "sort_predictions",
    "to_json_dict",
]

# Bumped when the prediction record shape changes. Written into every artifact
# so results produced under different semantics are never silently merged.
EVALUATION_SCHEMA_VERSION = "2b3.1"

# Log loss is undefined at p=0 and p=1 when the outcome disagrees: -log(0) is
# infinite. A single such prediction would make the whole mean infinite,
# destroying every other prediction's contribution.
#
# The clip applies ONLY inside the logarithm. The stored and reported
# probability is always the model's original, unmodified value. Clipping the
# reported number would be quietly editing a model's output to improve its own
# score - which is the difference between a referee and an accomplice.
LOG_LOSS_EPSILON = 1e-15


class BttsOutcome(str, Enum):
    """
    What actually happened, from a completed fixture.

    UNKNOWN is a first-class value, not an error. A postponed, abandoned or
    cancelled fixture is genuine history with no result, and it must be
    excluded from scoring rather than counted as NO.
    """

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class UnevaluableReason(str, Enum):
    """
    Why a target fixture produced no scored prediction.

    Reported per target, never aggregated away. `INSUFFICIENT_HISTORY` in
    particular is the honest description of POISSON_V1 in August, and Epic 2C
    exists precisely to change that number - so it must be measurable now.
    """

    NO_RESULT = "NO_RESULT"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MODEL_RETURNED_NONE = "MODEL_RETURNED_NONE"
    MODEL_ERROR = "MODEL_ERROR"
    NOT_MODEL_ELIGIBLE = "NOT_MODEL_ELIGIBLE"


def btts_outcome(
    home_goals: Optional[int],
    away_goals: Optional[int],
    *,
    completed: bool = True,
) -> BttsOutcome:
    """
    Derive the BTTS outcome from a final score.

        BTTS = YES  iff  home_goals > 0 AND away_goals > 0

    A missing score, or a fixture that never completed, yields UNKNOWN. It does
    NOT yield NO. The two are different facts and only one of them is evidence:
    0-0 is a real goalless draw, while "no score recorded" is an absence, and
    counting the absence as a goalless draw would fabricate an observation.

    Booleans are rejected explicitly. `True > 0` is True in Python, so a stray
    boolean would otherwise be read as a scoreline.
    """
    if not completed:
        return BttsOutcome.UNKNOWN
    if home_goals is None or away_goals is None:
        return BttsOutcome.UNKNOWN
    for goals in (home_goals, away_goals):
        if isinstance(goals, bool) or not isinstance(goals, int) or goals < 0:
            return BttsOutcome.UNKNOWN
    return BttsOutcome.YES if home_goals > 0 and away_goals > 0 else BttsOutcome.NO


def outcome_to_y(outcome: BttsOutcome) -> int:
    """The 0/1 label used by the metrics. UNKNOWN has no label and raises."""
    if outcome is BttsOutcome.YES:
        return 1
    if outcome is BttsOutcome.NO:
        return 0
    raise ValueError(
        "BttsOutcome.UNKNOWN has no numeric label. An unevaluable fixture must "
        "be excluded from scoring, never coerced to 0."
    )


def validate_probability(value: Any) -> float:
    """
    Accept a real probability in [0, 1]; refuse anything else.

    NaN is refused explicitly, and the check is `value != value` because every
    comparison involving NaN is False - `0 <= nan <= 1` is False, but so is
    `nan < 0`, so a naive range test would reject it for the wrong reason and a
    naive `not (nan < 0)` would let it through. A NaN reaching the metrics
    would poison every aggregate it touched, silently.

    Booleans are refused: `True` is a valid float in Python's eyes and would be
    read as a probability of 1.0.
    """
    if isinstance(value, bool):
        raise ValueError(f"probability must be a real number, got bool {value!r}")
    if not isinstance(value, (int, float)):
        raise ValueError(f"probability must be a real number, got {type(value).__name__}")
    numeric = float(value)
    if numeric != numeric:
        raise ValueError("probability is NaN")
    if math.isinf(numeric):
        raise ValueError("probability is infinite")
    if numeric < 0.0 or numeric > 1.0:
        raise ValueError(f"probability must lie in [0, 1]; got {numeric!r}")
    return numeric


@dataclass(frozen=True)
class PredictionRecord:
    """
    One model's attempt at one target fixture, and what happened.

    Carries enough provenance to reproduce the prediction without storing the
    history itself: the sample sizes and the count of prior matches identify
    the evidence set, while `event_id` plus the dataset checksum identify the
    data. Storing every contributing match per prediction would multiply the
    dataset by its own length for no additional traceability.

    `probability is None` exactly when `unevaluable_reason is not None`. There
    is no state in which a fixture has both a score and a reason it could not
    be scored, and `__post_init__` enforces that rather than trusting callers.
    """

    model_id: str
    model_version: str
    competition: str
    season: int
    event_id: str
    kickoff: datetime
    home_team_id: str
    away_team_id: str
    outcome: BttsOutcome
    probability: Optional[float] = None
    unevaluable_reason: Optional[UnevaluableReason] = None
    detail: Optional[str] = None

    # Provenance of the evidence set (never the evidence itself).
    history_matches: int = 0
    home_sample: int = 0
    away_sample: int = 0
    league_sample: int = 0

    def __post_init__(self) -> None:
        if self.probability is None and self.unevaluable_reason is None:
            raise ValueError(
                f"prediction for {self.event_id} has no probability and no reason. "
                "An unevaluable target must say why."
            )
        if self.probability is not None and self.unevaluable_reason is not None:
            raise ValueError(
                f"prediction for {self.event_id} has both a probability and an "
                "unevaluable reason; those states are mutually exclusive."
            )
        if self.probability is not None:
            validate_probability(self.probability)
        if self.kickoff.tzinfo is None:
            raise ValueError("PredictionRecord.kickoff must be timezone-aware")

    @property
    def is_scored(self) -> bool:
        """
        True when this record contributes to Brier / log loss.

        BOTH conditions are required. A probability against an UNKNOWN outcome
        is not scoreable, and an outcome without a probability is coverage
        information only.
        """
        return self.probability is not None and self.outcome is not BttsOutcome.UNKNOWN


@dataclass(frozen=True)
class CalibrationBin:
    """One probability band, and what happened inside it."""

    lower: float
    upper: float
    count: int
    mean_predicted: Optional[float]
    observed_rate: Optional[float]

    @property
    def label(self) -> str:
        """`[0.30, 0.40)` - closed below, open above, except the final bin."""
        closing = "]" if self.upper >= 1.0 else ")"
        return f"[{self.lower:.2f}, {self.upper:.2f}{closing}"

    @property
    def gap(self) -> Optional[float]:
        """
        observed - predicted. Positive means the model UNDER-predicted.

        Signed deliberately: the direction is the actionable part, and an
        absolute value would let a systematically over-confident model and a
        systematically under-confident one look identical.
        """
        if self.mean_predicted is None or self.observed_rate is None:
            return None
        return self.observed_rate - self.mean_predicted


@dataclass(frozen=True)
class MetricSummary:
    """Aggregate quality AND coverage. Both, always, in one object."""

    model_id: str
    model_version: str
    scored: int
    targets: int
    unevaluable: Dict[str, int]
    brier: Optional[float]
    log_loss: Optional[float]
    mean_predicted: Optional[float]
    observed_rate: Optional[float]
    accuracy_at_half: Optional[float]
    calibration: List[CalibrationBin]

    @property
    def coverage(self) -> Optional[float]:
        """
        scored / targets.

        Reported alongside every quality metric, never separately: a superb
        Brier score over 4% of fixtures is not a good model, and the two
        numbers are only meaningful together.
        """
        if self.targets <= 0:
            return None
        return self.scored / self.targets


def _scored(predictions: Iterable[PredictionRecord]) -> List[PredictionRecord]:
    return [p for p in predictions if p.is_scored]


def brier_score(predictions: Iterable[PredictionRecord]) -> Optional[float]:
    """
    Mean squared error of the probability.

        BS = (1/N) * sum (p_i - y_i)^2

    Range [0, 1], lower is better. Returns None for an empty set - a mean of
    nothing is not 0.0, and 0.0 here would read as a perfect score.
    """
    scored = _scored(predictions)
    if not scored:
        return None
    total = 0.0
    for prediction in scored:
        probability = float(prediction.probability or 0.0)
        y = outcome_to_y(prediction.outcome)
        total += (probability - y) ** 2
    return total / len(scored)


def log_loss(
    predictions: Iterable[PredictionRecord],
    *,
    epsilon: float = LOG_LOSS_EPSILON,
) -> Optional[float]:
    """
    Binary cross-entropy.

        LL = -(1/N) * sum [ y*log(p) + (1-y)*log(1-p) ]

    Lower is better, unbounded above. The epsilon clamps the LOGARITHM's
    argument only; `prediction.probability` is never modified, and the clamp is
    an explicit named constant rather than a magic number buried in the
    expression, because its size directly determines how harshly a confidently
    wrong prediction is punished.
    """
    scored = _scored(predictions)
    if not scored:
        return None
    total = 0.0
    for prediction in scored:
        probability = float(prediction.probability or 0.0)
        y = outcome_to_y(prediction.outcome)
        clamped = min(max(probability, epsilon), 1.0 - epsilon)
        total += y * math.log(clamped) + (1 - y) * math.log(1.0 - clamped)
    return -total / len(scored)


def _bin_edges(bin_count: int) -> List[Tuple[float, float]]:
    width = 1.0 / bin_count
    return [(i * width, (i + 1) * width) for i in range(bin_count)]


def calibration_table(
    predictions: Iterable[PredictionRecord],
    *,
    bin_count: int = 10,
) -> List[CalibrationBin]:
    """
    Observed BTTS rate against predicted probability, by band.

    BINNING CONVENTION, stated exactly because it decides where boundary
    predictions land:

        every bin is [lower, upper)  - closed below, open above
        the FINAL bin is [0.90, 1.00] - closed at both ends

    Without that last clause p=1.0 falls outside every bin and vanishes from
    the table while still counting in the Brier score, so the two reports would
    silently disagree.

    Empty bins are RETAINED with count=0 and None statistics. Dropping them
    would make a model that never predicts above 0.5 look identically shaped to
    one that does.
    """
    if bin_count <= 0:
        raise ValueError("bin_count must be positive")

    scored = _scored(predictions)
    buckets: List[List[PredictionRecord]] = [[] for _ in range(bin_count)]

    for prediction in scored:
        probability = float(prediction.probability or 0.0)
        index = int(probability * bin_count)
        if index >= bin_count:  # p == 1.0 exactly
            index = bin_count - 1
        buckets[index].append(prediction)

    table: List[CalibrationBin] = []
    # strict=True: the edges and the buckets are both built from `bin_count`,
    # so a length mismatch would mean a bug that silently dropped a bin.
    for (lower, upper), bucket in zip(_bin_edges(bin_count), buckets, strict=True):
        if not bucket:
            table.append(CalibrationBin(lower, upper, 0, None, None))
            continue
        mean_predicted = sum(float(p.probability or 0.0) for p in bucket) / len(bucket)
        observed = sum(outcome_to_y(p.outcome) for p in bucket) / len(bucket)
        table.append(CalibrationBin(lower, upper, len(bucket), mean_predicted, observed))
    return table


def summarise(
    predictions: Sequence[PredictionRecord],
    *,
    model_id: str,
    model_version: str,
    bin_count: int = 10,
) -> MetricSummary:
    """
    Every metric for one model over one prediction set.

    `accuracy_at_half` uses a 0.5 threshold and is DIAGNOSTIC ONLY. It is not a
    model-selection metric: a league where 55% of matches are BTTS can be
    "predicted" at 55% accuracy by a constant, and accuracy cannot distinguish
    a well-calibrated 0.51 from an overconfident 0.99. Brier and log loss are
    the primary metrics precisely because they grade the probability itself.
    """
    scored = _scored(predictions)
    unevaluable: Dict[str, int] = {}
    for prediction in predictions:
        if prediction.unevaluable_reason is not None:
            key = prediction.unevaluable_reason.value
            unevaluable[key] = unevaluable.get(key, 0) + 1
        elif prediction.outcome is BttsOutcome.UNKNOWN:
            key = UnevaluableReason.NO_RESULT.value
            unevaluable[key] = unevaluable.get(key, 0) + 1

    mean_predicted: Optional[float] = None
    observed_rate: Optional[float] = None
    accuracy: Optional[float] = None
    if scored:
        mean_predicted = sum(float(p.probability or 0.0) for p in scored) / len(scored)
        observed_rate = sum(outcome_to_y(p.outcome) for p in scored) / len(scored)
        correct = sum(
            1
            for p in scored
            if (float(p.probability or 0.0) >= 0.5) == (outcome_to_y(p.outcome) == 1)
        )
        accuracy = correct / len(scored)

    return MetricSummary(
        model_id=model_id,
        model_version=model_version,
        scored=len(scored),
        targets=len(predictions),
        unevaluable=dict(sorted(unevaluable.items())),
        brier=brier_score(scored),
        log_loss=log_loss(scored),
        mean_predicted=mean_predicted,
        observed_rate=observed_rate,
        accuracy_at_half=accuracy,
        calibration=calibration_table(scored, bin_count=bin_count),
    )


def prediction_sort_key(record: PredictionRecord) -> Tuple[str, str, int, str, str]:
    """
    Total order over predictions.

    Same shape as `domain.historical.sort_key` and for the same reason: kickoff
    is not unique, so the event id terminates the ordering and two runs over
    identical data produce byte-identical artifacts.
    """
    return (
        record.model_id,
        record.competition,
        record.season,
        record.kickoff.isoformat(),
        record.event_id,
    )


def sort_predictions(records: Iterable[PredictionRecord]) -> List[PredictionRecord]:
    """Deterministic ordering. Same input, same order, always."""
    return sorted(records, key=prediction_sort_key)


def to_json_dict(record: PredictionRecord) -> Dict[str, Any]:
    """Serialise one prediction with a fixed key order."""
    return {
        "model_id": record.model_id,
        "model_version": record.model_version,
        "competition": record.competition,
        "season": record.season,
        "event_id": record.event_id,
        "kickoff": record.kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team_id": record.home_team_id,
        "away_team_id": record.away_team_id,
        "probability": record.probability,
        "outcome": record.outcome.value,
        "unevaluable_reason": (
            record.unevaluable_reason.value if record.unevaluable_reason else None
        ),
        "detail": record.detail,
        "history_matches": record.history_matches,
        "home_sample": record.home_sample,
        "away_sample": record.away_sample,
        "league_sample": record.league_sample,
    }
