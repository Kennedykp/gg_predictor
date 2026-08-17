"""
The append-only ledger writer (Epic 2G).

The ledger's value rests entirely on one property: what it wrote yesterday is
still there today, unchanged. These tests attack that property directly - re-runs,
partial failures, malformed rows - because a log that quietly loses or rewrites
records is worse than no log, having the appearance of evidence without the
substance.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import prediction_ledger
from domain.prediction_log import LivePredictionStatus
from prediction_ledger import (
    DEFAULT_LEDGER_DIR,
    CaptureReport,
    build_records,
    code_revision,
    ledger_filename,
    load_records,
    record_predictions,
)

NOW = datetime(2026, 8, 17, 6, 30, tzinfo=timezone.utc)
TARGET = date(2026, 8, 17)


def scored_result(fixture_id="740123", **overrides):
    """A complete result as `main.process_fixture` returns one."""
    result = {
        "fixture_id": fixture_id,
        "datetime": "2026-08-17T14:00Z",
        "league_id": "eng.1",
        "league_name": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": "359",
        "away_team_id": "363",
        "lambda_home": 1.62,
        "lambda_away": 1.31,
        "gg_probability": 0.6234,
        "odds": 1.85,
        "implied_probability": 0.5405,
        "edge": 0.0829,
        "passes_filters": True,
        "filter_outcome": "PASSED",
        "decision": "BET",
        "rejection_reasons": [],
        "model_input_samples": {"home": 12, "away": 11, "league": 240},
    }
    result.update(overrides)
    return result


def refused_result(fixture_id="740999"):
    return {
        "fixture_id": fixture_id,
        "datetime": "2026-08-17T16:30Z",
        "league_id": "esp.1",
        "league_name": "La Liga",
        "home_team": "Getafe",
        "away_team": "Cadiz",
        "home_team_id": "2922",
        "away_team_id": "3842",
        "lambda_home": None,
        "lambda_away": None,
        "gg_probability": None,
        "odds": None,
        "implied_probability": None,
        "edge": None,
        "passes_filters": False,
        "decision": "NO BET",
        "rejection_reasons": [
            "Point-in-time model inputs unavailable: home_goals_scored_home"
        ],
        "model_input_samples": {"home": 0, "away": 3, "league": 40},
    }


@pytest.fixture
def ledger_dir(tmp_path):
    return tmp_path / "predictions"


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------
class TestAppendOnly:
    def test_a_second_run_does_not_overwrite_the_first(self, ledger_dir):
        """
        THE point of the module. `output_{date}.json` is overwritten by the next
        run of the same date, which is how every prediction has been lost so far.
        Two runs must leave two records.
        """
        first = record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )
        line_one = first.path.read_text(encoding="utf-8").splitlines()[0]

        record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )

        lines = first.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0] == line_one, "the first run's record was modified"

    def test_a_re_run_is_distinguishable_not_duplicated(self, ledger_dir):
        """
        Both records survive with different `prediction_id`s and different
        `run_id`s, so the pair reads as two observations rather than one
        duplicated row.
        """
        record_predictions([scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW)
        record_predictions([scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW)

        rows = load_records(ledger_dir)
        assert len({row["prediction_id"] for row in rows}) == 2
        assert len({row["run_id"] for row in rows}) == 2
        assert len({row["fixture_id"] for row in rows}) == 1

    def test_every_open_call_appends_or_reads(self):
        """
        Structural, not behavioural: no `open` call in this module may truncate.

        Checked by parsing the AST rather than scanning for `"w"` in the text. A
        substring scan reads comments and docstrings too, so it would fail on
        prose describing the rule while still passing a real truncating write
        hidden behind a variable - precisely backwards. The AST sees only code.
        """
        import ast

        source = Path(prediction_ledger.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        modes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", None
            )
            if name != "open":
                continue

            # `handle.open(mode)` puts mode first; builtin `open(path, mode)` second.
            positional = 0 if isinstance(node.func, ast.Attribute) else 1
            mode = None
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    mode = keyword.value
            if mode is None and len(node.args) > positional:
                mode = node.args[positional]

            assert isinstance(mode, ast.Constant), (
                "every open() in the ledger must state a literal mode; a computed "
                "mode cannot be audited"
            )
            modes.append(mode.value)

        assert modes, "no open() call found - has the writer moved?"
        for mode in modes:
            assert mode[0] in {"a", "r"}, f"truncating open mode {mode!r} in the ledger"
            assert "+" not in mode, f"read-write mode {mode!r} can overwrite records"


    def test_no_delete_or_update_function_exists(self):
        """
        A ledger with an edit function is not a ledger. Absence is the guarantee.
        """
        names = set(dir(prediction_ledger))
        for forbidden in ("delete_records", "update_record", "rewrite", "purge", "truncate"):
            assert forbidden not in names

    def test_appending_a_second_month_leaves_the_first_alone(self, ledger_dir):
        august = record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )
        september = record_predictions(
            [scored_result("999")],
            date(2026, 9, 1),
            ledger_dir=ledger_dir,
            created_at=datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc),
        )
        assert august.path != september.path
        assert len(august.path.read_text(encoding="utf-8").splitlines()) == 1


# ---------------------------------------------------------------------------
# Capture cannot change a prediction
# ---------------------------------------------------------------------------
class TestCaptureIsReadOnly:
    def test_the_results_list_is_untouched(self, ledger_dir):
        """
        The guarantee that makes this safe to run in production. Compared by deep
        copy, so a mutated nested `model_input_samples` would also be caught.
        """
        import copy

        results = [scored_result(), refused_result()]
        before = copy.deepcopy(results)

        record_predictions(results, TARGET, ledger_dir=ledger_dir, created_at=NOW)

        assert results == before

    def test_capture_does_not_reorder_results(self, ledger_dir):
        results = [scored_result("1"), scored_result("2"), scored_result("3")]
        record_predictions(results, TARGET, ledger_dir=ledger_dir, created_at=NOW)
        assert [r["fixture_id"] for r in results] == ["1", "2", "3"]

    def test_records_are_written_in_the_order_predicted(self, ledger_dir):
        """
        Storage order is append order. A reader that wants another order sorts
        explicitly; the file itself stays a faithful log of what happened when.
        """
        results = [scored_result("3"), scored_result("1"), scored_result("2")]
        record_predictions(results, TARGET, ledger_dir=ledger_dir, created_at=NOW)
        assert [row["fixture_id"] for row in load_records(ledger_dir)] == ["3", "1", "2"]


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------
class TestOneBadRowDoesNotCostTheBatch:
    def test_a_malformed_result_is_skipped_and_reported(self, ledger_dir):
        """
        One fixture missing `fixture_id` should lose one record, not nineteen -
        and must be reported, because a silent skip is indistinguishable from a
        day with fewer fixtures.
        """
        results = [scored_result("1"), {"league_id": "eng.1"}, scored_result("2")]

        report = record_predictions(results, TARGET, ledger_dir=ledger_dir, created_at=NOW)

        assert report.written == 2
        assert len(report.skipped) == 1
        assert report.ok is False
        assert [row["fixture_id"] for row in load_records(ledger_dir)] == ["1", "2"]

    def test_a_contradictory_result_is_skipped_not_stored(self, ledger_dir):
        """
        A probability beside a refusal status is a contradiction. The contract
        rejects it, and the ledger declines to store it rather than recording a
        row that cannot be true.
        """
        broken = scored_result("bad")
        broken["gg_probability"] = 5.0  # out of range

        report = record_predictions([broken], TARGET, ledger_dir=ledger_dir, created_at=NOW)

        assert report.written == 0
        assert len(report.skipped) == 1

    def test_an_empty_run_writes_nothing(self, ledger_dir):
        """
        A zero-fixture day is a real outcome the system accepts gracefully. It
        must not create an empty file that later looks like a failed write.
        """
        report = record_predictions([], TARGET, ledger_dir=ledger_dir, created_at=NOW)
        assert report.written == 0
        assert report.path is None
        assert not ledger_dir.exists()


# ---------------------------------------------------------------------------
# What is recorded
# ---------------------------------------------------------------------------
class TestRecordedContent:
    def test_a_recommendation_is_recorded_with_its_price(self, ledger_dir):
        """
        The price is what makes a recommendation auditable later. Without it,
        "was this a good bet?" is unanswerable even with the result known.
        """
        record_predictions([scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW)
        row = load_records(ledger_dir)[0]

        assert row["recommendation"] == "BET"
        assert row["odds"]["price"] == 1.85
        assert row["odds"]["edge"] == 0.0829
        assert row["odds"]["provenance"] == "PARTIAL_NO_BOOKMAKER"

    def test_a_refused_fixture_is_recorded_with_a_named_status(self, ledger_dir):
        record_predictions([refused_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW)
        row = load_records(ledger_dir)[0]

        assert row["status"] == LivePredictionStatus.NO_POINT_IN_TIME_INPUTS.value
        assert row["probability"] is None
        assert row["rejection_reasons"]

    def test_both_scored_and_refused_fixtures_are_kept(self, ledger_dir):
        """
        Coverage is only measurable if refusals are recorded. Storing just the
        scored rows would make a broken provider look like a quiet matchday.
        """
        report = record_predictions(
            [scored_result(), refused_result()],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        assert report.written == 2
        assert {row["status"] for row in load_records(ledger_dir)} == {
            "SCORED",
            "NO_POINT_IN_TIME_INPUTS",
        }

    def test_the_filter_verdict_is_recorded_as_three_valued(self, ledger_dir):
        record_predictions(
            [scored_result(filter_outcome="UNEVALUATED", decision="NO BET")],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        assert load_records(ledger_dir)[0]["filter_outcome"] == "UNEVALUATED"

    def test_provenance_is_attached_to_every_record(self, ledger_dir):
        record_predictions(
            [scored_result("1"), scored_result("2")],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        for row in load_records(ledger_dir):
            assert row["provenance"]["config_fingerprint"]
            assert row["provenance"]["model_version"]
            assert row["schema_version"]

    def test_one_run_shares_one_run_id(self, ledger_dir):
        report = record_predictions(
            [scored_result("1"), scored_result("2"), refused_result()],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        run_ids = {row["run_id"] for row in load_records(ledger_dir)}
        assert run_ids == {report.run_id}

    def test_each_prediction_has_its_own_id(self, ledger_dir):
        record_predictions(
            [scored_result("1"), scored_result("2")],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        rows = load_records(ledger_dir)
        assert rows[0]["prediction_id"] != rows[1]["prediction_id"]

    def test_the_season_is_resolved_for_settlement(self, ledger_dir):
        """
        Settlement needs the season the fixture belongs to, and `resolve_season`
        is the single authority (a July rollover, not a calendar year).
        """
        record_predictions([scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW)
        assert load_records(ledger_dir)[0]["season"] == 2026


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------
class TestStorageLayout:
    def test_the_file_is_named_for_the_creation_month(self):
        assert ledger_filename(NOW) == "2026-08.jsonl"

    def test_the_month_is_computed_in_utc(self):
        """
        A 23:30 local prediction on 31 August must not be filed under September
        because the writer read a local clock. Normalised to UTC first.
        """
        from datetime import timedelta

        local = datetime(2026, 8, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert ledger_filename(local) == "2026-09.jsonl"  # 04:30Z on 1 September

    def test_the_directory_is_created_on_demand(self, ledger_dir):
        assert not ledger_dir.exists()
        record_predictions([scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW)
        assert ledger_dir.exists()

    def test_the_default_location_is_outside_source(self):
        """
        `data/` is gitignored: operational evidence, not source. Writing into a
        package directory would put a growing log into the code history.
        """
        assert DEFAULT_LEDGER_DIR == Path("data/predictions")

    def test_every_line_is_valid_json(self, ledger_dir):
        report = record_predictions(
            [scored_result("1"), refused_result()],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        for line in report.path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)

    def test_the_file_ends_with_a_newline(self, ledger_dir):
        """
        Otherwise the next append lands on the same line and corrupts both
        records - the classic JSONL append bug.
        """
        report = record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )
        assert report.path.read_text(encoding="utf-8").endswith("\n")

    def test_reading_an_absent_ledger_is_not_an_error(self, tmp_path):
        assert load_records(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------
class TestCodeRevision:
    def test_it_returns_a_revision_or_none_but_never_a_guess(self):
        revision = code_revision()
        assert revision is None or isinstance(revision, str)
        if revision:
            assert revision.replace("-dirty", "").strip()

    def test_a_missing_git_degrades_to_none(self, monkeypatch):
        """
        Provenance is best-effort. An unknown revision recorded as unknown is
        useful; a crashed run is not.
        """
        def explode(*args, **kwargs):
            raise OSError("git not found")

        monkeypatch.setattr(prediction_ledger.subprocess, "run", explode)
        assert code_revision() is None

    def test_a_failing_git_degrades_to_none(self, monkeypatch):
        class Failed:
            returncode = 128
            stdout = ""

        monkeypatch.setattr(prediction_ledger.subprocess, "run", lambda *a, **k: Failed())
        assert code_revision() is None

    def test_capture_still_works_without_git(self, monkeypatch, ledger_dir):
        monkeypatch.setattr(prediction_ledger, "code_revision", lambda: None)
        report = record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )
        assert report.written == 1
        assert load_records(ledger_dir)[0]["provenance"]["code_revision"] is None


class TestCaptureReport:
    def test_a_clean_capture_is_ok(self, ledger_dir):
        report = record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )
        assert report.ok is True
        assert report.written == 1

    def test_the_summary_names_the_file(self, ledger_dir):
        report = record_predictions(
            [scored_result()], TARGET, ledger_dir=ledger_dir, created_at=NOW
        )
        assert "1 prediction" in report.summary()
        assert "2026-08.jsonl" in report.summary()

    def test_the_summary_reports_skips(self, ledger_dir):
        report = record_predictions(
            [scored_result(), {"league_id": "x"}],
            TARGET,
            ledger_dir=ledger_dir,
            created_at=NOW,
        )
        assert "1 skipped" in report.summary()

    def test_an_empty_summary_says_nothing_was_written(self):
        assert "nothing written" in CaptureReport(run_id="r").summary()


class TestBuildRecordsIsPure:
    def test_no_file_is_created(self, tmp_path, monkeypatch):
        """
        Separating construction from writing is what lets every field be tested
        without touching a disk - and keeps the only writer in one function.
        """
        monkeypatch.chdir(tmp_path)
        from domain.prediction_log import build_provenance

        records, skipped = build_records(
            [scored_result()],
            run_id="r",
            created_at=NOW,
            provenance=build_provenance(),
        )
        assert len(records) == 1 and not skipped
        assert list(tmp_path.iterdir()) == []
