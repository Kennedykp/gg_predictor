"""
The Gamma-Poisson estimator's mathematics and behavioural guarantees (Part 14).

Every test here is deterministic and closed-form. The estimator has no
randomness, so a test that needed a tolerance of 0.1 would be hiding something
rather than accommodating noise; tolerances appear only where floating-point
representation demands them.

Numbering follows the Part 14 checklist where it applies, so a reviewer can map
requirement to test without guessing.
"""

from __future__ import annotations

import math

import pytest

from domain.team_strength import (
    DEFAULT_CONFIG,
    EstimatorConfig,
    GammaPosterior,
    PriorSource,
    method_of_moments_prior_strength,
    posterior,
    posterior_mean,
    reliability_weight,
    shrink_rate,
)

PRIOR = 1.30


# ---------------------------------------------------------------------------
# Posterior algebra
# ---------------------------------------------------------------------------


def test_posterior_is_gamma_with_conjugate_updates():
    """
    Gamma(k*mu, k) + Poisson(n matches, y goals) -> Gamma(k*mu + y, k + n).

    Asserted on shape and rate separately rather than only through the mean,
    because two different (shape, rate) pairs can share a mean while carrying
    completely different uncertainty - and the variance is what a later Epic will
    consume.
    """
    dist = posterior(observed_goals=7, observed_matches=5, prior_mean=PRIOR, prior_strength=4.0)

    assert dist.shape == pytest.approx(4.0 * PRIOR + 7)
    assert dist.rate == pytest.approx(4.0 + 5)
    assert dist.mean == pytest.approx((4.0 * PRIOR + 7) / 9.0)


def test_posterior_variance_falls_as_evidence_accumulates():
    """More matches at the same rate must reduce posterior variance."""
    small = posterior(observed_goals=2, observed_matches=2, prior_mean=PRIOR, prior_strength=4.0)
    large = posterior(observed_goals=20, observed_matches=20, prior_mean=PRIOR, prior_strength=4.0)

    assert large.variance < small.variance
    # And the mean stays in the same neighbourhood, so the reduction is about
    # confidence rather than a moved estimate.
    assert abs(large.mean - small.mean) < 0.2


def test_gamma_rejects_degenerate_parameters():
    """A zero or negative shape/rate is not a distribution and must not exist."""
    with pytest.raises(ValueError):
        GammaPosterior(shape=0.0, rate=1.0)
    with pytest.raises(ValueError):
        GammaPosterior(shape=1.0, rate=0.0)


# ---------------------------------------------------------------------------
# Part 14.1 - 14.3: limiting behaviour
# ---------------------------------------------------------------------------


def test_zero_observations_returns_the_prior_not_a_fabricated_rate():
    """
    Part 14.1. n = 0 gives exactly the prior mean, and says so in provenance.

    The failure this prevents is 0/0 being reported as 0.0 - a fabricated
    observed rate that looks like measured evidence.
    """
    result = shrink_rate(
        observed_goals=0,
        observed_matches=0,
        prior_mean=PRIOR,
        prior_strength=6.0,
        prior_source=PriorSource.LEAGUE_BASELINE,
    )

    assert result.value == pytest.approx(PRIOR)
    assert result.observed_rate is None  # not 0.0
    assert result.reliability == 0.0
    assert result.provenance == "PRIOR_ONLY:LEAGUE_BASELINE"


def test_one_observation_is_strongly_shrunk():
    """
    Part 14.2. A single 0-goal match moves the estimate only slightly.

    With k = 6 the observation carries weight 1/7, so the estimate must remain
    much nearer the prior than the observation. Asserted as a comparison of
    distances rather than a hard-coded value, so the guarantee survives a
    parameter change.
    """
    result = shrink_rate(
        observed_goals=0,
        observed_matches=1,
        prior_mean=PRIOR,
        prior_strength=6.0,
        prior_source=PriorSource.LEAGUE_BASELINE,
    )

    assert result.value is not None
    assert result.value == pytest.approx((6.0 * PRIOR + 0) / 7.0)
    distance_to_prior = abs(result.value - PRIOR)
    distance_to_observation = abs(result.value - 0.0)
    assert distance_to_prior < distance_to_observation
    assert result.reliability == pytest.approx(1 / 7)


def test_large_samples_approach_the_observed_rate():
    """
    Part 14.3. As n grows the estimate converges on observation.

    Checked as a monotone sequence, which is a stronger statement than a single
    large-n spot check: it rules out an estimator that happens to be close at
    n = 200 while behaving erratically in between.
    """
    observed = 2.0
    errors = []
    for n in (5, 20, 100, 1000):
        result = shrink_rate(
            observed_goals=int(observed * n),
            observed_matches=n,
            prior_mean=PRIOR,
            prior_strength=6.0,
            prior_source=PriorSource.LEAGUE_BASELINE,
        )
        assert result.value is not None
        errors.append(abs(result.value - observed))

    assert errors == sorted(errors, reverse=True)
    assert errors[-1] < 0.01


def test_genuine_zero_goals_remains_valid_evidence():
    """
    Part 14.4. A real run of scoreless matches must still lower the estimate.

    The estimator tempers sparse zeros; it must not ignore substantiated ones.
    Nineteen scoreless matches is a real defensive record, and an estimator that
    returned the league average for it would have replaced evidence with a prior.
    """
    sparse = shrink_rate(
        observed_goals=0,
        observed_matches=1,
        prior_mean=PRIOR,
        prior_strength=6.0,
        prior_source=PriorSource.LEAGUE_BASELINE,
    )
    substantiated = shrink_rate(
        observed_goals=0,
        observed_matches=19,
        prior_mean=PRIOR,
        prior_strength=6.0,
        prior_source=PriorSource.LEAGUE_BASELINE,
    )

    assert sparse.value is not None and substantiated.value is not None
    assert substantiated.value < sparse.value
    assert substantiated.value < 0.35
    # Still strictly positive: no finite sample proves impossibility.
    assert substantiated.value > 0.0


def test_missing_data_stays_distinct_from_zero():
    """
    Part 14.5. No prior and no evidence is UNAVAILABLE, never 0.0.

    This is the safeguard the Epic is forbidden to weaken. `value is None`
    propagates to an incomplete input set and the fixture becomes unevaluable,
    exactly as before.
    """
    result = shrink_rate(
        observed_goals=0,
        observed_matches=0,
        prior_mean=None,
        prior_strength=6.0,
        prior_source=PriorSource.UNAVAILABLE,
    )

    assert result.value is None
    assert result.is_available is False
    assert result.provenance == "UNAVAILABLE"


def test_observation_survives_when_no_prior_exists():
    """
    Evidence without a prior is used as-is, and labelled OBSERVED_ONLY.

    Refusing here would discard real information and would reduce coverage below
    the baseline's for no statistical reason.
    """
    result = shrink_rate(
        observed_goals=3,
        observed_matches=2,
        prior_mean=None,
        prior_strength=6.0,
        prior_source=PriorSource.UNAVAILABLE,
    )

    assert result.value == pytest.approx(1.5)
    assert result.provenance == "OBSERVED_ONLY"


# ---------------------------------------------------------------------------
# Configuration and determinism
# ---------------------------------------------------------------------------


def test_zero_prior_strength_reproduces_raw_behaviour_exactly():
    """
    k = 0 collapses the estimator to the observed rate, bit for bit.

    This is what makes `DEFAULT_CONFIG` a legitimate null arm: the comparison
    between "no shrinkage" and "shrinkage" runs through the same code path, so a
    difference cannot come from a second implementation.
    """
    result = shrink_rate(
        observed_goals=0,
        observed_matches=1,
        prior_mean=PRIOR,
        prior_strength=0.0,
        prior_source=PriorSource.LEAGUE_BASELINE,
    )

    assert result.value == 0.0  # the GG-028 value, faithfully reproduced
    assert result.provenance == "OBSERVED_ONLY"
    assert DEFAULT_CONFIG.k_goals_for == 0.0


def test_estimator_is_deterministic():
    """Part 14.14. Same inputs, same output, repeatedly."""
    calls = [
        shrink_rate(
            observed_goals=4,
            observed_matches=3,
            prior_mean=PRIOR,
            prior_strength=6.0,
            prior_source=PriorSource.PREV_SEASON_TEAM,
        )
        for _ in range(5)
    ]
    assert len({call.value for call in calls}) == 1
    assert all(call == calls[0] for call in calls)


def test_reliability_weight_is_the_documented_ratio():
    """r = n / (n + k), with the degenerate case defined rather than raising."""
    assert reliability_weight(0, 6.0) == 0.0
    assert reliability_weight(6, 6.0) == pytest.approx(0.5)
    assert reliability_weight(0, 0.0) == 0.0
    assert reliability_weight(10, 0.0) == 1.0
    with pytest.raises(ValueError):
        reliability_weight(-1, 6.0)


def test_config_rejects_impossible_parameters():
    """Negative prior strengths and non-positive factors cannot be constructed."""
    with pytest.raises(ValueError):
        EstimatorConfig(k_goals_for=-1.0)
    with pytest.raises(ValueError):
        EstimatorConfig(new_team_attack_factor=0.0)


def test_config_with_values_returns_a_copy():
    """Frozen config: `with_values` must not mutate the original."""
    base = EstimatorConfig(k_goals_for=4.0)
    changed = base.with_values(k_goals_for=9.0)

    assert base.k_goals_for == 4.0
    assert changed.k_goals_for == 9.0


def test_negative_inputs_are_rejected():
    """Negative goals or matches are not sparse data; they are corrupt data."""
    with pytest.raises(ValueError):
        shrink_rate(
            observed_goals=-1,
            observed_matches=1,
            prior_mean=PRIOR,
            prior_strength=1.0,
            prior_source=PriorSource.LEAGUE_BASELINE,
        )
    with pytest.raises(ValueError):
        posterior(observed_goals=1, observed_matches=-1, prior_mean=PRIOR, prior_strength=1.0)


# ---------------------------------------------------------------------------
# Method of moments
# ---------------------------------------------------------------------------


def test_method_of_moments_returns_none_without_dispersion():
    """
    Identical rates carry no information about between-team variance.

    Returning None rather than an arbitrary number is the point: a prior strength
    invented from a degenerate sample would be exactly the undocumented constant
    this Epic forbids.
    """
    assert method_of_moments_prior_strength([1.3, 1.3, 1.3], [10, 10, 10]) is None
    assert method_of_moments_prior_strength([], []) is None


def test_method_of_moments_shrinks_harder_when_teams_are_alike():
    """
    Less between-team spread implies a larger k. Direction, not a magic value.

    The estimate is used as a REFERENCE POINT for the search bracket, never
    shipped directly, so its ordering property is what matters.

    Both cohorts here have spread exceeding Poisson noise at n = 19. A tighter
    cohort (1.25-1.35) returns None instead, which is not a defect - see
    `test_method_of_moments_returns_none_when_spread_is_pure_noise`.
    """
    alike = method_of_moments_prior_strength([1.0, 1.3, 1.6, 1.9], [19] * 4)
    spread = method_of_moments_prior_strength([0.6, 1.3, 2.1, 2.6], [19] * 4)

    assert alike is not None and spread is not None
    assert alike > spread
    assert math.isfinite(alike)


def test_method_of_moments_returns_none_when_spread_is_pure_noise():
    """
    A cohort tighter than Poisson noise yields None, not a huge k.

    Rates of 1.25-1.35 over 19 matches have observed variance ~0.002 while the
    within-team noise component alone is ~0.068. The between-team variance
    estimate is therefore negative: the spread is entirely explicable as
    sampling noise, and no between-team signal is detectable at this sample
    size. None is the honest answer; the alternative - clamping to a small
    positive variance - would manufacture an enormous, confident-looking k out of
    the absence of information.
    """
    assert method_of_moments_prior_strength([1.25, 1.30, 1.35, 1.28], [19] * 4) is None
    # The SAME rates measured over far more matches do resolve, because the noise
    # component shrinks with n while the real spread does not.
    resolved = method_of_moments_prior_strength([1.25, 1.30, 1.35, 1.28], [2000] * 4)
    assert resolved is not None and resolved > 0.0



def test_mismatched_input_lengths_are_rejected():
    """A rate without its sample size cannot be weighted, so this must not pass."""
    with pytest.raises(ValueError):
        method_of_moments_prior_strength([1.0, 2.0], [10])


def test_posterior_mean_matches_the_documented_formula():
    """(k*mu + y) / (k + n), and None when neither prior nor evidence exists."""
    assert posterior_mean(
        observed_goals=5, observed_matches=4, prior_mean=1.5, prior_strength=2.0
    ) == pytest.approx((2.0 * 1.5 + 5) / 6.0)
    assert posterior_mean(
        observed_goals=0, observed_matches=0, prior_mean=None, prior_strength=3.0
    ) is None
