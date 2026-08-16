"""
Epic 2D: does STRUCTURE add discrimination that better estimation cannot?

RESEARCH SCRIPT. Imports production code but modifies nothing. `poisson.py`,
config, filters, decision and odds logic are untouched and unreachable from here.

THE QUESTION
------------
Epic 2C answered "can shrinkage stabilise sparse inputs" (yes) but exposed
GG-029: Brier is an unsafe objective here. Because shrinkage flattens toward the
~0.52 base rate, and a CONSTANT predictor beats raw POISSON_V1 on Brier
(0.2496 vs 0.2615), minimising Brier drives the prior strength to infinity. The
"best" model by that objective is the one that has stopped saying anything.

So the open question is not calibration, it is DISCRIMINATION: can any model here
ORDER fixtures by BTTS likelihood? AUC depends only on ordering, so it is
invariant to the monotone flattening Brier rewards. That invariance is why it is
this Epic's primary metric.

PROTOCOL (fixed before any number was looked at)
------------------------------------------------
    development  2018, 2019   parameter search runs here, repeatedly
    validation   2021, 2022    confirms the choice generalises, inspected once
    holdout      2024          run ONCE, after parameters are frozen

WHY THESE PARTITIONS. Epic 2C used 2018-2019 for its search, 2020 for validation
and 2023 as its final test. 2020 and 2023 are therefore BURNED - 2023 especially,
since it was 2C's holdout and has been reported on by evidence bucket. Reusing
either as a 2D holdout would be dishonest. 2024 is fully cached (verified: 2,615
matches across 7 leagues, 0 gaps) and has never been a target in any Epic, so it
is the cleanest partition available. 2021-2022 sit chronologically between the
development seasons and the holdout and were never used by 2C.

Previous seasons ARE loaded as history (2017 for 2018, 2020 for 2021, 2023 for
2024). That is not contamination: those matches occurred before the targets, and
using them is the whole point of a point-in-time prior. "Burned" refers to a
season being INSPECTED as a target, not to its existence as history.

ROLLING ORIGIN comes free. `replay` rebuilds each target's history from matches
with kickoff strictly < the target's own kickoff, so a season's evaluation is
already a rolling-origin backtest. No resampling is added, and no second cutoff
implementation exists to drift from the first.

WHAT IS REUSED RATHER THAN REBUILT
----------------------------------
    fixture identity      domain.comparison.fixture_key
    point-in-time cutoff  evaluation_harness.replay / domain.historical
    intersection          domain.comparison.intersect / compare
    metrics               domain.evaluation.summarise
    AUC + bootstrap       domain.discrimination
    candidate structures  domain.goal_models
    cache parsing         research.epic2c_experiment.load_season

Nothing in that list is reimplemented here. This module contributes only
adapters, parameter selection, and reporting.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.comparison import (  # noqa: E402
    compare,
    evidence_bucket,
    fixture_key,
    intersect,
)
from domain.discrimination import (  # noqa: E402
    auc_from_labelled,
    constant_predictor_brier,
    paired_auc_delta,
    paired_brier_delta,
    prediction_spread,
    roc_auc,
)
from domain.evaluation import (  # noqa: E402
    BttsOutcome,
    PredictionRecord,
    UnevaluableReason,
    outcome_to_y,
    summarise,
)
from domain.goal_models import (  # noqa: E402
    TeamStrength,
    btts_bivariate,
    btts_dixon_coles,
    btts_independent,
    fit_team_strength,
    poisson_pmf,
    predict_lambdas,
    weighted_log_likelihood,
)
from domain.historical import HistoricalMatch, matches_before  # noqa: E402
from evaluation_harness import (  # noqa: E402
    ModelPrediction,
    PoissonV1Adapter,
    PredictionContext,
    replay,
)
from research.epic2c_experiment import (  # noqa: E402
    CONTEXT_LEAGUES,
    TARGET_LEAGUES,
    load_season,
)

DEVELOPMENT_SEASONS = [2018, 2019]
VALIDATION_SEASONS = [2021, 2022]
HOLDOUT_SEASONS = [2024]

#: Seasons already inspected as TARGETS by earlier Epics. Recorded so the report
#: cannot accidentally describe one of them as untouched.
BURNED_SEASONS = {
    2018: "Epic 2A/2B.3/2C development",
    2019: "Epic 2A/2B.3/2C development",
    2020: "Epic 2C validation",
    2023: "Epic 2C final test (reported by evidence bucket)",
}

RESULTS_DIR = REPO_ROOT / "research" / "epic2d_results"


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def build_dataset(seasons: Sequence[int]) -> Tuple[List[HistoricalMatch], List[str]]:
    """
    Load target seasons plus the season before each, via Epic 2C's cache loader.

    Using `load_season` means the production ESPN parser and the Epic 2B.1
    season-integrity rules apply identically here. A private reader would let
    this experiment score fixtures production would reject.
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
# Candidate adapters
# ---------------------------------------------------------------------------


class MaherAdapter:
    """
    Maher-style attack/defence/home-advantage, fitted point-in-time per target.

    The five-input venue ratios POISSON_V1 uses are replaced by a fitted model,
    but the BTTS mapping is the SAME function of the two rates (`btts_independent`
    is asserted identical to `poisson.calculate_gg_probability` in
    tests/unit/test_goal_models.py). So a difference in AUC between this and
    POISSON_V1_RAW is attributable to the rate ESTIMATES, not to a different
    probability formula.

    `mode` selects the dependence correction applied on top of the fitted rates:
        "independent"  - no correction (C1 with xi=0, C2 with xi>0)
        "dixon_coles"  - tau correction with the supplied rho (C3)
        "bivariate"    - shared-component model with the supplied lambda3 (C4)

    REFUSAL, NOT RESCUE. If either team has no fitted parameters (a promoted club,
    or any team absent from the fitting window) this reports INSUFFICIENT_HISTORY.
    Substituting attack = 1.0 would present "exactly average" as a measurement,
    which is the GG-001 error. Coverage differences are then measured by the fair
    intersection rather than hidden.
    """

    def __init__(
        self,
        *,
        model_id: str,
        xi: float = 0.0,
        mode: str = "independent",
        rho: float = 0.0,
        lambda_shared: float = 0.0,
        min_matches_per_team: int = 4,
        fit_cache: Optional[Dict[object, TeamStrength]] = None,
    ) -> None:
        self.model_id = model_id
        self.model_version = "2d.1"
        self.xi = xi
        self.mode = mode
        self.rho = rho
        self.lambda_shared = lambda_shared
        self.min_matches_per_team = min_matches_per_team
        # Fits are memoised because consecutive targets frequently share an
        # identical history prefix. The key includes everything that changes the
        # fit, so memoisation cannot silently reuse a stale model.
        self._cache: Dict[object, TeamStrength] = {} if fit_cache is None else fit_cache

    def _fit(self, context: PredictionContext) -> Optional[TeamStrength]:
        history = list(context.history)
        if not history:
            return None
        # A history set is always a chronological PREFIX of its competition's
        # matches, so (length, last event id) identifies it exactly. With decay
        # the weights also depend on the cutoff, so as_of joins the key.
        key = (
            context.competition,
            self.xi,
            len(history),
            history[-1].event_id,
            context.kickoff if self.xi > 0.0 else None,
        )
        if key not in self._cache:
            self._cache[key] = fit_team_strength(
                history,
                as_of=context.kickoff,
                xi=self.xi,
                min_matches_per_team=self.min_matches_per_team,
                tolerance=1e-7,
                max_iterations=60,
            )
        return self._cache[key]

    def predict(self, context: PredictionContext) -> ModelPrediction:
        model = self._fit(context)
        if model is None:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                detail="no matches before kickoff",
            )
        rates = predict_lambdas(model, context.home_team_id, context.away_team_id)
        if rates is None:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                detail="team absent from fitting window (no estimated strength)",
            )
        lambda_home, lambda_away = rates
        if lambda_home <= 0.0 or lambda_away <= 0.0:
            # A zero rate is GG-028's signature: the MLE really is 0 for a team
            # that never scored. Refuse rather than emit an unjustified 0%.
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                detail=f"degenerate rate ({lambda_home:.4f}, {lambda_away:.4f})",
            )

        if self.mode == "independent":
            probability = btts_independent(lambda_home, lambda_away)
        elif self.mode == "dixon_coles":
            probability = btts_dixon_coles(lambda_home, lambda_away, self.rho)
        elif self.mode == "bivariate":
            # Decompose so the MARGINAL means stay equal to the fitted rates;
            # adding lambda3 on top would inflate both and silently change model.
            shared = min(self.lambda_shared, lambda_home, lambda_away)
            probability = btts_bivariate(lambda_home - shared, lambda_away - shared, shared)
        else:  # pragma: no cover - guarded by CLI choices
            raise ValueError(f"unknown mode {self.mode!r}")

        appearances = sum(
            1
            for match in context.history
            if context.home_team_id in (match.home_team_id, match.away_team_id)
        )
        away_appearances = sum(
            1
            for match in context.history
            if context.away_team_id in (match.home_team_id, match.away_team_id)
        )
        return ModelPrediction(
            probability=probability,
            home_sample=appearances,
            away_sample=away_appearances,
            league_sample=model.diagnostics.raw_matches,
        )


class OracleCeilingProbe:
    """
    ############  QUARANTINED - DELIBERATELY LEAKY - NOT A MODEL  ############

    This probe is FITTED ON THE FULL DATASET, INCLUDING THE TARGET SEASON AND THE
    TARGET FIXTURE ITSELF. It therefore sees the future and its scores are
    meaningless as predictions. It exists for exactly one purpose:

        to estimate the CEILING on discrimination available from goal-count
        information under this model class.

    WHY THAT IS WORTH MEASURING. If the honest candidates reach AUC ~0.55 and
    this probe - with perfect knowledge of every team's season-long strength -
    also reaches ~0.55, then the limit is the MODEL CLASS, not estimation noise,
    and no better estimator can rescue it. That is a decisive answer to Epic 2D's
    question, and it cannot be obtained any other way.

    HARD RULES, enforced by construction and by tests:
      * `model_id` is prefixed ORACLE_LEAKY so it cannot be mistaken in artifacts
      * it is never registered in the harness model registry
      * it never appears in a candidate comparison table
      * its number is never described as an improvement over any model
      * production never imports this module at all

    ########################################################################
    """

    model_id = "ORACLE_LEAKY_CEILING"
    model_version = "2d.1-quarantined"

    def __init__(self, dataset: Sequence[HistoricalMatch]) -> None:
        self._fits: Dict[str, TeamStrength] = {}

        # One in-sample fit per competition over EVERY match, including targets.
        latest = max(match.kickoff for match in dataset)
        horizon = latest.replace(year=latest.year + 1)
        by_competition: Dict[str, List[HistoricalMatch]] = {}
        for match in dataset:
            by_competition.setdefault(match.competition, []).append(match)
        for competition, matches in by_competition.items():
            self._fits[competition] = fit_team_strength(
                matches,
                as_of=horizon,
                xi=0.0,
                min_matches_per_team=4,
                tolerance=1e-7,
                max_iterations=60,
            )

    def predict(self, context: PredictionContext) -> ModelPrediction:
        model = self._fits.get(context.competition)
        if model is None:
            return ModelPrediction(reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no fit")
        rates = predict_lambdas(model, context.home_team_id, context.away_team_id)
        if rates is None or rates[0] <= 0.0 or rates[1] <= 0.0:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="degenerate"
            )
        return ModelPrediction(probability=btts_independent(*rates))


# ---------------------------------------------------------------------------
# Parameter selection - by GOAL-COUNT likelihood, never by BTTS Brier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LikelihoodPoint:
    parameter: float
    log_likelihood: Optional[float]
    fixtures: int


def _origins(
    dataset: Sequence[HistoricalMatch], seasons: Sequence[int], per_season: int
) -> List[HistoricalMatch]:
    """
    Evenly spaced rolling origins within the given seasons.

    Parameter selection does not need every fixture - it needs enough
    out-of-sample goal observations at enough points in each season to profile a
    likelihood. Spacing them evenly (rather than taking the first N) avoids
    profiling xi entirely on early-season targets, which is precisely the regime
    where every candidate is starved of history.
    """
    targets = sorted(
        (
            match
            for match in dataset
            if match.season in set(seasons)
            and match.competition in TARGET_LEAGUES
            and match.completed
            and match.home_goals is not None
        ),
        key=lambda m: (m.kickoff, m.event_id),
    )
    by_season: Dict[Tuple[str, int], List[HistoricalMatch]] = {}
    for match in targets:
        by_season.setdefault((match.competition, match.season), []).append(match)
    chosen: List[HistoricalMatch] = []
    for group in by_season.values():
        # Skip the first third: with almost no history every candidate refuses,
        # and a likelihood computed there measures coverage, not fit quality.
        usable = group[len(group) // 3 :]
        if not usable:
            continue
        step = max(1, len(usable) // per_season)
        chosen.extend(usable[::step][:per_season])
    return chosen


def profile_xi(
    dataset: Sequence[HistoricalMatch],
    seasons: Sequence[int],
    grid: Sequence[float],
    *,
    per_season: int = 25,
) -> List[LikelihoodPoint]:
    """
    Out-of-sample goal-count log-likelihood as a function of the decay rate xi.

    THE OBJECTIVE IS THE GOALS, NOT THE BTTS SCORE. xi describes how fast a
    team's scoring rate is forgotten, which is a statement about the goal
    process, so the goal process is what grades it. Selecting xi by BTTS Brier
    would reward flattening and repeat GG-029 exactly.

    Each origin contributes the likelihood of the goals actually scored in the
    target fixture under a model fitted only on matches before it. Strictly
    out-of-sample: the target is never in its own fitting window.
    """
    origins = _origins(dataset, seasons, per_season)
    points: List[LikelihoodPoint] = []
    for xi in grid:
        total = 0.0
        counted = 0
        for target in origins:
            history = matches_before(dataset, target.kickoff, competition=target.competition)
            if not history:
                continue
            model = fit_team_strength(
                history,
                as_of=target.kickoff,
                xi=xi,
                min_matches_per_team=4,
                tolerance=1e-7,
                max_iterations=60,
            )
            contribution = weighted_log_likelihood([target], model)
            if contribution is None:
                continue
            total += contribution
            counted += 1
        points.append(
            LikelihoodPoint(
                parameter=xi,
                log_likelihood=total / counted if counted else None,
                fixtures=counted,
            )
        )
    return points


def profile_rho(
    dataset: Sequence[HistoricalMatch],
    seasons: Sequence[int],
    grid: Sequence[float],
    *,
    xi: float,
    per_season: int = 25,
) -> List[LikelihoodPoint]:
    """
    Out-of-sample goal-count log-likelihood as a function of Dixon-Coles rho.

    rho is estimated the same way as xi and for the same reason. Note the tau
    term enters the likelihood of the SCORELINE, so this is a direct estimate of
    the low-score dependence rather than a BTTS-driven fudge.
    """
    origins = _origins(dataset, seasons, per_season)
    points: List[LikelihoodPoint] = []
    for rho in grid:
        total = 0.0
        counted = 0
        for target in origins:
            history = matches_before(dataset, target.kickoff, competition=target.competition)
            if not history:
                continue
            model = fit_team_strength(
                history,
                as_of=target.kickoff,
                xi=xi,
                min_matches_per_team=4,
                tolerance=1e-7,
                max_iterations=60,
            )
            contribution = weighted_log_likelihood([target], model, rho=rho)
            if contribution is None:
                continue
            total += contribution
            counted += 1
        points.append(
            LikelihoodPoint(
                parameter=rho,
                log_likelihood=total / counted if counted else None,
                fixtures=counted,
            )
        )
    return points


def _bivariate_pmf(home_goals: int, away_goals: int, l1: float, l2: float, l3: float) -> float:
    """
    Bivariate Poisson pmf, for the lambda3 IDENTIFIABILITY DIAGNOSTIC only.

    P(X=x, Y=y) = e^-(l1+l2+l3) * sum_k l1^(x-k)/(x-k)! * l2^(y-k)/(y-k)! * l3^k/k!

    Deliberately local to this research script: it exists to test whether l3 can
    be estimated at all. If the profile turns out to be well behaved, C4 becomes
    a real candidate and this belongs in `domain/goal_models.py` with tests - not
    here. Promoting it before that is demonstrated would be adding a model on
    the strength of an assumption.
    """
    total = 0.0
    for k in range(min(home_goals, away_goals) + 1):
        total += (
            math.exp((home_goals - k) * math.log(l1) - math.lgamma(home_goals - k + 1))
            * math.exp((away_goals - k) * math.log(l2) - math.lgamma(away_goals - k + 1))
            * math.exp(k * math.log(l3) - math.lgamma(k + 1))
            if l1 > 0 and l2 > 0 and l3 > 0
            else 0.0
        )
    return math.exp(-(l1 + l2 + l3)) * total


def profile_lambda_shared(
    dataset: Sequence[HistoricalMatch],
    seasons: Sequence[int],
    grid: Sequence[float],
    *,
    xi: float,
    per_season: int = 25,
) -> List[LikelihoodPoint]:
    """
    Profile likelihood for the bivariate Poisson shared component lambda3.

    IDENTIFIABILITY IS CHECKED BEFORE THE CANDIDATE IS USED. If this profile is
    flat, or maximised at the boundary lambda3 = 0, then the data cannot
    distinguish a shared component from none, and fitting one would be inventing
    structure. In that case C4 is dropped and the reason recorded - not stabilised
    with a constraint or a prior chosen to make it behave.
    """
    origins = _origins(dataset, seasons, per_season)
    points: List[LikelihoodPoint] = []
    for shared in grid:
        total = 0.0
        counted = 0
        for target in origins:
            history = matches_before(dataset, target.kickoff, competition=target.competition)
            if not history:
                continue
            model = fit_team_strength(
                history,
                as_of=target.kickoff,
                xi=xi,
                min_matches_per_team=4,
                tolerance=1e-7,
                max_iterations=60,
            )
            rates = predict_lambdas(model, target.home_team_id, target.away_team_id)
            if rates is None:
                continue
            lambda_home, lambda_away = rates
            l3 = min(shared, lambda_home * 0.9, lambda_away * 0.9)
            l1, l2 = lambda_home - l3, lambda_away - l3
            if l1 <= 0.0 or l2 <= 0.0:
                continue
            if l3 == 0.0:
                probability = poisson_pmf(target.home_goals or 0, lambda_home) * poisson_pmf(
                    target.away_goals or 0, lambda_away
                )
            else:
                probability = _bivariate_pmf(
                    target.home_goals or 0, target.away_goals or 0, l1, l2, l3
                )
            if probability <= 0.0:
                continue
            total += math.log(probability)
            counted += 1
        points.append(
            LikelihoodPoint(
                parameter=shared,
                log_likelihood=total / counted if counted else None,
                fixtures=counted,
            )
        )
    return points


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _labelled(records: Sequence[PredictionRecord]) -> List[Tuple[float, int]]:
    return [
        (record.probability, outcome_to_y(record.outcome))
        for record in records
        if record.is_scored
        and record.probability is not None
        and record.outcome is not BttsOutcome.UNKNOWN
    ]


def report_arm(name: str, records: Sequence[PredictionRecord]) -> List[str]:
    """Raw coverage and standalone metrics for one model, before intersecting."""
    summary = summarise(records, model_id=name, model_version="-")
    spread = prediction_spread(records)
    auc = roc_auc(records)
    lines = [
        f"  {name:26s} targets={len(records):5d} scored={summary.scored:5d} "
        f"coverage={summary.scored / len(records) * 100 if records else 0:5.1f}%",
        f"  {'':26s} brier={summary.brier if summary.brier is None else round(summary.brier, 4)} "
        f"logloss={summary.log_loss if summary.log_loss is None else round(summary.log_loss, 4)} "
        f"auc={auc if auc is None else round(auc, 4)}",
        f"  {'':26s} spread sd={spread.sd if spread.sd is None else round(spread.sd, 4)} "
        f"distinct={spread.distinct} "
        f"range=[{spread.minimum if spread.minimum is None else round(spread.minimum, 3)}, "
        f"{spread.maximum if spread.maximum is None else round(spread.maximum, 3)}]",
    ]
    if summary.unevaluable:
        lines.append(f"  {'':26s} refusals={summary.unevaluable}")
    return lines


def report_pair(
    baseline_name: str,
    baseline: Sequence[PredictionRecord],
    candidate_name: str,
    candidate: Sequence[PredictionRecord],
    *,
    evidence: Optional[Dict[Tuple[str, int, str], int]] = None,
    bootstrap: int = 2000,
) -> List[str]:
    """
    Fair comparison on the identical intersection, AUC first.

    `compare` computes the intersection before summarising either arm, so there
    is no path through this function that scores the two models over different
    fixtures.
    """
    left, right = intersect(baseline, candidate)
    comparison = compare(baseline, candidate)
    lines = [
        "",
        f"  INTERSECTION {baseline_name} vs {candidate_name}: n={comparison.intersection_size}"
        f"  (raw scored {comparison.left.raw_scored}/{comparison.left.raw_targets}"
        f" vs {comparison.right.raw_scored}/{comparison.right.raw_targets};"
        f" left_only={comparison.left_only} right_only={comparison.right_only})",
    ]
    base_auc = auc_from_labelled(_labelled(left))
    cand_auc = auc_from_labelled(_labelled(right))
    constant = constant_predictor_brier(left)
    left_summary = comparison.left.summary
    right_summary = comparison.right.summary
    lines.append(
        f"    AUC        {baseline_name}={base_auc if base_auc is None else round(base_auc, 4)}"
        f"   {candidate_name}={cand_auc if cand_auc is None else round(cand_auc, 4)}"
    )
    lines.append(
        f"    Brier      {baseline_name}={round(left_summary.brier, 4) if left_summary.brier is not None else None}"
        f"   {candidate_name}={round(right_summary.brier, 4) if right_summary.brier is not None else None}"
        f"   [constant base-rate benchmark={round(constant, 4) if constant else None}]"
    )
    lines.append(
        f"    LogLoss    {baseline_name}={round(left_summary.log_loss, 4) if left_summary.log_loss is not None else None}"
        f"   {candidate_name}={round(right_summary.log_loss, 4) if right_summary.log_loss is not None else None}"
    )
    lines.append(
        f"    Extremes   {baseline_name}: p<=0.05={comparison.left.extremes.at_or_below_05}"
        f" p>=0.95={comparison.left.extremes.at_or_above_95}"
        f" exact0={comparison.left.extremes.exactly_zero}"
        f" exact1={comparison.left.extremes.exactly_one}"
        f" | {candidate_name}: p<=0.05={comparison.right.extremes.at_or_below_05}"
        f" p>=0.95={comparison.right.extremes.at_or_above_95}"
        f" exact0={comparison.right.extremes.exactly_zero}"
        f" exact1={comparison.right.extremes.exactly_one}"
    )
    lines.append("    calibration (intersection, high-confidence bins matter most):")
    for base_bin, cand_bin in zip(
        comparison.left.calibration, comparison.right.calibration, strict=False
    ):
        if base_bin.count == 0 and cand_bin.count == 0:
            continue
        lines.append(
            f"      {base_bin.label:14s}"
            f" base n={base_bin.count:5d}"
            f" pred={base_bin.mean_predicted if base_bin.mean_predicted is None else round(base_bin.mean_predicted, 3)}"
            f" obs={base_bin.observed_rate if base_bin.observed_rate is None else round(base_bin.observed_rate, 3)}"
            f" gap={base_bin.gap if base_bin.gap is None else round(base_bin.gap, 3)}"
            f" | cand n={cand_bin.count:5d}"
            f" pred={cand_bin.mean_predicted if cand_bin.mean_predicted is None else round(cand_bin.mean_predicted, 3)}"
            f" obs={cand_bin.observed_rate if cand_bin.observed_rate is None else round(cand_bin.observed_rate, 3)}"
            f" gap={cand_bin.gap if cand_bin.gap is None else round(cand_bin.gap, 3)}"
        )

    if len(left) > 30:
        auc_delta = paired_auc_delta(left, right, iterations=bootstrap)
        brier_delta = paired_brier_delta(left, right, iterations=bootstrap)
        lines.append(
            f"    dAUC       {round(auc_delta.point, 4) if auc_delta.point is not None else None}"
            f"  95% CI [{round(auc_delta.lower, 4) if auc_delta.lower is not None else None},"
            f" {round(auc_delta.upper, 4) if auc_delta.upper is not None else None}]"
            f"  -> {auc_delta.verdict}"
        )
        lines.append(
            f"    dBrier     {round(brier_delta.point, 4) if brier_delta.point is not None else None}"
            f"  95% CI [{round(brier_delta.lower, 4) if brier_delta.lower is not None else None},"
            f" {round(brier_delta.upper, 4) if brier_delta.upper is not None else None}]"
            f"  (negative favours {candidate_name})"
        )
    if evidence is not None:
        lines.append("    by evidence bucket (prior venue matches, shared count):")
        buckets: Dict[str, Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]] = {}
        for base_record, cand_record in zip(left, right, strict=True):
            bucket = evidence_bucket(evidence.get(fixture_key(base_record), 0))
            slot = buckets.setdefault(bucket, ([], []))
            for target, record in ((slot[0], base_record), (slot[1], cand_record)):
                if record.is_scored and record.outcome is not BttsOutcome.UNKNOWN:
                    target.append((record.probability or 0.0, outcome_to_y(record.outcome)))
        for bucket in sorted(buckets):
            base_pairs, cand_pairs = buckets[bucket]
            base_bucket_auc = auc_from_labelled(base_pairs)
            cand_bucket_auc = auc_from_labelled(cand_pairs)
            base_brier = (
                sum((p - y) ** 2 for p, y in base_pairs) / len(base_pairs) if base_pairs else None
            )
            cand_brier = (
                sum((p - y) ** 2 for p, y in cand_pairs) / len(cand_pairs) if cand_pairs else None
            )
            lines.append(
                f"      {bucket:8s} n={len(base_pairs):5d}"
                f"  AUC {base_bucket_auc if base_bucket_auc is None else round(base_bucket_auc, 3)}"
                f" -> {cand_bucket_auc if cand_bucket_auc is None else round(cand_bucket_auc, 3)}"
                f"   Brier {base_brier if base_brier is None else round(base_brier, 4)}"
                f" -> {cand_brier if cand_brier is None else round(cand_brier, 4)}"
            )
    return lines


def evidence_counts(
    dataset: Sequence[HistoricalMatch], targets: Sequence[HistoricalMatch]
) -> Dict[Tuple[str, int, str], int]:
    """
    Prior current-season HOME venue matches per target, counted once for both arms.

    One externally supplied count, so a fixture cannot land in different buckets
    for different models - which would quietly stop the comparison being
    like-for-like.
    """
    counts: Dict[Tuple[str, int, str], int] = {}
    for target in targets:
        prior = [
            match
            for match in dataset
            if match.competition == target.competition
            and match.season == target.season
            and match.kickoff < target.kickoff
            and match.home_team_id == target.home_team_id
            and match.completed
        ]
        counts[(target.competition, target.season, target.event_id)] = len(prior)
    return counts


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def run_candidates(
    dataset: Sequence[HistoricalMatch],
    seasons: Sequence[int],
    *,
    leagues: Sequence[str],
    xi: float,
    rho: Optional[float],
    lambda_shared: Optional[float],
    bootstrap: int,
) -> List[str]:
    """Replay the baseline and every candidate over the same target seasons."""
    targets = [
        match
        for match in dataset
        if match.season in set(seasons) and match.competition in set(leagues)
    ]
    lines = [
        f"targets={len(targets)} seasons={list(seasons)} leagues={list(leagues)}",
        "",
        "RAW COVERAGE AND STANDALONE METRICS",
    ]

    baseline = replay(dataset, PoissonV1Adapter(), targets=targets)
    arms: List[Tuple[str, List[PredictionRecord]]] = [("POISSON_V1_RAW", baseline)]

    shared_cache: Dict[object, TeamStrength] = {}
    candidates: List[Tuple[str, MaherAdapter]] = [
        (
            "C1_MAHER",
            MaherAdapter(model_id="C1_MAHER", xi=0.0, fit_cache=shared_cache),
        ),
    ]
    if xi > 0.0:
        candidates.append(("C2_MAHER_DECAY", MaherAdapter(model_id="C2_MAHER_DECAY", xi=xi)))
    if rho is not None:
        candidates.append(
            (
                "C3_DIXON_COLES",
                MaherAdapter(model_id="C3_DIXON_COLES", xi=xi, mode="dixon_coles", rho=rho),
            )
        )
    if lambda_shared is not None:
        candidates.append(
            (
                "C4_BIVARIATE",
                MaherAdapter(
                    model_id="C4_BIVARIATE",
                    xi=xi,
                    mode="bivariate",
                    lambda_shared=lambda_shared,
                ),
            )
        )

    for name, adapter in candidates:
        arms.append((name, replay(dataset, adapter, targets=targets)))

    for name, records in arms:
        lines.extend(report_arm(name, records))

    evidence = evidence_counts(dataset, targets)
    lines.append("")
    lines.append("FAIR INTERSECTION COMPARISONS (AUC is the primary metric)")
    for name, records in arms[1:]:
        lines.extend(
            report_pair(
                "POISSON_V1_RAW",
                baseline,
                name,
                records,
                evidence=evidence,
                bootstrap=bootstrap,
            )
        )

    lines.append("")
    lines.append("SEASON SPLITS (candidate vs baseline, same intersection each)")
    for season in seasons:
        season_baseline = [r for r in baseline if r.season == season]
        for name, records in arms[1:]:
            season_candidate = [r for r in records if r.season == season]
            left, right = intersect(season_baseline, season_candidate)
            base_auc = auc_from_labelled(_labelled(left))
            cand_auc = auc_from_labelled(_labelled(right))
            lines.append(
                f"  {season} {name:16s} n={len(left):5d}"
                f"  AUC {base_auc if base_auc is None else round(base_auc, 4)}"
                f" -> {cand_auc if cand_auc is None else round(cand_auc, 4)}"
            )

    lines.append("")
    lines.append("LEAGUE SPLITS")
    for league in leagues:
        league_baseline = [r for r in baseline if r.competition == league]
        for name, records in arms[1:]:
            league_candidate = [r for r in records if r.competition == league]
            left, right = intersect(league_baseline, league_candidate)
            base_auc = auc_from_labelled(_labelled(left))
            cand_auc = auc_from_labelled(_labelled(right))
            lines.append(
                f"  {league:6s} {name:16s} n={len(left):5d}"
                f"  AUC {base_auc if base_auc is None else round(base_auc, 4)}"
                f" -> {cand_auc if cand_auc is None else round(cand_auc, 4)}"
            )
    return lines


def run_oracle(
    dataset: Sequence[HistoricalMatch],
    seasons: Sequence[int],
    *,
    leagues: Sequence[str],
) -> List[str]:
    """The quarantined ceiling probe. Read the class docstring before using this."""
    targets = [
        match
        for match in dataset
        if match.season in set(seasons) and match.competition in set(leagues)
    ]
    probe = OracleCeilingProbe(dataset)
    records = replay(dataset, probe, targets=targets)
    baseline = replay(dataset, PoissonV1Adapter(), targets=targets)
    left, right = intersect(baseline, records)
    lines = [
        "#" * 74,
        "# ORACLE CEILING PROBE - DELIBERATELY LEAKY - NOT A MODEL",
        "# Fitted on the FULL dataset including the target season and the target",
        "# fixture itself. These numbers are NOT predictions and NOT an",
        "# improvement over anything. They bound the discrimination available",
        "# from goal-count information under this model class.",
        "#" * 74,
        "",
    ]
    lines.extend(report_arm("ORACLE_LEAKY_CEILING", records))
    oracle_auc = auc_from_labelled(_labelled(right))
    base_auc = auc_from_labelled(_labelled(left))
    lines.append("")
    lines.append(
        f"  On the intersection with POISSON_V1_RAW (n={len(left)}):"
        f" baseline AUC={base_auc if base_auc is None else round(base_auc, 4)},"
        f" LEAKY ceiling AUC={oracle_auc if oracle_auc is None else round(oracle_auc, 4)}"
    )
    lines.append("  INTERPRETATION: if the ceiling is close to the honest candidates, the")
    lines.append("  limit is the MODEL CLASS, not estimation noise, and better estimators")
    lines.append("  of the same quantities cannot help.")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        required=True,
        choices=["likelihood", "search", "validation", "final", "oracle"],
        help=(
            "likelihood=profile xi/rho/lambda3 on development; "
            "search=candidates on development; validation=confirm; "
            "final=frozen holdout ONCE; oracle=quarantined ceiling probe"
        ),
    )
    parser.add_argument(
        "--xi",
        type=float,
        default=None,
        help="frozen decay rate (per day); REQUIRED for validation/final",
    )
    parser.add_argument("--rho", type=float, default=None, help="frozen Dixon-Coles rho")
    parser.add_argument(
        "--lambda-shared", type=float, default=None, help="frozen bivariate lambda3"
    )
    parser.add_argument(
        "--leagues",
        default="eng.1",
        help="comma-separated target leagues (default eng.1 for speed)",
    )
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--per-season", type=int, default=25)
    parser.add_argument("--out", default=None, help="write report to this file")
    args = parser.parse_args()

    leagues = [league.strip() for league in args.leagues.split(",")]
    unknown = [league for league in leagues if league not in TARGET_LEAGUES]
    if unknown:
        parser.error(f"unknown target leagues {unknown}; choose from {TARGET_LEAGUES}")

    if args.stage in ("validation", "final") and args.xi is None:
        parser.error(
            "validation/final require --xi explicitly. Parameters must be frozen "
            "on development BEFORE the holdout is inspected, and naming them on "
            "the command line is what makes that visible."
        )

    if args.stage == "likelihood":
        seasons = DEVELOPMENT_SEASONS
    elif args.stage == "search":
        seasons = DEVELOPMENT_SEASONS
    elif args.stage == "validation":
        seasons = VALIDATION_SEASONS
    elif args.stage == "oracle":
        seasons = DEVELOPMENT_SEASONS
    else:
        seasons = HOLDOUT_SEASONS

    dataset, missing = build_dataset(seasons)
    lines = [
        "=" * 74,
        f"EPIC 2D  stage={args.stage}  seasons={seasons}  leagues={leagues}",
        "=" * 74,
        f"dataset matches={len(dataset)} missing_windows={len(missing)}",
    ]
    for season in seasons:
        if season in BURNED_SEASONS:
            lines.append(
                f"WARNING: season {season} was previously inspected "
                f"({BURNED_SEASONS[season]}); it is NOT untouched."
            )
    lines.append("")

    if args.stage == "likelihood":
        lines.append("XI PROFILE - out-of-sample GOAL-COUNT log-likelihood per fixture")
        lines.append("(selected on goals, never on BTTS Brier - see GG-029)")
        xi_grid = [0.0, 0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.02]
        for point in profile_xi(dataset, seasons, xi_grid, per_season=args.per_season):
            half_life = "inf" if point.parameter == 0 else f"{math.log(2)/point.parameter:.0f}d"
            lines.append(
                f"  xi={point.parameter:<7.4f} half_life={half_life:>6s}"
                f" mean_loglik={point.log_likelihood if point.log_likelihood is None else round(point.log_likelihood, 5)}"
                f" n={point.fixtures}"
            )
        scored_points = [
            point
            for point in profile_xi(dataset, seasons, xi_grid, per_season=args.per_season)
            if point.log_likelihood is not None
        ]
        best = (
            max(scored_points, key=lambda p: p.log_likelihood or -math.inf)
            if scored_points
            else None
        )
        chosen_xi = best.parameter if best else 0.0

        lines.append(f"  -> best xi by likelihood = {chosen_xi}")

        lines.append("")
        lines.append("RHO PROFILE (Dixon-Coles), at the selected xi")
        for point in profile_rho(
            dataset,
            seasons,
            [-0.2, -0.15, -0.1, -0.05, 0.0, 0.05, 0.1],
            xi=chosen_xi,
            per_season=args.per_season,
        ):
            lines.append(
                f"  rho={point.parameter:<7.3f}"
                f" mean_loglik={point.log_likelihood if point.log_likelihood is None else round(point.log_likelihood, 5)}"
                f" n={point.fixtures}"
            )

        lines.append("")
        lines.append("LAMBDA3 IDENTIFIABILITY PROFILE (bivariate Poisson)")
        lines.append("If flat or maximised at 0, C4 is DROPPED rather than constrained.")
        for point in profile_lambda_shared(
            dataset,
            seasons,
            [0.0, 0.05, 0.1, 0.2, 0.3, 0.5],
            xi=chosen_xi,
            per_season=args.per_season,
        ):
            lines.append(
                f"  lambda3={point.parameter:<5.2f}"
                f" mean_loglik={point.log_likelihood if point.log_likelihood is None else round(point.log_likelihood, 5)}"
                f" n={point.fixtures}"
            )
    elif args.stage == "oracle":
        lines.extend(run_oracle(dataset, seasons, leagues=leagues))
    else:
        lines.extend(
            run_candidates(
                dataset,
                seasons,
                leagues=leagues,
                xi=args.xi or 0.0,
                rho=args.rho,
                lambda_shared=args.lambda_shared,
                bootstrap=args.bootstrap,
            )
        )

    report = "\n".join(lines)
    print(report)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report + "\n")
        print(f"\n[written to {destination}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
