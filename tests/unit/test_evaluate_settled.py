"""
The evaluation runner (Epic 2H-3).

Pins the things the pure adapter cannot: that the runner READS both logs and
writes to neither, that it groups by the stored model version, and that the
artifact it produces states where its probabilities came from.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from helpers.settlement_fixtures import prediction, settlement, unresolved

from evaluate_settled import (
    build_report,
    evaluate,
    group_by_model,
    main,
    summarise_by_model,
    write_report,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def write_jsonl(directory: Path, name: str, rows) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def setup_dirs(tmp_path, predictions, settlements):
    ledger = tmp_path / "predictions"
    settled = tmp_path / "settlements"
    write_jsonl(ledger, "2026-08.jsonl", predictions)
    write_jsonl(settled, "2026-08.jsonl", settlements)
    return ledger, settled


def all_keys(payload):
    """
    Every key name anywhere in a nested structure.

    Keys rather than raw text, because the firewall is about what a field IS, not
    which letters appear in a value: "ledger" contains "edge".
    """
    found = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))
            found |= all_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= all_keys(item)
    return found


class TestEvaluate:
    def test_a_settled_prediction_is_graded_from_the_stored_probability(self, tmp_path):
        ledger, settled = setup_dirs(
            tmp_path, [prediction(probability=0.25)], [settlement(outcome="YES")]
        )
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)

        assert report.settled == 1
        summary = summaries[("POISSON_V1", "1.0.0")]
        # (0.25 - 1)^2 = 0.5625, computed from the ledger's number.
        assert summary.brier == 0.5625
        assert summary.mean_predicted == 0.25

    def test_unresolved_predictions_are_excluded_from_scoring(self, tmp_path):
        ledger, settled = setup_dirs(
            tmp_path,
            [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")],
            [settlement("a", fixture_id="1"), unresolved("b", fixture_id="2")],
        )
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)

        summary = summaries[("POISSON_V1", "1.0.0")]
        assert summary.scored == 1
        assert summary.targets == 2
        assert summary.coverage == 0.5
        assert report.unresolved == 1

    def test_models_are_never_pooled(self, tmp_path):
        """
        Two versions in one Brier score describe neither, and the pooled figure
        moves with the traffic mix rather than with the model.
        """
        ledger, settled = setup_dirs(
            tmp_path,
            [
                prediction("a", fixture_id="1"),
                prediction(
                    "b", fixture_id="2",
                    provenance={"model_id": "POISSON_V1", "model_version": "2.0.0"},
                ),
            ],
            [settlement("a", fixture_id="1"), settlement("b", fixture_id="2")],
        )
        _, _, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        assert set(summaries) == {("POISSON_V1", "1.0.0"), ("POISSON_V1", "2.0.0")}
        assert all(s.scored == 1 for s in summaries.values())

    def test_settlements_are_read_across_all_months(self, tmp_path):
        """
        A prediction made in August is often settled in September. Filtering both
        sides by one month would report a real result as awaiting settlement.
        """
        ledger = tmp_path / "predictions"
        settled = tmp_path / "settlements"
        write_jsonl(ledger, "2026-08.jsonl", [prediction()])
        write_jsonl(settled, "2026-09.jsonl", [settlement()])

        _, report, _ = evaluate(ledger_dir=ledger, settlement_dir=settled, month="2026-08")
        assert report.settled == 1

    def test_an_empty_ledger_produces_no_summaries(self, tmp_path):
        _, report, summaries = evaluate(
            ledger_dir=tmp_path / "none", settlement_dir=tmp_path / "nothing"
        )
        assert report.predictions == 0
        assert summaries == {}

    def test_a_prediction_with_no_settlement_yet_is_reported_not_dropped(self, tmp_path):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [])
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        assert report.missing_settlement == 1
        assert summaries[("POISSON_V1", "1.0.0")].targets == 1
        assert summaries[("POISSON_V1", "1.0.0")].brier is None


class TestNeitherLogIsWritten:
    def test_the_ledger_bytes_are_unchanged(self, tmp_path):
        ledger, settled = setup_dirs(
            tmp_path,
            [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")],
            [settlement("a", fixture_id="1")],
        )
        before = (ledger / "2026-08.jsonl").read_bytes()
        evaluate(ledger_dir=ledger, settlement_dir=settled)
        assert (ledger / "2026-08.jsonl").read_bytes() == before

    def test_the_settlement_bytes_are_unchanged(self, tmp_path):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        before = (settled / "2026-08.jsonl").read_bytes()
        evaluate(ledger_dir=ledger, settlement_dir=settled)
        assert (settled / "2026-08.jsonl").read_bytes() == before

    def test_no_file_is_added_to_either_directory(self, tmp_path):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        evaluate(ledger_dir=ledger, settlement_dir=settled)
        assert [p.name for p in ledger.iterdir()] == ["2026-08.jsonl"]
        assert [p.name for p in settled.iterdir()] == ["2026-08.jsonl"]

    def test_the_report_goes_somewhere_else_entirely(self, tmp_path):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        out = tmp_path / "evaluation"
        code = main(
            [
                "--ledger-dir", str(ledger),
                "--settlement-dir", str(settled),
                "--out", str(out),
            ]
        )
        assert code == 0
        assert list(out.glob("evaluation_*.json"))
        assert [p.name for p in ledger.iterdir()] == ["2026-08.jsonl"]


class TestTheArtifact:
    def test_it_states_that_the_probability_came_from_the_ledger(self, tmp_path):
        """
        A reader months from now must be able to tell a ledger-graded report from
        a replayed one without reading the code.
        """
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        payload = build_report(report, summaries, generated_at=NOW)

        assert payload["probability_source"] == "ledger"
        assert payload["replay_used"] is False

    def test_it_records_the_join_key(self, tmp_path):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        payload = build_report(report, summaries, generated_at=NOW)
        assert payload["join"]["key"] == ["competition", "season", "fixture_id"]

    def test_it_carries_no_price_or_edge_anywhere(self, tmp_path):
        """
        LEAK-001 applied to this Epic's artifact.

        Checked on the KEY NAMES, split into components, exactly as
        `test_evaluation_leakage.py` checks the harness artifacts. A raw substring
        scan of the JSON text is the obvious approach and it is wrong: "ledger"
        contains "edge", so `probability_source: "ledger"` would fail a check that
        is supposed to be about prices. Component matching is what makes the
        assertion mean what it says.
        """
        ledger, settled = setup_dirs(
            tmp_path,
            [prediction(odds={"provenance": "PARTIAL_NO_BOOKMAKER", "price": 1.85, "edge": 0.07})],
            [settlement()],
        )
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        payload = build_report(report, summaries, generated_at=NOW)

        banned = {"odds", "price", "edge", "stake", "bookmaker", "ev", "roi", "profit"}
        offending = {
            key for key in all_keys(payload)
            for part in key.lower().split("_")
            if part in banned
        }
        assert not offending, f"{sorted(offending)} leaked into an evaluation artifact"

    def test_no_price_value_reaches_the_artifact(self, tmp_path):
        """
        The companion to the key check: the stored PRICE itself must not appear.

        Pinned on the exact number written into the ledger record, so a leak that
        renamed the field would still be caught.
        """
        ledger, settled = setup_dirs(
            tmp_path,
            [prediction(odds={"provenance": "PARTIAL_NO_BOOKMAKER", "price": 1.85, "edge": 0.07})],
            [settlement()],
        )
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        text = json.dumps(build_report(report, summaries, generated_at=NOW))
        assert "1.85" not in text
        assert "0.07" not in text

    def test_a_report_is_never_overwritten(self, tmp_path):
        """
        Two evaluations at different times are two observations.
        `output_{date}.json` overwriting in place is what Epic 2G exists to stop.
        """
        out = tmp_path / "evaluation"
        first = write_report({"a": 1}, generated_at=NOW, evaluation_dir=out)
        second = write_report(
            {"a": 2},
            generated_at=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
            evaluation_dir=out,
        )
        assert first != second
        assert len(list(out.glob("*.json"))) == 2

    def test_the_report_is_valid_json_and_round_trips(self, tmp_path):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        _, report, summaries = evaluate(ledger_dir=ledger, settlement_dir=settled)
        path = write_report(
            build_report(report, summaries, generated_at=NOW),
            generated_at=NOW,
            evaluation_dir=tmp_path / "evaluation",
        )
        payload = json.loads(path.read_text())
        assert payload["models"][0]["model_id"] == "POISSON_V1"


class TestGrouping:
    def test_grouping_uses_the_stored_version_not_todays_constant(self, tmp_path):
        from domain.evaluation_input import join_for_evaluation

        inputs, _ = join_for_evaluation(
            [prediction(provenance={"model_id": "OLD", "model_version": "0.1.0"})],
            [settlement()],
        )
        assert set(group_by_model(inputs)) == {("OLD", "0.1.0")}

    def test_summaries_are_keyed_by_model_and_version(self, tmp_path):
        from domain.evaluation_input import join_for_evaluation

        inputs, _ = join_for_evaluation([prediction()], [settlement()])
        summaries = summarise_by_model(inputs)
        assert summaries[("POISSON_V1", "1.0.0")].model_version == "1.0.0"


class TestCli:
    def test_a_dry_run_writes_nothing(self, tmp_path, capsys):
        ledger, settled = setup_dirs(tmp_path, [prediction()], [settlement()])
        out = tmp_path / "evaluation"
        code = main(
            [
                "--ledger-dir", str(ledger),
                "--settlement-dir", str(settled),
                "--out", str(out),
                "--dry-run",
            ]
        )
        assert code == 0
        assert not out.exists()
        assert "DRY RUN" in capsys.readouterr().out

    def test_an_empty_ledger_succeeds(self, tmp_path, capsys):
        code = main(
            [
                "--ledger-dir", str(tmp_path / "none"),
                "--settlement-dir", str(tmp_path / "nothing"),
                "--out", str(tmp_path / "evaluation"),
            ]
        )
        assert code == 0
        assert "No predictions to evaluate" in capsys.readouterr().out

    def test_conflicting_settlements_fail_the_run(self, tmp_path, capsys):
        """
        Two sources describing one fixture differently is not a state to publish
        metrics from - every number would inherit the choice.
        """
        ledger, settled = setup_dirs(
            tmp_path,
            [prediction()],
            [
                settlement(home=2, away=1, settled_at="2026-08-17T12:00:00+00:00"),
                settlement(home=0, away=0, outcome="NO", settled_at="2026-08-18T12:00:00+00:00"),
            ],
        )
        code = main(
            [
                "--ledger-dir", str(ledger),
                "--settlement-dir", str(settled),
                "--out", str(tmp_path / "evaluation"),
                "--dry-run",
            ]
        )
        assert code == 1
        assert "CONFLICTING" in capsys.readouterr().out

    def test_a_zero_brier_is_printed_not_hidden(self, tmp_path, capsys):
        """
        GG-007 in the presentation layer: `if summary.brier:` would print a
        perfect score as "not available".
        """
        ledger, settled = setup_dirs(
            tmp_path, [prediction(probability=1.0)], [settlement(outcome="YES")]
        )
        main(
            [
                "--ledger-dir", str(ledger),
                "--settlement-dir", str(settled),
                "--out", str(tmp_path / "evaluation"),
                "--dry-run",
            ]
        )
        assert "brier       0.0000" in capsys.readouterr().out
