"""
The prediction ledger contract (Epic 2G).

WHAT THIS IS: the shape of one recorded prediction, and the provenance needed to
explain it months later. Pure data. No IO, no network, no clock of its own -
`created_at` is passed in so a test can pin it and two runs can be compared.

WHY IT IS NOT `domain/evaluation.PredictionRecord`. That class exists to score a
HISTORICAL target and is deliberately walled off from the market:
`tests/regression/test_evaluation_leakage.py` forbids the name components
`odds`, `price`, `edge`, `stake`, `bookmaker` from its serialised form, and
forbids the module from importing `odds_api`, `shared.odds`, `decision` or
`filters`. Epic 2G must record the price a recommendation was made against, so
extending that record would have required weakening the firewall. The firewall is
right; this is a second, parallel record. What is reused is the DISCIPLINE:
frozen dataclasses, invariants enforced in `__post_init__` rather than trusted,
timezone-aware datetimes only, a fixed key order so identical inputs serialise
to identical bytes, and absence represented as absence.

WHAT THIS MODULE MUST NEVER IMPORT: `poisson`, `filters`, `decision`. A contract
that can reach the model is a contract that can change a prediction. Enforced by
`tests/regression/test_ledger_isolation.py`. Note `domain.filter_evaluation`
imports `filters`, which is why `FilterOutcome` is carried here as a validated
STRING and not as that enum - see `FILTER_OUTCOMES`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

# The ledger's own shape. Bumped when this record's fields change - never reused
# from `EVALUATION_SCHEMA_VERSION`, because merging two different shapes under
# one version is exactly what a schema version exists to prevent.
LEDGER_SCHEMA_VERSION = "2g.1"

# ---------------------------------------------------------------------------
# The four version axes.
#
# Four independent things can change what the system publishes for the same
# fixture, so four independent identifiers are required. Recording only "the
# model" would leave a threshold change invisible.
#
# MODEL_VERSION deliberately matches `evaluation_harness.PoissonV1Adapter`'s
# "1.0.0" so the live path and the offline harness can state that they ran the
# same model - currently unprovable.
# ---------------------------------------------------------------------------
MODEL_ID = "POISSON_V1"
MODEL_VERSION = "1.0.0"      # poisson.py mathematics; frozen, regression-pinned
FILTER_VERSION = "1b3.1"     # filter semantics and the FilterStats mapping
DECISION_VERSION = "1.0.0"   # make_decision rules and the recommendation gate
DATA_SOURCE_VERSION = "espn/1b5.1"  # provider + point-in-time derivation

# The three filter states, as strings.
#
# Carried as strings so this module does not import `domain.filter_evaluation`
# (which imports `filters`). `tests/unit/test_prediction_log.py` asserts this set
# equals `{o.value for o in FilterOutcome}`, so the two cannot drift apart
# silently - the test may import what production must not.
#
# THREE, not two. UNEVALUATED is not FAILED. GG-002 went undetected for as long
# as it did precisely because "passed filters" was indistinguishable from
# "filters never ran", so a boolean here would re-create that defect in storage.
FILTER_OUTCOMES = frozenset({"PASSED", "FAILED", "UNEVALUATED"})

# Config values that are fingerprinted. Every one of these can change a
# published recommendation without changing a single line of model code.
FINGERPRINTED_KEYS: Tuple[str, ...] = (
    "EDGE_THRESHOLD",
    "MIN_ODDS",
    "MIN_AVG_GOALS",
    "MAX_CLEAN_SHEET_PCT",
    "ALLOWED_LEAGUES",
)


class LivePredictionStatus(str, Enum):
    """
    Why a live prediction does or does not carry a probability.

    Distinct from `domain.evaluation.UnevaluableReason`, which answers a
    different question: why a historical target could not be GRADED. Reusing it
    would conflate "the model declined to speak" with "the result is unknown",
    and the second is not knowable at prediction time at all.

    Every fixture the pipeline touched is recorded with one of these. A refused
    fixture is evidence - it says the system was asked and declined - and
    dropping it would make coverage unmeasurable.
    """

    SCORED = "SCORED"                                    # probability present
    NO_TEAM_STATS = "NO_TEAM_STATS"                      # provider gave nothing
    NO_POINT_IN_TIME_INPUTS = "NO_POINT_IN_TIME_INPUTS"  # thin/absent history
    MODEL_RETURNED_NONE = "MODEL_RETURNED_NONE"          # POISSON_V1 declined


class OddsProvenance(str, Enum):
    """
    How much is known about the recorded price.

    `PARTIAL_NO_BOOKMAKER` is the honest description of today's reality:
    `odds_api.get_btts_odds` is typed `-> Optional[float]` and returns a bare
    number, so which bookmaker quoted it and when it was observed are discarded
    before any caller sees them. Epic 2G may not change that signature, so the
    record states the provenance is partial rather than implying completeness by
    leaving the fields quietly null.
    """

    ABSENT = "ABSENT"                                # no price was available
    PARTIAL_NO_BOOKMAKER = "PARTIAL_NO_BOOKMAKER"    # price only
    COMPLETE = "COMPLETE"                            # price + book + timestamp


@dataclass(frozen=True)
class OddsSnapshot:
    """
    The market as it was when the prediction was made.

    `implied_probability` and `edge` are recorded with `is not None` semantics at
    every boundary. `shared/odds.py:319` uses `round(edge, 4) if edge else None`,
    which serialises a genuine 0.0 edge as null and makes "no edge"
    indistinguishable from "no odds" (GG-007). That bug is not inherited here.
    """

    provenance: OddsProvenance
    price: Optional[float] = None
    implied_probability: Optional[float] = None
    edge: Optional[float] = None
    bookmaker: Optional[str] = None
    observed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.provenance is OddsProvenance.ABSENT and self.price is not None:
            raise ValueError("OddsSnapshot.ABSENT cannot carry a price")
        if self.provenance is not OddsProvenance.ABSENT and self.price is None:
            raise ValueError(
                f"OddsSnapshot.{self.provenance.value} requires a price; "
                "use ABSENT when no price was available"
            )
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("OddsSnapshot.observed_at must be timezone-aware")

    @classmethod
    def absent(cls) -> "OddsSnapshot":
        return cls(provenance=OddsProvenance.ABSENT)


@dataclass(frozen=True)
class PredictionProvenance:
    """
    Everything needed to answer "why did this prediction happen?" later.

    `config_fingerprint` is the load-bearing field. The four version strings are
    promises a human must remember to keep; the fingerprint is a MEASUREMENT. If
    someone edits a threshold and forgets to bump `DECISION_VERSION`, the
    fingerprint changes regardless and the ledger shows a discontinuity. Same
    reasoning as `dataset_checksum` in `evaluation_harness.write_artifacts`:
    without it, a model change and a config change look identical.
    """

    schema_version: str
    model_id: str
    model_version: str
    filter_version: str
    decision_version: str
    data_source_version: str
    config_fingerprint: str
    code_revision: Optional[str] = None


@dataclass(frozen=True)
class LedgerRecord:
    """
    One prediction, as recorded at the moment it was produced.

    Immutable by construction (`frozen=True`) and immutable in storage: the
    ledger only ever appends. There is deliberately NO `outcome` field. Grading
    is a later, separate fact about the same fixture, and writing it back into
    this record would destroy the audit trail the record exists to provide.
    """

    prediction_id: str
    run_id: str
    created_at: datetime
    provenance: PredictionProvenance

    # Identity. `fixture_id` is the ESPN event id, the same identity space as
    # `domain.historical.HistoricalMatch.event_id`, so settlement is a lookup
    # and never a team-name match (GG-008).
    fixture_id: str
    competition: str
    home_team_id: str
    away_team_id: str
    home_team_name: Optional[str] = None
    away_team_name: Optional[str] = None
    competition_name: Optional[str] = None
    season: Optional[int] = None

    # `kickoff_raw` is the provider's own string, always kept. `kickoff` is the
    # parsed instant and is None when the string could not be parsed - never a
    # guessed instant, matching `espn.parse_kickoff`'s contract.
    kickoff: Optional[datetime] = None
    kickoff_raw: Optional[str] = None

    # Model output.
    status: LivePredictionStatus = LivePredictionStatus.SCORED
    probability: Optional[float] = None
    lambda_home: Optional[float] = None
    lambda_away: Optional[float] = None
    home_sample: Optional[int] = None
    away_sample: Optional[int] = None
    league_sample: Optional[int] = None

    # Filter state: three values, plus the fields that were unavailable.
    filter_outcome: Optional[str] = None
    filter_unavailable_fields: Tuple[str, ...] = ()

    # Published verdict.
    recommendation: Optional[str] = None
    rejection_reasons: Tuple[str, ...] = ()

    odds: OddsSnapshot = field(default_factory=OddsSnapshot.absent)

    def __post_init__(self) -> None:
        # A record is either scored or refused. Never both, never neither. The
        # same invariant `domain.evaluation.PredictionRecord` enforces, for the
        # same reason: a probability sitting beside a refusal reason is two
        # contradictory claims, and callers cannot be trusted to avoid it.
        scored = self.status is LivePredictionStatus.SCORED
        if scored and self.probability is None:
            raise ValueError(
                "LedgerRecord.status=SCORED requires a probability; "
                "use a refusal status when the model produced none"
            )
        if not scored and self.probability is not None:
            raise ValueError(
                f"LedgerRecord.status={self.status.value} cannot carry a "
                f"probability (got {self.probability!r})"
            )
        if self.probability is not None and not 0.0 <= self.probability <= 1.0:
            raise ValueError(
                f"LedgerRecord.probability out of range: {self.probability!r}"
            )

        # Naive datetimes compare silently against local time; on a UTC+1
        # machine a 23:30Z kickoff lands on the wrong matchday (GG-014). Closed
        # at the type level rather than by convention.
        if self.created_at.tzinfo is None:
            raise ValueError("LedgerRecord.created_at must be timezone-aware")
        if self.kickoff is not None and self.kickoff.tzinfo is None:
            raise ValueError("LedgerRecord.kickoff must be timezone-aware")

        if not self.prediction_id:
            raise ValueError("LedgerRecord.prediction_id must not be empty")
        if not self.run_id:
            raise ValueError("LedgerRecord.run_id must not be empty")
        if not self.fixture_id:
            raise ValueError("LedgerRecord.fixture_id must not be empty")

        if self.filter_outcome is not None and self.filter_outcome not in FILTER_OUTCOMES:
            raise ValueError(
                f"LedgerRecord.filter_outcome must be one of "
                f"{sorted(FILTER_OUTCOMES)}, got {self.filter_outcome!r}"
            )


def fingerprinted_config(module: Any = None) -> Dict[str, Any]:
    """
    Read the threshold values that decide a recommendation.

    `config` is imported lazily and read on every call, so monkeypatching a
    threshold is observed rather than baked in at import time. Nothing is
    written back - `config.py` is not modified by this Epic.
    """
    if module is None:
        import config as module  # local: keeps the contract free of import-time config

    values: Dict[str, Any] = {}
    for key in FINGERPRINTED_KEYS:
        value = getattr(module, key, None)
        # A mapping's keys are the identity that matters (which leagues), and
        # dict ordering must not change the fingerprint.
        values[key] = sorted(value) if isinstance(value, dict) else value
    return values


def config_fingerprint(values: Optional[Mapping[str, Any]] = None) -> str:
    """
    A short, stable hash of the effective configuration.

    Twelve hex characters: enough to distinguish configurations in a log line,
    and not a claim to cryptographic authority. Canonicalised with sorted keys
    and no whitespace so the same configuration always hashes identically.
    """
    payload = fingerprinted_config() if values is None else dict(values)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def build_provenance(
    *,
    config_values: Optional[Mapping[str, Any]] = None,
    code_revision: Optional[str] = None,
) -> PredictionProvenance:
    """The current provenance block. Versions declared, configuration measured."""
    return PredictionProvenance(
        schema_version=LEDGER_SCHEMA_VERSION,
        model_id=MODEL_ID,
        model_version=MODEL_VERSION,
        filter_version=FILTER_VERSION,
        decision_version=DECISION_VERSION,
        data_source_version=DATA_SOURCE_VERSION,
        config_fingerprint=config_fingerprint(config_values),
        code_revision=code_revision,
    )


def _optional_float(value: Any) -> Optional[float]:
    """`is not None`, never truthiness: a genuine 0.0 is a value (GG-007)."""
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def classify_status(result: Mapping[str, Any]) -> LivePredictionStatus:
    """
    Recover WHY a result carries no probability, from the reasons it recorded.

    `process_fixture` returns early with a reason string at three distinct
    points and this maps each back to a named status. Matching on the reason
    text is a coupling, and it is chosen deliberately over the alternative:
    adding a status field to `process_fixture` would edit the function where
    every probability, filter verdict and recommendation is decided. Reading its
    output cannot change a prediction; editing it might. If a reason string is
    ever reworded, `MODEL_RETURNED_NONE` is the conservative fallback and
    `tests/unit/test_prediction_log.py` pins all three phrases.
    """
    if result.get("gg_probability") is not None:
        return LivePredictionStatus.SCORED

    reasons = [str(reason) for reason in result.get("rejection_reasons") or ()]
    for reason in reasons:
        if "team stats" in reason:
            return LivePredictionStatus.NO_TEAM_STATS
        if "Point-in-time model inputs unavailable" in reason:
            return LivePredictionStatus.NO_POINT_IN_TIME_INPUTS
    return LivePredictionStatus.MODEL_RETURNED_NONE


def build_odds_snapshot(result: Mapping[str, Any]) -> OddsSnapshot:
    """
    Capture the market from a finished result.

    A price with no bookmaker and no observation time is recorded as
    `PARTIAL_NO_BOOKMAKER` - see `OddsProvenance`. Absence is `ABSENT`, which is
    a different fact from "the edge was zero", and the two are distinguishable
    here for the first time.
    """
    price = _optional_float(result.get("odds"))
    if price is None:
        return OddsSnapshot.absent()
    return OddsSnapshot(
        provenance=OddsProvenance.PARTIAL_NO_BOOKMAKER,
        price=price,
        implied_probability=_optional_float(result.get("implied_probability")),
        edge=_optional_float(result.get("edge")),
    )


def from_result_dict(
    result: Mapping[str, Any],
    *,
    prediction_id: str,
    run_id: str,
    created_at: datetime,
    provenance: PredictionProvenance,
    kickoff: Optional[datetime] = None,
    season: Optional[int] = None,
) -> LedgerRecord:
    """
    Adapt one `main.process_fixture` result into a ledger record.

    READ-ONLY. The mapping is never mutated and never consulted for anything the
    caller has to act on, which is what makes capture incapable of altering a
    prediction.

    `kickoff` and `season` are passed in rather than derived here: parsing and
    season resolution belong to the provider (`espn.parse_kickoff`,
    `espn.resolve_season`), and re-implementing either inside a pure contract
    would create a second source of truth for the field that decides which
    season a prediction belongs to.
    """
    samples = result.get("model_input_samples") or {}
    status = classify_status(result)

    return LedgerRecord(
        prediction_id=prediction_id,
        run_id=run_id,
        created_at=created_at,
        provenance=provenance,
        fixture_id=str(result["fixture_id"]),
        competition=str(result["league_id"]),
        competition_name=result.get("league_name"),
        home_team_id=str(result["home_team_id"]),
        away_team_id=str(result["away_team_id"]),
        home_team_name=result.get("home_team"),
        away_team_name=result.get("away_team"),
        season=season,
        kickoff=kickoff,
        kickoff_raw=result.get("datetime"),
        status=status,
        probability=_optional_float(result.get("gg_probability")),
        lambda_home=_optional_float(result.get("lambda_home")),
        lambda_away=_optional_float(result.get("lambda_away")),
        home_sample=_optional_int(samples.get("home")),
        away_sample=_optional_int(samples.get("away")),
        league_sample=_optional_int(samples.get("league")),
        filter_outcome=result.get("filter_outcome"),
        filter_unavailable_fields=tuple(result.get("filter_data_unavailable") or ()),
        recommendation=result.get("decision"),
        rejection_reasons=tuple(
            str(reason) for reason in result.get("rejection_reasons") or ()
        ),
        odds=build_odds_snapshot(result),
    )


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return None if moment is None else moment.isoformat()


def to_json_dict(record: LedgerRecord) -> Dict[str, Any]:
    """
    Serialise with a FIXED key order.

    Not cosmetic. A stable order means two identical records produce identical
    bytes, so a diff between two ledger files shows real change and nothing
    else. `dataclasses.asdict` would serialise whatever order the fields happen
    to be declared in and would silently reorder on any future edit.
    """
    return {
        "schema_version": record.provenance.schema_version,
        "prediction_id": record.prediction_id,
        "run_id": record.run_id,
        "created_at": _iso(record.created_at),
        "fixture_id": record.fixture_id,
        "competition": record.competition,
        "competition_name": record.competition_name,
        "season": record.season,
        "kickoff": _iso(record.kickoff),
        "kickoff_raw": record.kickoff_raw,
        "home_team_id": record.home_team_id,
        "home_team_name": record.home_team_name,
        "away_team_id": record.away_team_id,
        "away_team_name": record.away_team_name,
        "status": record.status.value,
        "probability": record.probability,
        "lambda_home": record.lambda_home,
        "lambda_away": record.lambda_away,
        "home_sample": record.home_sample,
        "away_sample": record.away_sample,
        "league_sample": record.league_sample,
        "filter_outcome": record.filter_outcome,
        "filter_unavailable_fields": list(record.filter_unavailable_fields),
        "recommendation": record.recommendation,
        "rejection_reasons": list(record.rejection_reasons),
        "odds": {
            "provenance": record.odds.provenance.value,
            "price": record.odds.price,
            "implied_probability": record.odds.implied_probability,
            "edge": record.odds.edge,
            "bookmaker": record.odds.bookmaker,
            "observed_at": _iso(record.odds.observed_at),
        },
        "provenance": {
            "model_id": record.provenance.model_id,
            "model_version": record.provenance.model_version,
            "filter_version": record.provenance.filter_version,
            "decision_version": record.provenance.decision_version,
            "data_source_version": record.provenance.data_source_version,
            "config_fingerprint": record.provenance.config_fingerprint,
            "code_revision": record.provenance.code_revision,
        },
    }


def to_jsonl_line(record: LedgerRecord) -> str:
    """One record, one line. No trailing newline - the writer owns that."""
    return json.dumps(to_json_dict(record), ensure_ascii=False, separators=(",", ":"))


def sort_key(record: LedgerRecord) -> Tuple[str, str, str]:
    """Deterministic ordering for readers. Storage order stays append order."""
    return (record.competition, str(record.kickoff_raw or ""), record.fixture_id)


def sorted_records(records: Sequence[LedgerRecord]) -> list:
    return sorted(records, key=sort_key)
