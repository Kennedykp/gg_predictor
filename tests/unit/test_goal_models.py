"""
Tests for `domain/goal_models.py` (Epic 2D candidate structures).

Two jobs here. First, pin the mathematics: a wrong Dixon-Coles renormalisation or
a mis-decomposed bivariate rate produces numbers that look like probabilities and
would silently become this Epic's conclusion. Second, prove the point-in-time
guard: the fitting step is where leakage would be both invisible and fatal,
because a model fitted on the target's own result looks excellent and means
nothing.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from domain.goal_models import (
    btts_bivariate,
    btts_dixon_coles,
    btts_independent,
    decay_weight,
    dixon_coles_tau,
    fit_team_strength,
    poisson_pmf,
    predict_lambdas,
    weighted_log_likelihood,
)
from domain.historical import HistoricalMatch
from poisson import calculate_gg_probability

KICKOFF = datetime(2020, 8, 1, 15, 0, tzinfo=timezone.utc)


def match(
    home: str,
    away: str,
    home_goals: int | None,
    away_goals: int | None,
    *,
    days: int = 0,
    event_id: str | None = None,
    completed: bool = True,
    competition: str = "eng.1",
    season: int = 2020,
) -> HistoricalMatch:
    return HistoricalMatch(
        event_id=event_id or f"{home}-{away}-{days}",
        competition=competition,
        season=season,
        kickoff=KICKOFF + timedelta(days=days),
        home_team_id=home,
        away_team_id=away,
        completed=completed,
        home_goals=home_goals,
        away_goals=away_goals,
    )


class TestIndependentPoissonAgreesWithProduction:
    """
    The anti-drift guard.

    `btts_independent` reimplements the mapping `poisson.py` uses, because Epic 2D
    may not modify production and should not thread production's dict-returning
    signature through every candidate. That duplication is only safe if it is
    verified, otherwise Epic 2D could end up measuring a subtly different model
    than the one in production and attribute the difference to "structure".
    """

    @pytest.mark.parametrize(
        "home_scored,home_conceded,away_scored,away_conceded,league",
        [
            (1.5, 1.2, 1.1, 1.4, 1.35),
            (2.4, 0.8, 0.9, 2.0, 1.40),
            (0.7, 1.9, 1.8, 0.6, 1.30),
            (1.0, 1.0, 1.0, 1.0, 1.00),
        ],
    )
    def test_matches_poisson_v1_to_machine_precision(
        self, home_scored, home_conceded, away_scored, away_conceded, league
    ):
        production = calculate_gg_probability(
            league_avg_goals=league,
            home_goals_scored_home=home_scored,
            home_goals_conceded_home=home_conceded,
            away_goals_scored_away=away_scored,
            away_goals_conceded_away=away_conceded,
        )
        assert production is not None
        mine = btts_independent(
            production["lambda_home"], production["lambda_away"]
        )
        assert mine == pytest.approx(production["gg_probability"], abs=1e-15)

    def test_is_strictly_increasing_in_both_rates(self):
        """
        The monotonicity that makes AUC insensitive to rescaling - the core
        analytic claim behind this Epic's candidate selection.
        """
        base = btts_independent(1.2, 1.1)
        assert btts_independent(1.3, 1.1) > base
        assert btts_independent(1.2, 1.2) > base

    def test_zero_rate_gives_zero_probability(self):
        # GG-028's mechanism, preserved here as a property of the mapping rather
        # than a bug: the mapping is correct, the INPUT was unjustified.
        assert btts_independent(0.0, 1.5) == 0.0

    def test_rejects_negative_rates(self):
        with pytest.raises(ValueError):
            btts_independent(-0.1, 1.0)


class TestPoissonPmf:
    def test_sums_to_one(self):
        total = sum(poisson_pmf(k, 1.4) for k in range(60))
        assert total == pytest.approx(1.0, abs=1e-12)

    def test_zero_rate_is_a_point_mass(self):
        assert poisson_pmf(0, 0.0) == 1.0
        assert poisson_pmf(1, 0.0) == 0.0

    def test_known_value(self):
        assert poisson_pmf(2, 1.0) == pytest.approx(math.exp(-1.0) / 2.0)


class TestDecayWeight:
    def test_xi_zero_is_uniform(self):
        assert decay_weight(KICKOFF, KICKOFF + timedelta(days=900), 0.0) == 1.0

    def test_older_matches_weigh_less(self):
        as_of = KICKOFF + timedelta(days=400)
        recent = decay_weight(as_of - timedelta(days=10), as_of, 0.005)
        old = decay_weight(as_of - timedelta(days=300), as_of, 0.005)
        assert recent > old
        assert 0.0 < old < 1.0

    def test_half_life_is_ln2_over_xi(self):
        """Documents the units: xi is per DAY, so this is checkable arithmetic."""
        xi = 0.0065
        half_life_days = math.log(2) / xi
        as_of = KICKOFF + timedelta(days=1000)
        weight = decay_weight(
            as_of - timedelta(days=half_life_days), as_of, xi
        )
        assert weight == pytest.approx(0.5, abs=1e-9)

    def test_future_match_is_an_error(self):
        with pytest.raises(ValueError, match="point-in-time"):
            decay_weight(KICKOFF + timedelta(days=1), KICKOFF, 0.01)


class TestFitPointInTimeSafety:
    def test_match_at_the_cutoff_is_rejected(self):
        """
        STRICTLY `<`. A match kicking off exactly at the cutoff is the target
        itself in every realistic replay, and including it is total leakage.
        """
        as_of = KICKOFF + timedelta(days=5)
        matches = [match("A", "B", 1, 1, days=5)]
        with pytest.raises(ValueError, match="strictly before"):
            fit_team_strength(matches, as_of=as_of)

    def test_future_match_is_rejected(self):
        as_of = KICKOFF + timedelta(days=5)
        with pytest.raises(ValueError, match="strictly before"):
            fit_team_strength([match("A", "B", 3, 3, days=10)], as_of=as_of)

    def test_history_strictly_before_is_accepted(self):
        as_of = KICKOFF + timedelta(days=5)
        model = fit_team_strength([match("A", "B", 1, 1, days=0)], as_of=as_of)
        assert model.diagnostics.raw_matches == 1


class TestFitRecoversStructure:
    def _round_robin(self, strong: str, weak: str, matches_each: int = 6):
        """
        `strong` outscores `weak`, but BOTH score sometimes.

        The weak side must score at least occasionally, otherwise its attack MLE
        is exactly 0 and the fit becomes degenerate - see
        `TestDegenerateEvidence`, which tests that case deliberately instead of
        stumbling into it here.
        """
        fixtures = []
        for index in range(matches_each):
            fixtures.append(match(strong, weak, 3, 1, days=index * 2, event_id=f"s{index}"))
            fixtures.append(match(weak, strong, 1, 2, days=index * 2 + 1, event_id=f"w{index}"))
        return fixtures


    def test_attack_ranks_the_stronger_team_higher(self):
        as_of = KICKOFF + timedelta(days=100)
        model = fit_team_strength(self._round_robin("STRONG", "WEAK"), as_of=as_of)
        assert model.attack["STRONG"] > model.attack["WEAK"]

    def test_defence_is_unidentifiable_in_a_two_team_league(self):
        """
        A measured limitation, recorded rather than worked around.

        With only two clubs, every goal a team concedes was scored by the SAME
        opponent, so "poor defence" and "strong opposing attack" are the same
        parameter and the fit returns equal defences (0.857 each) even though
        STRONG conceded 2 goals per pair of matches and WEAK conceded 5. The
        estimator is behaving correctly; the DESIGN is degenerate.

        This matters beyond the test: it means defence estimates are weakly
        identified early in a season when few distinct opponents have been
        played, which is a real caveat on C1/C2's benefit in exactly the
        sparse-evidence regime Epic 2C cared about.
        """
        as_of = KICKOFF + timedelta(days=100)
        model = fit_team_strength(self._round_robin("STRONG", "WEAK"), as_of=as_of)
        assert model.defence["STRONG"] == pytest.approx(model.defence["WEAK"], abs=1e-6)

    def test_defence_ranks_the_stronger_team_lower_with_three_teams(self):
        """
        With three clubs the confound above is broken and defence is recovered.

        MEAN concedes little against both opponents; LEAKY concedes heavily
        against both. Because each team now faces more than one opponent, its
        conceding is no longer collinear with a single opposing attack.
        """
        as_of = KICKOFF + timedelta(days=200)
        fixtures = []
        day = 0
        for index in range(4):
            # LEAKY ships goals to everyone; MEAN concedes almost nothing.
            fixtures.append(match("MEAN", "LEAKY", 3, 1, days=day, event_id=f"a{index}"))
            day += 1
            fixtures.append(match("LEAKY", "MEAN", 1, 3, days=day, event_id=f"b{index}"))
            day += 1
            fixtures.append(match("MID", "LEAKY", 3, 1, days=day, event_id=f"c{index}"))
            day += 1
            fixtures.append(match("LEAKY", "MID", 1, 3, days=day, event_id=f"d{index}"))
            day += 1
            fixtures.append(match("MEAN", "MID", 1, 1, days=day, event_id=f"e{index}"))
            day += 1
            fixtures.append(match("MID", "MEAN", 1, 1, days=day, event_id=f"f{index}"))
            day += 1
        model = fit_team_strength(fixtures, as_of=as_of)
        # defence is a conceding multiplier: lower is better.
        assert model.defence["MEAN"] < model.defence["LEAKY"]


    def test_attack_normalisation_holds(self):
        """
        Without the mean(attack)=1 constraint the parameters are unidentified.

        Tolerance is 1e-5 rather than exact: normalisation is applied before the
        defence and gamma updates, which perturb the attack scale slightly within
        an iteration, so the constraint holds at convergence to within the
        convergence tolerance rather than to machine precision.
        """
        as_of = KICKOFF + timedelta(days=100)
        model = fit_team_strength(self._round_robin("STRONG", "WEAK"), as_of=as_of)
        mean_attack = sum(model.attack.values()) / len(model.attack)
        assert mean_attack == pytest.approx(1.0, abs=1e-5)


    def test_home_advantage_detected_when_present(self):
        as_of = KICKOFF + timedelta(days=200)
        fixtures = []
        # Symmetric teams, but home sides score more - gamma must exceed 1.
        for index in range(20):
            fixtures.append(match("A", "B", 2, 1, days=index * 2, event_id=f"h{index}"))
            fixtures.append(match("B", "A", 2, 1, days=index * 2 + 1, event_id=f"g{index}"))
        model = fit_team_strength(fixtures, as_of=as_of)
        assert model.home_advantage > 1.0

    def test_deterministic(self):
        as_of = KICKOFF + timedelta(days=100)
        fixtures = self._round_robin("STRONG", "WEAK")
        first = fit_team_strength(fixtures, as_of=as_of)
        second = fit_team_strength(list(reversed(fixtures)), as_of=as_of)
        for team in first.attack:
            assert first.attack[team] == pytest.approx(second.attack[team], abs=1e-9)
        assert first.home_advantage == pytest.approx(second.home_advantage, abs=1e-9)

    def test_postponed_matches_ignored_not_zeroed(self):
        """
        A postponed match is not a 0-0. Epic 2C's GG-028 was exactly this class
        of error, so it is asserted rather than assumed.

        Note only the NOT-completed case is constructible: `HistoricalMatch`
        already refuses `completed=True` with a missing score ("Zero is never
        substituted for an unknown result"), which is a stronger contract than
        this test originally assumed and is verified separately below.
        """
        as_of = KICKOFF + timedelta(days=100)
        clean = self._round_robin("STRONG", "WEAK")
        polluted = clean + [
            match(
                "STRONG", "WEAK", None, None, days=50, event_id="pp", completed=False
            ),
        ]
        baseline = fit_team_strength(clean, as_of=as_of)
        with_gaps = fit_team_strength(polluted, as_of=as_of)
        assert with_gaps.attack["STRONG"] == pytest.approx(
            baseline.attack["STRONG"], abs=1e-9
        )
        assert with_gaps.diagnostics.raw_matches == baseline.diagnostics.raw_matches

    def test_contract_forbids_completed_match_without_a_score(self):
        """
        Confirms the upstream guarantee this module relies on, so that the
        no-zero-substitution rule is pinned at the boundary rather than assumed.
        """
        with pytest.raises(ValueError, match="Zero is never substituted"):
            match("STRONG", "WEAK", None, None, days=50, event_id="void")



class TestUnknownTeamsRefuse:
    def test_predict_returns_none_for_unseen_team(self):
        """
        A promoted club has no parameters, and inventing attack=1.0 would present
        "average" as a measurement.
        """
        as_of = KICKOFF + timedelta(days=100)
        model = fit_team_strength(
            [match("A", "B", 1, 1, days=1), match("B", "A", 2, 0, days=2)],
            as_of=as_of,
        )
        assert predict_lambdas(model, "A", "B") is not None
        assert predict_lambdas(model, "A", "PROMOTED") is None
        assert predict_lambdas(model, "PROMOTED", "B") is None

    def test_empty_history_yields_no_parameters(self):
        model = fit_team_strength([], as_of=KICKOFF)
        assert model.attack == {}
        assert predict_lambdas(model, "A", "B") is None
        assert not model.diagnostics.converged


class TestDegenerateEvidence:
    """
    GG-028's mechanism arising INSIDE the richer model.

    A team that never scored in the fitting window has an exact maximum-likelihood
    attack of 0, which forces lambda = 0 and P(BTTS) = 0 - a claim of certainty
    from a handful of matches. Adding team parameters does NOT by itself fix the
    sparse-evidence pathology that Epic 2C addressed with shrinkage; that is a
    finding for the report, so it is pinned here rather than papered over with a
    silent floor.
    """

    def test_team_that_never_scored_gets_exactly_zero_attack(self):
        as_of = KICKOFF + timedelta(days=100)
        fixtures = []
        for index in range(6):
            fixtures.append(
                match("STRONG", "SILENT", 3, 0, days=index * 2, event_id=f"s{index}")
            )
            fixtures.append(
                match("SILENT", "STRONG", 0, 2, days=index * 2 + 1, event_id=f"w{index}")
            )
        model = fit_team_strength(fixtures, as_of=as_of)

        assert model.attack["SILENT"] == 0.0
        assert "SILENT" in model.diagnostics.zero_attack_teams

        rates = predict_lambdas(model, "STRONG", "SILENT")
        assert rates is not None
        # The away rate is exactly 0, so the unmodified POISSON_V1 mapping
        # produces exactly 0% - precisely the GG-028 signature.
        assert rates[1] == 0.0
        assert btts_independent(*rates) == 0.0

    def test_diagnostic_is_empty_when_every_team_scores(self):
        as_of = KICKOFF + timedelta(days=100)
        fixtures = [
            match("A", "B", 2, 1, days=1),
            match("B", "A", 1, 1, days=2),
        ]
        model = fit_team_strength(fixtures, as_of=as_of)
        assert model.diagnostics.zero_attack_teams == ()


class TestDixonColes:
    def test_rho_zero_reduces_to_independent(self):
        """
        rho = 0 makes every tau exactly 1, so Dixon-Coles must collapse onto
        independent Poisson.

        The tolerance is 1e-6 rather than tighter because the truncated score
        matrix omits mass beyond MAX_GOALS and the renormalisation divides it
        back out; the residual is the truncation error, not a modelling
        difference.
        """
        for rates in [(1.2, 1.1), (0.5, 2.0), (2.5, 2.5)]:
            assert btts_dixon_coles(*rates, 0.0) == pytest.approx(
                btts_independent(*rates), abs=1e-6
            )


    def test_tau_touches_only_four_cells(self):
        """
        The structural fact behind this Epic's prediction about C3: only 1-1 is a
        BTTS cell among the four adjusted scorelines.
        """
        for home in range(4):
            for away in range(4):
                tau = dixon_coles_tau(home, away, 1.3, 1.1, -0.1)
                if (home, away) in {(0, 0), (0, 1), (1, 0), (1, 1)}:
                    assert tau != 1.0
                else:
                    assert tau == 1.0

    def test_negative_rho_raises_btts(self):
        """
        rho < 0 inflates 1-1 (tau = 1 - rho > 1), the only BTTS cell it touches,
        so P(BTTS) must rise relative to independence.
        """
        independent = btts_independent(1.3, 1.1)
        corrected = btts_dixon_coles(1.3, 1.1, -0.1)
        assert corrected > independent

    def test_stays_a_probability(self):
        for rho in (-0.2, -0.05, 0.05, 0.15):
            value = btts_dixon_coles(1.4, 1.2, rho)
            assert 0.0 <= value <= 1.0

    def test_tau_adjustment_is_mass_preserving(self):
        """
        A property worth recording: the tau adjustments cancel EXACTLY.

        Writing p = exp(-lh-la), the four perturbations are
            -lh*la*rho*p  +  lh*rho*(la*p)  +  la*rho*(lh*p)  -  rho*(lh*la*p)
        = rho*lh*la*p * (-1 + 1 + 1 - 1) = 0.

        So Dixon-Coles is normalised by construction, and the renormalisation in
        `btts_dixon_coles` is a safety net against truncation and negative-tau
        flooring rather than a correction of a real skew. The residual below is
        the mass beyond MAX_GOALS, not a tau effect - which is why the
        renormalisation must stay: with a large negative rho, flooring clipped
        cells WOULD break the cancellation.
        """
        rho = -0.15
        rates = (1.5, 1.3)
        raw_total = 0.0
        for home in range(11):
            for away in range(11):
                raw_total += (
                    poisson_pmf(home, rates[0])
                    * poisson_pmf(away, rates[1])
                    * dixon_coles_tau(home, away, rates[0], rates[1], rho)
                )
        # Mass preserved to within Poisson truncation error only.
        assert raw_total == pytest.approx(1.0, abs=1e-5)
        assert 0.0 <= btts_dixon_coles(*rates, rho) <= 1.0



class TestBivariatePoisson:
    def test_zero_covariance_reduces_to_independent(self):
        assert btts_bivariate(1.2, 1.1, 0.0) == pytest.approx(
            btts_independent(1.2, 1.1), abs=1e-12
        )

    def test_shared_component_raises_btts(self):
        """
        A positive shared component makes both teams score together more often,
        so P(BTTS) must rise - and, importantly, it also raises both MARGINAL
        means, which is why callers must decompose rather than add on top.
        """
        assert btts_bivariate(1.2, 1.1, 0.2) > btts_independent(1.2, 1.1)

    def test_rejects_negative_covariance(self):
        # The structural limitation: this model cannot express negative
        # correlation, which is the direction Dixon-Coles addresses.
        with pytest.raises(ValueError):
            btts_bivariate(1.2, 1.1, -0.1)

    def test_stays_a_probability(self):
        for shared in (0.0, 0.1, 0.5, 1.0):
            value = btts_bivariate(1.0, 1.0, shared)
            assert 0.0 <= value <= 1.0


class TestLikelihood:
    def test_better_fitting_model_scores_higher(self):
        """
        A model fitted on lopsided results explains them better than one fitted
        on level ones - the property that makes the likelihood usable for
        selecting xi and rho.

        Both teams must score somewhere in the window, otherwise the degenerate
        attack = 0 case (see `TestDegenerateEvidence`) makes every rate zero and
        the likelihood is undefined rather than merely poor.
        """
        as_of = KICKOFF + timedelta(days=100)
        fixtures = [
            match("A", "B", 3, 1, days=1),
            match("B", "A", 1, 3, days=2),
            match("A", "B", 3, 1, days=3),
            match("B", "A", 1, 2, days=4),
        ]
        model = fit_team_strength(fixtures, as_of=as_of)
        fitted = weighted_log_likelihood(fixtures, model)
        assert fitted is not None
        # A model forced to equal strengths must fit these lopsided results worse.
        flat = fit_team_strength(
            [
                match("A", "B", 2, 2, days=1),
                match("B", "A", 2, 2, days=2),
            ],
            as_of=as_of,
        )
        flat_likelihood = weighted_log_likelihood(fixtures, flat)
        assert flat_likelihood is not None
        assert fitted > flat_likelihood

    def test_undefined_when_every_rate_is_degenerate(self):
        """
        The likelihood must REFUSE rather than return a plausible number when a
        zero-attack team makes every rate zero.
        """
        as_of = KICKOFF + timedelta(days=100)
        fixtures = [
            match("A", "B", 3, 0, days=1),
            match("B", "A", 0, 3, days=2),
        ]
        model = fit_team_strength(fixtures, as_of=as_of)
        assert "B" in model.diagnostics.zero_attack_teams
        assert weighted_log_likelihood(fixtures, model) is None


    def test_returns_none_when_nothing_scoreable(self):
        model = fit_team_strength([], as_of=KICKOFF)
        assert weighted_log_likelihood([match("A", "B", 1, 1, days=1)], model) is None
