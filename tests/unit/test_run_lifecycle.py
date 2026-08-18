"""
Epic 2H-4 — the operational entry point, end to end on a real filesystem.

Every test here drives the FULL path (settle -> evaluate -> report) against
`tmp_path` with an injected result source, so the whole lifecycle is exercised
with no network and no wall clock.

The four proofs the Epic demands live in this file:
  * reruns append nothing and duplicate nothing   (TestIdempotency)
  * the ledger is byte-for-byte unchanged         (TestLedgerImmutability)
  * stored probabilities are graded verbatim      (TestStoredProbability)
  * conflicts fail loudly rather than resolve     (TestFailureModes)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from helpers.lifecycle_fixtures import (
    empty_result_source,
    failing_result_source,
    historical_match,
    result_source,
)
from helpers.settlement_fixtures import prediction, settlement, unresolved

import run_lifecycle
from domain.settlement import SettlementStatus
from run_lifecycle import (
    EXIT_BACKLOG,
    EXIT_CONFLICT,
    EXIT_OK,
    build_operations_report,
    ledger_digest,
    main,
    run,
    write_operations_report,
)
from settle_predictions import load_settlements

KICKOFF = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
AFTER = KICKOFF + timedelta(hours=6)
DATASET_SOURCE_NAME = "dataset/historical"


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def write_ledger(directory: Path, records: List[Dict[str, Any]], month: str = "2026-08") -> Path:
    """Lay down a ledger month exactly as `prediction_ledger.append_records` does."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{month}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def write_settlements(directory: Path, records: List[Dict[str, Any]], month: str = "2026-08") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{month}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def dirs(tmp_path: Path) -> Dict[str, Path]:
    return {
        "ledger_dir": tmp_path / "predictions",
        "settlement_dir": tmp_path / "settlements",
        "out": tmp_path / "evaluation",
    }


def settlement_lines(settlement_dir: Path) -> List[str]:
    """Raw lines, so an appended duplicate is visible even if it parses equal."""
    lines: List[str] = []
    for path in sorted(settlement_dir.glob("*.jsonl")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return lines


def go(paths: Dict[str, Path], source: Any, *, now: datetime = AFTER, **kwargs: Any) -> Any:
    """One operational pass with the standard offline wiring."""
    return run(
        result_source=source,
        source=DATASET_SOURCE_NAME,
        now=now,
        ledger_dir=paths["ledger_dir"],
        settlement_dir=paths["settlement_dir"],
        **kwargs,
    )


class TestOrderOfOperations:
    """Settlement must precede evaluation, or the metrics describe yesterday."""

    def test_a_fixture_settled_this_run_is_graded_this_run(self, tmp_path: Path) -> None:
        """
        The point of the script. Before this Epic an operator had to run two
        commands in the right order and remember which; getting it wrong reported
        finished matches as pending.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])

        _, _, lifecycle, inputs, join, _ = go(paths, result_source([historical_match()]))

        assert lifecycle.settled == 1
        assert join.scored == 1
        assert inputs[0].settlement_state.value == "SETTLED"

    def test_no_settle_grades_without_contacting_a_source(self, tmp_path: Path) -> None:
        """
        `--no-settle` must be genuinely offline: the source is a function that
        raises, so any contact fails the test rather than silently succeeding.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        write_settlements(paths["settlement_dir"], [settlement("a")])

        def exploding(competition: str, season: Optional[int]) -> Any:
            raise AssertionError("--no-settle must not contact a result source")

        _, _, lifecycle, inputs, _, _ = go(paths, exploding, settle_first=False)
        assert lifecycle.settled == 1
        assert len(inputs) == 1

    def test_dry_run_writes_no_settlement(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        go(paths, result_source([historical_match()]), dry_run=True)
        assert settlement_lines(paths["settlement_dir"]) == []


class TestIdempotency:
    """Requirement 11: a second run must not duplicate anything."""

    def test_a_second_run_appends_no_settlement(self, tmp_path: Path) -> None:
        """
        Idempotence is inherited from `unsettled()`, not re-implemented here. This
        proves the orchestrator does not defeat it.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        source = result_source([historical_match()])

        go(paths, source)
        after_first = settlement_lines(paths["settlement_dir"])
        go(paths, source)
        after_second = settlement_lines(paths["settlement_dir"])

        assert len(after_first) == 1
        assert after_first == after_second

    def test_a_second_run_does_not_double_count_the_evaluation(self, tmp_path: Path) -> None:
        """
        The specific corruption to avoid: one fixture graded twice would halve the
        apparent error of whatever it was, purely because the job ran twice.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a"), prediction("b", fixture_id="740999")])
        source = result_source([historical_match(), historical_match(event_id="740999")])

        go(paths, source)
        _, _, _, first_inputs, first_join, first_summaries = go(paths, source)

        assert first_join.predictions == 2
        assert first_join.scored == 2
        assert len(first_inputs) == 2
        assert sum(s.scored for s in first_summaries.values()) == 2

    def test_the_second_run_asks_the_provider_nothing(self, tmp_path: Path) -> None:
        """
        Terminally settled predictions are not re-fetched. Beyond wasted calls,
        re-asking invites a provider to change its mind about a settled result.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        source = result_source([historical_match()])

        go(paths, source)
        calls_after_first = len(source.calls)
        go(paths, source)
        assert len(source.calls) == calls_after_first

    def test_metrics_are_identical_across_reruns(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        source = result_source([historical_match()])

        _, _, _, _, _, first = go(paths, source)
        _, _, _, _, _, second = go(paths, source)
        assert {k: v.brier for k, v in first.items()} == {k: v.brier for k, v in second.items()}

    def test_a_postponed_fixture_is_retried_and_can_later_settle(self, tmp_path: Path) -> None:
        """
        POSTPONED is deliberately NOT terminal: the match is usually replayed. The
        correction is a NEW line, which is how an append-only log represents a
        change of belief without mutation.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])

        # First pass: the provider has the fixture, unplayed.
        postponed = historical_match(
            home_goals=None, away_goals=None, completed=False, status="STATUS_POSTPONED"
        )
        _, _, first_lifecycle, _, _, _ = go(paths, result_source([postponed]))
        assert first_lifecycle.unresolved == 1

        # Second pass: it has been played.
        _, _, second_lifecycle, _, join, _ = go(paths, result_source([historical_match()]))
        assert second_lifecycle.settled == 1
        assert second_lifecycle.unresolved == 0
        assert join.scored == 1

        # Both beliefs survive on disk. The first line was never edited.
        assert len(settlement_lines(paths["settlement_dir"])) == 2

    def test_an_unreachable_provider_is_retried_not_recorded_as_absent(self, tmp_path: Path) -> None:
        """
        `(None, False)` means "no answer", which must not settle to "no fixture".
        The prediction stays outstanding so the next run asks again.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])

        go(paths, failing_result_source)
        _, _, lifecycle, _, join, _ = go(paths, result_source([historical_match()]))
        assert lifecycle.settled == 1
        assert join.scored == 1


class TestLedgerImmutability:
    """Requirement 10: prove the ledger is untouched, byte for byte."""

    def test_the_ledger_bytes_are_identical_after_a_full_run(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        path = write_ledger(paths["ledger_dir"], [prediction("a"), prediction("b")])
        before = path.read_bytes()

        go(paths, result_source([historical_match()]))

        assert path.read_bytes() == before

    def test_the_digest_is_unchanged_after_a_full_run(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        before = ledger_digest(paths["ledger_dir"])

        go(paths, result_source([historical_match()]))

        assert ledger_digest(paths["ledger_dir"]) == before

    def test_the_stored_probability_is_unchanged_on_disk(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a", probability=0.6172839506172839)])

        go(paths, result_source([historical_match()]))

        line = json.loads((paths["ledger_dir"] / "2026-08.jsonl").read_text().splitlines()[0])
        assert line["probability"] == 0.6172839506172839

    def test_no_file_is_added_to_the_ledger_directory(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        before = sorted(p.name for p in paths["ledger_dir"].iterdir())

        go(paths, result_source([historical_match()]))

        assert sorted(p.name for p in paths["ledger_dir"].iterdir()) == before

    def test_the_digest_notices_an_edited_line(self, tmp_path: Path) -> None:
        """
        A guard that cannot fail is not a guard. This proves the digest actually
        detects tampering rather than always agreeing.
        """
        paths = dirs(tmp_path)
        path = write_ledger(paths["ledger_dir"], [prediction("a", probability=0.55)])
        before = ledger_digest(paths["ledger_dir"])

        path.write_text(
            json.dumps(prediction("a", probability=0.99), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        assert ledger_digest(paths["ledger_dir"]) != before

    def test_the_digest_notices_a_deleted_month(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")], month="2026-07")
        write_ledger(paths["ledger_dir"], [prediction("b")], month="2026-08")
        before = ledger_digest(paths["ledger_dir"])

        (paths["ledger_dir"] / "2026-07.jsonl").unlink()
        assert ledger_digest(paths["ledger_dir"]) != before

    def test_an_absent_ledger_digests_to_a_stable_sentinel(self, tmp_path: Path) -> None:
        assert ledger_digest(tmp_path / "nope") == "sha256:empty"


class TestStoredProbability:
    """Requirement 9 / 4: the graded number comes from storage, verbatim."""

    def test_the_stored_probability_reaches_the_metrics_unchanged(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        # A float chosen to be corrupted by any rounding or re-derivation.
        stored = 0.6172839506172839
        write_ledger(paths["ledger_dir"], [prediction("a", probability=stored)])

        _, _, _, inputs, _, summaries = go(paths, result_source([historical_match()]))

        assert inputs[0].stored_probability == stored
        assert inputs[0].prediction.probability == stored
        # 2-1 is a YES, so brier is (1 - p)^2 for that single observation.
        summary = next(iter(summaries.values()))
        assert summary.brier == (1.0 - stored) ** 2

    def test_the_report_declares_the_ledger_as_the_source(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        _, _, lifecycle, _, join, summaries = go(paths, result_source([historical_match()]))

        payload = build_operations_report(
            lifecycle,
            join,
            summaries,
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[DATASET_SOURCE_NAME],
            settled_now=1,
        )
        assert payload["probability_source"] == "ledger"
        assert payload["replay_used"] is False


class TestReportContents:
    """Requirement 3 / 8: the lifecycle must be legible in the artifact."""

    def test_the_report_separates_every_lifecycle_state(self, tmp_path: Path) -> None:
        """
        The heart of the Epic: five different situations, five different numbers,
        none of them collapsed into each other.
        """
        paths = dirs(tmp_path)
        write_ledger(
            paths["ledger_dir"],
            [
                prediction("settled", fixture_id="1"),
                prediction("postponed", fixture_id="2"),
                prediction("late", fixture_id="3"),
                prediction("future", fixture_id="4", kickoff="2026-08-25T15:00:00+00:00"),
                prediction("undated", fixture_id="5", kickoff=None),
            ],
        )
        write_settlements(
            paths["settlement_dir"],
            [
                settlement("settled", fixture_id="1"),
                unresolved(prediction_id="postponed", fixture_id="2"),
            ],
        )

        _, _, lifecycle, _, join, summaries = go(paths, empty_result_source, settle_first=False)
        payload = build_operations_report(
            lifecycle,
            join,
            summaries,
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[DATASET_SOURCE_NAME],
            settled_now=0,
        )
        block = payload["lifecycle"]
        assert block["discovered"] == 5
        assert block["settled"] == 1
        assert block["unresolved"] == 1       # football
        assert block["awaiting_settlement"] == 1  # our pipeline
        assert block["pending"] == 1
        assert block["undated"] == 1
        assert block["accounted_for"] is True

    def test_the_report_carries_evaluated_and_excluded(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a"), prediction("b", fixture_id="2")])
        write_settlements(paths["settlement_dir"], [settlement("a")])

        _, _, lifecycle, _, join, summaries = go(paths, empty_result_source, settle_first=False)
        payload = build_operations_report(
            lifecycle,
            join,
            summaries,
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[],
            settled_now=0,
        )
        assert payload["join"]["evaluated"] == 1
        assert payload["join"]["excluded_from_evaluation"] == 1

    def test_the_report_records_the_settlement_source(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        go(paths, result_source([historical_match()]))

        sources = run_lifecycle._settlement_sources(load_settlements(paths["settlement_dir"]))
        assert sources == [DATASET_SOURCE_NAME]

    def test_the_report_carries_the_immutability_proof(self, tmp_path: Path) -> None:
        payload = build_operations_report(
            *_empty_inputs(),
            generated_at=AFTER,
            ledger_digest_before="sha256:aaa",
            ledger_digest_after="sha256:aaa",
            settlement_sources=[],
            settled_now=0,
        )
        assert payload["ledger_unchanged"] is True

    def test_a_changed_digest_is_reported_as_changed(self) -> None:
        payload = build_operations_report(
            *_empty_inputs(),
            generated_at=AFTER,
            ledger_digest_before="sha256:aaa",
            ledger_digest_after="sha256:bbb",
            settlement_sources=[],
            settled_now=0,
        )
        assert payload["ledger_unchanged"] is False

    def test_the_report_states_both_join_keys(self) -> None:
        """
        Two different keys for two different questions. Stating both prevents a
        future reader assuming the lifecycle was keyed on the fixture triple.
        """
        payload = build_operations_report(
            *_empty_inputs(),
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[],
            settled_now=0,
        )
        assert payload["lifecycle"]["key"] == ["prediction_id"]
        assert payload["join"]["key"] == ["competition", "season", "fixture_id"]

    def test_no_market_language_reaches_the_artifact(self, tmp_path: Path) -> None:
        """
        LEAK-001. The same firewall `evaluate_settled.py` is held to: an
        evaluation artifact is about probability quality, never betting value.

        Checked on KEY NAME COMPONENTS, matching
        `tests/regression/test_evaluation_leakage.py`, not on the raw JSON text. A
        substring scan reports `ledger_digest_before` as containing "edge", and a
        firewall that cries wolf on a legitimate field is one that gets deleted.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        _, _, lifecycle, _, join, summaries = go(paths, result_source([historical_match()]))
        payload = build_operations_report(
            lifecycle,
            join,
            summaries,
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[DATASET_SOURCE_NAME],
            settled_now=1,
        )
        banned = {"odds", "price", "edge", "stake", "value", "bookmaker", "ev", "roi", "profit"}
        for key in _all_keys(payload):
            parts = set(key.lower().split("_"))
            assert not (parts & banned), f"{key!r} must not appear in an evaluation artifact"


class TestArtifactWriting:
    def test_two_runs_write_two_files(self, tmp_path: Path) -> None:
        """
        Never an overwrite: two runs are two observations of pipeline health, and
        the second does not correct the first.
        """
        payload = build_operations_report(
            *_empty_inputs(),
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[],
            settled_now=0,
        )
        first = write_operations_report(payload, generated_at=AFTER, evaluation_dir=tmp_path)
        second = write_operations_report(payload, generated_at=AFTER, evaluation_dir=tmp_path)
        assert first != second
        assert first.exists() and second.exists()

    def test_the_filename_does_not_collide_with_evaluate_settled(self, tmp_path: Path) -> None:
        payload = build_operations_report(
            *_empty_inputs(),
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[],
            settled_now=0,
        )
        path = write_operations_report(payload, generated_at=AFTER, evaluation_dir=tmp_path)
        assert path.name.startswith("lifecycle_")

    def test_the_artifact_is_valid_json(self, tmp_path: Path) -> None:
        payload = build_operations_report(
            *_empty_inputs(),
            generated_at=AFTER,
            ledger_digest_before="sha256:x",
            ledger_digest_after="sha256:x",
            settlement_sources=[],
            settled_now=0,
        )
        path = write_operations_report(payload, generated_at=AFTER, evaluation_dir=tmp_path)
        assert json.loads(path.read_text())["probability_source"] == "ledger"


class TestFailureModes:
    """Requirement 6: conflicts fail loudly. Nothing is resolved by guessing."""

    def test_a_duplicate_prediction_id_fails_the_run(self, tmp_path: Path, capsys: Any) -> None:
        paths = dirs(tmp_path)
        write_ledger(
            paths["ledger_dir"],
            [prediction("a", probability=0.55), prediction("a", probability=0.61)],
        )
        write_settlements(paths["settlement_dir"], [settlement("a")])

        code = main(_argv(paths, "--no-settle"))
        assert code == EXIT_CONFLICT
        assert "conflicting records" in capsys.readouterr().err.lower()

    def test_contradictory_settlements_fail_the_run(self, tmp_path: Path, capsys: Any) -> None:
        """
        Two sources claiming different scores for one fixture. Every metric would
        inherit whichever was picked, so the run fails instead.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        write_settlements(
            paths["settlement_dir"],
            [settlement("a", home=2, away=1), settlement("a", home=0, away=3)],
        )

        code = main(_argv(paths, "--no-settle"))
        assert code == EXIT_CONFLICT
        assert "settlement" in capsys.readouterr().err.lower()

    def test_an_unresolved_then_settled_progression_is_not_a_conflict(self, tmp_path: Path) -> None:
        """
        The normal life of a postponed fixture that was later played. Treating
        this as a conflict would fail a healthy pipeline every matchweek.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        write_settlements(paths["settlement_dir"], [unresolved(prediction_id="a"), settlement("a")])

        assert main(_argv(paths, "--no-settle")) == EXIT_OK

    def test_fail_on_backlog_is_opt_in(self, tmp_path: Path) -> None:
        """
        Default success: a backlog is normal mid-matchday and a scheduled job
        should not page anyone for it. The strict mode is explicit.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        write_settlements(paths["settlement_dir"], [settlement("b")])

        assert main(_argv(paths, "--no-settle")) == EXIT_OK
        assert main(_argv(paths, "--no-settle", "--fail-on-backlog")) == EXIT_BACKLOG

    def test_a_backlog_warning_names_the_pipeline_not_football(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        main(_argv(paths, "--no-settle"))
        out = capsys.readouterr().out
        assert "Re-run settlement" in out

    def test_an_empty_ledger_is_success_not_failure(self, tmp_path: Path) -> None:
        """
        Nothing to do is not an error. A non-zero exit on the first ever run would
        train an operator to ignore the exit code.
        """
        paths = dirs(tmp_path)
        paths["ledger_dir"].mkdir(parents=True)
        assert main(_argv(paths, "--no-settle")) == EXIT_OK


class TestCli:
    def test_the_cli_writes_an_artifact(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        write_settlements(paths["settlement_dir"], [settlement("a")])

        assert main(_argv(paths, "--no-settle")) == EXIT_OK
        assert len(list(paths["out"].glob("lifecycle_*.json"))) == 1

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])
        write_settlements(paths["settlement_dir"], [settlement("a")])

        main(_argv(paths, "--no-settle", "--dry-run"))
        assert not paths["out"].exists() or list(paths["out"].glob("*.json")) == []

    def test_the_month_filter_applies_to_the_ledger(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("july")], month="2026-07")
        write_ledger(paths["ledger_dir"], [prediction("august")], month="2026-08")
        write_settlements(paths["settlement_dir"], [settlement("july"), settlement("august")])

        _, _, lifecycle, _, _, _ = run(
            result_source=empty_result_source,
            source=DATASET_SOURCE_NAME,
            now=AFTER,
            ledger_dir=paths["ledger_dir"],
            settlement_dir=paths["settlement_dir"],
            month="2026-08",
            settle_first=False,
        )
        assert lifecycle.discovered == 1

    def test_settlements_are_read_in_full_regardless_of_month(self, tmp_path: Path) -> None:
        """
        An August prediction is routinely settled in September. Filtering both
        sides by month would report a real result as still pending.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")], month="2026-08")
        write_settlements(paths["settlement_dir"], [settlement("a")], month="2026-09")

        _, _, lifecycle, _, _, _ = run(
            result_source=empty_result_source,
            source=DATASET_SOURCE_NAME,
            now=AFTER,
            ledger_dir=paths["ledger_dir"],
            settlement_dir=paths["settlement_dir"],
            month="2026-08",
            settle_first=False,
        )
        assert lifecycle.settled == 1

    def test_the_grace_window_is_configurable_from_the_cli(self, tmp_path: Path) -> None:
        paths = dirs(tmp_path)
        # Kickoff far in the past relative to a huge grace window.
        write_ledger(paths["ledger_dir"], [prediction("a")])
        assert main(_argv(paths, "--no-settle", "--grace-hours", "100000", "--fail-on-backlog")) == EXIT_OK


class TestUnresolvedIsNeverManufactured:
    """Requirement 7: no observation is ever invented."""

    def test_a_missing_fixture_does_not_become_a_nil_nil(self, tmp_path: Path) -> None:
        """
        The single worst failure available to a settlement system: fabricating
        0-0 for a fixture the provider never returned would create a fake NO and
        silently bias every metric downward.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])

        settled, _, lifecycle, _, join, _ = go(paths, empty_result_source)

        assert lifecycle.settled == 0
        assert join.scored == 0
        assert all(r.settlement_status is not SettlementStatus.SETTLED for r in settled)
        assert all(r.final_home_goals is None for r in settled)

    def test_a_real_goalless_draw_is_settled_and_scored(self, tmp_path: Path) -> None:
        """
        The mirror image, and why the previous test matters: a genuine 0-0 IS
        evidence and must be graded as a NO.
        """
        paths = dirs(tmp_path)
        write_ledger(paths["ledger_dir"], [prediction("a")])

        _, _, lifecycle, inputs, join, _ = go(
            paths, result_source([historical_match(home_goals=0, away_goals=0)])
        )

        assert lifecycle.settled == 1
        assert join.scored == 1
        assert inputs[0].prediction.outcome.value == "NO"


def _all_keys(payload: Any) -> List[str]:
    """Every key name anywhere in the artifact, nested dicts and lists included."""
    found: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.append(str(key))
            found.extend(_all_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_all_keys(item))
    return found


def _empty_inputs() -> Any:
    """A zero-state (lifecycle, join, summaries) triple for report-shape tests."""
    from domain.evaluation_input import JoinReport
    from domain.lifecycle import LifecycleReport

    return (
        LifecycleReport(discovered=0, by_stage={}),
        JoinReport(
            predictions=0,
            joined=0,
            scored=0,
            settled=0,
            unresolved=0,
            missing_settlement=0,
            unjoinable={},
        ),
        {},
    )


def _argv(paths: Dict[str, Path], *extra: str) -> List[str]:
    return [
        "--ledger-dir",
        str(paths["ledger_dir"]),
        "--settlement-dir",
        str(paths["settlement_dir"]),
        "--out",
        str(paths["out"]),
        *extra,
    ]
