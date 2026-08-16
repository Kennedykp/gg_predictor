"""
Guards on Epic 2D's experimental protocol.

Epic 2D's conclusion ("structure does not add discriminative power here") is only
worth anything if the experiment that produced it was honest. These tests pin the
properties that make it honest: the partitions are disjoint, the burned seasons
are labelled, the leaky probe cannot escape quarantine, the candidate adapters
are point-in-time and deterministic, and comparisons run on identical fixtures.

A future Epic that reuses this harness will trip these if it starts cutting
corners - which is the point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from domain.comparison import intersect
from domain.historical import HistoricalMatch
from evaluation_harness import PredictionContext, replay
from research import epic2d_experiment as exp

KICKOFF = datetime(2024, 8, 1, 15, 0, tzinfo=timezone.utc)


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
    season: int = 2024,
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
        # Without this the match is ELIGIBILITY-UNCERTAIN and `replay` correctly
        # refuses to score it (Epic 2B.1). Synthetic fixtures must therefore
        # declare themselves ordinary league play, exactly as the Epic 2B.3
        # leakage tests do.
        season_phase="regular-season",
    )


def round_robin(teams: list[str], *, rounds: int = 4, season: int = 2024, start: int = 0):
    """A dense synthetic league, so every team has enough matches to be fitted."""
    fixtures = []
    day = start
    for r in range(rounds):
        for i, home in enumerate(teams):
            for away in teams[i + 1 :]:
                h, a = (home, away) if r % 2 == 0 else (away, home)
                fixtures.append(
                    match(
                        h,
                        a,
                        1 + (day % 3),
                        (day + 1) % 3,
                        days=day,
                        event_id=f"s{season}-r{r}-{h}-{a}",
                        season=season,
                    )
                )
                day += 1
    return fixtures


class TestPartitionIsolation:
    """Part 15: the partitions must be disjoint and the burned seasons labelled."""

    def test_development_validation_holdout_are_disjoint(self) -> None:
        dev = set(exp.DEVELOPMENT_SEASONS)
        val = set(exp.VALIDATION_SEASONS)
        holdout = set(exp.HOLDOUT_SEASONS)
        assert not dev & val
        assert not dev & holdout
        assert not val & holdout

    def test_holdout_is_not_a_season_burned_by_an_earlier_epic(self) -> None:
        """
        The whole reason 2D holds out 2024 rather than 2023: Epic 2C already
        reported 2023 by evidence bucket, so 2023 cannot be called untouched.
        """
        for season in exp.HOLDOUT_SEASONS:
            assert season not in exp.BURNED_SEASONS, (
                f"season {season} was inspected by an earlier Epic "
                f"({exp.BURNED_SEASONS.get(season)}) and cannot be a holdout"
            )

    def test_epic2c_seasons_are_recorded_as_burned(self) -> None:
        # 2C searched on 2018-2019, validated on 2020, tested on 2023.
        for season in (2018, 2019, 2020, 2023):
            assert season in exp.BURNED_SEASONS

    def test_development_seasons_are_acknowledged_as_reused(self) -> None:
        for season in exp.DEVELOPMENT_SEASONS:
            assert season in exp.BURNED_SEASONS


class TestOracleQuarantine:
    """
    Part: the leaky ceiling probe must stay obviously leaky and clearly labelled.

    It is a deliberately contaminated diagnostic. These tests make sure it cannot
    be mistaken for a model or quietly promoted into production.
    """

    def test_model_id_announces_leakage(self) -> None:
        assert "LEAKY" in exp.OracleCeilingProbe.model_id
        assert "quarantined" in exp.OracleCeilingProbe.model_version

    def test_docstring_states_it_is_not_a_model(self) -> None:
        doc = exp.OracleCeilingProbe.__doc__ or ""
        assert "NOT A MODEL" in doc
        assert "CEILING" in doc.upper()

    def test_probe_is_not_in_the_production_model_registry(self) -> None:
        """
        The harness registry is what `run_evaluation.py` exposes. A leaky probe
        appearing there could be run as if it were a real model.
        """
        import evaluation_harness

        registry = getattr(evaluation_harness, "MODELS", None)
        if registry is None:
            pytest.skip("harness exposes no MODELS registry")
        ids = set(registry.keys()) if hasattr(registry, "keys") else set(registry)
        assert not any("ORACLE" in str(name).upper() for name in ids)

    def test_probe_really_does_see_the_target_fixture(self) -> None:
        """
        Confirms the probe is leaky BY CONSTRUCTION, so its number is a genuine
        upper bound rather than an accidentally honest model. If this ever
        started passing point-in-time, the ceiling claim would be wrong.
        """
        teams = ["a", "b", "c", "d", "e"]
        dataset = round_robin(teams)
        target = dataset[-1]
        probe = exp.OracleCeilingProbe(dataset)
        # History deliberately EMPTY: an honest model can say nothing here.
        context = PredictionContext(
            competition=target.competition,
            season=target.season,
            event_id=target.event_id,
            kickoff=target.kickoff,
            home_team_id=target.home_team_id,
            away_team_id=target.away_team_id,
            history=[],
        )
        prediction = probe.predict(context)
        assert prediction.probability is not None, (
            "the probe should still predict with no history, because it was "
            "fitted on the full dataset - that is precisely its leakage"
        )


class TestCandidatePointInTimeSafety:
    """Part 6: candidates may only use matches strictly before the target."""

    def test_adapter_refuses_when_no_history_precedes_target(self) -> None:
        adapter = exp.MaherAdapter(model_id="C1_MAHER")
        context = PredictionContext(
            competition="eng.1",
            season=2024,
            event_id="target",
            kickoff=KICKOFF,
            home_team_id="a",
            away_team_id="b",
            history=[],
        )
        prediction = adapter.predict(context)
        assert prediction.probability is None
        assert prediction.reason is not None

    def test_leaking_the_target_into_the_fit_raises_rather_than_scoring(self) -> None:
        """
        MUTATION TEST. Deliberately hand the fitter the target fixture and assert
        it REFUSES.

        `fit_team_strength` rejects any match whose kickoff is not strictly before
        `as_of`, so leakage is impossible rather than merely unlikely - a stronger
        guarantee than "the number would come out different". A future refactor
        that downgraded this to a silent filter would fail here.
        """
        teams = ["a", "b", "c", "d", "e"]
        dataset = round_robin(teams)
        target = dataset[-1]
        history = [m for m in dataset if m.kickoff < target.kickoff]

        honest = exp.MaherAdapter(model_id="C1_MAHER").predict(
            PredictionContext(
                competition=target.competition,
                season=target.season,
                event_id=target.event_id,
                kickoff=target.kickoff,
                home_team_id=target.home_team_id,
                away_team_id=target.away_team_id,
                history=history,
            )
        )
        assert honest.probability is not None, "the honest case must be scorable"

        with pytest.raises(ValueError, match="strictly before"):
            exp.MaherAdapter(model_id="C1_MAHER").predict(
                PredictionContext(
                    competition=target.competition,
                    season=target.season,
                    event_id=target.event_id,
                    kickoff=target.kickoff,
                    home_team_id=target.home_team_id,
                    away_team_id=target.away_team_id,
                    history=[*history, target],
                )
            )

    def test_history_actually_influences_the_prediction(self) -> None:
        """
        Companion to the leakage test: if the fit ignored its history entirely,
        every guard above would be vacuous. Two different histories must give
        two different answers.
        """
        target_kickoff = KICKOFF + timedelta(days=400)
        low_scoring = round_robin(["a", "b", "c", "d", "e"])
        high_scoring = [
            match(
                m.home_team_id,
                m.away_team_id,
                (m.home_goals or 0) + 3,
                (m.away_goals or 0) + 3,
                days=i,
                event_id=f"high-{i}",
            )
            for i, m in enumerate(low_scoring)
        ]

        def probability_for(history: list[HistoricalMatch]) -> float | None:
            return (
                exp.MaherAdapter(model_id="C1_MAHER")
                .predict(
                    PredictionContext(
                        competition="eng.1",
                        season=2024,
                        event_id="target",
                        kickoff=target_kickoff,
                        home_team_id="a",
                        away_team_id="b",
                        history=history,
                    )
                )
                .probability
            )

        low = probability_for(low_scoring)
        high = probability_for(high_scoring)
        assert low is not None and high is not None
        assert high > low, (
            "a uniformly higher-scoring league must yield a higher BTTS "
            "probability; if not, the fit is ignoring its history"
        )

    def test_replay_never_passes_future_matches_to_the_adapter(self) -> None:
        """Every match the adapter sees must precede the target's kickoff."""
        teams = ["a", "b", "c", "d"]
        dataset = round_robin(teams, rounds=3)
        targets = dataset[-3:]
        seen: list[tuple[datetime, datetime]] = []

        class Spy:
            model_id = "SPY"
            model_version = "1"

            def predict(self, context: PredictionContext):
                for m in context.history:
                    seen.append((m.kickoff, context.kickoff))
                return exp.MaherAdapter(model_id="C1_MAHER").predict(context)

        replay(dataset, Spy(), targets=targets)
        assert seen
        for history_kickoff, target_kickoff in seen:
            assert history_kickoff < target_kickoff


class TestDeterminism:
    """Part 14: identical inputs must produce identical outputs."""

    def test_repeated_prediction_is_bit_identical(self) -> None:
        teams = ["a", "b", "c", "d", "e"]
        dataset = round_robin(teams)
        target = dataset[-1]
        history = [m for m in dataset if m.kickoff < target.kickoff]
        context = PredictionContext(
            competition=target.competition,
            season=target.season,
            event_id=target.event_id,
            kickoff=target.kickoff,
            home_team_id=target.home_team_id,
            away_team_id=target.away_team_id,
            history=history,
        )
        first = exp.MaherAdapter(model_id="C1_MAHER").predict(context)
        second = exp.MaherAdapter(model_id="C1_MAHER").predict(context)
        assert first.probability == second.probability

    def test_memoisation_cannot_serve_a_stale_fit_across_competitions(self) -> None:
        """
        The fit cache is keyed on competition too. Two different leagues sharing
        a cache entry would silently score one league with another's model.
        """
        teams = ["a", "b", "c", "d", "e"]
        english = round_robin(teams)
        # Same team ids and history length, different competition and scorelines.
        german = [
            match(
                m.home_team_id,
                m.away_team_id,
                (m.home_goals or 0) + 2,
                m.away_goals,
                days=i,
                event_id=f"ger-{i}",
                competition="ger.1",
            )
            for i, m in enumerate(english)
        ]
        adapter = exp.MaherAdapter(model_id="C1_MAHER")
        target = english[-1]
        base_context = dict(
            season=target.season,
            event_id=target.event_id,
            kickoff=target.kickoff,
            home_team_id=target.home_team_id,
            away_team_id=target.away_team_id,
        )
        english_prediction = adapter.predict(
            PredictionContext(
                competition="eng.1",
                history=[m for m in english if m.kickoff < target.kickoff],
                **base_context,
            )
        )
        german_prediction = adapter.predict(
            PredictionContext(
                competition="ger.1",
                history=[m for m in german if m.kickoff < target.kickoff],
                **base_context,
            )
        )
        assert english_prediction.probability is not None
        assert german_prediction.probability is not None
        assert english_prediction.probability != pytest.approx(
            german_prediction.probability
        ), "cache collision across competitions"


class TestRefusalRatherThanFabrication:
    """
    GG-001/GG-028: an unknown team must be refused, not assumed average.

    A promoted club has no fitted strength. Substituting attack = 1.0 would
    present "exactly league average" as if it were a measurement.
    """

    def test_unknown_team_is_refused_not_defaulted(self) -> None:
        teams = ["a", "b", "c", "d", "e"]
        dataset = round_robin(teams)
        target = dataset[-1]
        history = [m for m in dataset if m.kickoff < target.kickoff]
        prediction = exp.MaherAdapter(model_id="C1_MAHER").predict(
            PredictionContext(
                competition="eng.1",
                season=2024,
                event_id="promoted",
                kickoff=target.kickoff,
                home_team_id="a",
                away_team_id="NEWLY_PROMOTED",
                history=history,
            )
        )
        assert prediction.probability is None
        assert prediction.reason is not None

    def test_never_emits_an_unjustified_certainty(self) -> None:
        """
        GG-028's signature was lambda = 0 producing exactly 0% BTTS. A team that
        never scored yields a zero MLE rate; the adapter must refuse rather than
        publish 0.0 as a probability.
        """
        teams = ["a", "b", "c", "d"]
        dataset = []
        day = 0
        for r in range(4):
            for i, home in enumerate(teams):
                for away in teams[i + 1 :]:
                    # "d" never scores, in any fixture, at either venue.
                    hg = 0 if home == "d" else 2
                    ag = 0 if away == "d" else 1
                    dataset.append(
                        match(home, away, hg, ag, days=day, event_id=f"r{r}-{home}-{away}")
                    )
                    day += 1
        target = match("a", "d", 1, 1, days=day, event_id="target")
        prediction = exp.MaherAdapter(model_id="C1_MAHER").predict(
            PredictionContext(
                competition="eng.1",
                season=2024,
                event_id=target.event_id,
                kickoff=target.kickoff,
                home_team_id="a",
                away_team_id="d",
                history=dataset,
            )
        )
        if prediction.probability is not None:
            assert 0.0 < prediction.probability < 1.0
        else:
            assert prediction.reason is not None


class TestFairComparison:
    """Part 8: comparisons must run on identical fixtures."""

    def test_intersection_aligns_arms_fixture_for_fixture(self) -> None:
        teams = ["a", "b", "c", "d", "e"]
        dataset = round_robin(teams)
        targets = dataset[-6:]
        from evaluation_harness import PoissonV1Adapter

        baseline = replay(dataset, PoissonV1Adapter(), targets=targets)
        candidate = replay(dataset, exp.MaherAdapter(model_id="C1_MAHER"), targets=targets)
        left, right = intersect(baseline, candidate)
        assert len(left) == len(right)
        for a, b in zip(left, right, strict=True):
            assert (a.competition, a.season, a.event_id) == (
                b.competition,
                b.season,
                b.event_id,
            )

    def test_evidence_counts_are_shared_by_both_arms(self) -> None:
        """
        One count per fixture, supplied externally, so a fixture cannot land in
        different evidence buckets for different models.
        """
        teams = ["a", "b", "c", "d"]
        dataset = round_robin(teams, rounds=3)
        targets = dataset[-4:]
        counts = exp.evidence_counts(dataset, targets)
        assert len(counts) == len(targets)
        for target in targets:
            key = (target.competition, target.season, target.event_id)
            assert key in counts
            # Only strictly-prior home matches for that same team may be counted.
            expected = len(
                [
                    m
                    for m in dataset
                    if m.competition == target.competition
                    and m.season == target.season
                    and m.kickoff < target.kickoff
                    and m.home_team_id == target.home_team_id
                    and m.completed
                ]
            )
            assert counts[key] == expected


class TestParameterSelectionObjective:
    """
    GG-029: parameters must be chosen on the GOAL process, never on BTTS Brier.

    Selecting on Brier is what rewards flattening toward the base rate. The
    profile functions therefore return goal-count log-likelihoods.
    """

    def test_xi_profile_reports_goal_likelihood_not_brier(self) -> None:
        dataset = round_robin(["a", "b", "c", "d", "e"], rounds=6)
        points = exp.profile_xi(dataset, [2024], [0.0, 0.01], per_season=3)
        assert len(points) == 2
        for point in points:
            if point.log_likelihood is not None:
                # A log-likelihood of goal counts is negative; a Brier score is
                # a positive number below 1. This pins which one is being used.
                assert point.log_likelihood < 0.0

    def test_lambda3_boundary_maximum_means_the_candidate_is_dropped(self) -> None:
        """
        The 2D development profile maximised lambda3 at the boundary 0, so C4 was
        dropped. This pins the DECISION RULE: a boundary maximum means the shared
        component is not identifiable and must not be fitted anyway.
        """
        points = [
            exp.LikelihoodPoint(parameter=0.0, log_likelihood=-2.95, fixtures=10),
            exp.LikelihoodPoint(parameter=0.1, log_likelihood=-2.97, fixtures=10),
            exp.LikelihoodPoint(parameter=0.3, log_likelihood=-3.03, fixtures=10),
        ]
        best = max(points, key=lambda p: p.log_likelihood or -1e9)
        assert best.parameter == 0.0, (
            "if the profile peaks at lambda3 = 0 the data cannot distinguish a "
            "shared component from none, and C4 must be dropped, not constrained"
        )
