"""
Point-in-time model evaluation harness (Epic 2B.3).

    historical dataset
            |
            v
    target fixture T  ---->  history strictly before T.kickoff
            |                          |
            |                          v
            |                   model adapter  (POISSON_V1, unchanged)
            |                          |
            v                          v
     actual BTTS outcome   <----   P(BTTS YES)
            |
            v
    PredictionRecord  ->  Brier / log loss / calibration / coverage

THIS BUILDS THE REFEREE, NOT A MODEL. No probability mathematics is defined in
this file. POISSON_V1 is reached through an adapter that calls the existing
production functions, so the harness cannot drift from what production computes:
a reimplemented formula would be scored here and never shipped, which is the
worst of both worlds.

WHAT THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------
No cold-start behaviour. When POISSON_V1 cannot assemble its five inputs from
pre-kickoff history, the target is marked UNEVALUABLE and counted. It is not
rescued with a league average, a previous-season rate, a pseudo-match or a
relaxed sample-size rule. Those are Epic 2C decisions, and inventing one here
would mean 2C could never measure whether it helped - the baseline it needs to
beat would already have been quietly moved.

No odds, prices, edges or thresholds, and no import path to them.

This module is an offline tool. It is not imported by main.py, analyze_all.py
or run3/.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

import poisson
from domain.evaluation import (
    EVALUATION_SCHEMA_VERSION,
    BttsOutcome,
    MetricSummary,
    PredictionRecord,
    UnevaluableReason,
    btts_outcome,
    sort_predictions,
    summarise,
    to_json_dict,
    validate_probability,
)
from domain.historical import HistoricalMatch, ModelEligibility, matches_before
from domain.match_records import MatchRecord, Venue
from domain.poisson_inputs import (
    build_poisson_inputs,
    derive_league_baseline,
    derive_venue_averages,
)

__all__ = [
    "PredictionContext",
    "ModelPrediction",
    "ModelAdapter",
    "PoissonV1Adapter",
    "ReferenceBaseRateAdapter",
    "MODEL_REGISTRY",
    "register_model",
    "get_model",
    "replay",
    "evaluate",
    "EvaluationRun",
    "write_artifacts",
    "SeasonPartition",
]


@dataclass(frozen=True)
class PredictionContext:
    """
    Everything a model may see about one target fixture.

    `history` has ALREADY been cut at the target's kickoff by `replay`. A model
    receives evidence, never the dataset, so an adapter has no opportunity to
    re-query and accidentally read past the cutoff. That is why the cut happens
    in the harness rather than being each model's responsibility: a leak in one
    adapter would be invisible in every other model's numbers.

    The target's own scoreline is NOT on this object. `target` carries identity
    and kickoff only, so a model cannot read the result it is predicting.
    """

    competition: str
    season: int
    event_id: str
    kickoff: datetime
    home_team_id: str
    away_team_id: str
    history: Sequence[HistoricalMatch]


@dataclass(frozen=True)
class ModelPrediction:
    """
    A model's answer: a probability, or an explicit refusal.

    Never both, and never neither. A model that cannot answer says so with a
    reason; it does not return 0.5 to keep the pipeline moving. A default
    probability is indistinguishable from a real one downstream, and would be
    scored as though the model had an opinion.
    """

    probability: Optional[float] = None
    reason: Optional[UnevaluableReason] = None
    detail: Optional[str] = None
    home_sample: int = 0
    away_sample: int = 0
    league_sample: int = 0

    def __post_init__(self) -> None:
        if self.probability is None and self.reason is None:
            raise ValueError("ModelPrediction needs a probability or a reason")
        if self.probability is not None and self.reason is not None:
            raise ValueError("ModelPrediction cannot have both a probability and a reason")
        if self.probability is not None:
            validate_probability(self.probability)


class ModelAdapter(Protocol):
    """
    The contract every evaluated model satisfies.

    Intentionally tiny. A larger interface would push feature construction into
    the harness, and each new model would then need harness changes - which is
    exactly the coupling Epic 2D must not have when it compares POISSON_V1
    against Dixon-Coles.

    `model_version` is separate from `model_id` so that re-running the same
    model after a change produces artifacts that cannot be silently compared
    against the old ones.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def predict(self, context: PredictionContext) -> ModelPrediction: ...


# ---------------------------------------------------------------------------
# Fixture-level history -> team-perspective records
# ---------------------------------------------------------------------------


def to_team_records(
    matches: Iterable[HistoricalMatch],
    team_id: str,
) -> List[MatchRecord]:
    """
    Re-express fixture-level history from ONE team's point of view.

    The dataset stores a fixture once (home and away in a single row); the
    production derivations in `domain.poisson_inputs` consume `MatchRecord`,
    which is one team's view of one fixture. This is the bridge, and it exists
    so the harness can reuse those derivations verbatim instead of re-deriving
    venue averages - a second implementation of the same statistic is the
    thing most likely to make the evaluation disagree with production.

    The venue flip is the substance: for the away side, goals_for and
    goals_against swap. Getting that backwards would not raise; it would just
    silently score every away team with its opponents' figures.
    """
    records: List[MatchRecord] = []
    for match in matches:
        if team_id == match.home_team_id:
            venue, goals_for, goals_against = (
                Venue.HOME,
                match.home_goals,
                match.away_goals,
            )
        elif team_id == match.away_team_id:
            venue, goals_for, goals_against = (
                Venue.AWAY,
                match.away_goals,
                match.home_goals,
            )
        else:
            continue

        records.append(
            MatchRecord(
                venue=venue,
                goals_for=goals_for,
                goals_against=goals_against,
                completed=match.completed,
                kickoff=match.kickoff,
                event_id=match.event_id,
                competition=match.competition,
                team_id=team_id,
                opponent_id=(
                    match.away_team_id if venue == Venue.HOME else match.home_team_id
                ),
                season=match.season,
                season_phase=match.season_phase,
                provider=match.provider,
            )
        )
    return records


def to_league_records(matches: Iterable[HistoricalMatch]) -> List[MatchRecord]:
    """
    Fixture-level history as HOME-perspective records, for the league baseline.

    `derive_league_baseline` reads only HOME records and divides by
    `2 * fixtures`, because it was written for history assembled from per-team
    schedules where every fixture appears twice. The dataset stores each fixture
    ONCE, so emitting only the home perspective reproduces exactly the input
    that function expects, and the established 1.375 EPL cross-check continues
    to hold. Emitting both perspectives here would double the fixture count and
    halve nothing - the divisor would grow with it - but it would also let a
    single fixture contribute twice to a small sample.
    """
    return [
        MatchRecord(
            venue=Venue.HOME,
            goals_for=match.home_goals,
            goals_against=match.away_goals,
            completed=match.completed,
            kickoff=match.kickoff,
            event_id=match.event_id,
            competition=match.competition,
            team_id=match.home_team_id,
            opponent_id=match.away_team_id,
            season=match.season,
            season_phase=match.season_phase,
            provider=match.provider,
        )
        for match in matches
    ]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PoissonV1Adapter:
    """
    The production POISSON_V1 model, evaluated exactly as production computes it.

    THE FORMULA IS NOT REPRODUCED HERE. This adapter derives the five inputs
    with `domain.poisson_inputs` and calls `poisson.calculate_gg_probability`.
    Both are the same functions the live pipeline uses; `poisson.py` is not
    imported for reference and re-implemented, it is CALLED. A copied formula
    would let the evaluated model and the shipped model diverge without a
    single failing test.

    NO COLD-START RESCUE. `build_poisson_inputs` returns None for any input it
    cannot derive, and this adapter reports INSUFFICIENT_HISTORY rather than
    substituting anything. Early-season targets are therefore unevaluable, in
    numbers this harness measures rather than hides.
    """

    model_id = "POISSON_V1"
    # Tracks the model's mathematics, which are frozen. Bumping this without a
    # change to poisson.py would be a lie; changing poisson.py is out of scope
    # for this Epic entirely.
    model_version = "1.0.0"

    def predict(self, context: PredictionContext) -> ModelPrediction:
        history = list(context.history)
        if not history:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                detail="no eligible matches before kickoff",
            )

        home_averages = derive_venue_averages(
            to_team_records(history, context.home_team_id),
            target_kickoff=context.kickoff,
            venue=Venue.HOME,
            competition=context.competition,
            exclude_event_id=context.event_id,
        )
        away_averages = derive_venue_averages(
            to_team_records(history, context.away_team_id),
            target_kickoff=context.kickoff,
            venue=Venue.AWAY,
            competition=context.competition,
            exclude_event_id=context.event_id,
        )
        baseline = derive_league_baseline(
            to_league_records(history),
            target_kickoff=context.kickoff,
            competition=context.competition,
            exclude_event_id=context.event_id,
        )

        inputs = build_poisson_inputs(
            home_averages,
            away_averages,
            baseline,
            target_kickoff=context.kickoff,
            competition=context.competition,
            season=context.season,
        )

        # Passed explicitly rather than splatted from a dict: the sample counts
        # travel with every outcome, including refusals, so how much evidence
        # the model had is always recoverable from the record.
        if not inputs.is_complete:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                detail=f"missing inputs: {','.join(inputs.missing)}",
                home_sample=inputs.home_sample,
                away_sample=inputs.away_sample,
                league_sample=inputs.league_sample,
            )

        result = poisson.calculate_gg_probability(
            league_avg_goals=float(inputs.league_avg_goals or 0.0),
            home_goals_scored_home=float(inputs.home_goals_scored_home or 0.0),
            home_goals_conceded_home=float(inputs.home_goals_conceded_home or 0.0),
            away_goals_scored_away=float(inputs.away_goals_scored_away or 0.0),
            away_goals_conceded_away=float(inputs.away_goals_conceded_away or 0.0),
        )

        if result is None:
            # POISSON_V1 refuses a zero league baseline (division by zero) and
            # negative inputs. Reported as the model's own refusal, not as a
            # harness failure, because it is the model's documented behaviour.
            return ModelPrediction(
                reason=UnevaluableReason.MODEL_RETURNED_NONE,
                detail="POISSON_V1 rejected its inputs",
                home_sample=inputs.home_sample,
                away_sample=inputs.away_sample,
                league_sample=inputs.league_sample,
            )

        return ModelPrediction(
            probability=float(result["gg_probability"]),
            home_sample=inputs.home_sample,
            away_sample=inputs.away_sample,
            league_sample=inputs.league_sample,
        )


class ReferenceBaseRateAdapter:
    """
    A REFERENCE, not a model. The prior BTTS rate in this competition.

    Exists because a Brier score is meaningless in isolation: 0.24 is good or
    bad only relative to what a naive predictor achieves on the same fixtures.
    This is the naive predictor, and it obeys the identical point-in-time rule -
    it sees exactly the history the model saw, never the season's final rate.

    Deliberately untuned and unweighted: no recency decay, no home/away split,
    no shrinkage. Every one of those is a modelling choice, and a tuned
    reference would stop being a floor and start being a competitor.

    `min_matches` guards against a "base rate" computed from a handful of
    fixtures. It is a stated requirement, not a fallback - below it the
    reference declines to predict, exactly as the model does.
    """

    model_id = "REFERENCE_BASE_RATE"
    model_version = "1.0.0"

    def __init__(self, min_matches: int = 20) -> None:
        self.min_matches = min_matches

    def predict(self, context: PredictionContext) -> ModelPrediction:
        played = [match for match in context.history if match.has_result]
        if len(played) < self.min_matches:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                detail=f"{len(played)} prior matches < required {self.min_matches}",
                league_sample=len(played),
            )
        btts = sum(
            1
            for match in played
            if (match.home_goals or 0) > 0 and (match.away_goals or 0) > 0
        )
        return ModelPrediction(
            probability=btts / len(played),
            league_sample=len(played),
        )


MODEL_REGISTRY: Dict[str, Callable[[], ModelAdapter]] = {
    "POISSON_V1": PoissonV1Adapter,
    "REFERENCE_BASE_RATE": ReferenceBaseRateAdapter,
}


def register_model(model_id: str, factory: Callable[[], ModelAdapter]) -> None:
    """
    Add a model to the registry.

    Epic 2C's shrunk Poisson and Epic 2D's Dixon-Coles register here and need
    no change to replay, metrics, calibration or reporting.
    """
    if model_id in MODEL_REGISTRY:
        raise ValueError(f"model {model_id!r} is already registered")
    MODEL_REGISTRY[model_id] = factory


def get_model(model_id: str) -> ModelAdapter:
    """Instantiate a registered model, or fail loudly."""
    if model_id not in MODEL_REGISTRY:
        raise KeyError(
            f"unknown model {model_id!r}; registered: {sorted(MODEL_REGISTRY)}"
        )
    return MODEL_REGISTRY[model_id]()


# ---------------------------------------------------------------------------
# Partitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonPartition:
    """
    An explicit development / validation / test split by season.

    NOT hard-coded and NOT applied by default - the repository defines no split,
    and inventing one here would bury a protocol decision in a utility. Epic 2D
    states its own partition when it performs rolling-origin comparison.

    The point of the type is that a partition must be REQUESTED by name, so
    reading the final test season is a visible act in the caller rather than an
    accident of which seasons happened to be in the dataset.
    """

    name: str
    seasons: Tuple[int, ...]

    def contains(self, season: int) -> bool:
        return season in self.seasons


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(
    dataset: Sequence[HistoricalMatch],
    model: ModelAdapter,
    *,
    targets: Optional[Sequence[HistoricalMatch]] = None,
    competition: Optional[str] = None,
    seasons: Optional[Sequence[int]] = None,
    same_competition_only: bool = True,
) -> List[PredictionRecord]:
    """
    Predict every target fixture using only what was knowable before it started.

    THE RULE, in one line:

        history = { m in dataset : m.kickoff < T.kickoff }

    Strictly `<`. `matches_before` enforces it, so there is ONE cutoff
    implementation shared with the dataset layer rather than a second one here
    that could drift. Consequences that follow from the rule alone, with no
    special-casing:

        - the target cannot see its own result (its kickoff is not < itself)
        - no later fixture from the same season contributes
        - no future season contributes
        - a fixture kicking off at exactly T contributes nothing

    Cross-competition leakage is prevented separately, by `competition=`, and
    defaults to on. Season is deliberately NOT filtered: a March fixture may
    legitimately learn from the previous September, and restricting history to
    the current season would be a modelling choice, not a leakage guard.

    Chronological by construction: each target's history is recomputed at its
    own kickoff. No season-wide aggregate is built once and reused, which is
    the mistake that makes an evaluation look excellent and mean nothing.
    """
    pool = sorted(dataset, key=lambda m: (m.kickoff, m.event_id))

    if targets is None:
        candidates: List[HistoricalMatch] = list(pool)
    else:
        candidates = sorted(targets, key=lambda m: (m.kickoff, m.event_id))

    if competition is not None:
        candidates = [m for m in candidates if m.competition == competition]
    if seasons is not None:
        allowed = set(seasons)
        candidates = [m for m in candidates if m.season in allowed]

    records: List[PredictionRecord] = []

    for target in candidates:
        outcome = btts_outcome(
            target.home_goals,
            target.away_goals,
            completed=target.completed,
        )

        history = matches_before(
            pool,
            target.kickoff,
            competition=target.competition if same_competition_only else None,
            eligible_only=True,
        )
        # Defence in depth behind the strict cutoff: a duplicated event id
        # sharing the target's own kickoff could otherwise slip through.
        history = [m for m in history if m.event_id != target.event_id]

        # A target that is not itself ordinary league play is reported rather
        # than scored. Grading a playoff with a regular-season model would
        # measure the wrong thing and quietly move every aggregate.
        if target.eligibility.verdict is not ModelEligibility.ELIGIBLE:
            records.append(
                _record(
                    model,
                    target,
                    outcome,
                    ModelPrediction(
                        reason=UnevaluableReason.NOT_MODEL_ELIGIBLE,
                        detail=target.eligibility.reason,
                    ),
                    len(history),
                )
            )
            continue

        if outcome is BttsOutcome.UNKNOWN:
            records.append(
                _record(
                    model,
                    target,
                    outcome,
                    ModelPrediction(
                        reason=UnevaluableReason.NO_RESULT,
                        detail=target.status or "no result recorded",
                    ),
                    len(history),
                )
            )
            continue

        context = PredictionContext(
            competition=target.competition,
            season=target.season,
            event_id=target.event_id,
            kickoff=target.kickoff,
            home_team_id=target.home_team_id,
            away_team_id=target.away_team_id,
            history=history,
        )

        try:
            prediction = model.predict(context)
        except Exception as exc:  # pragma: no cover - defensive
            # A model that raises is reported, not fatal: one broken adapter
            # must not discard an entire evaluation run's other results.
            prediction = ModelPrediction(
                reason=UnevaluableReason.MODEL_ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )

        records.append(_record(model, target, outcome, prediction, len(history)))

    return sort_predictions(records)


def _record(
    model: ModelAdapter,
    target: HistoricalMatch,
    outcome: BttsOutcome,
    prediction: ModelPrediction,
    history_size: int,
) -> PredictionRecord:
    return PredictionRecord(
        model_id=model.model_id,
        model_version=model.model_version,
        competition=target.competition,
        season=target.season,
        event_id=target.event_id,
        kickoff=target.kickoff,
        home_team_id=target.home_team_id,
        away_team_id=target.away_team_id,
        outcome=outcome,
        probability=prediction.probability,
        unevaluable_reason=prediction.reason,
        detail=prediction.detail,
        history_matches=history_size,
        home_sample=prediction.home_sample,
        away_sample=prediction.away_sample,
        league_sample=prediction.league_sample,
    )


# ---------------------------------------------------------------------------
# Runs and artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationRun:
    """One model over one target set: the predictions and their summary."""

    model_id: str
    model_version: str
    predictions: List[PredictionRecord]
    summary: MetricSummary

    def breakdown(self, key: str) -> Dict[str, MetricSummary]:
        """
        Re-summarise by `competition`, `season`, or `evidence` bucket.

        `evidence` buckets on the count of prior matches the HOME side had at
        its own venue - a directly observed number. ESPN's matchweek labels are
        deliberately not used: Epic 2B.1 found the season metadata unreliable
        enough to veto seasons over, so an ordering derived from it would be a
        weaker foundation than counting the evidence itself.
        """
        groups: Dict[str, List[PredictionRecord]] = {}
        for record in self.predictions:
            if key == "competition":
                bucket = record.competition
            elif key == "season":
                bucket = str(record.season)
            elif key == "evidence":
                bucket = _evidence_bucket(record.home_sample)
            else:
                raise ValueError(f"unknown breakdown key {key!r}")
            groups.setdefault(bucket, []).append(record)

        return {
            name: summarise(
                records,
                model_id=self.model_id,
                model_version=self.model_version,
            )
            for name, records in sorted(groups.items())
        }


def _evidence_bucket(home_sample: int) -> str:
    """Prior HOME matches available for the home side. Boundaries are stated."""
    if home_sample == 0:
        return "0"
    if home_sample <= 2:
        return "1-2"
    if home_sample <= 5:
        return "3-5"
    if home_sample <= 9:
        return "6-9"
    return "10+"


def evaluate(
    dataset: Sequence[HistoricalMatch],
    model: ModelAdapter,
    **kwargs: object,
) -> EvaluationRun:
    """Replay, then summarise. The two steps a caller almost always wants."""
    predictions = replay(dataset, model, **kwargs)  # type: ignore[arg-type]
    return EvaluationRun(
        model_id=model.model_id,
        model_version=model.model_version,
        predictions=predictions,
        summary=summarise(
            predictions,
            model_id=model.model_id,
            model_version=model.model_version,
        ),
    )


def _summary_json(summary: MetricSummary) -> Dict[str, object]:
    return {
        "model_id": summary.model_id,
        "model_version": summary.model_version,
        "targets": summary.targets,
        "scored": summary.scored,
        "coverage": summary.coverage,
        "brier": summary.brier,
        "log_loss": summary.log_loss,
        "mean_predicted": summary.mean_predicted,
        "observed_rate": summary.observed_rate,
        "accuracy_at_half": summary.accuracy_at_half,
        "unevaluable": summary.unevaluable,
    }


def _calibration_json(summary: MetricSummary) -> List[Dict[str, object]]:
    return [
        {
            "bin": bucket.label,
            "lower": bucket.lower,
            "upper": bucket.upper,
            "count": bucket.count,
            "mean_predicted": bucket.mean_predicted,
            "observed_rate": bucket.observed_rate,
            "gap": bucket.gap,
        }
        for bucket in summary.calibration
    ]


def write_artifacts(
    runs: Sequence[EvaluationRun],
    out_dir: Path,
    *,
    dataset_checksum: Optional[str] = None,
) -> Dict[str, Path]:
    """
    Write deterministic evaluation artifacts.

    Separate from the historical dataset, which is never modified: the same
    corpus must be re-evaluable by many models, and a run that wrote back into
    its own inputs could not be repeated.

    `dataset_checksum` ties results to the exact data they came from. Without
    it, two summaries with different numbers are unattributable - a model
    change and a data change look identical.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}

    predictions_path = out_dir / "evaluation_predictions.jsonl"
    with predictions_path.open("w", encoding="utf-8") as handle:
        for run in runs:
            for record in run.predictions:
                handle.write(
                    json.dumps(to_json_dict(record), sort_keys=False, ensure_ascii=False)
                )
                handle.write("\n")
    written["predictions"] = predictions_path

    summary_path = out_dir / "evaluation_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": EVALUATION_SCHEMA_VERSION,
                "dataset_checksum": dataset_checksum,
                "models": [_summary_json(run.summary) for run in runs],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    written["summary"] = summary_path

    calibration_path = out_dir / "calibration.json"
    calibration_path.write_text(
        json.dumps(
            {
                "schema_version": EVALUATION_SCHEMA_VERSION,
                "bin_convention": (
                    "[lower, upper) for every bin; the final bin is closed at 1.0"
                ),
                "models": {
                    run.model_id: _calibration_json(run.summary) for run in runs
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    written["calibration"] = calibration_path

    return written
