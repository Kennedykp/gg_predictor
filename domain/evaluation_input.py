"""
The evaluation input adapter (Epic 2H-3).

Joins stored predictions to stored settlements and hands the result to
`domain.evaluation` for grading. Pure: no IO, no network, no clock. The caller
supplies both sides as plain dicts, exactly as `load_records` and
`load_settlements` return them.

    Prediction Ledger (dicts)  ->|
                                 |-- join_for_evaluation -> EvaluationInput[]
    Settlement Records (dicts) ->|                              |
                                                                v
                                                    domain.evaluation.summarise

WHY THIS MODULE EXISTS AT ALL, rather than calling the harness:

`evaluation_harness.replay()` RE-RUNS THE MODEL. It takes a dataset and a model
adapter and computes a probability from whatever data is present at replay time.
Used here it would silently answer a different question - "what would the model
say today?" instead of "how good was what we actually published?" - and the two
numbers are indistinguishable once written to a file. Every probability in this
module arrives from the ledger and is copied, never computed. There is no import
path from here to `poisson`, and no arithmetic is performed on a probability
anywhere below.

THE ODDS FIREWALL. Ledger records legitimately carry a price, an implied
probability and an edge (Epic 2G). `domain.evaluation` is walled off from all
three by `tests/regression/test_evaluation_leakage.py`. This module sits on that
boundary: it reads records that contain prices and must never copy one forward.
`_ADAPTED_LEDGER_FIELDS` is the explicit allow-list that makes that structural
rather than careful - the `odds` subtree is never read, so it cannot leak.

THE JOIN IS EXACT. `(competition, season, fixture_id)` and nothing else. Never a
team name, never a date, never a fuzzy comparison. GG-008 is the standing
reminder of why: the odds clients match teams by substring and pair "Athletic"
with "Athletic Club". A settlement join that could do that would produce
confident, wrong evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from domain.evaluation import (
    BttsOutcome,
    PredictionRecord,
    UnevaluableReason,
)

__all__ = [
    "EVALUATION_INPUT_SCHEMA_VERSION",
    "JoinKey",
    "SettlementState",
    "UnjoinableReason",
    "StoredProvenance",
    "EvaluationInput",
    "JoinReport",
    "join_key_of_prediction",
    "join_key_of_settlement",
    "index_settlements",
    "adapt_one",
    "join_for_evaluation",
    "scoreable",
    "to_prediction_records",
    "LEDGER_STATUS_TO_REASON",
]

EVALUATION_INPUT_SCHEMA_VERSION = "2h3.1"

# The join key. A three-part composite, because an ESPN event id is not
# documented as unique across competitions and demonstrably repeats across
# seasons. `domain/historical.py` keys its own duplicates the same way.
JoinKey = Tuple[str, Optional[int], str]

# Exactly the ledger fields this module reads. Anything absent from this tuple is
# structurally incapable of reaching the evaluation layer - which is the point:
# "odds", "implied_probability" and "edge" are not here, and no code below
# indexes a ledger record by a name that is not in this list.
_ADAPTED_LEDGER_FIELDS: Tuple[str, ...] = (
    "prediction_id",
    "created_at",
    "fixture_id",
    "competition",
    "season",
    "kickoff",
    "home_team_id",
    "away_team_id",
    "status",
    "probability",
    "home_sample",
    "away_sample",
    "league_sample",
    "provenance",
)

# Ledger refusal statuses mapped onto the evaluation layer's vocabulary.
#
# LOSSY BY NECESSITY, AND RECOVERABLE. `domain/evaluation.py` is frozen by this
# Epic, so its five `UnevaluableReason` members cannot be extended, and two
# distinct ledger statuses ("the provider gave us nothing" and "the history was
# too thin") both land on INSUFFICIENT_HISTORY. The original status is therefore
# copied verbatim into `PredictionRecord.detail`, so the collapse is a
# presentation detail and never a loss of evidence.
LEDGER_STATUS_TO_REASON: Dict[str, UnevaluableReason] = {
    "NO_TEAM_STATS": UnevaluableReason.INSUFFICIENT_HISTORY,
    "NO_POINT_IN_TIME_INPUTS": UnevaluableReason.INSUFFICIENT_HISTORY,
    "MODEL_RETURNED_NONE": UnevaluableReason.MODEL_RETURNED_NONE,
}


class SettlementState(str, Enum):
    """
    What settlement had to say about this prediction, from its point of view.

    `MISSING` is not `UNRESOLVED`. Unresolved means settlement ran and reported a
    named reason; missing means no settlement record exists for the fixture at
    all. The first is a fact about football or a provider; the second is a fact
    about the pipeline - most often that the settlement job has not been run for
    that day yet. Merging them would make an operational gap look like a
    postponement.
    """

    SETTLED = "SETTLED"
    UNRESOLVED = "UNRESOLVED"
    MISSING = "MISSING"


class UnjoinableReason(str, Enum):
    """
    Why a ledger record could not be turned into an evaluation input at all.

    Distinct from every "unevaluable" concept in `domain.evaluation`, which
    describes a prediction that WAS adapted but cannot be scored. These records
    never reach the metrics, so the count is reported separately rather than
    folded into coverage - a record with no parsed kickoff is a data-quality
    problem, not a model limitation, and the two must not average together.
    """

    NO_PREDICTION_ID = "NO_PREDICTION_ID"
    NO_FIXTURE_ID = "NO_FIXTURE_ID"
    NO_COMPETITION = "NO_COMPETITION"
    NO_KICKOFF = "NO_KICKOFF"
    NO_TEAM_IDS = "NO_TEAM_IDS"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True)
class StoredProvenance:
    """
    The provenance block as the ledger recorded it, copied verbatim.

    Read from storage and never rebuilt from today's `config`. Rebuilding would
    silently stamp the current configuration onto a prediction made under an
    older one, which is precisely the discontinuity `config_fingerprint` exists
    to expose.
    """

    model_id: str
    model_version: str
    filter_version: Optional[str] = None
    decision_version: Optional[str] = None
    data_source_version: Optional[str] = None
    config_fingerprint: Optional[str] = None
    code_revision: Optional[str] = None

    @classmethod
    def from_ledger(cls, block: Optional[Mapping[str, Any]]) -> "StoredProvenance":
        """
        Adapt the ledger's `provenance` subtree.

        A record written before provenance existed still has to be evaluable, so
        the model identity falls back to "UNKNOWN" rather than raising. An
        unknown provenance recorded as unknown is useful; a fabricated one is
        not, and refusing the record outright would discard a real prediction
        over a metadata gap.
        """
        block = block or {}
        return cls(
            model_id=str(block.get("model_id") or "UNKNOWN"),
            model_version=str(block.get("model_version") or "UNKNOWN"),
            filter_version=_optional_str(block.get("filter_version")),
            decision_version=_optional_str(block.get("decision_version")),
            data_source_version=_optional_str(block.get("data_source_version")),
            config_fingerprint=_optional_str(block.get("config_fingerprint")),
            code_revision=_optional_str(block.get("code_revision")),
        )


@dataclass(frozen=True)
class EvaluationInput:
    """
    One stored prediction joined to its stored outcome.

    Carries the ledger's own identity and timing - `prediction_id`, `created_at`,
    `provenance` - alongside the `PredictionRecord` the metrics consume.
    `domain/evaluation.py` is frozen by this Epic and its `PredictionRecord` has
    no field for any of the three, so they are held here rather than by widening
    a frozen contract.

    `prediction` is built, never recomputed: its probability is the float that
    was read from the ledger.
    """

    prediction_id: str
    created_at: Optional[datetime]
    join_key: JoinKey
    provenance: StoredProvenance
    prediction: PredictionRecord

    settlement_state: SettlementState
    unresolved_reason: Optional[str] = None
    settlement_source: Optional[str] = None
    settled_at: Optional[str] = None
    matched_season: Optional[int] = None
    ledger_status: Optional[str] = None

    @property
    def is_scored(self) -> bool:
        """Delegated, never re-derived. One definition of scoreable, in one place."""
        return self.prediction.is_scored

    @property
    def stored_probability(self) -> Optional[float]:
        """The ledger's probability, unmodified. The whole point of this Epic."""
        return self.prediction.probability


@dataclass(frozen=True)
class JoinReport:
    """
    What the join did, in numbers an operator can act on.

    Every count here answers a different question, and collapsing any two would
    hide the failure that matters. `missing_settlement` high means the settlement
    job has not run; `unresolved` high means football or a provider; `unjoinable`
    non-zero means malformed ledger rows.
    """

    predictions: int
    joined: int
    scored: int
    settled: int
    unresolved: int
    missing_settlement: int
    unjoinable: Dict[str, int]
    settlement_conflicts: Tuple[str, ...] = ()

    @property
    def join_rate(self) -> Optional[float]:
        """joined / predictions. None for an empty ledger - not 1.0."""
        if self.predictions <= 0:
            return None
        return self.joined / self.predictions

    @property
    def settlement_coverage(self) -> Optional[float]:
        """
        settled / joined: how much of the ledger has a usable result yet.

        Reported next to the scoring metrics for the reason `MetricSummary`
        reports coverage next to Brier: a superb score over a tenth of the
        fixtures is not a good model.
        """
        if self.joined <= 0:
            return None
        return self.settled / self.joined

    def summary(self) -> str:
        text = (
            f"{self.joined}/{self.predictions} joined, {self.settled} settled, "
            f"{self.unresolved} unresolved, {self.missing_settlement} awaiting settlement"
        )
        if self.unjoinable:
            total = sum(self.unjoinable.values())
            text += f", {total} unjoinable"
        if self.settlement_conflicts:
            text += f", {len(self.settlement_conflicts)} CONFLICTING"
        return text


def _optional_str(value: Any) -> Optional[str]:
    """`is not None`, never truthiness: an empty string is not absence (GG-007)."""
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sample(value: Any) -> int:
    """A sample count for provenance. Absent means 0 contributing matches."""
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed >= 0 else 0


def _parse_moment(value: Any) -> Optional[datetime]:
    """
    Parse an ISO timestamp from storage. `None` on anything unparseable.

    A naive result is REFUSED rather than assumed to be UTC. On a UTC+1 machine
    a 23:30Z kickoff assumed local lands on the wrong matchday (GG-014), so an
    unusable timestamp is reported as absent instead of guessed.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ---------------------------------------------------------------------------
# The join key
# ---------------------------------------------------------------------------
def join_key_of_prediction(record: Mapping[str, Any]) -> Optional[JoinKey]:
    """
    `(competition, season, fixture_id)` from a ledger record, or None.

    The event id is normalised to `str` because the ledger stores a string while
    a provider may hand back an int, and `"740123" != 740123` would turn a
    correct join into a silent miss.
    """
    competition = _optional_str(record.get("competition"))
    fixture_id = _optional_str(record.get("fixture_id"))
    if competition is None or fixture_id is None:
        return None
    return (competition, _optional_int(record.get("season")), fixture_id)


def join_key_of_settlement(record: Mapping[str, Any]) -> Optional[JoinKey]:
    """
    The same key from a settlement record.

    Deliberately reads `season`, NOT `matched_season`. Settlement stores the
    PREDICTION's season in `season` and records where the provider actually filed
    the fixture in `matched_season` (Epic 2H, finding 2H-F3). Joining on
    `matched_season` would re-introduce the very rollover mismatch that
    settlement already absorbed.
    """
    competition = _optional_str(record.get("competition"))
    fixture_id = _optional_str(record.get("fixture_id"))
    if competition is None or fixture_id is None:
        return None
    return (competition, _optional_int(record.get("season")), fixture_id)


def _settlement_sort_key(record: Mapping[str, Any]) -> str:
    """Later `settled_at` wins. ISO-8601 UTC strings sort chronologically."""
    return str(record.get("settled_at") or "")


def index_settlements(
    settlements: Iterable[Mapping[str, Any]],
) -> Tuple[Dict[JoinKey, Mapping[str, Any]], Tuple[str, ...]]:
    """
    Index settlements by join key, latest per fixture. Returns `(index, conflicts)`.

    ONE FIXTURE HAS ONE RESULT, so the index is keyed by fixture and not by
    prediction: two predictions for the same fixture (a re-run) share the single
    thing that happened on the pitch.

    Settlement is append-only, so a corrected settlement is a NEW line and the
    latest must win. But a later line that disagrees on the SCORE is not a
    correction to apply quietly - it means two sources described the same fixture
    differently, and every metric downstream would inherit that. Such keys are
    returned as conflicts so a caller can refuse to publish rather than pick one.
    """
    index: Dict[JoinKey, Mapping[str, Any]] = {}
    conflicts: List[str] = []

    for record in sorted(settlements, key=_settlement_sort_key):
        key = join_key_of_settlement(record)
        if key is None:
            continue
        previous = index.get(key)
        if previous is not None and _scores_disagree(previous, record):
            conflicts.append(
                f"{key[0]} {key[1]} fixture {key[2]}: "
                f"{_score_text(previous)} then {_score_text(record)}"
            )
        index[key] = record

    return index, tuple(sorted(set(conflicts)))


def _score_text(record: Mapping[str, Any]) -> str:
    return f"{record.get('final_home_goals')}-{record.get('final_away_goals')}"


def _scores_disagree(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """
    Whether two settlements of one fixture state different final scores.

    Only two SETTLED records can conflict. An unresolved record carries no score
    at all, so "unresolved then settled" is the normal, expected progression of a
    fixture that has since finished - not a disagreement.
    """
    settled = "SETTLED"
    if first.get("settlement_status") != settled or second.get("settlement_status") != settled:
        return False
    return (
        first.get("final_home_goals"),
        first.get("final_away_goals"),
    ) != (
        second.get("final_home_goals"),
        second.get("final_away_goals"),
    )


# ---------------------------------------------------------------------------
# Adapting one prediction
# ---------------------------------------------------------------------------
def _outcome_of(settlement: Optional[Mapping[str, Any]]) -> BttsOutcome:
    """
    Read the settled outcome. Anything that is not an explicit YES/NO is UNKNOWN.

    The outcome is READ, never re-derived from the score. `domain/settlement.py`
    already refuses to record an outcome that contradicts its own score, and a
    second derivation here would be a second place for that rule to drift.
    """
    if settlement is None:
        return BttsOutcome.UNKNOWN
    value = settlement.get("gg_outcome")
    if value == BttsOutcome.YES.value:
        return BttsOutcome.YES
    if value == BttsOutcome.NO.value:
        return BttsOutcome.NO
    return BttsOutcome.UNKNOWN


def _state_of(settlement: Optional[Mapping[str, Any]]) -> SettlementState:
    if settlement is None:
        return SettlementState.MISSING
    if settlement.get("settlement_status") == SettlementState.SETTLED.value:
        return SettlementState.SETTLED
    return SettlementState.UNRESOLVED


def adapt_one(
    prediction: Mapping[str, Any],
    settlement: Optional[Mapping[str, Any]],
) -> Tuple[Optional[EvaluationInput], Optional[UnjoinableReason]]:
    """
    Turn one stored prediction plus its settlement into an evaluation input.

    Returns `(input, None)` or `(None, reason)`. Never raises on a malformed
    record and never guesses a missing field: one bad ledger row must cost one
    row, and a fabricated kickoff or team id would be indistinguishable from a
    real one downstream.

    THE PROBABILITY IS COPIED. `probability` is read from the ledger dict and
    passed straight to `PredictionRecord`. No model is consulted, and the value
    is not rounded, clipped or rescaled - `domain/evaluation.py` clamps only
    inside its own logarithm and leaves the stored number intact.
    """
    prediction_id = _optional_str(prediction.get("prediction_id"))
    if prediction_id is None:
        return None, UnjoinableReason.NO_PREDICTION_ID

    key = join_key_of_prediction(prediction)
    if key is None:
        if _optional_str(prediction.get("competition")) is None:
            return None, UnjoinableReason.NO_COMPETITION
        return None, UnjoinableReason.NO_FIXTURE_ID

    # `PredictionRecord` requires a timezone-aware kickoff and this Epic may not
    # widen it. A ledger row whose kickoff never parsed is therefore reported
    # rather than admitted with an invented instant.
    kickoff = _parse_moment(prediction.get("kickoff"))
    if kickoff is None:
        return None, UnjoinableReason.NO_KICKOFF

    home_team_id = _optional_str(prediction.get("home_team_id"))
    away_team_id = _optional_str(prediction.get("away_team_id"))
    if home_team_id is None or away_team_id is None:
        return None, UnjoinableReason.NO_TEAM_IDS

    competition, season, fixture_id = key
    status = _optional_str(prediction.get("status")) or "SCORED"
    probability = prediction.get("probability")
    outcome = _outcome_of(settlement)

    # A probability and a refusal reason are mutually exclusive in
    # `PredictionRecord`, so exactly one is supplied. A SCORED prediction whose
    # fixture is unresolved keeps its probability and takes an UNKNOWN outcome:
    # that combination is not scoreable but IS coverage, and dropping the
    # probability would misreport it as a model refusal.
    reason: Optional[UnevaluableReason] = None
    detail: Optional[str] = None
    if probability is None:
        reason = LEDGER_STATUS_TO_REASON.get(status)
        if reason is None:
            return None, UnjoinableReason.UNKNOWN_STATUS
        # The lossy mapping is recoverable: the exact ledger status is kept.
        detail = f"ledger_status={status}"

    provenance = StoredProvenance.from_ledger(prediction.get("provenance"))

    try:
        record = PredictionRecord(
            model_id=provenance.model_id,
            model_version=provenance.model_version,
            competition=competition,
            season=season if season is not None else 0,
            event_id=fixture_id,
            kickoff=kickoff,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            outcome=outcome,
            probability=probability,
            unevaluable_reason=reason,
            detail=detail,
            history_matches=0,
            home_sample=_sample(prediction.get("home_sample")),
            away_sample=_sample(prediction.get("away_sample")),
            league_sample=_sample(prediction.get("league_sample")),
        )
    except (ValueError, TypeError):
        # `PredictionRecord` enforces its own invariants; a row that violates one
        # (a probability outside [0, 1], say) is reported, never repaired.
        return None, UnjoinableReason.MALFORMED

    return (
        EvaluationInput(
            prediction_id=prediction_id,
            created_at=_parse_moment(prediction.get("created_at")),
            join_key=key,
            provenance=provenance,
            prediction=record,
            settlement_state=_state_of(settlement),
            unresolved_reason=(
                _optional_str(settlement.get("unresolved_reason")) if settlement else None
            ),
            settlement_source=_optional_str(settlement.get("source")) if settlement else None,
            settled_at=_optional_str(settlement.get("settled_at")) if settlement else None,
            matched_season=(
                _optional_int(settlement.get("matched_season")) if settlement else None
            ),
            ledger_status=status,
        ),
        None,
    )


def join_for_evaluation(
    predictions: Sequence[Mapping[str, Any]],
    settlements: Sequence[Mapping[str, Any]],
) -> Tuple[List[EvaluationInput], JoinReport]:
    """
    Join the ledger to the settlement log. The entry point of this module.

    Deterministic: the same two inputs always produce the same list in the same
    order (ledger order preserved), so two evaluation runs over unchanged data
    are byte-comparable.

    EVERY prediction is accounted for - joined, or counted in `unjoinable`.
    Silently dropping a row would make a shrinking ledger look like an improving
    model.
    """
    index, conflicts = index_settlements(settlements)

    inputs: List[EvaluationInput] = []
    unjoinable: Dict[str, int] = {}

    for prediction in predictions:
        key = join_key_of_prediction(prediction)
        settlement = index.get(key) if key is not None else None
        adapted, reason = adapt_one(prediction, settlement)
        if adapted is None:
            label = (reason or UnjoinableReason.MALFORMED).value
            unjoinable[label] = unjoinable.get(label, 0) + 1
            continue
        inputs.append(adapted)

    report = JoinReport(
        predictions=len(predictions),
        joined=len(inputs),
        scored=sum(1 for item in inputs if item.is_scored),
        settled=sum(1 for item in inputs if item.settlement_state is SettlementState.SETTLED),
        unresolved=sum(
            1 for item in inputs if item.settlement_state is SettlementState.UNRESOLVED
        ),
        missing_settlement=sum(
            1 for item in inputs if item.settlement_state is SettlementState.MISSING
        ),
        unjoinable=dict(sorted(unjoinable.items())),
        settlement_conflicts=conflicts,
    )
    return inputs, report


def scoreable(inputs: Iterable[EvaluationInput]) -> List[EvaluationInput]:
    """
    Only the inputs that carry both a probability and a known outcome.

    An unresolved fixture is excluded here and counted in `JoinReport` instead.
    Passing it to `brier_score` would be harmless (the metrics filter too) but
    relying on that would leave the exclusion asserted in only one place.
    """
    return [item for item in inputs if item.is_scored]


def to_prediction_records(inputs: Iterable[EvaluationInput]) -> List[PredictionRecord]:
    """
    Unwrap for `domain.evaluation.summarise`.

    ALL inputs, not just the scoreable ones: `summarise` needs the unresolved
    records to compute coverage, and filtering here would make coverage read as
    100% of whatever survived.
    """
    return [item.prediction for item in inputs]
