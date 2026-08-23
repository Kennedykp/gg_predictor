"""
Epic 2J — the quality gate as it reaches a human.

`test_quality.py` proves the gate classifies correctly and
`test_quality_gates_isolation.py` proves it cannot reach the model. This file
covers the last stretch, which is where the original defect actually lived: the
JSON artifact and the console lines a person reads.

The distinction matters. A correct assessment that the renderer drops is a report
that still presents a bare Brier, and every test in the other two files would
pass. So these assertions are deliberately about OUTPUT — captured stdout and
serialised keys — not about the objects behind it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest
from helpers.settlement_fixtures import prediction, settlement, unresolved

from domain.evaluation_input import EvaluationInput, join_for_evaluation
from domain.quality import MINIMUM_REPORTABLE_N, ReportStatus
from domain.reporting import Dimension, summarise_dimension, summarise_dimensions
from report_evaluation import _group_dict, _print, _quality_dict, build_report

GENERATED_AT = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


def _joined(
    predictions: Sequence[Dict[str, Any]],
    settlements: Sequence[Dict[str, Any]],
) -> List[EvaluationInput]:
    inputs, _join = join_for_evaluation(list(predictions), list(settlements))
    return inputs


def _pair(
    index: int,
    *,
    competition: str = "eng.1",
    season: int = 2026,
    probability: float = 0.55,
    home: int = 2,
    away: int = 1,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """One prediction and its settlement, consistent by construction."""
    pid = f"p-{competition}-{season}-{index}"
    fid = f"74{index:04d}"
    return (
        prediction(
            prediction_id=pid,
            fixture_id=fid,
            competition=competition,
            season=season,
            probability=probability,
        ),
        settlement(
            prediction_id=pid,
            fixture_id=fid,
            competition=competition,
            season=season,
            home=home,
            away=away,
            outcome="YES" if home > 0 and away > 0 else "NO",
        ),
    )


def _population(
    n_yes: int,
    n_no: int,
    *,
    competition: str = "eng.1",
    season: int = 2026,
    probability: float = 0.55,
    start: int = 0,
) -> List[EvaluationInput]:
    predictions: List[Dict[str, Any]] = []
    settlements: List[Dict[str, Any]] = []
    for offset in range(n_yes):
        p, s = _pair(
            start + offset,
            competition=competition,
            season=season,
            probability=probability,
            home=2,
            away=1,
        )
        predictions.append(p)
        settlements.append(s)
    for offset in range(n_no):
        p, s = _pair(
            start + n_yes + offset,
            competition=competition,
            season=season,
            probability=probability,
            home=2,
            away=0,
        )
        predictions.append(p)
        settlements.append(s)
    return _joined(predictions, settlements)


class TestJsonArtifact:
    def test_every_group_carries_a_quality_block(self) -> None:
        payload = _group_dict(summarise_dimension(_population(1, 0), Dimension.OVERALL)[0])
        assert "quality" in payload

    def test_the_quality_block_states_all_eight_required_facts(self) -> None:
        # The list from the Epic brief: n, Brier, log loss, AUC, constant-predictor
        # Brier, delta, calibration and coverage/status must all be recoverable.
        group = summarise_dimension(_population(60, 60), Dimension.OVERALL)[0]
        payload = _group_dict(group)
        quality = payload["quality"]
        assert quality["scored"] == 120
        assert quality["brier"] is not None
        assert payload["metrics"]["log_loss"] is not None
        assert quality["auc"] is not None
        assert quality["constant_predictor_brier"] is not None
        assert quality["brier_delta_vs_baseline"] is not None
        assert payload["metrics"]["calibration"]
        assert quality["status"] == ReportStatus.REPORTABLE.value

    def test_the_block_is_json_serialisable(self) -> None:
        # Enums must be emitted as values; a bare Enum would raise here.
        group = summarise_dimension(_population(60, 60), Dimension.OVERALL)[0]
        round_tripped = json.loads(json.dumps(_group_dict(group)))
        assert isinstance(round_tripped["quality"]["status"], str)
        assert isinstance(round_tripped["quality"]["baseline_verdict"], str)

    def test_a_flat_probability_loses_to_the_base_rate(self) -> None:
        """
        The finding this whole Epic is built to surface, arithmetically.

        A constant p=0.55 over a 50/50 population scores
        (0.45**2 + 0.55**2)/2 = 0.2525, while the base-rate predictor scores 0.25.
        The model is WORSE — and a report showing only "brier 0.2525" would read
        as unremarkable rather than as a failure to beat a coin.
        """
        payload = _group_dict(summarise_dimension(_population(60, 60), Dimension.OVERALL)[0])
        quality = payload["quality"]
        assert quality["brier"] == pytest.approx(0.2525)
        assert quality["constant_predictor_brier"] == pytest.approx(0.25)
        assert quality["brier_delta_vs_baseline"] == pytest.approx(0.0025)
        assert quality["baseline_verdict"] == "WORSE"


    def test_a_gated_group_says_so_in_the_artifact(self) -> None:
        payload = _group_dict(summarise_dimension(_population(1, 0), Dimension.OVERALL)[0])
        assert payload["quality"]["status"] == ReportStatus.INSUFFICIENT_SAMPLE.value
        assert payload["quality"]["is_quality_claim"] is False
        assert payload["quality"]["shortfall"] == MINIMUM_REPORTABLE_N - 1

    def test_a_gated_group_keeps_its_metrics(self) -> None:
        # Auditable, not erased.
        payload = _group_dict(summarise_dimension(_population(1, 0), Dimension.OVERALL)[0])
        assert payload["metrics"]["brier"] is not None
        assert payload["quality"]["brier"] is not None

    def test_the_quality_brier_matches_the_metrics_brier(self) -> None:
        # One computation, two views. A divergence here would mean the gate is
        # judging a different number than the report displays.
        payload = _group_dict(summarise_dimension(_population(60, 60), Dimension.OVERALL)[0])
        assert payload["quality"]["brier"] == payload["metrics"]["brier"]

    def test_undefined_auc_ships_its_reason_not_a_number(self) -> None:
        payload = _group_dict(summarise_dimension(_population(120, 0), Dimension.OVERALL)[0])
        assert payload["quality"]["auc"] is None
        assert "single-class" in payload["quality"]["auc_undefined_reason"]

    def test_the_report_stamps_the_policy_it_applied(self) -> None:
        inputs = _population(1, 0)
        breakdowns = summarise_dimensions(inputs, [Dimension.OVERALL])
        _inputs, join = join_for_evaluation([], [])
        report = build_report(
            inputs,
            join,
            breakdowns,
            generated_at=GENERATED_AT,
            ledger_dir=Path("data/predictions"),
            settlement_dir=Path("data/settlements"),
            month=None,
            bin_count=10,
        )
        assert report["inputs"]["minimum_reportable_n"] == MINIMUM_REPORTABLE_N
        assert report["quality_schema_version"] == "2j.1"
        assert report["schema_version"] == "2j.1"

    def test_the_artifact_still_declares_its_probability_source(self) -> None:
        # 2H-4's guarantee must survive the 2J change untouched.
        _inputs, join = join_for_evaluation([], [])
        report = build_report(
            [],
            join,
            {},
            generated_at=GENERATED_AT,
            ledger_dir=Path("data/predictions"),
            settlement_dir=Path("data/settlements"),
            month=None,
            bin_count=10,
        )
        assert report["probability_source"] == "ledger"
        assert report["replay_used"] is False


class TestConsoleOutput:
    def _rendered(self, inputs: Sequence[EvaluationInput], capsys: Any) -> str:
        _joined_inputs, join = join_for_evaluation([], [])
        _print(join, summarise_dimensions(list(inputs), [Dimension.OVERALL]))
        return capsys.readouterr().out

    def test_a_gated_brier_never_appears_without_its_qualifier(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE 2G-R5 TEST, at the surface where the defect was seen.
        out = self._rendered(_population(1, 0, probability=0.9), capsys)
        assert "brier" in out
        assert ReportStatus.INSUFFICIENT_SAMPLE.value in out

    def test_the_sample_size_is_printed_beside_the_score(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._rendered(_population(1, 0, probability=0.9), capsys)
        assert "scored" in out
        assert "1" in out

    def test_the_baseline_is_printed_with_its_direction_named(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._rendered(_population(60, 60), capsys)
        assert "baseline" in out
        assert "than baseline" in out

    def test_auc_is_printed_when_defined(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = self._rendered(_population(60, 60), capsys)
        assert "auc" in out

    def test_undefined_auc_prints_the_reason_not_a_bare_na(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._rendered(_population(120, 0), capsys)
        assert "auc undefined" in out
        assert "single-class" in out

    def test_class_counts_are_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = self._rendered(_population(70, 50), capsys)
        assert "70 YES" in out
        assert "50 NO" in out

    def test_the_policy_threshold_is_announced_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._rendered(_population(1, 0), capsys)
        assert f"minimum reportable n: {MINIMUM_REPORTABLE_N}" in out

    def test_a_reportable_group_carries_no_warning_marker(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._rendered(_population(60, 60), capsys)
        assert ReportStatus.INSUFFICIENT_SAMPLE.value not in out
        assert ReportStatus.AUC_UNDEFINED.value not in out

    def test_an_unsettled_group_reads_as_operational_not_poor(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = _joined(
            [prediction(prediction_id="p-1", fixture_id="740001")],
            [unresolved(prediction_id="p-1", fixture_id="740001")],
        )
        out = self._rendered(inputs, capsys)
        assert "not yet measurable" in out
        assert "brier" not in out

    def test_nothing_at_all_prints_a_placeholder_not_a_number(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = self._rendered([], capsys)
        assert "(nothing to report)" in out


class TestPerDimensionGating:
    """
    The gate is applied per group, which is where slicing does its damage.

    A season with enough settled fixtures to clear the threshold overall breaks
    into per-competition groups that do not. Each must be judged on its own
    evidence, or the breakdown reintroduces exactly the defect the overall row
    was fixed for.
    """

    def _two_uneven_competitions(self) -> List[EvaluationInput]:
        return _population(60, 60, competition="eng.1") + _population(
            1, 0, competition="esp.1", start=500
        )

    def test_the_overall_row_can_be_reportable(self) -> None:
        group = summarise_dimension(self._two_uneven_competitions(), Dimension.OVERALL)[0]
        assert group.quality.scored == 121
        assert group.is_quality_claim is True

    def test_while_a_thin_competition_inside_it_is_not(self) -> None:
        groups = {
            g.label: g
            for g in summarise_dimension(self._two_uneven_competitions(), Dimension.COMPETITION)
        }
        assert groups["eng.1"].is_quality_claim is True
        assert groups["esp.1"].is_quality_claim is False
        assert groups["esp.1"].status is ReportStatus.INSUFFICIENT_SAMPLE

    def test_clearing_the_gate_overall_does_not_license_the_slices(self) -> None:
        # The inverse claim is the dangerous one: "the season is fine, so each
        # league is fine".
        overall = summarise_dimension(self._two_uneven_competitions(), Dimension.OVERALL)[0]
        thin = {
            g.label: g
            for g in summarise_dimension(self._two_uneven_competitions(), Dimension.COMPETITION)
        }["esp.1"]
        assert overall.is_quality_claim is True
        assert thin.is_quality_claim is False

    def test_each_group_baseline_uses_only_its_own_observations(self) -> None:
        # A per-competition baseline computed from the global base rate would be a
        # different, easier comparison.
        groups = {
            g.label: g
            for g in summarise_dimension(self._two_uneven_competitions(), Dimension.COMPETITION)
        }
        assert groups["esp.1"].quality.positives == 1
        assert groups["esp.1"].quality.negatives == 0
        assert groups["esp.1"].quality.constant_brier == pytest.approx(0.0)

    def test_every_dimension_is_gated(self) -> None:
        breakdowns = summarise_dimensions(
            _population(1, 0),
            [Dimension.OVERALL, Dimension.MODEL, Dimension.COMPETITION, Dimension.SEASON],
        )
        for groups in breakdowns.values():
            for group in groups:
                assert group.is_quality_claim is False

    def test_a_tighter_threshold_can_be_requested(self) -> None:
        # Callers may tighten the policy; the default is never loosened silently.
        group = summarise_dimension(_population(60, 60), Dimension.OVERALL, minimum_n=500)[0]
        assert group.status is ReportStatus.INSUFFICIENT_SAMPLE

    def test_quality_dict_is_stable_for_the_same_group(self) -> None:
        group = summarise_dimension(_population(60, 60), Dimension.OVERALL)[0]
        assert _quality_dict(group.quality) == _quality_dict(group.quality)
