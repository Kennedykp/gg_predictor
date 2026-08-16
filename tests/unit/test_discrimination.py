"""
Tests for `domain/discrimination.py` (Epic 2D).

The metric itself is the instrument this Epic's conclusions rest on. If AUC is
wrong, every statement about "structure does/does not add discrimination" is
wrong in the same direction and nothing downstream would reveal it - so these
tests pin the mathematics against hand-computable cases rather than against the
implementation's own output.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from domain.discrimination import (
    auc_from_labelled,
    constant_predictor_brier,
    paired_auc_delta,
    paired_brier_delta,
    prediction_spread,
    roc_auc,
    summarise_discrimination,
)
from domain.evaluation import BttsOutcome, PredictionRecord, UnevaluableReason


def record(
    probability: float | None,
    outcome: BttsOutcome,
    *,
    event_id: str = "1",
    model_id: str = "M",
    reason: UnevaluableReason | None = None,
) -> PredictionRecord:
    return PredictionRecord(
        model_id=model_id,
        model_version="1.0.0",
        competition="eng.1",
        season=2020,
        event_id=event_id,
        kickoff=datetime(2021, 1, 1, tzinfo=timezone.utc),
        home_team_id="H",
        away_team_id="A",
        outcome=outcome,
        probability=probability,
        unevaluable_reason=reason,
    )


class TestAucMathematics:
    def test_perfect_separation_is_one(self):
        pairs = [(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]
        assert auc_from_labelled(pairs) == 1.0

    def test_perfect_inversion_is_zero(self):
        pairs = [(0.1, 1), (0.2, 1), (0.8, 0), (0.9, 0)]
        assert auc_from_labelled(pairs) == 0.0

    def test_constant_prediction_is_exactly_half(self):
        """
        THE test that motivates midranks.

        A model emitting one value for every fixture has no ordering information,
        and the only defensible AUC is 0.5. A naive strict-inequality count would
        return 0.0 here and make a useless model look catastrophically wrong -
        or, with the comparison reversed, perfect.
        """
        pairs = [(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]
        assert auc_from_labelled(pairs) == 0.5

    def test_partial_ties_credited_as_half(self):
        # One positive at 0.7, one negative at 0.7, one negative at 0.1.
        # Pairs: (0.7 pos vs 0.7 neg) = 0.5, (0.7 pos vs 0.1 neg) = 1.0
        # AUC = 1.5 / 2 = 0.75
        pairs = [(0.7, 1), (0.7, 0), (0.1, 0)]
        assert auc_from_labelled(pairs) == pytest.approx(0.75)

    def test_known_value_by_hand(self):
        # positives 0.6, 0.4; negatives 0.5, 0.3
        # (0.6>0.5)=1, (0.6>0.3)=1, (0.4<0.5)=0, (0.4>0.3)=1 -> 3/4
        pairs = [(0.6, 1), (0.4, 1), (0.5, 0), (0.3, 0)]
        assert auc_from_labelled(pairs) == pytest.approx(0.75)

    def test_auc_is_invariant_to_monotone_flattening(self):
        """
        The property that makes AUC the right primary metric for Epic 2D.

        Shrinking every prediction toward the base rate is a strictly monotone
        transformation, so it CANNOT change AUC - while it does change Brier.
        This is the mechanical distinction between calibration and
        discrimination that GG-029 says the project must be able to make.
        """
        pairs = [(0.9, 1), (0.7, 0), (0.6, 1), (0.2, 0)]
        flattened = [(0.5 + (p - 0.5) * 0.01, y) for p, y in pairs]
        assert auc_from_labelled(flattened) == pytest.approx(auc_from_labelled(pairs))

    def test_undefined_when_single_class(self):
        assert auc_from_labelled([(0.5, 1), (0.7, 1)]) is None
        assert auc_from_labelled([(0.5, 0), (0.7, 0)]) is None

    def test_undefined_when_empty(self):
        assert auc_from_labelled([]) is None


class TestAucOverRecords:
    def test_refusals_are_not_ranked(self):
        records = [
            record(0.9, BttsOutcome.YES, event_id="1"),
            record(0.1, BttsOutcome.NO, event_id="2"),
            record(
                None,
                BttsOutcome.YES,
                event_id="3",
                reason=UnevaluableReason.INSUFFICIENT_HISTORY,
            ),

        ]
        # Only the two scored records participate; perfect separation.
        assert roc_auc(records) == 1.0

    def test_unknown_outcomes_excluded(self):
        records = [
            record(0.9, BttsOutcome.YES, event_id="1"),
            record(0.1, BttsOutcome.NO, event_id="2"),
            record(0.5, BttsOutcome.UNKNOWN, event_id="3"),
        ]
        summary = summarise_discrimination(records)
        assert summary.scored == 2
        assert summary.auc == 1.0


class TestPredictionSpread:
    def test_detects_collapse(self):
        collapsed = [record(0.52, BttsOutcome.YES, event_id=str(i)) for i in range(5)]
        spread = prediction_spread(collapsed)
        assert spread.sd == pytest.approx(0.0)
        assert spread.distinct == 1
        assert spread.span == pytest.approx(0.0)

    def test_reports_variation(self):
        varied = [
            record(0.2, BttsOutcome.YES, event_id="1"),
            record(0.8, BttsOutcome.NO, event_id="2"),
        ]
        spread = prediction_spread(varied)
        assert spread.distinct == 2
        assert spread.span == pytest.approx(0.6)

    def test_empty_is_none_not_zero(self):
        spread = prediction_spread([])
        assert spread.n == 0
        assert spread.sd is None
        assert spread.mean is None


class TestConstantPredictorBenchmark:
    def test_matches_variance_of_labels(self):
        """
        For p = base rate, Brier equals rate*(1-rate) - the label variance.

        This is the number every candidate must beat before a Brier improvement
        can be called skill, so it is pinned against its closed form.
        """
        records = [
            record(0.9, BttsOutcome.YES, event_id="1"),
            record(0.9, BttsOutcome.YES, event_id="2"),
            record(0.9, BttsOutcome.NO, event_id="3"),
            record(0.9, BttsOutcome.NO, event_id="4"),
        ]
        # rate = 0.5 -> 0.5 * 0.5 = 0.25
        assert constant_predictor_brier(records) == pytest.approx(0.25)

    def test_ignores_the_models_own_probabilities(self):
        good = [record(0.99, BttsOutcome.YES, event_id="1"), record(0.01, BttsOutcome.NO, event_id="2")]
        bad = [record(0.01, BttsOutcome.YES, event_id="1"), record(0.99, BttsOutcome.NO, event_id="2")]
        assert constant_predictor_brier(good) == constant_predictor_brier(bad)


class TestPairedBootstrap:
    def _arms(self):
        left = [
            record(0.5, BttsOutcome.YES, event_id=str(i), model_id="L")
            for i in range(40)
        ] + [
            record(0.5, BttsOutcome.NO, event_id=str(100 + i), model_id="L")
            for i in range(40)
        ]
        # Right arm separates perfectly on the same fixtures.
        right = [
            record(0.9, BttsOutcome.YES, event_id=str(i), model_id="R")
            for i in range(40)
        ] + [
            record(0.1, BttsOutcome.NO, event_id=str(100 + i), model_id="R")
            for i in range(40)
        ]
        return left, right

    def test_detects_a_real_improvement(self):
        left, right = self._arms()
        interval = paired_auc_delta(left, right, iterations=200)
        assert interval.point == pytest.approx(0.5)  # 1.0 - 0.5
        assert interval.verdict == "IMPROVEMENT"
        assert not interval.contains_zero

    def test_identical_arms_are_indistinguishable(self):
        left, _ = self._arms()
        same = [
            record(r.probability, r.outcome, event_id=r.event_id, model_id="R")
            for r in left
        ]
        interval = paired_auc_delta(left, same, iterations=200)
        assert interval.point == pytest.approx(0.0)
        assert interval.contains_zero
        assert interval.verdict == "INDISTINGUISHABLE"

    def test_is_deterministic_for_a_fixed_seed(self):
        left, right = self._arms()
        first = paired_auc_delta(left, right, iterations=200, seed=7)
        second = paired_auc_delta(left, right, iterations=200, seed=7)
        assert (first.point, first.lower, first.upper) == (
            second.point,
            second.lower,
            second.upper,
        )

    def test_misaligned_arms_are_rejected(self):
        """
        A silent misalignment would score model A's probability against model B's
        label and still return a plausible number, so it must be an error.
        """
        left, right = self._arms()
        shuffled = list(reversed(right))
        with pytest.raises(ValueError, match="misaligned"):
            paired_auc_delta(left, shuffled, iterations=10)

    def test_unequal_lengths_rejected(self):
        left, right = self._arms()
        with pytest.raises(ValueError, match="intersected and aligned"):
            paired_auc_delta(left, right[:-1], iterations=10)

    def test_brier_sign_convention_is_documented_and_negative_favours_right(self):
        left, right = self._arms()
        interval = paired_brier_delta(left, right, iterations=200)
        # Right is the better model, and lower Brier is better, so the delta is
        # negative. Asserted explicitly because the sign differs from AUC.
        assert interval.point is not None and interval.point < 0.0
