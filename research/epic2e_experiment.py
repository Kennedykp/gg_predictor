"""
Epic 2E STAGE 0 - the shot-informed CEILING PROBE. RESEARCH ONLY, DELIBERATELY LEAKY.

THE QUESTION THIS FILE ANSWERS, AND NOTHING ELSE.

    Epic 2D established that goal-count information carries a discrimination
    ceiling: a probe that cheated outright - fitted in-sample on the whole
    dataset including the fixtures it was scoring - still only reached AUC
    0.5679. Honest goal-count models reached 0.537-0.546.

    Stage 0 asks whether SHOT information has a higher ceiling. It does so the
    cheapest possible way: by cheating as hard as it is possible to cheat.

WHY A CEILING PROBE IS THE SMALLEST RIGOROUS EXPERIMENT. Building an honest
shot-based model is weeks of work and answers the strategic question only if it
succeeds. A ceiling probe answers it in either direction in a day, because the
logic is one-way and airtight:

    if a probe that KNOWS THE ANSWER cannot rank fixtures well,
    then no honest estimator of the same information can either.

A ceiling is an upper bound. Failing to clear the goal-count ceiling closes the
direction; clearing it does NOT prove an honest model can be built, which is
exactly why Stage 1 is a separate, separately-approved piece of work.

THE THREE ARMS, IN INCREASING ORDER OF DISHONESTY.

  GOAL_ORACLE_LEAKY   Epic 2D's probe, recomputed here. In-sample team strength
                      from GOALS. Present because 2D's published 0.5679 was
                      measured on eng.1 2018-19 only (n=760); comparing a new
                      five-league number against it would be comparing different
                      fixtures and different leagues. The ceiling must be
                      re-measured on the SAME fixtures or the comparison is void.

  SHOT_ORACLE_LEAKY   In-sample team strength from SHOTS ON TARGET, converted to
                      goal rates by the in-sample conversion ratio. The direct
                      analogue of 2D's probe with shots substituted for goals.

  SHOT_ORACLE_ACTUAL  The strict upper bound, and the arm that matters most. It
                      reads THE TARGET FIXTURE'S OWN SHOT COUNTS - what actually
                      happened during the 90 minutes it is predicting - and
                      converts them to goal rates. This is not a model in any
                      sense; it is the answer to "if you knew exactly how many
                      chances each side would create, could you rank BTTS?"

                      No pre-match shot model can ever beat this, because a
                      forecast of a quantity cannot beat perfect knowledge of it.
                      If THIS does not clear the goal-count ceiling, the entire
                      shot direction is closed, and closed rigorously.

HOW THE ABSOLUTE CONFIDENCE INTERVALS ARE OBTAINED. The approval requires AUC
with a CI, not a point estimate. `domain.discrimination` provides a PAIRED
bootstrap (audited, seeded, used by 2D) but no single-arm interval. Rather than
write a second, unaudited bootstrap, each arm is paired against a CONSTANT
predictor. A constant predictor's AUC is exactly 0.5 in every resample - all
pairs tie, and the tie convention contributes 0.5 (verified: `auc_from_labelled`
on all-equal probabilities returns exactly 0.5). Therefore

    absolute AUC CI = 0.5 + (paired delta CI against the constant arm)

which is the arm's own sampling distribution, computed by code that already has
tests. Same seed and iteration count as 2D, so the intervals are comparable.

QUARANTINE. Every arm here is leaky. Every `model_id` says so. None is
registered in the harness model registry, none is a candidate for anything, and
no number in the output file is an "improvement" over any model. Production is
untouched: this module imports `espn` only to reuse its cache-key and season-
window helpers, and adds no shot parsing to it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.comparison import (  # noqa: E402
    compare,
    extreme_probability_stats,
    intersect,
)
from domain.discrimination import (  # noqa: E402
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    paired_auc_delta,
    summarise_discrimination,
)
from domain.evaluation import (  # noqa: E402
    PredictionRecord,
    UnevaluableReason,
    summarise,
)
from domain.goal_models import (  # noqa: E402
    TeamStrength,
    btts_independent,
    fit_team_strength,
    predict_lambdas,
)
from domain.historical import HistoricalMatch  # noqa: E402
from evaluation_harness import ModelPrediction, PredictionContext, replay  # noqa: E402
from research.epic2c_experiment import (  # noqa: E402
    TARGET_LEAGUES,
    load_season,
)
from research.epic2e_shot_stats import (  # noqa: E402
    ShotProfile,
    agreement_diagnostic,
    load_many,
)

# ---------------------------------------------------------------------------
# Protocol constants. Pre-registered in docs/EPIC_2E_PROTOCOL.md and approved
# BEFORE any number below was computed.
# ---------------------------------------------------------------------------

#: Stage 0 runs on already-burned development seasons. A ceiling probe cannot
#: "use up" a season in the sense that matters - it is not selecting anything and
#: not being promoted - but it must still not touch the holdout, because the
#: reported ceiling would then be a number measured on 2025.
STAGE0_SEASONS = [2018, 2019]

#: The 2D comparison was published on eng.1 alone. Reproduced separately so the
#: recomputation can be checked against 0.5679 before any new claim is made.
EPIC2D_ORACLE_LEAGUES = ["eng.1"]

#: Epic 2D's published leaky goal-count ceiling (eng.1 2018-19, n=748 on the
#: intersection with POISSON_V1_RAW). The gate is expressed against this.
EPIC2D_PUBLISHED_CEILING = 0.5679

#: PRE-REGISTERED AND IMMOVABLE. Approved before the experiment ran. Stage 1 is
#: authorised only if the shot ceiling is materially above the goal-count
#: ceiling; 0.60 is the agreed meaning of "materially".
STAGE0_GATE = 0.60

#: Every season ever scored as a BTTS TARGET by any epic. Epic 2D's own constant
#: omitted 2021/2022 (its validation) and 2024 (its holdout); that file is
#: historical and is deliberately NOT edited, so the complete list lives here.
BURNED_SEASONS = {
    2018: "Epic 2B.3 baseline / 2C search / 2D development",
    2019: "Epic 2B.3 baseline / 2C search / 2D development",
    2020: "Epic 2B.3 / 2C validation",
    2021: "Epic 2D validation",
    2022: "Epic 2D validation",
    2023: "Epic 2B.3 / 2C final test",
    2024: "Epic 2D final holdout",
}

#: The single untouched season. Named here so any future stage that tries to
#: read it has to do so deliberately, and so Stage 0 can assert it never did.
HOLDOUT_SEASON = 2025

RESULTS_DIR = REPO_ROOT / "research" / "epic2e_results"


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def build_dataset(
    leagues: Sequence[str],
    seasons: Sequence[int],
) -> Tuple[List[HistoricalMatch], Dict[str, ShotProfile], List[str]]:
    """
    Matches plus their shot profiles, for the requested leagues and seasons.

    The season BEFORE each target is loaded too. For a ceiling probe the prior
    season is not strictly required, but loading exactly what an honest Stage 1
    would load keeps the fixture sets comparable, and the in-sample fit is then
    built from the same evidence pool an honest model would have had.
    """
    needed = sorted({s for season in seasons for s in (season - 1, season)})
    if HOLDOUT_SEASON in needed:
        raise AssertionError(
            f"Stage 0 must not load the untouched holdout season {HOLDOUT_SEASON}"
        )

    matches: List[HistoricalMatch] = []
    missing: List[str] = []
    for league in leagues:
        for season in needed:
            loaded, gaps = load_season(league, season)
            matches.extend(loaded)
            missing.extend(gaps)

    profiles, profile_gaps = load_many(leagues, needed)
    missing.extend(profile_gaps)
    return matches, profiles, missing


# ---------------------------------------------------------------------------
# Arms. All three are leaky. None is a model.
# ---------------------------------------------------------------------------


def _shot_surrogate(
    match: HistoricalMatch,
    profiles: Dict[str, ShotProfile],
) -> Optional[HistoricalMatch]:
    """
    A copy of `match` whose goal counts are replaced by SHOTS ON TARGET.

    A surrogate, rather than a second estimator, so the audited
    `fit_team_strength` does the fitting. The Poisson attack/defence estimator
    does not care what is being counted; feeding it shots on target yields a
    shot-creation and shot-suppression strength per team on exactly the same
    algebra, with the same convergence behaviour and the same
    `min_matches_per_team` refusal semantics.

    Returns None when the fixture has no usable statistics, so an unavailable
    match is dropped from the fit rather than entering it as a 0-0.
    """
    profile = profiles.get(match.event_id)
    if profile is None or not profile.available:
        return None
    return replace(
        match,
        home_goals=profile.home.shots_on_target,
        away_goals=profile.away.shots_on_target,
    )


def _conversion_ratio(
    matches: Sequence[HistoricalMatch],
    profiles: Dict[str, ShotProfile],
) -> Dict[str, float]:
    """
    Goals per shot on target, per competition, over the whole in-sample pool.

    Needed because `btts_independent` consumes GOAL rates. A shot-strength fit
    predicts shots on target, so the two must be on the same scale before the
    BTTS mapping can be applied - and using the identical mapping for every arm
    is what makes the AUC difference attributable to the INFORMATION rather than
    to a different probability formula.

    In-sample and therefore leaky, like everything else in this file.
    """
    goals: Dict[str, int] = {}
    on_target: Dict[str, int] = {}
    for match in matches:
        profile = profiles.get(match.event_id)
        if profile is None or not profile.available:
            continue
        if match.home_goals is None or match.away_goals is None:
            continue
        goals[match.competition] = (
            goals.get(match.competition, 0) + match.home_goals + match.away_goals
        )
        on_target[match.competition] = (
            on_target.get(match.competition, 0)
            + (profile.home.shots_on_target or 0)
            + (profile.away.shots_on_target or 0)
        )
    return {
        competition: goals[competition] / on_target[competition]
        for competition in goals
        if on_target.get(competition, 0) > 0
    }


class GoalOracleLeaky:
    """
    ########  QUARANTINED - DELIBERATELY LEAKY - NOT A MODEL  ########

    Epic 2D's ORACLE CEILING PROBE, recomputed. In-sample goal-count team
    strength fitted over EVERY match including the targets, with 2D's exact
    settings (xi=0, min_matches_per_team=4, tol=1e-7, max_iter=60).

    Present for one reason: Epic 2D published its ceiling on eng.1 2018-19 only.
    A five-league shot number compared against that would differ in fixtures,
    leagues and sample size simultaneously, and any gap would be uninterpretable.
    This arm re-measures the goal-count ceiling on whatever fixture set is being
    used, so the shot arms are compared like for like.

    ################################################################
    """

    model_id = "ORACLE_LEAKY_GOALS"
    model_version = "2e.1-quarantined"

    def __init__(self, dataset: Sequence[HistoricalMatch]) -> None:
        self._fits: Dict[str, TeamStrength] = {}
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
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no fit"
            )
        rates = predict_lambdas(model, context.home_team_id, context.away_team_id)
        if rates is None or rates[0] <= 0.0 or rates[1] <= 0.0:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="degenerate"
            )
        return ModelPrediction(probability=btts_independent(*rates))


class ShotOracleLeaky:
    """
    ########  QUARANTINED - DELIBERATELY LEAKY - NOT A MODEL  ########

    The direct shot analogue of the arm above. In-sample team strength fitted on
    SHOTS ON TARGET over every match including the targets, then converted to
    goal rates by the in-sample goals-per-shot-on-target ratio and mapped through
    the same `btts_independent`.

    What it bounds: the discrimination available from knowing each team's
    season-long shot-creation and shot-suppression profile perfectly.

    ################################################################
    """

    model_id = "ORACLE_LEAKY_SHOTS_INSAMPLE"
    model_version = "2e.1-quarantined"

    def __init__(
        self,
        dataset: Sequence[HistoricalMatch],
        profiles: Dict[str, ShotProfile],
    ) -> None:
        self._conversion = _conversion_ratio(dataset, profiles)
        self._fits: Dict[str, TeamStrength] = {}

        surrogates: Dict[str, List[HistoricalMatch]] = {}
        for match in dataset:
            surrogate = _shot_surrogate(match, profiles)
            if surrogate is None:
                continue
            surrogates.setdefault(surrogate.competition, []).append(surrogate)

        for competition, matches in surrogates.items():
            latest = max(match.kickoff for match in matches)
            self._fits[competition] = fit_team_strength(
                matches,
                as_of=latest.replace(year=latest.year + 1),
                xi=0.0,
                min_matches_per_team=4,
                tolerance=1e-7,
                max_iterations=60,
            )

    def predict(self, context: PredictionContext) -> ModelPrediction:
        model = self._fits.get(context.competition)
        conversion = self._conversion.get(context.competition)
        if model is None or conversion is None:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no fit"
            )
        rates = predict_lambdas(model, context.home_team_id, context.away_team_id)
        if rates is None or rates[0] <= 0.0 or rates[1] <= 0.0:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="degenerate"
            )
        return ModelPrediction(
            probability=btts_independent(rates[0] * conversion, rates[1] * conversion)
        )


class ShotOracleActual:
    """
    ####  QUARANTINED - MAXIMALLY LEAKY - THE STRICT UPPER BOUND - NOT A MODEL  ####

    This arm reads the target fixture's OWN shot counts: the shots on target each
    side actually recorded during the match it is being asked about. It converts
    them to goal rates with the in-sample conversion ratio and applies the same
    BTTS mapping.

    THIS IS THE DECISIVE NUMBER OF STAGE 0. It is an upper bound on every
    conceivable pre-match shot model, because no forecast of a quantity can
    outperform perfect knowledge of that quantity. Concretely, it bounds:

        - any team-level shot-rate estimator, however well fitted
        - any xG model, since xG is a weighted function of shots
        - any form, lineup or market feature whose only route to BTTS is
          through changing how many chances the two sides create

    The only thing it does NOT bound is information about CONVERSION - whether
    these particular chances were taken - which is precisely the quantity Epic
    2D's H0 says is irreducible.

    It obtains the target's statistics by looking its `event_id` up in the
    sidecar. That is the leak, it is deliberate, and it is why this class is
    quarantined and named the way it is.

    ###############################################################################
    """

    model_id = "ORACLE_LEAKY_SHOTS_ACTUAL"
    model_version = "2e.1-quarantined"

    def __init__(
        self,
        dataset: Sequence[HistoricalMatch],
        profiles: Dict[str, ShotProfile],
    ) -> None:
        self._profiles = profiles
        self._conversion = _conversion_ratio(dataset, profiles)

    def predict(self, context: PredictionContext) -> ModelPrediction:
        conversion = self._conversion.get(context.competition)
        if conversion is None:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no conversion"
            )
        profile = self._profiles.get(context.event_id)
        if profile is None or not profile.available:
            # A refusal, not a guess. The fair intersection then removes this
            # fixture from BOTH arms, so no arm is credited for coverage it
            # does not have.
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no shot data"
            )
        home = (profile.home.shots_on_target or 0) * conversion
        away = (profile.away.shots_on_target or 0) * conversion
        return ModelPrediction(probability=btts_independent(home, away))


class ShotOracleActualExcludingGoals:
    """
    ####  QUARANTINED - LEAKY - THE CONFOUND CONTROL - NOT A MODEL  ####

    THE ARM THAT KEEPS STAGE 0 HONEST. It exists because of a definitional trap
    that would otherwise inflate the headline number without anybody noticing.

    THE TRAP. Every goal IS a shot on target. The containment is definitional, so
    `ShotOracleActual` does not merely know how many chances a side created - it
    partially knows WHETHER THEY SCORED:

        shots_on_target == 0  =>  that team did not score  =>  BTTS is FALSE

    with certainty, not with probability. Those fixtures are free correct answers
    drawn straight from the label. `ShotOracleActual` emits exactly-zero
    probabilities on them, which is the visible symptom in its output.

    So its AUC is NOT purely a bound on chance-creation information. It is a
    bound on chance-creation information PLUS a partial copy of the outcome, and
    quoting it as available headroom would overstate the case for Stage 1.

    THE CONTROL. This arm uses NON-SCORING shots on target, `SOT - goals`: the
    chances a team created and did NOT convert. The definitional link to the
    label is removed - a team can have zero non-scoring shots on target and still
    have scored - so whatever discrimination survives here is attributable to
    chance creation rather than to the outcome leaking through the feature.

    Read the two together:
      * if ACTUAL is high and this is at the goal-count ceiling, the headline
        gain was containment, and no pre-match shot model can inherit it
      * if this is also high, chance-creation genuinely carries signal

    ###################################################################
    """

    model_id = "ORACLE_LEAKY_SHOTS_EXCL_GOALS"
    model_version = "2e.1-quarantined"

    def __init__(
        self,
        dataset: Sequence[HistoricalMatch],
        profiles: Dict[str, ShotProfile],
    ) -> None:
        self._profiles = profiles
        self._lookup = {match.event_id: match for match in dataset}

        # Conversion is recomputed on the NON-SCORING basis. Reusing the
        # goals-per-SOT ratio would put this arm on a different scale from its
        # own feature and quietly change the BTTS mapping rather than the
        # information, which is the one thing every arm must hold constant.
        goals: Dict[str, int] = {}
        residual: Dict[str, int] = {}
        for match in dataset:
            profile = profiles.get(match.event_id)
            if profile is None or not profile.available:
                continue
            if match.home_goals is None or match.away_goals is None:
                continue
            goals[match.competition] = (
                goals.get(match.competition, 0) + match.home_goals + match.away_goals
            )
            residual[match.competition] = residual.get(match.competition, 0) + max(
                (profile.home.shots_on_target or 0) - match.home_goals, 0
            ) + max((profile.away.shots_on_target or 0) - match.away_goals, 0)
        self._conversion = {
            competition: goals[competition] / residual[competition]
            for competition in goals
            if residual.get(competition, 0) > 0
        }

    def predict(self, context: PredictionContext) -> ModelPrediction:
        conversion = self._conversion.get(context.competition)
        profile = self._profiles.get(context.event_id)
        match = self._lookup.get(context.event_id)
        if conversion is None or profile is None or not profile.available:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no shot data"
            )
        if match is None or match.home_goals is None or match.away_goals is None:
            return ModelPrediction(
                reason=UnevaluableReason.INSUFFICIENT_HISTORY, detail="no result"
            )
        home = max((profile.home.shots_on_target or 0) - match.home_goals, 0)
        away = max((profile.away.shots_on_target or 0) - match.away_goals, 0)
        return ModelPrediction(
            probability=btts_independent(home * conversion, away * conversion)
        )


class ConstantArm:
    """
    A constant predictor, used ONLY as the pairing partner for absolute CIs.

    Its AUC is exactly 0.5 in every bootstrap resample because every pair ties,
    so `paired_auc_delta(constant, arm)` is the arm's own AUC distribution
    shifted by -0.5. That lets the audited paired bootstrap produce a single-arm
    interval, instead of this file introducing a second resampler with no tests.

    The value is 0.5 rather than the base rate because only the RANKING matters
    for this purpose, and 0.5 makes the tie behaviour obvious to a reader.
    """

    model_id = "CONSTANT_REFERENCE"
    model_version = "2e.1"

    def predict(self, context: PredictionContext) -> ModelPrediction:
        return ModelPrediction(probability=0.5)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmResult:
    """One arm's absolute discrimination, with the interval the gate needs."""

    model_id: str
    scored: int
    targets: int
    auc: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    brier: Optional[float]
    exact_zero: int
    exact_one: int


def absolute_auc_interval(
    arm: Sequence[PredictionRecord],
    constant: Sequence[PredictionRecord],
) -> Tuple[Optional[float], Optional[float]]:
    """
    A 95% interval for one arm's own AUC. See the module docstring for the logic.

    Both sequences are intersected first so the pairing is fixture for fixture;
    `paired_auc_delta` does that alignment itself via `_aligned`.
    """
    interval = paired_auc_delta(
        constant,
        arm,
        iterations=DEFAULT_BOOTSTRAP_ITERATIONS,
        seed=DEFAULT_BOOTSTRAP_SEED,
    )
    if interval.lower is None or interval.upper is None:
        return None, None
    return 0.5 + interval.lower, 0.5 + interval.upper


def evaluate_arm(
    arm: Sequence[PredictionRecord],
    constant: Sequence[PredictionRecord],
) -> ArmResult:
    """Absolute metrics for one arm, on the fixtures it actually scored."""
    discrimination = summarise_discrimination(arm)
    metrics = summarise(arm, model_id=arm[0].model_id, model_version=arm[0].model_version)
    extremes = extreme_probability_stats(arm)
    lower, upper = absolute_auc_interval(arm, constant)
    return ArmResult(
        model_id=arm[0].model_id,
        scored=discrimination.scored,
        targets=len(arm),
        auc=discrimination.auc,
        lower=lower,
        upper=upper,
        brier=metrics.brier,
        exact_zero=extremes.exactly_zero,
        exact_one=extremes.exactly_one,
    )


def _fmt(value: Optional[float], places: int = 4) -> str:
    return "None" if value is None else f"{value:.{places}f}"


def run_stage0(leagues: Sequence[str], seasons: Sequence[int]) -> str:
    """Run Stage 0 for one league set and return the report text."""
    lines: List[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)

    matches, profiles, missing = build_dataset(leagues, seasons)
    targets = [
        match
        for match in matches
        if match.season in seasons and match.competition in leagues
    ]

    emit("=" * 74)
    emit(f"EPIC 2E  stage=0 CEILING PROBE  seasons={list(seasons)}  leagues={list(leagues)}")
    emit("=" * 74)
    emit("")
    emit("#" * 74)
    emit("# ALL ARMS BELOW ARE DELIBERATELY LEAKY. NONE IS A MODEL. NONE IS A")
    emit("# CANDIDATE. NO NUMBER HERE IS AN IMPROVEMENT OVER ANYTHING. They")
    emit("# bound the discrimination available from an information source.")
    emit("#" * 74)
    emit("")
    emit(f"dataset matches={len(matches)} missing_windows={len(missing)}")
    emit(f"shot profiles={len(profiles)} available={sum(1 for p in profiles.values() if p.available)}")
    emit(f"targets={len(targets)}")
    emit("")
    for season in sorted({match.season for match in targets}):
        note = BURNED_SEASONS.get(season)
        if note:
            emit(f"NOTE: season {season} was already inspected as a target ({note}).")
    emit(f"NOTE: holdout season {HOLDOUT_SEASON} was NOT loaded by this stage.")
    emit("")

    agreement = agreement_diagnostic(targets, profiles)
    emit("PER-MATCH VALIDATION (are the statistics this match, or the season?)")
    emit(
        f"  block totalGoals == recorded score: {agreement.agreeing}/{agreement.checked}"
        f"  rate={_fmt(agreement.rate)}"
    )
    emit("  (a season-cumulative block would disagree grossly; this licenses")
    emit("   treating the fields as per-match observations)")
    emit("")

    arms = {
        GoalOracleLeaky.model_id: GoalOracleLeaky(matches),
        ShotOracleLeaky.model_id: ShotOracleLeaky(matches, profiles),
        ShotOracleActual.model_id: ShotOracleActual(matches, profiles),
        ShotOracleActualExcludingGoals.model_id: ShotOracleActualExcludingGoals(
            matches, profiles
        ),
        ConstantArm.model_id: ConstantArm(),
    }

    records: Dict[str, List[PredictionRecord]] = {}
    for model_id, adapter in arms.items():
        records[model_id] = replay(matches, adapter, targets=targets)

    constant = records[ConstantArm.model_id]

    emit("STANDALONE (each arm on the fixtures it scored; CI via paired")
    emit("bootstrap against a constant predictor, seed=%d, iterations=%d)"
         % (DEFAULT_BOOTSTRAP_SEED, DEFAULT_BOOTSTRAP_ITERATIONS))
    emit("")
    results: Dict[str, ArmResult] = {}
    for model_id in (
        GoalOracleLeaky.model_id,
        ShotOracleLeaky.model_id,
        ShotOracleActual.model_id,
        ShotOracleActualExcludingGoals.model_id,
    ):
        result = evaluate_arm(records[model_id], constant)
        results[model_id] = result
        emit(f"  {model_id}")
        emit(
            f"      scored={result.scored}/{result.targets}"
            f"  coverage={100.0 * result.scored / result.targets:.1f}%"
        )
        emit(
            f"      AUC={_fmt(result.auc)}  95% CI [{_fmt(result.lower)}, {_fmt(result.upper)}]"
        )
        emit(f"      brier={_fmt(result.brier)}  exact0={result.exact_zero} exact1={result.exact_one}")
        emit("")

    emit("PAIRED COMPARISONS ON THE FAIR INTERSECTION")
    emit("(the goal-count ceiling is RE-MEASURED on these same fixtures, because")
    emit(" Epic 2D published its 0.5679 on eng.1 only - a cross-sample comparison")
    emit(" would be meaningless)")
    emit("")
    for challenger in (
        ShotOracleLeaky.model_id,
        ShotOracleActual.model_id,
        ShotOracleActualExcludingGoals.model_id,
    ):
        left, right = intersect(records[GoalOracleLeaky.model_id], records[challenger])
        if not left:
            emit(f"  {challenger}: empty intersection")
            continue
        comparison = compare(left, right)
        delta = paired_auc_delta(left, right)
        # AUC is recomputed on the INTERSECTED records rather than read off the
        # standalone summaries: the intersection is a different (smaller) fixture
        # set, and quoting a whole-sample AUC beside a paired delta measured on
        # the intersection would be two numbers from two populations.
        left_auc = summarise_discrimination(left).auc
        right_auc = summarise_discrimination(right).auc
        emit(f"  GOALS vs {challenger}: n={comparison.intersection_size}")
        emit(f"    AUC  goals={_fmt(left_auc)}   {challenger}={_fmt(right_auc)}")
        emit(
            f"    dAUC {_fmt(delta.point)}  95% CI [{_fmt(delta.lower)}, {_fmt(delta.upper)}]"
            f"  -> {delta.verdict}"
        )
        emit("")

    decisive = results[ShotOracleActual.model_id]
    emit("=" * 74)
    emit("STAGE 0 GATE")
    emit("=" * 74)
    emit(f"  pre-registered threshold          : {STAGE0_GATE:.4f} (immovable)")
    emit(f"  Epic 2D published goal ceiling    : {EPIC2D_PUBLISHED_CEILING:.4f}")
    emit(
        f"  goal ceiling re-measured here     : "
        f"{_fmt(results[GoalOracleLeaky.model_id].auc)}"
    )
    emit(f"  DECISIVE ARM ({decisive.model_id})")
    emit(
        f"    AUC={_fmt(decisive.auc)}  95% CI [{_fmt(decisive.lower)}, {_fmt(decisive.upper)}]"
        f"  n={decisive.scored}"
    )
    emit("")

    control = results[ShotOracleActualExcludingGoals.model_id]
    goal_ceiling = results[GoalOracleLeaky.model_id]
    emit(f"  CONFOUND CONTROL ({control.model_id})")
    emit(
        f"    AUC={_fmt(control.auc)}  95% CI [{_fmt(control.lower)}, {_fmt(control.upper)}]"
        f"  n={control.scored}"
    )
    emit("    Goals are a SUBSET of shots on target, so the decisive arm knows")
    emit("    part of the label outright (SOT==0 implies the team did not score).")
    emit("    This control removes scoring shots; what survives is attributable to")
    emit("    chance creation rather than to the outcome leaking through.")
    emit("")

    # The gate is judged on the CONTROL, not on the raw decisive arm, because the
    # raw arm's advantage is partly definitional. A PASS on a definitional
    # advantage would authorise weeks of work that no honest model can inherit,
    # which is precisely the mistake this stage exists to prevent.
    if decisive.auc is None or decisive.lower is None or decisive.upper is None:
        verdict = "INCONCLUSIVE"
        because = "the interval is undefined"
    elif control.upper is not None and goal_ceiling.auc is not None and (
        control.upper < STAGE0_GATE
    ):
        verdict = "FAIL"
        because = (
            f"the raw upper bound ({_fmt(decisive.auc)}) clears the threshold only "
            "because goals are a subset of shots on target - it is reading part of "
            "the label. With scoring shots removed, the confound-controlled ceiling "
            f"is {_fmt(control.auc)} CI [{_fmt(control.lower)}, {_fmt(control.upper)}], "
            f"entirely below the pre-registered {STAGE0_GATE:.2f}, so the headroom is "
            "an artefact and not information an honest pre-match model could use"
        )
    elif decisive.lower >= STAGE0_GATE:
        verdict = "PASS"
        because = (
            f"the entire interval lies at or above the {STAGE0_GATE:.2f} threshold, "
            "and the confound-controlled arm also clears it, so the headroom is not "
            "an artefact of goals being contained in shots on target"
        )
    elif decisive.upper < EPIC2D_PUBLISHED_CEILING:
        verdict = "FAIL"
        because = (
            "the whole interval lies BELOW the goal-count ceiling of "
            f"{EPIC2D_PUBLISHED_CEILING:.4f}, so perfect knowledge of shots "
            "discriminates no better than goal counts already did"
        )
    elif decisive.upper < STAGE0_GATE:
        verdict = "FAIL"
        because = (
            f"the whole interval lies below the pre-registered {STAGE0_GATE:.2f} "
            "threshold, so there is no material headroom for an honest model"
        )
    else:
        verdict = "INCONCLUSIVE"
        because = (
            f"the interval straddles the {STAGE0_GATE:.2f} threshold; the point "
            "estimate alone must not decide the gate"
        )

    emit(f"  VERDICT: {verdict}")
    emit(f"  because {because}.")
    emit("")
    if verdict == "PASS":
        emit("  Stage 1 becomes ELIGIBLE for separate approval. It is NOT authorised")
        emit("  by this result, and no honest model is built by this file.")
    else:
        emit("  Stage 1 is NOT to be built. Report the negative result and stop.")
    emit("")
    emit("REMINDER: a ceiling is an upper bound. Clearing it would not prove an")
    emit("honest model can reach it; failing it does prove none can.")

    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Epic 2E Stage 0 ceiling probe")
    parser.add_argument(
        "--scope",
        choices=["epic2d-replication", "all-leagues", "both"],
        default="both",
        help=(
            "epic2d-replication: eng.1 only, to check the recomputed goal ceiling "
            "against 2D's published 0.5679. all-leagues: the five production "
            "leagues, for power."
        ),
    )
    parser.add_argument("--write", action="store_true", help="write to research/epic2e_results/")
    args = parser.parse_args(argv)

    plans: List[Tuple[str, Sequence[str]]] = []
    if args.scope in ("epic2d-replication", "both"):
        plans.append(("stage0_eng1_replication", EPIC2D_ORACLE_LEAGUES))
    if args.scope in ("all-leagues", "both"):
        plans.append(("stage0_all_leagues", TARGET_LEAGUES))

    for name, leagues in plans:
        report = run_stage0(leagues, STAGE0_SEASONS)
        print(report)
        if args.write:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            (RESULTS_DIR / f"{name}_LEAKY.txt").write_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
