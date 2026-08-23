"""
Epic 2J — unit tests for the evaluation quality gate.

WHAT THESE TESTS ARE DEFENDING
------------------------------
One sentence: a Brier score must never reach a human as evidence of model
quality without a sample size, discrimination evidence and a naive baseline
beside it. `TestTheOriginalDefect` is the test that would have failed before this
Epic; everything else exists so that the gate cannot be quietly loosened.

`PredictionRecord` is constructed directly here rather than driven through the
join. `domain/quality.py` is pure and takes records, so this is its real input
type; the ledger-to-records path is already covered by `test_evaluation_input.py`
and `test_reporting.py`, and going through it would test the join twice while
making the class-balance cases (which need exact YES/NO counts) unreadable.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import List, Optional, Sequence

import pytest

from domain.evaluation import BttsOutcome, PredictionRecord, UnevaluableReason, summarise
from domain.quality import (
    BASELINE_EQUALITY_TOLERANCE,
    MINIMUM_REPORTABLE_N,
    QUALITY_SCHEMA_VERSION,
    BaselineVerdict,
    QualityAssessment,
    ReportStatus,
    assess,
    assess_records,
)

KICKOFF = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def _record(
    probability: Optional[float],
    outcome: BttsOutcome,
    *,
    event_id: str = "740000",
    unevaluable_reason: Optional[UnevaluableReason] = None,
) -> PredictionRecord:
    return PredictionRecord(
        model_id="POISSON_V1",
        model_version="1.0.0",
        competition="eng.1",
        season=2026,
        event_id=event_id,
        kickoff=KICKOFF,
        home_team_id="H",
        away_team_id="A",
        outcome=outcome,
        probability=probability,
        unevaluable_reason=unevaluable_reason,
    )


def _records(
    pairs: Sequence[tuple[float, BttsOutcome]],
) -> List[PredictionRecord]:
    return [
        _record(p, outcome, event_id=f"7400{index:02d}")
        for index, (p, outcome) in enumerate(pairs)
    ]


def _uniform(n: int, probability: float, outcome: BttsOutcome) -> List[PredictionRecord]:
    return _records([(probability, outcome)] * n)


def _balanced(n_yes: int, n_no: int, probability: float = 0.5) -> List[PredictionRecord]:
    """A sample with an exact class split, so AUC definedness is controlled."""
    return _records(
        [(probability, BttsOutcome.YES)] * n_yes + [(probability, BttsOutcome.NO)] * n_no
    )


def _assess(
    records: Sequence[PredictionRecord],
    *,
    minimum_n: int = MINIMUM_REPORTABLE_N,
) -> QualityAssessment:
    return assess_records(
        records,
        model_id="POISSON_V1",
        model_version="1.0.0",
        minimum_n=minimum_n,
    )


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


class TestTheOriginalDefect:
    """
    `Brier: 0.0100, n: 1` must never be presentable as a quality claim.

    This is the exact output 2G-R5 was written against: p=0.9 on a fixture that
    finished GG=YES gives a Brier of 0.01, which reads as a superb model and is
    one observation.
    """

    def test_a_single_lucky_observation_is_not_a_quality_claim(self) -> None:
        result = _assess([_record(0.9, BttsOutcome.YES)])
        assert result.summary.brier == pytest.approx(0.01)
        assert result.scored == 1
        assert result.is_quality_claim is False
        assert result.status is ReportStatus.INSUFFICIENT_SAMPLE

    def test_the_sample_size_is_stated_in_the_headline(self) -> None:
        # The original report printed the Brier and not the n.
        headline = _assess([_record(0.9, BttsOutcome.YES)]).headline
        assert "1 scored" in headline
        assert str(MINIMUM_REPORTABLE_N) in headline
        assert "not evidence of model quality" in headline

    def test_the_metrics_survive_the_gate(self) -> None:
        # Gated, not hidden: withholding the evidence is the opposite failure.
        result = _assess([_record(0.9, BttsOutcome.YES)])
        assert result.summary.brier is not None
        assert result.summary.log_loss is not None
        assert result.constant_brier is not None

    def test_a_beaten_baseline_is_reported_even_when_gated(self) -> None:
        # n=1 with a correct call: the constant predictor also scores 0.0 here,
        # so the "model looks brilliant" reading is doubly unsupported.
        result = _assess([_record(0.9, BttsOutcome.YES)])
        assert result.constant_brier == pytest.approx(0.0)
        assert result.baseline_verdict is BaselineVerdict.WORSE


# ---------------------------------------------------------------------------
# Sample-size gate
# ---------------------------------------------------------------------------


class TestMinimumN:
    def test_zero_scored_is_not_measurable(self) -> None:
        result = _assess([])
        assert result.status is ReportStatus.NOT_MEASURABLE
        assert result.is_quality_claim is False
        assert result.summary.brier is None

    def test_unresolved_only_is_not_measurable_not_insufficient(self) -> None:
        # An operational gap must not read as a weak model.
        records = [
            _record(0.5, BttsOutcome.UNKNOWN),
            _record(
                None,
                BttsOutcome.UNKNOWN,
                unevaluable_reason=UnevaluableReason.INSUFFICIENT_HISTORY,
            ),

        ]
        assert _assess(records).status is ReportStatus.NOT_MEASURABLE

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 10, 99])
    def test_below_threshold_is_gated(self, n: int) -> None:
        result = _assess(_balanced(n - n // 2, n // 2), minimum_n=100)
        assert result.scored == n
        assert result.is_quality_claim is False
        assert result.status is ReportStatus.INSUFFICIENT_SAMPLE

    def test_exactly_at_threshold_is_reportable(self) -> None:
        # Boundary is inclusive: `scored < minimum_n` gates, so n == threshold passes.
        result = _assess(_balanced(50, 50), minimum_n=100)
        assert result.scored == 100
        assert result.status is ReportStatus.REPORTABLE
        assert result.is_quality_claim is True

    def test_one_short_of_threshold_is_gated(self) -> None:
        result = _assess(_balanced(50, 49), minimum_n=100)
        assert result.scored == 99
        assert result.status is ReportStatus.INSUFFICIENT_SAMPLE

    def test_above_threshold_is_reportable(self) -> None:
        assert _assess(_balanced(80, 70), minimum_n=100).is_quality_claim is True

    def test_shortfall_counts_what_is_still_needed(self) -> None:
        assert _assess(_balanced(2, 1), minimum_n=100).shortfall == 97

    def test_shortfall_is_zero_once_cleared(self) -> None:
        assert _assess(_balanced(60, 60), minimum_n=100).shortfall == 0

    def test_the_threshold_judged_against_is_recorded(self) -> None:
        # An artifact must state the policy it was judged under, not today's.
        assert _assess(_balanced(3, 3), minimum_n=42).minimum_n == 42

    def test_unresolved_records_do_not_count_towards_n(self) -> None:
        # Padding the sample with unscoreable records must not open the gate.
        records = _balanced(50, 49) + [
            _record(0.5, BttsOutcome.UNKNOWN, event_id="799999"),
        ]
        result = _assess(records, minimum_n=100)
        assert result.scored == 99
        assert result.status is ReportStatus.INSUFFICIENT_SAMPLE


class TestPolicyDefault:
    def test_the_default_threshold_is_pinned(self) -> None:
        # Derived from SE = 0.5/sqrt(n) <= 0.05. Changing it is a policy decision
        # and must be a deliberate edit here, with the derivation updated.
        assert MINIMUM_REPORTABLE_N == 100

    def test_the_default_is_used_when_not_overridden(self) -> None:
        assert _assess(_balanced(1, 1)).minimum_n == MINIMUM_REPORTABLE_N

    def test_schema_version_is_stated(self) -> None:
        assert QUALITY_SCHEMA_VERSION == "2j.1"


# ---------------------------------------------------------------------------
# AUC
# ---------------------------------------------------------------------------


class TestAucAvailability:
    def test_two_class_sample_has_auc(self) -> None:
        # Perfect separation: every YES priced above every NO.
        records = _records(
            [(0.8, BttsOutcome.YES)] * 50 + [(0.2, BttsOutcome.NO)] * 50
        )
        result = _assess(records, minimum_n=100)
        assert result.auc == pytest.approx(1.0)
        assert result.auc_undefined_reason is None
        assert result.status is ReportStatus.REPORTABLE

    def test_all_positive_leaves_auc_undefined(self) -> None:
        result = _assess(_uniform(120, 0.6, BttsOutcome.YES), minimum_n=100)
        assert result.auc is None
        assert result.status is ReportStatus.AUC_UNDEFINED
        assert result.is_quality_claim is False

    def test_all_negative_leaves_auc_undefined(self) -> None:
        result = _assess(_uniform(120, 0.6, BttsOutcome.NO), minimum_n=100)
        assert result.auc is None
        assert result.status is ReportStatus.AUC_UNDEFINED
        assert result.is_quality_claim is False

    def test_the_undefined_reason_names_the_class_counts(self) -> None:
        # So a reader can see WHY, and cannot substitute 0.5.
        result = _assess(_uniform(120, 0.6, BttsOutcome.YES), minimum_n=100)
        assert result.auc_undefined_reason is not None
        assert "single-class" in result.auc_undefined_reason
        assert "120 YES" in result.auc_undefined_reason
        assert "0 NO" in result.auc_undefined_reason

    def test_no_scored_predictions_says_so(self) -> None:
        assert _assess([]).auc_undefined_reason == "no scored predictions"

    def test_auc_and_its_reason_are_never_both_present(self) -> None:
        for records in (
            _balanced(60, 60),
            _uniform(120, 0.6, BttsOutcome.YES),
            _uniform(120, 0.6, BttsOutcome.NO),
            [],
        ):
            result = _assess(records, minimum_n=100)
            assert (result.auc is None) != (result.auc_undefined_reason is None)

    def test_auc_is_never_defaulted_to_a_half(self) -> None:
        # 0.5 is "no discrimination", a finding. Absence must stay absent.
        assert _assess(_uniform(120, 0.6, BttsOutcome.YES), minimum_n=100).auc is None

    def test_all_ties_give_exactly_one_half(self) -> None:
        # Every record priced identically: no ranking information, and the
        # midpoint credit must produce 0.5 rather than 0.0 or 1.0.
        result = _assess(_balanced(60, 60, probability=0.5), minimum_n=100)
        assert result.auc == pytest.approx(0.5)

    def test_class_counts_are_reported(self) -> None:
        result = _assess(_balanced(70, 30), minimum_n=100)
        assert (result.positives, result.negatives) == (70, 30)

    def test_class_counts_sum_to_scored(self) -> None:
        result = _assess(_balanced(70, 30) + [_record(0.5, BttsOutcome.UNKNOWN, event_id="799998")])
        assert result.positives + result.negatives == result.scored

    def test_sample_size_is_checked_before_discrimination(self) -> None:
        # A tiny single-class group must report INSUFFICIENT_SAMPLE, not
        # AUC_UNDEFINED: the latter would imply n was the only thing missing.
        result = _assess(_uniform(3, 0.6, BttsOutcome.YES), minimum_n=100)
        assert result.status is ReportStatus.INSUFFICIENT_SAMPLE
        assert result.auc is None

    def test_a_tiny_two_class_sample_is_still_gated(self) -> None:
        # AUC being defined does not license the claim.
        result = _assess(_records([(0.8, BttsOutcome.YES), (0.2, BttsOutcome.NO)]), minimum_n=100)
        assert result.auc is not None
        assert result.is_quality_claim is False


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


class TestConstantPredictorBaseline:
    def test_baseline_uses_the_same_settled_observations(self) -> None:
        # 70 YES of 100 -> base rate 0.7 -> constant Brier 0.7*0.09 + 0.3*0.49.
        result = _assess(_balanced(70, 30), minimum_n=100)
        expected = 0.7 * (1 - 0.7) ** 2 + 0.3 * (0.0 - 0.7) ** 2
        assert result.constant_brier == pytest.approx(expected)

    def test_unresolved_records_do_not_move_the_baseline(self) -> None:
        # The comparison is only honest over an identical population.
        clean = _assess(_balanced(70, 30), minimum_n=100)
        padded = _assess(
            _balanced(70, 30) + [_record(0.99, BttsOutcome.UNKNOWN, event_id="799997")],
            minimum_n=100,
        )
        assert padded.constant_brier == pytest.approx(clean.constant_brier)

    def test_model_better_than_baseline(self) -> None:
        # Perfectly separated and confident: beats a constant easily.
        records = _records([(0.99, BttsOutcome.YES)] * 50 + [(0.01, BttsOutcome.NO)] * 50)
        result = _assess(records, minimum_n=100)
        assert result.brier_delta is not None and result.brier_delta < 0
        assert result.baseline_verdict is BaselineVerdict.BETTER

    def test_model_worse_than_baseline(self) -> None:
        # Confidently backwards - the 2D finding that motivated the requirement.
        records = _records([(0.01, BttsOutcome.YES)] * 50 + [(0.99, BttsOutcome.NO)] * 50)
        result = _assess(records, minimum_n=100)
        assert result.brier_delta is not None and result.brier_delta > 0
        assert result.baseline_verdict is BaselineVerdict.WORSE

    def test_model_equal_to_baseline(self) -> None:
        # Predicting the base rate itself IS the constant predictor.
        result = _assess(_balanced(50, 50, probability=0.5), minimum_n=100)
        assert result.brier_delta == pytest.approx(0.0, abs=1e-12)
        assert result.baseline_verdict is BaselineVerdict.EQUAL

    def test_float_noise_is_not_reported_as_a_difference(self) -> None:
        # A 1e-17 residue must not become "WORSE than baseline".
        result = _assess(_balanced(60, 60, probability=0.5), minimum_n=100)
        assert result.baseline_verdict is BaselineVerdict.EQUAL

    def test_equality_tolerance_is_tight_enough_to_be_meaningless_operationally(self) -> None:
        assert BASELINE_EQUALITY_TOLERANCE < 1e-9

    def test_no_scored_records_leave_the_verdict_undefined(self) -> None:
        result = _assess([])
        assert result.brier_delta is None
        assert result.baseline_verdict is BaselineVerdict.UNDEFINED

    def test_delta_is_model_minus_baseline(self) -> None:
        result = _assess(_balanced(70, 30, probability=0.4), minimum_n=100)
        assert result.summary.brier is not None and result.constant_brier is not None
        assert result.brier_delta == pytest.approx(result.summary.brier - result.constant_brier)

    def test_a_single_class_baseline_is_zero_and_beatable_only_by_certainty(self) -> None:
        # Base rate 1.0 -> constant Brier 0.0. Any p < 1 loses. This is why a
        # good-looking Brier in an all-YES group is the most misleading case.
        result = _assess(_uniform(120, 0.9, BttsOutcome.YES), minimum_n=100)
        assert result.constant_brier == pytest.approx(0.0)
        assert result.baseline_verdict is BaselineVerdict.WORSE
        assert result.status is ReportStatus.AUC_UNDEFINED


# ---------------------------------------------------------------------------
# Published-probability integrity
# ---------------------------------------------------------------------------


class TestPublishedProbabilityIsGraded:
    def test_the_stored_probability_is_used_verbatim(self) -> None:
        # A value no re-derivation would land on.
        p = 0.6172839506172839
        result = _assess([_record(p, BttsOutcome.YES)])
        assert result.summary.brier == (1 - p) ** 2

    def test_assessment_does_not_mutate_its_records(self) -> None:
        records = _balanced(3, 2, probability=0.61)
        before = [r.probability for r in records]
        _assess(records)
        assert [r.probability for r in records] == before

    def test_assessment_does_not_reorder_its_records(self) -> None:
        records = _balanced(3, 2)
        before = [r.event_id for r in records]
        _assess(records)
        assert [r.event_id for r in records] == before

    def test_repeated_assessment_is_identical(self) -> None:
        records = _balanced(60, 60, probability=0.55)
        assert _assess(records, minimum_n=100) == _assess(records, minimum_n=100)

    def test_input_order_does_not_change_the_verdict(self) -> None:
        records = _balanced(60, 40, probability=0.55)
        forward = _assess(records, minimum_n=100)
        backward = _assess(list(reversed(records)), minimum_n=100)
        assert forward.status is backward.status
        assert forward.auc == pytest.approx(backward.auc)
        assert forward.brier_delta == pytest.approx(backward.brier_delta)


class TestAssessReusesTheCallerSummary:
    def test_the_given_summary_is_carried_not_recomputed(self) -> None:
        # `assess` must attach the summary it was handed, so the reported Brier is
        # the one the report already showed - identity, not just equality.
        records = _balanced(60, 60)
        summary = summarise(records, model_id="POISSON_V1", model_version="1.0.0")
        assert assess(records, summary, minimum_n=100).summary is summary

    def test_the_caller_bin_count_is_preserved(self) -> None:
        records = _balanced(60, 60)
        summary = summarise(records, model_id="X", model_version="1", bin_count=4)
        assert len(assess(records, summary, minimum_n=100).summary.calibration) == 4

    def test_model_identity_comes_from_the_summary(self) -> None:
        # Including MIXED, which a group spanning versions legitimately reports.
        records = _balanced(60, 60)
        summary = summarise(records, model_id="MIXED", model_version="MIXED")
        result = assess(records, summary, minimum_n=100)
        assert result.summary.model_version == "MIXED"


class TestImmutability:
    def test_the_assessment_is_frozen(self) -> None:
        result = _assess(_balanced(60, 60), minimum_n=100)
        with pytest.raises(FrozenInstanceError):
            result.status = ReportStatus.REPORTABLE  # type: ignore[misc]

    def test_the_status_cannot_be_overwritten(self) -> None:
        result = _assess([_record(0.9, BttsOutcome.YES)])
        with pytest.raises(FrozenInstanceError):
            result.minimum_n = 1  # type: ignore[misc]


class TestStatusValues:
    def test_every_status_is_serialisable_as_a_string(self) -> None:
        # The JSON artifact writes `.value`; a bare Enum would not serialise.
        for status in ReportStatus:
            assert isinstance(status.value, str)

    def test_only_reportable_licenses_a_claim(self) -> None:
        licensed = {
            status
            for status in ReportStatus
            if _stub(status).is_quality_claim
        }
        assert licensed == {ReportStatus.REPORTABLE}

    def test_headline_is_never_empty(self) -> None:
        for status in ReportStatus:
            assert _stub(status).headline.strip()


def _stub(status: ReportStatus) -> QualityAssessment:
    """A minimal assessment with a chosen status, for exhaustive enum coverage."""
    records = _balanced(1, 1)
    return QualityAssessment(
        summary=summarise(records, model_id="X", model_version="1"),
        scored=2,
        positives=1,
        negatives=1,
        auc=None if status is ReportStatus.AUC_UNDEFINED else 0.5,
        auc_undefined_reason="single-class sample (2 YES, 0 NO)"
        if status is ReportStatus.AUC_UNDEFINED
        else None,
        constant_brier=0.25,
        brier_delta=0.0,
        baseline_verdict=BaselineVerdict.EQUAL,
        status=status,
        minimum_n=MINIMUM_REPORTABLE_N,
    )
