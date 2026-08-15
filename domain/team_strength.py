"""
Gamma-Poisson team-strength estimation (Epic 2C).

WHY THIS MODULE EXISTS
----------------------
Epic 2B.3 measured POISSON_V1 over 7,234 real fixtures and found the deficit is
not in the probability formula - it is in the five numbers fed to it. With 10+
prior venue matches the model scores Brier 0.2555; with 1-2 it scores 0.4241.
The mechanism is recorded as GG-028: a team whose only prior away match was
goalless has an observed away scoring rate of exactly 0.0, so `lambda_away` is
0.0 and `P(BTTS)` is exactly 0.0 - an absolute claim about the future drawn from
a single observation.

This module fixes the ESTIMATE, not the model. `poisson.py` is not imported here
and not touched anywhere in this Epic. Nothing below knows what a probability is.

    observed goals + matches  ->  posterior mean rate  ->  (still) POISSON_V1

THE STATISTICS, STATED IN FULL
------------------------------
Epic 2A recommended family D (pseudo-observation priors) on the grounds that
Gamma-Poisson conjugacy is the *correct* model for goal counts rather than an
analogy. The derivation is written out because the reparameterisation below is
the whole reason a single parameter `k` is interpretable, and a reader who
cannot check it has to take the formula on trust.

LIKELIHOOD. Goals scored by one team in one match at one venue:

    G_j ~ Poisson(lambda)          j = 1 .. n, independent

so the sufficient statistic is the total, and

    Y = sum_j G_j ~ Poisson(n * lambda)

PRIOR. The conjugate prior for a Poisson rate is Gamma with shape `alpha` and
RATE `beta` (not scale - the distinction silently inverts the answer):

    p(lambda) proportional to lambda^(alpha - 1) * exp(-beta * lambda)
    E[lambda]   = alpha / beta
    Var[lambda] = alpha / beta^2

POSTERIOR. Multiply and collect exponents:

    p(lambda | Y) proportional to lambda^Y exp(-n lambda)
                                  * lambda^(alpha - 1) exp(-beta lambda)
                                = lambda^(alpha + Y - 1) exp(-(beta + n) lambda)

which is again Gamma - that is what conjugacy means:

    lambda | Y ~ Gamma(alpha + Y, beta + n)

POSTERIOR MEAN:

    E[lambda | Y] = (alpha + Y) / (beta + n)

REPARAMETERISATION - where `k` comes from. Set

    alpha = k * mu          beta = k

Then E[lambda] = k*mu/k = mu exactly, and the posterior mean becomes

    lambda_hat = (k * mu + Y) / (k + n)

INTERPRETATION OF PRIOR STRENGTH. `k` is a number of MATCHES. The prior enters
the arithmetic as `k` extra matches in which `k * mu` goals were scored, so
"k = 4" reads as "before kicking a ball, this team is credited with four
matches' worth of evidence at the prior rate". It is not a tuning knob with an
opaque scale, which is exactly why this parameterisation was chosen over
(alpha, beta) directly.

UNITS. Dimensional consistency is not decoration here - `league_avg_goals` being
per-team rather than per-fixture is the difference between correct lambdas and
lambdas that are wrong by a factor of two (see `domain/poisson_inputs.py`).

    mu      goals / match          Y   goals
    k       matches                n   matches
    alpha = k * mu  ->  goals      beta = k  ->  matches

    lambda_hat = (goals + goals) / (matches + matches) = goals / match

The output therefore has the same units as the raw venue average it replaces,
which is what allows it to be dropped into POISSON_V1 unchanged.

BEHAVIOUR AS n -> 0:

    lambda_hat = (k*mu + 0) / (k + 0) = mu

The prior mean exactly. No special case, no branch, no fabricated observation.

BEHAVIOUR AS n -> infinity:

    lambda_hat = (k*mu + Y) / (k + n) = (k*mu/n + Y/n) / (k/n + 1) -> Y/n

The observed rate. The prior does not persist: its weight is k/(n+k), which is
monotonically decreasing in n. This is precisely the property Epic 2A found the
fixed-weight blend (family B) lacks - "a constant is the wrong functional form".

Equivalently, as a shrinkage identity:

    lambda_hat = mu + (n / (n + k)) * (Y/n - mu)

so the reliability weight on observation is r = n/(n+k). At n=k the estimate
sits exactly halfway between prior and observation.

WHY THIS KILLS GG-028. For k > 0 and mu > 0 the posterior mean is strictly
positive whatever Y is, because the numerator contains k*mu > 0. A genuine
Y = 0 over n = 1 no longer asserts a zero rate; it produces mu/(1 + 1/k)-ish
evidence-weighted doubt. Zero goals remains real evidence and pulls the estimate
DOWN - it is not discarded - but it cannot alone certify impossibility.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No probability mathematics. No clipping, flooring or clamping of any output: the
extreme probabilities disappear because the estimate improves, not because a
`max(0.05, ...)` was added, and those two are distinguishable only if the second
is never written. No arbitrary constants - every parameter is supplied by the
caller from `EstimatorConfig`, and `method_of_moments_prior_strength` exists so
`k` can be ESTIMATED from data rather than chosen by taste.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Sequence

__all__ = [
    "ESTIMATOR_VERSION",
    "PriorSource",
    "GammaPosterior",
    "ShrunkRate",
    "EstimatorConfig",
    "DEFAULT_CONFIG",
    "posterior",
    "posterior_mean",
    "shrink_rate",
    "reliability_weight",
    "method_of_moments_prior_strength",
]

# Bumped when the estimator's MATHEMATICS or parameter semantics change, so two
# sets of results computed under different rules can never be silently merged.
# Separate from the model_version an adapter reports, because the same estimator
# can be wired into more than one model.
ESTIMATOR_VERSION = "2c.1"


class PriorSource(str, Enum):
    """
    Where the prior mean came from. Part of the answer, never inferred later.

    A rate of 1.30 looks identical whether it was a team's own previous-season
    form or the league average standing in for a club with no history. Epic 1B.2
    lost months to a fabricated 1.35 that was indistinguishable from a measured
    figure; recording the source is how that is not repeated.
    """

    #: The team's own previous-season venue rate, itself shrunk toward the
    #: previous-season league venue baseline (two-level prior, Part 3).
    PREV_SEASON_TEAM = "PREV_SEASON_TEAM"

    #: No previous season for this team IN THIS COMPETITION. The prior is the
    #: destination-league venue baseline, optionally adjusted. Named for the
    #: observable fact ("new to this league") rather than for "promoted", which
    #: would claim knowledge of the division they arrived from.
    NEW_TO_LEAGUE = "NEW_TO_LEAGUE"

    #: League venue baseline, used because the team's previous season exists but
    #: is not usable evidence (no venue matches in it).
    LEAGUE_BASELINE = "LEAGUE_BASELINE"

    #: No prior could be constructed at all. The estimate is UNAVAILABLE - never
    #: substituted with a plausible number.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class GammaPosterior:
    """
    The full posterior Gamma(shape, rate), not merely its mean.

    The variance is carried because "estimate" and "confident estimate" are
    different claims, and a consumer holding only the mean cannot tell a rate
    backed by 19 matches from one backed by the prior alone. Epic 2A noted that
    a Bayesian treatment yields uncertainty for free and that a decision layer
    which already declines on missing data can express "too uncertain to bet"
    without new concepts. Nothing in Epic 2C consumes it; it is recorded so a
    later Epic does not have to re-derive it.
    """

    shape: float
    rate: float

    def __post_init__(self) -> None:
        if self.shape <= 0.0:
            raise ValueError(f"Gamma shape must be positive; got {self.shape!r}")
        if self.rate <= 0.0:
            raise ValueError(f"Gamma rate must be positive; got {self.rate!r}")

    @property
    def mean(self) -> float:
        """alpha / beta."""
        return self.shape / self.rate

    @property
    def variance(self) -> float:
        """alpha / beta^2. Falls as evidence accumulates, which is the point."""
        return self.shape / (self.rate**2)


@dataclass(frozen=True)
class ShrunkRate:
    """
    One estimated rate, with every input that produced it.

    Deliberately verbose. The alternative - returning a bare float - makes
    `1.30` from a 19-match sample and `1.30` from an empty one identical
    downstream, which is the class of defect this repository has spent four
    Epics removing (GG-001, GG-003, GG-024).

    `value is None` means UNAVAILABLE and is distinct from 0.0. That separation
    is load-bearing and predates this Epic: 0.0 asserts "scores nothing", None
    says "we cannot judge".
    """

    value: Optional[float]
    observed_goals: int
    observed_matches: int
    prior_mean: Optional[float]
    prior_strength: float
    prior_source: PriorSource
    posterior_dist: Optional[GammaPosterior] = None

    @property
    def is_available(self) -> bool:
        return self.value is not None

    @property
    def observed_rate(self) -> Optional[float]:
        """
        The RAW rate POISSON_V1 would have used. Kept for comparison.

        This is the quantity that becomes 0.0 in GG-028, so keeping it beside
        the shrunk value is what lets a diagnostic show the difference rather
        than assert it.
        """
        if self.observed_matches <= 0:
            return None
        return self.observed_goals / self.observed_matches

    @property
    def reliability(self) -> float:
        """
        Weight placed on observation, r = n / (n + k). 0.0 when n = 0.

        Reported rather than recomputed by callers so the number in a diagnostic
        is the number the estimator actually used.
        """
        return reliability_weight(self.observed_matches, self.prior_strength)

    @property
    def provenance(self) -> str:
        """
        A short, stable label describing how this value was produced.

        Answers Part 5's requirement directly: current-season observation,
        previous-season team prior, league prior, or a shrinkage combination.
        """
        if self.value is None:
            return "UNAVAILABLE"
        if self.observed_matches == 0:
            return f"PRIOR_ONLY:{self.prior_source.value}"
        # No prior mean means nothing was shrunk toward anything, whatever
        # prior_strength was configured. Reporting SHRUNK here would describe a
        # combination that did not happen, and Part 5 forbids exactly that kind
        # of misattributed value.
        if self.prior_strength == 0.0 or self.prior_mean is None:
            return "OBSERVED_ONLY"
        return f"SHRUNK:{self.prior_source.value}"



@dataclass(frozen=True)
class EstimatorConfig:
    """
    Every tunable number in one place, so none can hide in a function body.

    NO VALUE HERE IS JUSTIFIED BY LOOKING REASONABLE. `DEFAULT_CONFIG` below is
    a neutral, deliberately inert starting point; the shipped values are chosen
    by `research/epic2c_experiment.py` over a documented grid on development
    seasons only, and are recorded in docs/EPIC_2C_COLD_START_MODEL.md with the
    scores that selected them.

    SEPARATE PARAMETERS FOR SEPARATE PROBLEMS. Epic 2A: "Sharing one shrinkage
    constant between them would be a category error." A team venue rate
    aggregates ~19 matches of a fairly stable quantity; a league baseline
    aggregates ~380 fixtures of a quantity that genuinely drifts between
    seasons. Their units differ too - team k is in MATCHES, league k is in
    TEAM-GAMES - so one shared constant would not even be dimensionally
    comparable.

    `k_goals_against >= k_goals_for` is EXPECTED but not enforced. Epic 2A
    measured split-half reliability r(GF) 0.37-0.69 against r(GA) 0.21-0.50, so
    defence is the noisier quantity and warrants stronger shrinkage. Whether the
    extra parameter earns its keep is a validation question, and hard-coding the
    inequality would answer it by assertion instead of by measurement.
    """

    #: Prior strength for a team's venue GOALS FOR, in matches.
    k_goals_for: float = 0.0
    #: Prior strength for a team's venue GOALS AGAINST, in matches.
    k_goals_against: float = 0.0
    #: Prior strength when shrinking the PREVIOUS season's team venue rate
    #: toward that season's league venue baseline (the upper level of the
    #: two-level prior). In matches.
    k_prev_season: float = 0.0
    #: Prior strength for the league baseline, in TEAM-GAMES - a season is
    #: ~760 team-games, so this parameter lives on a different scale entirely.
    k_league: float = 0.0
    #: Multiplies the league prior mean for a team new to this competition when
    #: estimating GOALS FOR. Epic 2A measured promoted clubs scoring ~0.72 of
    #: the destination-league mean, but explicitly forbade hardcoding it from
    #: nine cohorts of three clubs. 1.0 means "no adjustment".
    new_team_attack_factor: float = 1.0
    #: The same for GOALS AGAINST. Expected >= 1.0 (newly promoted sides tend to
    #: concede more), searched rather than assumed.
    new_team_defence_factor: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "k_goals_for",
            "k_goals_against",
            "k_prev_season",
            "k_league",
        ):
            value = getattr(self, name)
            if value < 0.0:
                raise ValueError(f"{name} must be >= 0; got {value!r}")
        for name in ("new_team_attack_factor", "new_team_defence_factor"):
            value = getattr(self, name)
            if value <= 0.0:
                raise ValueError(f"{name} must be > 0; got {value!r}")

    def with_values(self, **changes: float) -> "EstimatorConfig":
        """A copy with some parameters replaced. Frozen type, explicit copy."""
        return replace(self, **changes)


#: Neutral baseline: k = 0 everywhere, so every posterior mean collapses to the
#: observed rate and the estimator reproduces raw POISSON_V1 behaviour exactly.
#: Used as the null arm of the parameter search - if shrinkage helps, it must
#: beat this, and having it be a *configuration* rather than a separate code
#: path means the comparison exercises the same code.
DEFAULT_CONFIG = EstimatorConfig()


def reliability_weight(observed_matches: int, prior_strength: float) -> float:
    """
    r = n / (n + k). The weight the posterior mean places on observation.

    Returns 0.0 when n = 0 and k = 0 - the degenerate case where there is
    neither evidence nor prior - rather than raising, because the caller has
    already been told the rate is UNAVAILABLE by `value is None`.
    """
    if observed_matches < 0:
        raise ValueError(f"observed_matches must be >= 0; got {observed_matches!r}")
    denominator = observed_matches + prior_strength
    if denominator <= 0.0:
        return 0.0
    return observed_matches / denominator


def posterior(
    observed_goals: int,
    observed_matches: int,
    prior_mean: float,
    prior_strength: float,
) -> GammaPosterior:
    """
    The posterior Gamma(k*mu + Y, k + n).

    Both arguments are validated rather than trusted: a negative goal count or a
    negative prior mean is not a slightly-wrong number, it is evidence that a
    caller has swapped arguments, and the resulting rate would look plausible.
    """
    if observed_goals < 0:
        raise ValueError(f"observed_goals must be >= 0; got {observed_goals!r}")
    if observed_matches < 0:
        raise ValueError(f"observed_matches must be >= 0; got {observed_matches!r}")
    if prior_mean <= 0.0:
        raise ValueError(
            f"prior_mean must be > 0 for a Gamma prior; got {prior_mean!r}. "
            "A zero prior mean would reintroduce GG-028 through the prior."
        )
    if prior_strength <= 0.0:
        raise ValueError(
            f"prior_strength must be > 0 to form a proper Gamma prior; got "
            f"{prior_strength!r}. Use posterior_mean() for the k=0 case."
        )
    return GammaPosterior(
        shape=prior_strength * prior_mean + observed_goals,
        rate=prior_strength + observed_matches,
    )


def posterior_mean(
    observed_goals: int,
    observed_matches: int,
    prior_mean: Optional[float],
    prior_strength: float,
) -> Optional[float]:
    """
    (k * mu + Y) / (k + n), or None when nothing supports an estimate.

    THE THREE DEGENERATE CASES, each returning a different thing on purpose:

        k = 0, n > 0   ->  Y / n, the raw observed rate. k=0 means "no prior",
                           and the estimator must then be exactly the status
                           quo, so that the null arm of the parameter search is
                           genuinely the baseline.
        k = 0, n = 0   ->  None. No evidence and no prior is UNAVAILABLE.
        mu is None     ->  Y / n if n > 0, else None. A missing prior does not
                           become zero; it simply cannot contribute.
    """
    if observed_goals < 0:
        raise ValueError(f"observed_goals must be >= 0; got {observed_goals!r}")
    if observed_matches < 0:
        raise ValueError(f"observed_matches must be >= 0; got {observed_matches!r}")
    if prior_strength < 0.0:
        raise ValueError(f"prior_strength must be >= 0; got {prior_strength!r}")
    if prior_mean is not None and prior_mean < 0.0:
        raise ValueError(f"prior_mean must be >= 0 when present; got {prior_mean!r}")

    usable_prior = prior_mean is not None and prior_strength > 0.0
    if not usable_prior:
        if observed_matches <= 0:
            return None
        return observed_goals / observed_matches

    # mypy: `usable_prior` guarantees prior_mean is not None.
    assert prior_mean is not None
    return (prior_strength * prior_mean + observed_goals) / (prior_strength + observed_matches)


def shrink_rate(
    observed_goals: int,
    observed_matches: int,
    *,
    prior_mean: Optional[float],
    prior_strength: float,
    prior_source: PriorSource,
) -> ShrunkRate:
    """
    The posterior mean plus the provenance of every ingredient.

    This is the function the rest of the Epic calls. `prior_source` is REQUIRED
    and keyword-only: there is no signature that produces a shrunk rate without
    recording where its prior came from, because a value whose provenance is
    optional will eventually be produced without any.
    """
    value = posterior_mean(observed_goals, observed_matches, prior_mean, prior_strength)

    distribution: Optional[GammaPosterior] = None
    if prior_mean is not None and prior_mean > 0.0 and prior_strength > 0.0:
        distribution = posterior(observed_goals, observed_matches, prior_mean, prior_strength)

    resolved_source = prior_source
    if prior_mean is None or prior_strength <= 0.0:
        # The prior did not participate in the arithmetic, so it must not be
        # named as the source. Claiming a prior that contributed nothing would
        # corrupt the one field a reader uses to audit the estimate.
        resolved_source = PriorSource.UNAVAILABLE


    return ShrunkRate(
        value=value,
        observed_goals=observed_goals,
        observed_matches=observed_matches,
        prior_mean=prior_mean,
        prior_strength=prior_strength,
        prior_source=resolved_source,
        posterior_dist=distribution,
    )


def method_of_moments_prior_strength(
    observed_rates: Sequence[float],
    sample_sizes: Sequence[int],
) -> Optional[float]:
    """
    Estimate `k` from data instead of choosing it, by matching moments.

    WHY THIS EXISTS. Epic 2A: "k is not a taste parameter - it is estimable from
    the ratio of between-team variance to within-team variance." This function
    is that estimate, and it exists so the grid search has an independent anchor
    to be checked against. A search optimum that lands near the moment estimate
    is evidence the parameter is real; one that lands far away is a warning that
    the search is fitting something else.

    THE DERIVATION. Under the Gamma(k*mu, k) prior,

        Var[lambda] = alpha / beta^2 = k*mu / k^2 = mu / k      =>   k = mu / Var[lambda]

    The observed venue rates are not lambda itself - each is lambda plus Poisson
    sampling noise. For a team with n matches,

        Var[Y/n] = n*lambda / n^2 = lambda / n

    so by the law of total variance, over teams,

        Var[observed] = Var[lambda] + E[lambda / n] ~= Var[lambda] + mu * mean(1/n)

    Rearranged, the between-team (signal) variance is the observed spread MINUS
    the within-team (noise) component:

        Var[lambda] ~= Var[observed] - mu * mean(1/n)

    Returns None when that difference is <= 0, which is a real and informative
    outcome: it means the observed spread is entirely explicable as Poisson
    noise, i.e. the data contain no detectable between-team signal at these
    sample sizes. The honest response is "this method cannot estimate k here",
    not a large number that would look like a confident answer.
    """
    if len(observed_rates) != len(sample_sizes):
        raise ValueError("observed_rates and sample_sizes must have equal length")
    pairs = [
        (rate, size)
        for rate, size in zip(observed_rates, sample_sizes, strict=True)
        if size > 0
    ]
    if len(pairs) < 2:
        return None

    rates = [rate for rate, _ in pairs]
    mean_rate = sum(rates) / len(rates)
    if mean_rate <= 0.0:
        return None

    # Sample variance with Bessel's correction: these are teams drawn from a
    # league, not the population of all possible teams.
    observed_variance = sum((rate - mean_rate) ** 2 for rate in rates) / (len(rates) - 1)
    mean_inverse_n = sum(1.0 / size for _, size in pairs) / len(pairs)
    within_component = mean_rate * mean_inverse_n

    between_variance = observed_variance - within_component
    if between_variance <= 0.0:
        return None

    return mean_rate / between_variance
