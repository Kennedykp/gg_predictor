"""
Epic 2H-5 — unit tests for the pure reporting core.

Fixtures come from `helpers.settlement_fixtures`, the factories Epic 2H-3
established. Building ledger dicts by hand here would let this suite drift from
the real ledger schema and pass against records the pipeline would reject.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, Dict, List, Optional, Sequence

import pytest
from helpers.settlement_fixtures import prediction, settlement, unresolved

from domain.evaluation import BttsOutcome
from domain.evaluation_input import EvaluationInput, SettlementState, join_for_evaluation
from domain.reporting import (
    MIXED,
    REPORTING_SCHEMA_VERSION,
    UNKNOWN_SEASON,
    Dimension,
    Group,
    GroupCounts,
    count_states,
    group_inputs,
    summarise_dimension,
    summarise_dimensions,
)


def _inputs(
    predictions: Sequence[Dict[str, Any]],
    settlements: Sequence[Dict[str, Any]],
) -> List[EvaluationInput]:
    """Join through the real 2H-3 path, so these tests exercise real inputs."""
    inputs, _join = join_for_evaluation(list(predictions), list(settlements))
    return inputs


def _gg(home: int, away: int) -> str:
    """
    The GG outcome implied by a score.

    `settlement()` takes `outcome` SEPARATELY from the goals and defaults it to
    "YES", faithfully mirroring the stored record — `settle_predictions` derives
    the outcome once, at settlement time, and evaluation reads that stored value
    rather than re-deriving it from the score. That is correct for an immutable
    settlement, but it means a fixture built with `home=2, away=0` and the default
    outcome would assert against an internally inconsistent record. Deriving it
    here keeps every fixture in this module self-consistent.
    """
    return "YES" if home > 0 and away > 0 else "NO"


def _one(
    prediction_id: str = "pred-1",
    fixture_id: str = "740123",
    competition: Optional[str] = "eng.1",
    season: Optional[int] = 2026,
    probability: float = 0.55,
    home: int = 2,
    away: int = 1,
    model_version: str = "1.0.0",
) -> List[EvaluationInput]:
    return _inputs(
        [
            prediction(
                prediction_id=prediction_id,
                fixture_id=fixture_id,
                competition=competition,
                season=season,
                probability=probability,
                provenance={"model_id": "POISSON_V1", "model_version": model_version},
            )
        ],
        [
            settlement(
                prediction_id=prediction_id,
                fixture_id=fixture_id,
                competition=competition,
                season=season,
                home=home,
                away=away,
                outcome=_gg(home, away),
            )
        ],
    )


class TestEmptySet:
    def test_no_inputs_produces_no_groups(self) -> None:
        for dimension in Dimension:
            assert summarise_dimension([], dimension) == []

    def test_empty_is_not_an_error(self) -> None:
        assert count_states([]) == GroupCounts(total=0, settled=0, unresolved=0, missing=0)

    def test_empty_counts_are_accounted_for(self) -> None:
        # 0 == 0. Guards against a truthiness check treating empty as a failure.
        assert count_states([]).accounted_for is True


class TestSingleSettledPrediction:
    def test_overall_has_exactly_one_group(self) -> None:
        groups = summarise_dimension(_one(), Dimension.OVERALL)
        assert len(groups) == 1
        assert groups[0].counts == GroupCounts(total=1, settled=1, unresolved=0, missing=0)

    def test_overall_key_is_empty_and_label_names_the_dimension(self) -> None:
        group = summarise_dimension(_one(), Dimension.OVERALL)[0]
        assert group.key == ()
        assert group.label == "overall"

    def test_brier_uses_the_stored_probability(self) -> None:
        # 2-1 is GG=YES, so the Brier of p=0.55 is (1 - 0.55)**2 exactly.
        group = summarise_dimension(_one(probability=0.55, home=2, away=1), Dimension.OVERALL)[0]
        assert group.summary.brier == pytest.approx((1 - 0.55) ** 2)

    def test_awkward_probability_is_not_rounded(self) -> None:
        # A value no rounding or re-derivation would reproduce.
        p = 0.6172839506172839
        group = summarise_dimension(_one(probability=p, home=1, away=1), Dimension.OVERALL)[0]
        assert group.summary.brier == (1 - p) ** 2

    def test_no_goal_from_one_side_scores_as_no(self) -> None:
        # 2-0 is GG=NO, so the outcome term flips.
        group = summarise_dimension(_one(probability=0.55, home=2, away=0), Dimension.OVERALL)[0]
        assert group.summary.brier == pytest.approx(0.55**2)
        assert group.summary.observed_rate == pytest.approx(0.0)

    def test_group_is_reportable(self) -> None:
        assert summarise_dimension(_one(), Dimension.OVERALL)[0].is_reportable is True


class TestMultiplePredictions:
    def test_all_predictions_are_counted(self) -> None:
        inputs = _one("pred-1", "740001") + _one("pred-2", "740002")
        group = summarise_dimension(inputs, Dimension.OVERALL)[0]
        assert group.counts.total == 2
        assert group.summary.scored == 2

    def test_brier_is_the_mean_of_both(self) -> None:
        inputs = _one("pred-1", "740001", probability=0.6, home=2, away=1) + _one(
            "pred-2", "740002", probability=0.4, home=2, away=0
        )
        group = summarise_dimension(inputs, Dimension.OVERALL)[0]
        assert group.summary.brier == pytest.approx(((1 - 0.6) ** 2 + 0.4**2) / 2)

    def test_counts_always_reconcile(self) -> None:
        inputs = _one("pred-1", "740001") + _one("pred-2", "740002")
        assert summarise_dimension(inputs, Dimension.OVERALL)[0].counts.accounted_for is True


class TestMixedStates:
    """Settled, unresolved and awaiting-settlement must stay distinguishable."""

    def _mixed(self) -> List[EvaluationInput]:
        return _inputs(
            [
                prediction(prediction_id="p-settled", fixture_id="740001"),
                prediction(prediction_id="p-unresolved", fixture_id="740002"),
                prediction(prediction_id="p-missing", fixture_id="740003"),
            ],
            [
                settlement(prediction_id="p-settled", fixture_id="740001", home=2, away=1),
                unresolved(prediction_id="p-unresolved", fixture_id="740002"),
                # p-missing has no settlement at all.
            ],
        )

    def test_each_state_is_counted_separately(self) -> None:
        counts = summarise_dimension(self._mixed(), Dimension.OVERALL)[0].counts
        assert (counts.total, counts.settled, counts.unresolved, counts.missing) == (3, 1, 1, 1)

    def test_unresolved_is_not_counted_as_missing(self) -> None:
        # The 2H-4 distinction: football's problem is not an operational gap.
        counts = summarise_dimension(self._mixed(), Dimension.OVERALL)[0].counts
        assert counts.unresolved == 1
        assert counts.missing == 1

    def test_only_the_settled_prediction_is_scored(self) -> None:
        summary = summarise_dimension(self._mixed(), Dimension.OVERALL)[0].summary
        assert summary.scored == 1

    def test_unresolved_records_remain_in_targets(self) -> None:
        # They must stay in the denominator, or coverage becomes a fraction of
        # whatever happened to survive.
        summary = summarise_dimension(self._mixed(), Dimension.OVERALL)[0].summary
        assert summary.targets == 3
        assert summary.coverage == pytest.approx(1 / 3)

    def test_unresolved_does_not_become_a_settled_outcome(self) -> None:
        rows = {i.prediction_id: i for i in self._mixed()}
        assert rows["p-unresolved"].settlement_state is SettlementState.UNRESOLVED
        assert rows["p-unresolved"].prediction.outcome is BttsOutcome.UNKNOWN

    def test_a_group_with_nothing_settled_is_not_reportable(self) -> None:
        inputs = _inputs(
            [prediction(prediction_id="p-1", fixture_id="740002")],
            [unresolved(prediction_id="p-1", fixture_id="740002")],
        )
        group = summarise_dimension(inputs, Dimension.OVERALL)[0]
        assert group.is_reportable is False
        assert group.summary.brier is None
        assert group.counts.total == 1


class TestCompetitionBreakdown:
    def _two_competitions(self) -> List[EvaluationInput]:
        return _inputs(
            [
                prediction(prediction_id="p-1", fixture_id="740001", competition="eng.1"),
                prediction(prediction_id="p-2", fixture_id="740002", competition="esp.1"),
                prediction(prediction_id="p-3", fixture_id="740003", competition="esp.1"),
            ],
            [
                settlement(prediction_id="p-1", fixture_id="740001", competition="eng.1"),
                settlement(prediction_id="p-2", fixture_id="740002", competition="esp.1"),
                settlement(prediction_id="p-3", fixture_id="740003", competition="esp.1"),
            ],
        )

    def test_one_group_per_competition(self) -> None:
        groups = summarise_dimension(self._two_competitions(), Dimension.COMPETITION)
        assert [g.label for g in groups] == ["eng.1", "esp.1"]

    def test_records_land_in_the_right_competition(self) -> None:
        groups = {g.label: g for g in summarise_dimension(self._two_competitions(), Dimension.COMPETITION)}
        assert groups["eng.1"].counts.total == 1
        assert groups["esp.1"].counts.total == 2

    def test_totals_across_groups_equal_the_input_count(self) -> None:
        groups = summarise_dimension(self._two_competitions(), Dimension.COMPETITION)
        assert sum(g.counts.total for g in groups) == 3

    def test_a_bad_competition_is_visible_when_the_average_hides_it(self) -> None:
        """The reason this Epic exists."""
        inputs = (
            _one("p-1", "740001", competition="eng.1", probability=0.9, home=2, away=1)
            + _one("p-2", "740002", competition="esp.1", probability=0.9, home=2, away=0)
            + _one("p-3", "740003", competition="esp.1", probability=0.9, home=1, away=0)
        )
        overall = summarise_dimension(inputs, Dimension.OVERALL)[0]
        by_comp = {g.label: g for g in summarise_dimension(inputs, Dimension.COMPETITION)}

        good = by_comp["eng.1"].summary.brier
        bad = by_comp["esp.1"].summary.brier
        average = overall.summary.brier
        assert good is not None and bad is not None and average is not None

        assert good == pytest.approx(0.01)
        assert bad == pytest.approx(0.81)
        # The overall figure sits between the two and describes neither.
        assert good < average < bad


class TestSeasonBreakdown:
    def test_one_group_per_season(self) -> None:
        inputs = _one("p-1", "740001", season=2025) + _one("p-2", "740002", season=2026)
        assert [g.label for g in summarise_dimension(inputs, Dimension.SEASON)] == ["2025", "2026"]

    def test_seasons_sort_numerically_not_lexically(self) -> None:
        inputs = (
            _one("p-1", "740001", season=2009)
            + _one("p-2", "740002", season=2010)
            + _one("p-3", "740003", season=1999)
        )
        assert [g.label for g in summarise_dimension(inputs, Dimension.SEASON)] == [
            "1999",
            "2009",
            "2010",
        ]

    def test_competition_season_pairs(self) -> None:
        inputs = (
            _one("p-1", "740001", competition="eng.1", season=2025)
            + _one("p-2", "740002", competition="eng.1", season=2026)
            + _one("p-3", "740003", competition="esp.1", season=2026)
        )
        groups = summarise_dimension(inputs, Dimension.COMPETITION_SEASON)
        assert [g.label for g in groups] == ["eng.1 / 2025", "eng.1 / 2026", "esp.1 / 2026"]

    def test_pair_keys_keep_both_components(self) -> None:
        inputs = _one("p-1", "740001", competition="eng.1", season=2026)
        assert summarise_dimension(inputs, Dimension.COMPETITION_SEASON)[0].key == ("eng.1", "2026")


class TestModelBreakdown:
    def test_versions_are_graded_separately(self) -> None:
        inputs = _one("p-1", "740001", model_version="1.0.0") + _one(
            "p-2", "740002", model_version="1.1.0"
        )
        groups = summarise_dimension(inputs, Dimension.MODEL)
        assert [g.label for g in groups] == ["POISSON_V1 / 1.0.0", "POISSON_V1 / 1.1.0"]

    def test_model_group_reports_its_own_identity(self) -> None:
        inputs = _one("p-1", "740001", model_version="1.1.0")
        summary = summarise_dimension(inputs, Dimension.MODEL)[0].summary
        assert (summary.model_id, summary.model_version) == ("POISSON_V1", "1.1.0")

    def test_a_mixed_version_group_is_labelled_mixed(self) -> None:
        # A competition predicted by two model versions must not be attributed to
        # either one of them.
        inputs = _one("p-1", "740001", model_version="1.0.0") + _one(
            "p-2", "740002", model_version="1.1.0"
        )
        summary = summarise_dimension(inputs, Dimension.COMPETITION)[0].summary
        assert summary.model_version == MIXED
        assert summary.model_id == "POISSON_V1"


class TestDeterminism:
    def test_group_order_is_independent_of_input_order(self) -> None:
        a = _one("p-1", "740001", competition="eng.1")
        b = _one("p-2", "740002", competition="esp.1")
        c = _one("p-3", "740003", competition="ger.1")
        forward = [g.label for g in summarise_dimension(a + b + c, Dimension.COMPETITION)]
        reversed_ = [g.label for g in summarise_dimension(c + b + a, Dimension.COMPETITION)]
        assert forward == reversed_ == ["eng.1", "esp.1", "ger.1"]

    def test_metrics_are_identical_across_input_orderings(self) -> None:
        a = _one("p-1", "740001", probability=0.6, home=2, away=1)
        b = _one("p-2", "740002", probability=0.3, home=1, away=0)
        assert (
            summarise_dimension(a + b, Dimension.OVERALL)[0].summary.brier
            == summarise_dimension(b + a, Dimension.OVERALL)[0].summary.brier
        )

    def test_repeated_calls_return_equal_results(self) -> None:
        inputs = _one("p-1", "740001") + _one("p-2", "740002", competition="esp.1")
        first = summarise_dimension(inputs, Dimension.COMPETITION)
        second = summarise_dimension(inputs, Dimension.COMPETITION)
        assert first == second

    def test_grouping_does_not_mutate_its_input(self) -> None:
        inputs = _one("p-1", "740001") + _one("p-2", "740002")
        before = list(inputs)
        summarise_dimension(inputs, Dimension.COMPETITION)
        assert inputs == before

    def test_group_keys_are_returned_sorted(self) -> None:
        inputs = (
            _one("p-1", "740001", competition="ger.1")
            + _one("p-2", "740002", competition="eng.1")
            + _one("p-3", "740003", competition="esp.1")
        )
        keys = list(group_inputs(inputs, Dimension.COMPETITION))
        assert keys == sorted(keys)


class TestUnknownSeason:
    def _no_season(self) -> List[EvaluationInput]:
        return _inputs(
            [prediction(prediction_id="p-1", fixture_id="740001", season=None)],
            [settlement(prediction_id="p-1", fixture_id="740001", season=None)],
        )

    def test_absent_season_is_labelled_not_zeroed(self) -> None:
        groups = summarise_dimension(self._no_season(), Dimension.SEASON)
        assert [g.label for g in groups] == [UNKNOWN_SEASON]

    def test_absent_season_sorts_after_real_seasons(self) -> None:
        inputs = self._no_season() + _one("p-2", "740002", season=2026)
        labels = [g.label for g in summarise_dimension(inputs, Dimension.SEASON)]
        assert labels == ["2026", UNKNOWN_SEASON]

    def test_absent_season_does_not_crash_the_sort(self) -> None:
        # `None` vs `int` in a sort key would raise TypeError.
        inputs = self._no_season() + _one("p-2", "740002", season=2026)
        assert len(summarise_dimension(inputs, Dimension.COMPETITION_SEASON)) == 2


class TestMalformedInput:
    def test_unhandled_settlement_state_raises(self) -> None:
        class Fake:
            prediction_id = "p-x"
            settlement_state = "NOT_A_STATE"

        with pytest.raises(ValueError, match="unhandled settlement state"):
            count_states([Fake()])  # type: ignore[list-item]

    def test_unsupported_dimension_raises(self) -> None:
        inputs = _one()
        with pytest.raises(ValueError, match="unsupported reporting dimension"):
            group_inputs(inputs, "not_a_dimension")  # type: ignore[arg-type]

    def test_unjoinable_predictions_are_excluded_from_groups(self) -> None:
        # A record with no competition cannot be keyed; 2H-3 rejects it at the
        # join, and it must not appear as a phantom group here.
        inputs, join = join_for_evaluation(
            [prediction(prediction_id="p-1", fixture_id="740001", competition=None)],
            [],
        )
        assert join.unjoinable
        assert summarise_dimension(inputs, Dimension.COMPETITION) == []

    def test_malformed_records_do_not_silently_become_zero(self) -> None:
        _inputs_list, join = join_for_evaluation([{"garbage": True}], [])
        assert join.unjoinable
        assert summarise_dimension(_inputs_list, Dimension.OVERALL) == []


class TestSummariseDimensions:
    def test_returns_one_entry_per_requested_dimension(self) -> None:
        result = summarise_dimensions(_one(), [Dimension.OVERALL, Dimension.COMPETITION])
        assert list(result) == [Dimension.OVERALL, Dimension.COMPETITION]

    def test_duplicate_dimensions_are_collapsed(self) -> None:
        result = summarise_dimensions(_one(), [Dimension.OVERALL, Dimension.OVERALL])
        assert list(result) == [Dimension.OVERALL]

    def test_caller_order_is_preserved(self) -> None:
        order = [Dimension.SEASON, Dimension.OVERALL, Dimension.COMPETITION]
        assert list(summarise_dimensions(_one(), order)) == order

    def test_no_dimensions_requested_returns_empty(self) -> None:
        assert summarise_dimensions(_one(), []) == {}

    def test_bin_count_is_forwarded(self) -> None:
        result = summarise_dimensions(_one(), [Dimension.OVERALL], bin_count=4)
        assert len(result[Dimension.OVERALL][0].summary.calibration) == 4


class TestFrozenContract:
    def test_schema_version_is_stated(self) -> None:
        assert REPORTING_SCHEMA_VERSION == "2h5.1"

    def test_groups_are_immutable(self) -> None:
        group = summarise_dimension(_one(), Dimension.OVERALL)[0]
        with pytest.raises(FrozenInstanceError):
            group.label = "changed"  # type: ignore[misc]

    def test_counts_are_immutable(self) -> None:
        counts = count_states(_one())
        with pytest.raises(FrozenInstanceError):
            counts.total = 99  # type: ignore[misc]

    def test_group_exposes_the_frozen_summary_type(self) -> None:
        # Reporting must hand back the frozen MetricSummary, not a reshaped copy.
        group = summarise_dimension(_one(), Dimension.OVERALL)[0]
        assert isinstance(group, Group)
        assert hasattr(group.summary, "brier")
        assert hasattr(group.summary, "log_loss")
        assert hasattr(group.summary, "calibration")
