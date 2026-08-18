"""
Epic 2H-5 — tests for the reporting entry point.

Every test drives the real CLI against `tmp_path`. No network (blocked by the
autouse fixture in `conftest.py`), no wall-clock dependence in any assertion, and
no file written outside the temporary directory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from helpers.settlement_fixtures import prediction, settlement, unresolved

from domain.reporting import Dimension
from report_evaluation import (
    EXIT_CONFLICT,
    EXIT_NO_DATA,
    EXIT_OK,
    build_report,
    main,
    report,
    write_report,
)

GENERATED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _write(path: Path, records: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _gg(home: int, away: int) -> str:
    return "YES" if home > 0 and away > 0 else "NO"


def _setup(
    tmp_path: Path,
    predictions: Sequence[Dict[str, Any]],
    settlements: Sequence[Dict[str, Any]],
    month: str = "2026-08",
) -> Tuple[Path, Path]:
    ledger_dir = tmp_path / "ledger"
    settlement_dir = tmp_path / "settlements"
    _write(ledger_dir / f"{month}.jsonl", predictions)
    _write(settlement_dir / f"{month}.jsonl", settlements)
    return ledger_dir, settlement_dir


def _pair(
    prediction_id: str,
    fixture_id: str,
    *,
    competition: str = "eng.1",
    season: Optional[int] = 2026,
    probability: float = 0.55,
    home: int = 2,
    away: int = 1,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return (
        prediction(
            prediction_id=prediction_id,
            fixture_id=fixture_id,
            competition=competition,
            season=season,
            probability=probability,
        ),
        settlement(
            prediction_id=prediction_id,
            fixture_id=fixture_id,
            competition=competition,
            season=season,
            home=home,
            away=away,
            outcome=_gg(home, away),
        ),
    )


def _cli(tmp_path: Path, ledger_dir: Path, settlement_dir: Path, *extra: str) -> int:
    return main(
        [
            "--ledger-dir",
            str(ledger_dir),
            "--settlement-dir",
            str(settlement_dir),
            "--out",
            str(tmp_path / "out"),
            *extra,
        ]
    )


def _artifact(tmp_path: Path) -> Dict[str, Any]:
    files = sorted((tmp_path / "out").glob("report_*.json"))
    assert len(files) == 1, f"expected one artifact, found {[f.name for f in files]}"
    return json.loads(files[0].read_text(encoding="utf-8"))


class TestEmptyRun:
    def test_no_data_exits_ok(self, tmp_path: Path) -> None:
        # Silence before the first settled matchday is normal, not a failure.
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        assert _cli(tmp_path, ledger_dir, settlement_dir) == EXIT_OK

    def test_empty_run_still_writes_an_artifact(self, tmp_path: Path) -> None:
        # "We looked and there was nothing" is a result worth keeping.
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert _artifact(tmp_path)["join"]["predictions"] == 0

    def test_fail_on_empty_is_opt_in(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        assert _cli(tmp_path, ledger_dir, settlement_dir, "--fail-on-empty") == EXIT_NO_DATA

    def test_missing_directories_do_not_crash(self, tmp_path: Path) -> None:
        assert (
            _cli(tmp_path, tmp_path / "absent-ledger", tmp_path / "absent-settlements") == EXIT_OK
        )


class TestSingleSettledRun:
    def test_exits_ok(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        assert _cli(tmp_path, ledger_dir, settlement_dir) == EXIT_OK

    def test_reports_the_stored_probability(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001", probability=0.55, home=2, away=1)
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        _cli(tmp_path, ledger_dir, settlement_dir)
        overall = _artifact(tmp_path)["breakdowns"]["overall"][0]
        assert overall["metrics"]["brier"] == pytest.approx((1 - 0.55) ** 2)

    def test_awkward_probability_survives_the_json_round_trip(self, tmp_path: Path) -> None:
        # Guards against a format string or rounding entering the artifact.
        p = 0.6172839506172839
        pred, settle = _pair("p-1", "740001", probability=p, home=1, away=1)
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        _cli(tmp_path, ledger_dir, settlement_dir)
        overall = _artifact(tmp_path)["breakdowns"]["overall"][0]
        assert overall["metrics"]["brier"] == (1 - p) ** 2

    def test_join_block_counts_the_prediction(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        _cli(tmp_path, ledger_dir, settlement_dir)
        join = _artifact(tmp_path)["join"]
        assert (join["predictions"], join["joined"], join["evaluated"]) == (1, 1, 1)


class TestBreakdownArtifact:
    def _multi(self, tmp_path: Path) -> Tuple[Path, Path]:
        pairs = [
            _pair("p-1", "740001", competition="eng.1", probability=0.9, home=2, away=1),
            _pair("p-2", "740002", competition="esp.1", probability=0.9, home=2, away=0),
            _pair("p-3", "740003", competition="esp.1", season=2025, probability=0.9, home=1, away=0),
        ]
        return _setup(tmp_path, [p for p, _ in pairs], [s for _, s in pairs])

    def test_default_dimensions_are_present(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert _artifact(tmp_path)["dimensions"] == ["overall", "model", "competition", "season"]

    def test_competition_rows_are_sorted(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        rows = _artifact(tmp_path)["breakdowns"]["competition"]
        assert [row["label"] for row in rows] == ["eng.1", "esp.1"]

    def test_season_rows_are_sorted(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        rows = _artifact(tmp_path)["breakdowns"]["season"]
        assert [row["label"] for row in rows] == ["2025", "2026"]

    def test_a_selected_dimension_replaces_the_defaults(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir, "--dimension", "competition")
        assert _artifact(tmp_path)["dimensions"] == ["competition"]

    def test_competition_season_is_available_on_request(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir, "--dimension", "competition_season")
        rows = _artifact(tmp_path)["breakdowns"]["competition_season"]
        assert [row["label"] for row in rows] == ["eng.1 / 2026", "esp.1 / 2025", "esp.1 / 2026"]

    def test_per_group_totals_sum_to_the_prediction_count(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        rows = _artifact(tmp_path)["breakdowns"]["competition"]
        assert sum(row["counts"]["total"] for row in rows) == 3

    def test_every_group_is_accounted_for(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        breakdowns = _artifact(tmp_path)["breakdowns"]
        assert all(
            row["counts"]["accounted_for"] for rows in breakdowns.values() for row in rows
        )

    def test_the_breakdown_separates_what_the_average_hides(self, tmp_path: Path) -> None:
        """The finding this Epic exists to make visible."""
        ledger_dir, settlement_dir = self._multi(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        payload = _artifact(tmp_path)
        rows = {row["label"]: row for row in payload["breakdowns"]["competition"]}
        overall = payload["breakdowns"]["overall"][0]["metrics"]["brier"]
        assert rows["eng.1"]["metrics"]["brier"] == pytest.approx(0.01)
        assert rows["esp.1"]["metrics"]["brier"] == pytest.approx(0.81)
        assert rows["eng.1"]["metrics"]["brier"] < overall < rows["esp.1"]["metrics"]["brier"]


class TestMixedStates:
    def _mixed(self, tmp_path: Path) -> Tuple[Path, Path]:
        pred_ok, settle_ok = _pair("p-1", "740001")
        return _setup(
            tmp_path,
            [
                pred_ok,
                prediction(prediction_id="p-2", fixture_id="740002"),
                prediction(prediction_id="p-3", fixture_id="740003"),
            ],
            [settle_ok, unresolved(prediction_id="p-2", fixture_id="740002")],
        )

    def test_states_are_reported_separately(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._mixed(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        counts = _artifact(tmp_path)["breakdowns"]["overall"][0]["counts"]
        assert (counts["settled"], counts["unresolved"], counts["missing"]) == (1, 1, 1)

    def test_join_block_distinguishes_unresolved_from_missing(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._mixed(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        join = _artifact(tmp_path)["join"]
        assert join["unresolved"] == 1
        assert join["missing_settlement"] == 1

    def test_only_the_settled_prediction_is_evaluated(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = self._mixed(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert _artifact(tmp_path)["join"]["evaluated"] == 1

    def test_excluded_count_is_explicit(self, tmp_path: Path) -> None:
        # Stated rather than left to be inferred by subtraction.
        ledger_dir, settlement_dir = self._mixed(tmp_path)
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert _artifact(tmp_path)["join"]["excluded_from_evaluation"] == 2


class TestConflicts:
    def test_contradictory_settlements_exit_nonzero(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001", home=2, away=1)
        contradiction = settlement(
            prediction_id="p-1", fixture_id="740001", home=0, away=0, outcome="NO"
        )
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle, contradiction])
        assert _cli(tmp_path, ledger_dir, settlement_dir) == EXIT_CONFLICT

    def test_a_conflicting_run_writes_no_artifact(self, tmp_path: Path) -> None:
        # Nothing misleading is left behind for someone to find and trust.
        pred, settle = _pair("p-1", "740001", home=2, away=1)
        contradiction = settlement(
            prediction_id="p-1", fixture_id="740001", home=0, away=0, outcome="NO"
        )
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle, contradiction])
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert list((tmp_path / "out").glob("report_*.json")) == []

    def test_an_identical_repeated_settlement_is_not_a_conflict(self, tmp_path: Path) -> None:
        # A re-run appending the same result must not fail the report.
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle, dict(settle)])
        assert _cli(tmp_path, ledger_dir, settlement_dir) == EXIT_OK


class TestDeterminism:
    def test_identical_inputs_produce_identical_reports(self, tmp_path: Path) -> None:
        pairs = [_pair("p-1", "740001"), _pair("p-2", "740002", competition="esp.1")]
        ledger_dir, settlement_dir = _setup(
            tmp_path, [p for p, _ in pairs], [s for _, s in pairs]
        )
        inputs, join, breakdowns = report(
            ledger_dir=ledger_dir, settlement_dir=settlement_dir
        )
        first = build_report(
            inputs,
            join,
            breakdowns,
            generated_at=GENERATED_AT,
            ledger_dir=ledger_dir,
            settlement_dir=settlement_dir,
            month=None,
            bin_count=10,
        )
        inputs2, join2, breakdowns2 = report(
            ledger_dir=ledger_dir, settlement_dir=settlement_dir
        )
        second = build_report(
            inputs2,
            join2,
            breakdowns2,
            generated_at=GENERATED_AT,
            ledger_dir=ledger_dir,
            settlement_dir=settlement_dir,
            month=None,
            bin_count=10,
        )
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_generated_at_is_the_only_time_dependent_field(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        inputs, join, breakdowns = report(
            ledger_dir=ledger_dir, settlement_dir=settlement_dir
        )
        kwargs: Dict[str, Any] = dict(
            ledger_dir=ledger_dir, settlement_dir=settlement_dir, month=None, bin_count=10
        )
        early = build_report(inputs, join, breakdowns, generated_at=GENERATED_AT, **kwargs)
        later = build_report(
            inputs,
            join,
            breakdowns,
            generated_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            **kwargs,
        )
        assert early.pop("generated_at") != later.pop("generated_at")
        assert json.dumps(early, sort_keys=True) == json.dumps(later, sort_keys=True)

    def test_generated_at_is_stamped_in_utc(self, tmp_path: Path) -> None:
        inputs, join, breakdowns = report(
            ledger_dir=tmp_path / "none", settlement_dir=tmp_path / "none"
        )
        payload = build_report(
            inputs,
            join,
            breakdowns,
            generated_at=GENERATED_AT,
            ledger_dir=tmp_path,
            settlement_dir=tmp_path,
            month=None,
            bin_count=10,
        )
        assert payload["generated_at"] == "2026-08-20T12:00:00+00:00"


class TestIdempotency:
    def test_running_twice_does_not_change_the_ledger(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        ledger_file = ledger_dir / "2026-08.jsonl"
        before = hashlib.sha256(ledger_file.read_bytes()).hexdigest()
        _cli(tmp_path, ledger_dir, settlement_dir)
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert hashlib.sha256(ledger_file.read_bytes()).hexdigest() == before

    def test_running_twice_does_not_change_the_settlements(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        settlement_file = settlement_dir / "2026-08.jsonl"
        before = hashlib.sha256(settlement_file.read_bytes()).hexdigest()
        _cli(tmp_path, ledger_dir, settlement_dir)
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert hashlib.sha256(settlement_file.read_bytes()).hexdigest() == before

    def test_metrics_are_identical_on_a_rerun(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001", probability=0.6172839506172839, home=1, away=1)
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        briers: List[Optional[float]] = []
        for _ in range(2):
            _, _, breakdowns = report(ledger_dir=ledger_dir, settlement_dir=settlement_dir)
            briers.append(breakdowns[Dimension.OVERALL][0].summary.brier)
        assert briers[0] == briers[1]

    def test_a_second_report_does_not_overwrite_the_first(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        inputs, join, breakdowns = report(
            ledger_dir=ledger_dir, settlement_dir=settlement_dir
        )
        payload = build_report(
            inputs,
            join,
            breakdowns,
            generated_at=GENERATED_AT,
            ledger_dir=ledger_dir,
            settlement_dir=settlement_dir,
            month=None,
            bin_count=10,
        )
        out = tmp_path / "out"
        first = write_report(payload, generated_at=GENERATED_AT, evaluation_dir=out)
        second = write_report(payload, generated_at=GENERATED_AT, evaluation_dir=out)
        assert first != second
        assert first.exists() and second.exists()

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        assert _cli(tmp_path, ledger_dir, settlement_dir, "--dry-run") == EXIT_OK
        assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


class TestProvenanceAndSchema:
    def test_artifact_states_the_probability_source(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        _cli(tmp_path, ledger_dir, settlement_dir)
        payload = _artifact(tmp_path)
        assert payload["probability_source"] == "ledger"
        assert payload["replay_used"] is False

    def test_artifact_stamps_every_schema_version(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        _cli(tmp_path, ledger_dir, settlement_dir)
        payload = _artifact(tmp_path)
        for key in (
            "schema_version",
            "reporting_schema_version",
            "evaluation_schema_version",
            "evaluation_input_schema_version",
        ):
            assert payload[key], f"{key} missing"

    def test_artifact_records_the_join_key(self, tmp_path: Path) -> None:
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert _artifact(tmp_path)["join"]["key"] == ["competition", "season", "fixture_id"]

    def test_artifact_records_its_inputs(self, tmp_path: Path) -> None:
        # Which directories produced these numbers is not recoverable later.
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        _cli(tmp_path, ledger_dir, settlement_dir, "--month", "2026-08")
        inputs = _artifact(tmp_path)["inputs"]
        assert inputs["ledger_dir"] == str(ledger_dir)
        assert inputs["month"] == "2026-08"

    def test_report_filename_is_prefixed(self, tmp_path: Path) -> None:
        # Must not collide with evaluate_settled or lifecycle artifacts.
        ledger_dir, settlement_dir = _setup(tmp_path, [], [])
        _cli(tmp_path, ledger_dir, settlement_dir)
        assert sorted((tmp_path / "out").glob("report_*.json"))[0].name.startswith("report_")

    def test_bins_flag_reaches_the_calibration_table(self, tmp_path: Path) -> None:
        pred, settle = _pair("p-1", "740001")
        ledger_dir, settlement_dir = _setup(tmp_path, [pred], [settle])
        _cli(tmp_path, ledger_dir, settlement_dir, "--bins", "4")
        overall = _artifact(tmp_path)["breakdowns"]["overall"][0]
        assert len(overall["metrics"]["calibration"]) == 4
