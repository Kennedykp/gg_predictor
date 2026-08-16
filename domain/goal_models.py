"""
Goal-scoring model structures for Epic 2D discrimination research.

RESEARCH CODE. Nothing in production imports this module, and it imports nothing
from production except the historical contracts. `poisson.py` is untouched.

WHAT THIS IS FOR
----------------
POISSON_V1 builds its two rates from four venue-specific ratios:

    lambda_home = home_goals_scored_home * away_goals_conceded_away / league_avg
    lambda_away = away_goals_scored_away * home_goals_conceded_home / league_avg

Each ratio is an average over that team's matches AT THAT VENUE ONLY. In a
38-match season a team plays 19 home games, so by mid-season each input rests on
roughly 9 matches of about 1.4 goals each - a dozen or so goal events. Epic 2C
showed the resulting estimates are noisy enough that a single goalless away match
drove a rate to 0.0.

The models here share information the venue-split ratios cannot:

  * every match a team played informs its attack and defence, not just its home
    or away subset (2 parameters per team instead of 4 disjoint averages);
  * home advantage is estimated ONCE from the whole league rather than being
    implicit in each team's separate home and away numbers;
  * older matches can be down-weighted continuously instead of being either in
    or out of a window.

WHY AUC IS THE TARGET, AND WHAT CANNOT MOVE IT
-----------------------------------------------
`poisson.py` maps rates to a probability with

    P(BTTS) = (1 - exp(-lambda_home)) * (1 - exp(-lambda_away))

which is strictly increasing in both arguments. AUC depends only on the ORDER of
predictions. So any transformation that is monotone in (lambda_home, lambda_away)
- including a different scale factor, a different intercept, or shrinkage toward
a prior - cannot change AUC at all. Only a change to the ESTIMATES themselves,
or a correction that reorders fixtures, can.

That is a mathematical statement about what the candidates can possibly achieve,
made before measuring them, and it is why C1/C2 (better rate estimates) and
C3/C4 (dependence corrections) are expected to behave differently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from domain.historical import HistoricalMatch

__all__ = [
    "btts_independent",
    "MAX_GOALS",
    "TeamStrength",
    "FitDiagnostics",
    "fit_team_strength",
    "predict_lambdas",
    "dixon_coles_tau",
    "btts_dixon_coles",
    "btts_bivariate",
    "poisson_pmf",
    "weighted_log_likelihood",
    "decay_weight",
]


#: Score-matrix truncation limit, set from MEASURED tail mass rather than by
#: eyeballing. Measured P(X >= n) for the Poisson tail:
#:
#:     lambda   n=11       n=16       n=21
#:     1.4      2.8e-07    2.8e-12    ~0
#:     2.5      6.2e-05    1.1e-08    4.1e-13
#:     4.0      2.8e-03    4.9e-06    1.9e-09
#:
#: An earlier draft used 10 on the assumption that the tail was below 1e-4 for
#: any plausible rate. That was wrong: at lambda = 4.0 the discarded mass is
#: 2.8e-03, which is the same order as the Brier differences this Epic is trying
#: to resolve, and it showed up as a Dixon-Coles/independent disagreement at
#: high rates. 15 keeps the worst case near 5e-06 while costing only a 16x16
#: matrix.
MAX_GOALS = 15



def poisson_pmf(k: int, rate: float) -> float:
    """P(X = k) for X ~ Poisson(rate). Guards rate = 0 exactly."""
    if rate <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-rate + k * math.log(rate) - math.lgamma(k + 1))


def btts_independent(lambda_home: float, lambda_away: float) -> float:
    """
    P(both teams score) under independent Poisson.

    This is the SAME mapping `poisson.py` uses, written here because Epic 2D must
    not import-and-adapt production code paths, and must not modify them either.
    `tests/unit/test_goal_models.py` asserts this agrees with
    `poisson.calculate_gg_probability` to machine precision on a grid of inputs,
    so the duplication cannot silently drift into a second, different model.

    Strictly increasing in both arguments - see the module docstring on AUC.
    """
    if lambda_home < 0.0 or lambda_away < 0.0:
        raise ValueError(f"rates must be >= 0; got {lambda_home}, {lambda_away}")
    return (1.0 - math.exp(-lambda_home)) * (1.0 - math.exp(-lambda_away))


def decay_weight(match_kickoff: datetime, as_of: datetime, xi: float) -> float:
    """
    Exponential time-decay weight w(t) = exp(-xi * t), t in DAYS before `as_of`.

    Units matter and are stated because xi is meaningless without them: xi is per
    day, so a half-life in days is ln(2)/xi. xi = 0 disables decay and every
    match weighs 1, which is the un-decayed C1 model - so C1 is a special case of
    C2 rather than separate code, and a difference between them cannot be a
    difference in implementation.
    """
    if xi < 0.0:
        raise ValueError(f"xi must be >= 0; got {xi}")
    if xi == 0.0:
        return 1.0
    days = (as_of - match_kickoff).total_seconds() / 86400.0
    if days < 0.0:
        raise ValueError(
            "decay_weight received a match at or after the cutoff; "
            "point-in-time filtering must happen before fitting"
        )
    return math.exp(-xi * days)


@dataclass(frozen=True)
class FitDiagnostics:
    """Evidence about whether the fit is trustworthy, kept with the fit."""

    iterations: int
    converged: bool
    max_update: float
    weighted_matches: float
    raw_matches: int
    teams: int
    dropped_teams: Tuple[str, ...] = ()
    #: Teams whose fitted attack is exactly 0.0 because they scored no goals in
    #: the whole fitting window. This is GG-028's mechanism reappearing inside
    #: the MLE: attack = 0 forces lambda = 0 and therefore P(BTTS) = 0 exactly.
    #: The maximum-likelihood estimate really is 0 here - the estimator is not
    #: broken, the EVIDENCE is degenerate - so it is surfaced as a diagnostic
    #: rather than silently floored, because a hidden floor would be an
    #: undocumented prior and Epic 2D forbids arbitrary constants.
    zero_attack_teams: Tuple[str, ...] = ()



@dataclass(frozen=True)
class TeamStrength:
    """
    A fitted Maher-style model.

    Parameterisation (log-linear Poisson, the standard form):

        E[home goals] = mu * gamma * attack[home] * defence[away]
        E[away goals] = mu * attack[away] * defence[home]

    where `mu` is the league scoring level, `gamma > 1` is home advantage, and
    each team has one attack and one defence multiplier. Identifiability requires
    a normalisation because multiplying every attack by c and dividing every
    defence by c leaves the likelihood unchanged; here the constraint is
    mean(attack) = 1, enforced after every iteration. Without it the parameters
    drift without bound while the fit appears to converge.

    Teams absent from the fitting window have NO parameters and are reported in
    `diagnostics.dropped_teams`. They are not given attack = 1.0: a default would
    silently assert "exactly average", which is the GG-001 mistake in a different
    costume. Callers must decide explicitly (see `predict_lambdas`, which
    returns None).
    """

    attack: Dict[str, float]
    defence: Dict[str, float]
    home_advantage: float
    league_mean: float
    xi: float
    as_of: datetime
    diagnostics: FitDiagnostics
    competitions: Tuple[str, ...] = field(default_factory=tuple)

    def known(self, team_id: str) -> bool:
        return team_id in self.attack and team_id in self.defence


def fit_team_strength(
    matches: Sequence[HistoricalMatch],
    *,
    as_of: datetime,
    xi: float = 0.0,
    max_iterations: int = 200,
    tolerance: float = 1e-9,
    min_matches_per_team: int = 1,
) -> TeamStrength:
    """
    Weighted maximum-likelihood fit by alternating closed-form updates.

    THE ESTIMATOR. For independent Poisson goal counts with a log-linear mean,
    the conditional maximiser of each parameter given the others is available in
    closed form, so no numerical optimiser is needed (there is no scipy in this
    repository, and a hand-rolled gradient descent would add tuning parameters
    with no statistical justification):

        attack[i]  = (weighted goals scored by i) / (weighted sum over i's
                      matches of mu * defence[opponent] * gamma-if-home)
        defence[j] = (weighted goals conceded by j) / (weighted sum over j's
                      matches of mu * attack[opponent] * gamma-if-j-at-home)
        gamma      = (weighted home goals) / (weighted sum of mu*attack*defence
                      over home sides)

    Each update strictly increases the weighted likelihood, so the sequence
    converges; this is the standard iterative-scaling argument for Poisson
    log-linear models. `mu` is fixed at the weighted mean goals per team-innings
    and the multipliers are normalised to mean(attack) = 1, which pins the
    otherwise unidentified scale.

    POINT-IN-TIME SAFETY IS THE CALLER'S JOB, AND IS CHECKED HERE ANYWAY: every
    match must have `kickoff < as_of` or this raises. Fitting is the step where
    leakage would be invisible and catastrophic - a model fitted on the target's
    own result would look excellent and mean nothing - so the guard is an
    exception rather than a filter. Silently dropping offending matches would let
    a caller pass a whole future season and receive a plausible fit.

    Only completed matches with both scores contribute. A missing score is not
    treated as 0.
    """
    if xi < 0.0:
        raise ValueError(f"xi must be >= 0; got {xi}")

    usable: List[Tuple[HistoricalMatch, float]] = []
    for match in matches:
        if match.kickoff >= as_of:
            raise ValueError(
                f"fit_team_strength received match {match.event_id!r} at "
                f"{match.kickoff.isoformat()} which is not strictly before "
                f"as_of {as_of.isoformat()}"
            )
        if not match.completed:
            continue
        if match.home_goals is None or match.away_goals is None:
            continue
        usable.append((match, decay_weight(match.kickoff, as_of, xi)))

    appearances: Dict[str, float] = {}
    for match, _weight in usable:
        appearances[match.home_team_id] = appearances.get(match.home_team_id, 0.0) + 1
        appearances[match.away_team_id] = appearances.get(match.away_team_id, 0.0) + 1

    eligible = {
        team for team, count in appearances.items() if count >= min_matches_per_team
    }
    dropped = tuple(sorted(set(appearances) - eligible))

    fitted = [
        (match, weight)
        for match, weight in usable
        if match.home_team_id in eligible and match.away_team_id in eligible
    ]

    competitions = tuple(sorted({match.competition for match, _ in fitted}))
    total_weight = sum(weight for _, weight in fitted)

    if not fitted or total_weight <= 0.0:
        return TeamStrength(
            attack={},
            defence={},
            home_advantage=1.0,
            league_mean=0.0,
            xi=xi,
            as_of=as_of,
            diagnostics=FitDiagnostics(
                iterations=0,
                converged=False,
                max_update=0.0,
                weighted_matches=0.0,
                raw_matches=len(fitted),
                teams=0,
                dropped_teams=dropped,
            ),
            competitions=competitions,
        )

    # mu: weighted mean goals per team-innings (2 innings per match), which is
    # the same "goals per team per match" unit Epic 1B.2 pinned for the league
    # average. Using the per-FIXTURE figure here would double every rate.
    weighted_goals = sum(
        weight * ((match.home_goals or 0) + (match.away_goals or 0))
        for match, weight in fitted
    )
    league_mean = weighted_goals / (2.0 * total_weight)

    attack: Dict[str, float] = {team: 1.0 for team in eligible}
    defence: Dict[str, float] = {team: 1.0 for team in eligible}
    gamma = 1.0

    iterations = 0
    max_update = 0.0
    converged = False

    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        max_update = 0.0

        # --- attack ---
        scored: Dict[str, float] = {team: 0.0 for team in eligible}
        expected_attack: Dict[str, float] = {team: 0.0 for team in eligible}
        for match, weight in fitted:
            home, away = match.home_team_id, match.away_team_id
            scored[home] += weight * (match.home_goals or 0)
            scored[away] += weight * (match.away_goals or 0)
            expected_attack[home] += weight * league_mean * gamma * defence[away]
            expected_attack[away] += weight * league_mean * defence[home]
        for team in eligible:
            if expected_attack[team] > 0.0:
                updated = scored[team] / expected_attack[team]
                max_update = max(max_update, abs(updated - attack[team]))
                attack[team] = updated

        # Normalise to mean(attack) = 1 - the identifiability constraint.
        mean_attack = sum(attack.values()) / len(attack)
        if mean_attack > 0.0:
            for team in eligible:
                attack[team] /= mean_attack

        # --- defence ---
        conceded: Dict[str, float] = {team: 0.0 for team in eligible}
        expected_defence: Dict[str, float] = {team: 0.0 for team in eligible}
        for match, weight in fitted:
            home, away = match.home_team_id, match.away_team_id
            conceded[home] += weight * (match.away_goals or 0)
            conceded[away] += weight * (match.home_goals or 0)
            expected_defence[home] += weight * league_mean * attack[away]
            expected_defence[away] += weight * league_mean * gamma * attack[home]
        for team in eligible:
            if expected_defence[team] > 0.0:
                updated = conceded[team] / expected_defence[team]
                max_update = max(max_update, abs(updated - defence[team]))
                defence[team] = updated

        # --- home advantage ---
        home_goals_total = sum(
            weight * (match.home_goals or 0) for match, weight in fitted
        )
        expected_home = sum(
            weight * league_mean * attack[match.home_team_id] * defence[match.away_team_id]
            for match, weight in fitted
        )
        if expected_home > 0.0:
            updated_gamma = home_goals_total / expected_home
            max_update = max(max_update, abs(updated_gamma - gamma))
            gamma = updated_gamma

        if max_update < tolerance:
            converged = True
            break

    # A team that scored no goals at all in the window has an exact MLE of 0,
    # which forces lambda = 0 and P(BTTS) = 0 - GG-028's mechanism arising inside
    # a richer model. Recorded, not silently floored.
    zero_attack = tuple(sorted(team for team in eligible if attack[team] <= 0.0))

    return TeamStrength(
        attack=attack,
        defence=defence,
        home_advantage=gamma,
        league_mean=league_mean,
        xi=xi,
        as_of=as_of,
        diagnostics=FitDiagnostics(
            iterations=iterations,
            converged=converged,
            max_update=max_update,
            weighted_matches=total_weight,
            raw_matches=len(fitted),
            teams=len(eligible),
            dropped_teams=dropped,
            zero_attack_teams=zero_attack,
        ),
        competitions=competitions,
    )



def predict_lambdas(
    model: TeamStrength,
    home_team_id: str,
    away_team_id: str,
) -> Optional[Tuple[float, float]]:
    """
    (lambda_home, lambda_away) for a fixture, or None if either team is unknown.

    None rather than a default is the whole point: a promoted club with no
    matches in the fitting window has no estimated strength, and inventing
    attack = 1.0 would present "average" as a measurement. The harness already
    knows how to record a refusal, and Epic 2C established that refusals must
    stay distinguishable from predictions.
    """
    if not model.known(home_team_id) or not model.known(away_team_id):
        return None
    lambda_home = (
        model.league_mean
        * model.home_advantage
        * model.attack[home_team_id]
        * model.defence[away_team_id]
    )
    lambda_away = (
        model.league_mean * model.attack[away_team_id] * model.defence[home_team_id]
    )
    return (lambda_home, lambda_away)


# ---------------------------------------------------------------------------
# C3: Dixon-Coles dependence correction
# ---------------------------------------------------------------------------


def dixon_coles_tau(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> float:
    """
    The Dixon-Coles tau factor, which perturbs ONLY the four lowest scorelines.

        tau(0,0) = 1 - lambda_home * lambda_away * rho
        tau(0,1) = 1 + lambda_home * rho
        tau(1,0) = 1 + lambda_away * rho
        tau(1,1) = 1 - rho
        tau(x,y) = 1 otherwise

    Dixon and Coles (1997) introduced this because independent Poisson
    underestimates low-scoring draws. Note what that means for BTTS: of the four
    adjusted cells, 0-0, 0-1 and 1-0 are all NON-BTTS and only 1-1 is BTTS. So
    rho moves probability between the non-BTTS clump and a single BTTS cell, and
    the effect on P(BTTS) is nearly a function of (lambda_home, lambda_away)
    alone - which is why this correction is expected to change calibration far
    more than ranking. That is a prediction to be tested, not an excuse to skip
    the candidate.
    """
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lambda_home * lambda_away * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lambda_home * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + lambda_away * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def btts_dixon_coles(
    lambda_home: float,
    lambda_away: float,
    rho: float,
    *,
    max_goals: int = MAX_GOALS,
) -> float:
    """
    P(BTTS) under Dixon-Coles, by explicit summation over the score matrix.

    The corrected cell probabilities need not sum to 1, so the matrix is
    renormalised by its own total. Omitting that step is a common error which
    quietly turns "probability" into "unnormalised mass" and makes calibration
    unreachable. tau can go negative for extreme rho, so cells are floored at 0
    and the result is validated - an out-of-range rho is a real risk when rho is
    fitted rather than assumed.
    """
    total = 0.0
    both = 0.0
    for home_goals in range(max_goals + 1):
        p_home = poisson_pmf(home_goals, lambda_home)
        for away_goals in range(max_goals + 1):
            cell = (
                p_home
                * poisson_pmf(away_goals, lambda_away)
                * dixon_coles_tau(home_goals, away_goals, lambda_home, lambda_away, rho)
            )
            if cell < 0.0:
                cell = 0.0
            total += cell
            if home_goals >= 1 and away_goals >= 1:
                both += cell
    if total <= 0.0:
        raise ValueError(
            f"Dixon-Coles score matrix has no mass for rates "
            f"({lambda_home}, {lambda_away}) and rho={rho}"
        )
    probability = both / total
    return min(1.0, max(0.0, probability))


# ---------------------------------------------------------------------------
# C4: bivariate Poisson
# ---------------------------------------------------------------------------


def btts_bivariate(
    lambda_home: float,
    lambda_away: float,
    lambda_shared: float,
    *,
    max_goals: int = MAX_GOALS,
) -> float:
    """
    P(BTTS) under the bivariate Poisson of Karlis & Ntzoufras (2003).

    Construction: X = X1 + X3, Y = X2 + X3 with X1, X2, X3 independent Poisson.
    X3 is a shared component inducing Cov(X,Y) = lambda_shared >= 0. The marginal
    means are lambda_home + lambda_shared and lambda_away + lambda_shared, so the
    caller must pass the DECOMPOSED rates; adding lambda_shared on top of
    already-correct marginals would inflate both means and produce a different
    model that still looks plausible.

    Only NON-NEGATIVE covariance is representable - a structural limitation worth
    stating, since football scores are often argued to be negatively correlated,
    which is precisely the direction Dixon-Coles addresses and this cannot.
    """
    if lambda_shared < 0.0:
        raise ValueError(f"lambda_shared must be >= 0; got {lambda_shared}")
    if lambda_shared == 0.0:
        return btts_independent(lambda_home, lambda_away)

    # P(X=0) and P(Y=0) both require X3 = 0, hence the shared term appears in
    # each marginal-zero probability.
    p_home_zero = math.exp(-(lambda_home + lambda_shared))
    p_away_zero = math.exp(-(lambda_away + lambda_shared))
    # P(X=0 and Y=0) = P(X1=0)P(X2=0)P(X3=0), which is NOT the product of the two
    # marginals - that is exactly the dependence being modelled.
    p_both_zero = math.exp(-(lambda_home + lambda_away + lambda_shared))
    # Inclusion-exclusion: P(X>=1, Y>=1) = 1 - P(X=0) - P(Y=0) + P(X=0,Y=0)
    probability = 1.0 - p_home_zero - p_away_zero + p_both_zero
    return min(1.0, max(0.0, probability))


# ---------------------------------------------------------------------------
# Likelihood, for parameter selection
# ---------------------------------------------------------------------------


def weighted_log_likelihood(
    matches: Sequence[HistoricalMatch],
    model: TeamStrength,
    *,
    rho: Optional[float] = None,
) -> Optional[float]:
    """
    Poisson log-likelihood of observed GOAL COUNTS under a fitted model.

    Used to select xi and rho. The objective is deliberately the likelihood of
    the goals rather than a BTTS score: Epic 2C's GG-029 showed that optimising
    a BTTS proper score rewards flattening, and the parameters here describe the
    goal process, so the goal process is what they should be judged on. Selecting
    xi by BTTS Brier would repeat exactly the mistake this Epic exists to avoid.

    Matches whose teams are unknown to the model contribute nothing (they cannot
    be scored) and are not counted as zero-likelihood, which would confuse "no
    parameters" with "terrible fit". Returns None when nothing is scoreable.
    """
    total = 0.0
    counted = 0
    for match in matches:
        if not match.completed or match.home_goals is None or match.away_goals is None:
            continue
        rates = predict_lambdas(model, match.home_team_id, match.away_team_id)
        if rates is None:
            continue
        lambda_home, lambda_away = rates
        if lambda_home <= 0.0 or lambda_away <= 0.0:
            continue
        contribution = math.log(
            poisson_pmf(match.home_goals, lambda_home)
        ) + math.log(poisson_pmf(match.away_goals, lambda_away))
        if rho is not None:
            tau = dixon_coles_tau(
                match.home_goals, match.away_goals, lambda_home, lambda_away, rho
            )
            if tau <= 0.0:
                # An invalid rho makes this observation impossible; reject the
                # candidate rho rather than silently clamping it.
                return None
            contribution += math.log(tau)
        total += contribution
        counted += 1
    if counted == 0:
        return None
    return total
