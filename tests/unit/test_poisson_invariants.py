"""
Mathematical invariant tests for POISSON_V1.

Only properties that genuinely follow from the CURRENT implementation are asserted.
No new model requirements are introduced here.
"""

import pytest

from poisson import calculate_gg_probability

# A grid of valid inputs spanning weak/average/strong rates and low/normal/high
# scoring environments. Reused across invariants so each property is checked
# broadly rather than at a single point.
GRID = [
    (league_avg, hgs, hgc, ags, agc)
    for league_avg in (0.95, 1.35, 1.80)
    for hgs in (0.4, 1.5, 2.8)
    for hgc in (0.4, 1.2, 2.5)
    for ags in (0.4, 1.3, 2.6)
    for agc in (0.45, 1.4, 2.7)
]


@pytest.mark.invariant
@pytest.mark.parametrize("args", GRID)
def test_probability_within_unit_interval(args):
    result = calculate_gg_probability(*args)
    assert result is not None
    assert 0.0 <= result["gg_probability"] <= 1.0


@pytest.mark.invariant
@pytest.mark.parametrize("args", GRID)
def test_lambdas_non_negative_for_valid_inputs(args):
    result = calculate_gg_probability(*args)
    assert result is not None
    assert result["lambda_home"] >= 0.0
    assert result["lambda_away"] >= 0.0


@pytest.mark.invariant
@pytest.mark.parametrize("args", GRID)
def test_identical_inputs_produce_identical_outputs(args):
    assert calculate_gg_probability(*args) == calculate_gg_probability(*args)


@pytest.mark.invariant
@pytest.mark.parametrize("agc", [0.45, 1.4, 2.7])
def test_higher_home_scoring_rate_never_lowers_gg_probability(agc):
    """
    Monotonicity in the home team's scoring rate.

    lambda_home rises with home_goals_scored_home, and P(home scores at least
    once) = 1 - e^-lambda_home is increasing in lambda_home. Since lambda_away
    is held fixed here, P(GG) must be non-decreasing.
    """
    previous = -1.0
    for hgs in (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        result = calculate_gg_probability(1.35, hgs, 1.2, 1.3, agc)
        assert result is not None
        assert result["gg_probability"] >= previous
        previous = result["gg_probability"]


@pytest.mark.invariant
@pytest.mark.parametrize("hgc", [0.4, 1.2, 2.5])
def test_higher_away_scoring_rate_never_lowers_gg_probability(hgc):
    """Same monotonicity argument applied to the away side."""
    previous = -1.0
    for ags in (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        result = calculate_gg_probability(1.35, 1.5, hgc, ags, 1.4)
        assert result is not None
        assert result["gg_probability"] >= previous
        previous = result["gg_probability"]


@pytest.mark.invariant
def test_higher_league_average_lowers_both_lambdas():
    """
    The league average is the divisor for both lambdas, so raising it with all
    other inputs fixed must reduce both. This is why the hardcoded 1.35 fallback
    (GG-003) scales every prediction the system makes.
    """
    low = calculate_gg_probability(1.00, 1.5, 1.2, 1.3, 1.4)
    high = calculate_gg_probability(2.00, 1.5, 1.2, 1.3, 1.4)
    assert low is not None and high is not None
    assert high["lambda_home"] < low["lambda_home"]
    assert high["lambda_away"] < low["lambda_away"]
    assert high["gg_probability"] < low["gg_probability"]


@pytest.mark.invariant
def test_probability_is_product_of_two_independent_marginals():
    """
    POISSON_V1 treats the two sides as independent: P(GG) is exactly the product
    of the two marginal scoring probabilities. This is the modelling assumption
    a future Dixon-Coles challenger is intended to relax, so pinning it here
    makes any change to it visible.
    """
    import math

    result = calculate_gg_probability(1.35, 1.5, 1.2, 1.3, 1.4)
    assert result is not None
    p_home = 1 - math.exp(-result["lambda_home"])
    p_away = 1 - math.exp(-result["lambda_away"])
    assert result["gg_probability"] == pytest.approx(p_home * p_away, rel=1e-15)


@pytest.mark.invariant
def test_zero_lambda_on_either_side_forces_zero_probability():
    """
    A consequence of the product form: if either side cannot score, P(GG) = 0.
    Relevant to GG-001 - a missing statistic arriving as 0.0 collapses the
    entire probability to zero rather than signalling that data was absent.
    """
    home_zero = calculate_gg_probability(1.35, 0.0, 1.2, 1.3, 1.4)
    away_zero = calculate_gg_probability(1.35, 1.5, 0.0, 1.3, 1.4)
    assert home_zero is not None and away_zero is not None
    assert home_zero["gg_probability"] == 0.0
    assert away_zero["gg_probability"] == 0.0


@pytest.mark.invariant
def test_probability_below_one_for_realistic_inputs():
    """
    In exact arithmetic 1 - e^-lambda is asymptotic to 1 and never reaches it.
    That holds in floating point too across any realistic football scoring rate.
    """
    result = calculate_gg_probability(1.35, 3.5, 3.5, 3.5, 3.5)
    assert result is not None
    assert result["lambda_home"] > 9.0
    assert result["gg_probability"] < 1.0


@pytest.mark.characterization
def test_probability_saturates_to_exactly_one_at_extreme_lambda():
    """
    CHARACTERIZATION — float64 saturation, discovered while writing this suite.

    For lambda beyond roughly 37.43, e^-lambda falls below float64 resolution at
    1.0 and `1 - math.exp(-lambda)` evaluates to EXACTLY 1.0. P(GG) is then
    reported as a probability of 1.0 - absolute certainty - with no warning.

    Reaching that lambda requires absurd inputs, so this is not a live

    production concern. It matters because POISSON_V1 applies no upper bound to
    lambda (see test_no_upper_bound_on_lambda), and a corrupt or mis-parsed
    statistic combined with a small league average could produce it. Recorded
    here so the behaviour is known rather than discovered later.

    Not a bug being fixed in Epic 1A. No numerical safeguard was added.
    """
    import math

    # Measured boundary on this platform: still below 1.0 at 37.4, saturated at 38.
    assert 1 - math.exp(-37.4) < 1.0
    assert 1 - math.exp(-38.0) == 1.0

    result = calculate_gg_probability(0.5, 6.0, 6.0, 6.0, 6.0)

    assert result is not None
    assert result["lambda_home"] == 72.0
    assert result["gg_probability"] == 1.0

