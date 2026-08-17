"""
The settlement job (Epic 2H-2).

Two things are pinned here that the pure contract cannot pin on its own:

  1. **The ledger is never modified.** Settlement reads predictions and writes a
     separate file. A settler that could edit a prediction could rewrite history
     to agree with the result, which would make the whole ledger worthless as
     evidence.

  2. **Every prediction produces a record, including the unresolved ones.** A
     skipped prediction is indistinguishable from one that was never attempted,
     and that difference is exactly what tells an operator whether the job works.

The result source is injected, so none of this touches the network.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from domain.historical import HistoricalMatch
from domain.settlement import SettlementStatus, UnresolvedReason
from settle_predictions import (
    append_settlements,
    build_settlements,
    latest_by_prediction,
    load_settlements,
    main,
    settle,
    settlement_filename,
    unsettled,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
SOURCE = "test/source"


def prediction(prediction_id="pred-1", fixture_id="740123", competition="eng.1", season=2026, **extra):
    record = {
        "prediction_id": prediction_id,
        "fixture_id": fixture_id,
        "competition": competition,
        "season": season,
        "probability": 0.55,
        "status": "SCORED",
    }
    record.update(extra)
    return record


def match(event_id="740123", competition="eng.1", season=2026, home=2, away=1, **overrides):
    fields = {
        "event_id": event_id,
        "competition": competition,
        "season": season,
        "kickoff": KICKOFF,
        "home_team_id": "H",
        "away_team_id": "A",
        "completed": True,
        "home_goals": home,
        "away_goals": away,
        "status": "STATUS_FULL_TIME",
    }
    fields.update(overrides)
    return HistoricalMatch(**fields)


def source_of(*matches, available=True):
    """A result source serving a fixed corpus, indexed by league-season."""

    def source(competition, season):
        if not available:
            return None, False
        return [m for m in matches if m.competition == competition and m.season == season], True

    return source


def write_ledger(tmp_path: Path, *predictions) -> Path:
    """A ledger file in the real format: one JSON object per line."""
    ledger_dir = tmp_path / "predictions"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "2026-08.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for record in predictions:
            handle.write(json.dumps(record) + "\n")
    return ledger_dir


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
class TestMatching:
    def test_a_fixture_is_settled_from_its_event_id(self):
        records = build_settlements(
            [prediction()], result_source=source_of(match()), settled_at=NOW, source=SOURCE
        )
        assert len(records) == 1
        assert records[0].settlement_status is SettlementStatus.SETTLED
        assert (records[0].final_home_goals, records[0].final_away_goals) == (2, 1)

    def test_the_same_event_id_in_another_competition_does_not_settle_it(self):
        """
        DUPLICATE EVENT ID PROTECTION, end to end.

        The corpus holds event 740123 in esp.1. The prediction is for eng.1. A
        bare-id index would settle this with the wrong match and produce a
        confident, wrong result; the composite key reports it not found.
        """
        records = build_settlements(
            [prediction(competition="eng.1")],
            result_source=source_of(match(competition="esp.1")),
            settled_at=NOW,
            source=SOURCE,
        )
        assert records[0].unresolved_reason is UnresolvedReason.FIXTURE_NOT_FOUND

    def test_two_predictions_of_one_fixture_get_two_settlements(self):
        """
        RE-RUN PROTECTION. `new_prediction_id()` is random precisely so a re-run
        is distinguishable rather than duplicated. A settlement keyed on the
        fixture would collapse these two into one and discard that evidence.
        """
        records = build_settlements(
            [prediction("pred-1"), prediction("pred-2")],
            result_source=source_of(match()),
            settled_at=NOW,
            source=SOURCE,
        )
        assert {r.prediction_id for r in records} == {"pred-1", "pred-2"}
        assert all(r.settlement_status is SettlementStatus.SETTLED for r in records)

    def test_an_outage_is_reported_for_the_whole_group(self):
        records = build_settlements(
            [prediction("pred-1"), prediction("pred-2")],
            result_source=source_of(match(), available=False),
            settled_at=NOW,
            source=SOURCE,
        )
        assert len(records) == 2
        assert all(
            r.unresolved_reason is UnresolvedReason.PROVIDER_UNAVAILABLE for r in records
        )

    def test_an_empty_readout_is_not_found_not_an_outage(self):
        """
        An empty corpus is a real answer: ESPN has nothing here. Reporting it as
        an outage would hide a join bug behind an infrastructure excuse.
        """
        records = build_settlements(
            [prediction()], result_source=source_of(), settled_at=NOW, source=SOURCE
        )
        assert records[0].unresolved_reason is UnresolvedReason.FIXTURE_NOT_FOUND

    def test_every_prediction_yields_a_record(self):
        """Unresolved records are WRITTEN, not skipped."""
        predictions = [
            prediction("a", fixture_id="1"),
            prediction("b", fixture_id="2"),
            prediction("c", fixture_id="3"),
        ]
        records = build_settlements(
            predictions,
            result_source=source_of(match(event_id="1")),
            settled_at=NOW,
            source=SOURCE,
        )
        assert len(records) == 3
        assert sum(r.settlement_status is SettlementStatus.SETTLED for r in records) == 1

    def test_one_fetch_per_league_season(self):
        """Grouping is per league-season, not per fixture."""
        calls = []

        def counting_source(competition, season):
            calls.append((competition, season))
            return [match(event_id="1"), match(event_id="2")], True

        build_settlements(
            [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")],
            result_source=counting_source,
            settled_at=NOW,
            source=SOURCE,
        )
        assert calls == [("eng.1", 2026)]


# ---------------------------------------------------------------------------
# The season mismatch, tolerated at the boundary only
# ---------------------------------------------------------------------------
class TestSeasonBoundary:
    def test_a_fixture_filed_under_the_next_season_still_settles(self):
        """
        2H-F3 end to end: the ledger says 2026 (July rollover), ESPN files the
        event under 2027. Without the adjacent-season retry this is a silent
        FIXTURE_NOT_FOUND for an entire league-season.
        """
        records = build_settlements(
            [prediction(season=2026)],
            result_source=source_of(match(season=2027)),
            settled_at=NOW,
            source=SOURCE,
        )
        assert records[0].settlement_status is SettlementStatus.SETTLED
        assert records[0].season == 2026, "the ledger's season is reported unchanged"
        assert records[0].matched_season == 2027, "and the drift is recorded"

    def test_the_prediction_record_on_disk_is_untouched(self, tmp_path):
        """
        The fix is at the lookup boundary. The stored prediction is evidence of
        what was believed at prediction time and is never corrected.
        """
        ledger_dir = write_ledger(tmp_path, prediction(season=2026))
        before = (ledger_dir / "2026-08.jsonl").read_bytes()

        settle(
            result_source=source_of(match(season=2027)),
            source=SOURCE,
            settled_at=NOW,
            ledger_dir=ledger_dir,
            settlement_dir=tmp_path / "settlements",
        )
        assert (ledger_dir / "2026-08.jsonl").read_bytes() == before

    def test_a_mixed_matchday_settles_both_halves(self):
        """
        REGRESSION (found by the 2H-2 end-to-end proof run).

        The first implementation only widened the search when EVERY fixture in a
        league-season was missing. But the real 2H-F3 case is a matchday
        straddling the 1 July rollover: some fixtures are filed under the stored
        season and some under the next. In a mixed group the "all missing" guard
        never fired, so the drifted half reported FIXTURE_NOT_FOUND while the
        rest settled normally - a partial, plausible-looking result that would
        have been read as a provider gap rather than a join bug.

        Both halves must settle.
        """
        corpus = {2026: [match(event_id="1")], 2027: [match(event_id="2", season=2027)]}

        def straddling(competition, season):
            return corpus.get(season, []), True

        records = build_settlements(
            [prediction("a", fixture_id="1", season=2026),
             prediction("b", fixture_id="2", season=2026)],
            result_source=straddling,
            settled_at=NOW,
            source=SOURCE,
        )
        by_id = {r.prediction_id: r for r in records}
        assert by_id["a"].settlement_status is SettlementStatus.SETTLED
        assert by_id["b"].settlement_status is SettlementStatus.SETTLED, (
            "the fixture filed under the next season must still settle"
        )
        assert by_id["a"].matched_season == 2026
        assert by_id["b"].matched_season == 2027

    def test_the_widening_is_bounded_to_two_extra_reads(self):
        """
        Cost control. The retry must be per league-season and stop once every
        fixture is accounted for - never a per-fixture fetch, which would turn
        one matchday into hundreds of requests.
        """
        calls = []

        def counting(competition, season):
            calls.append(season)
            return {2026: [], 2027: [match(event_id="1", season=2027)]}.get(season, []), True

        build_settlements(
            [prediction("a", fixture_id="1", season=2026)],
            result_source=counting,
            settled_at=NOW,
            source=SOURCE,
        )
        assert calls == [2026, 2027], "stops as soon as nothing is missing"

    def test_an_outage_is_not_widened_to_other_seasons(self):
        """
        Widening after an outage would attribute the outage to the wrong season.
        Availability is answered before the season question is re-asked.
        """
        calls = []

        def failing(competition, season):
            calls.append((competition, season))
            return None, False

        records = build_settlements(
            [prediction()], result_source=failing, settled_at=NOW, source=SOURCE
        )
        assert calls == [("eng.1", 2026)], "no adjacent-season retry after an outage"
        assert records[0].unresolved_reason is UnresolvedReason.PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Storage: append-only, idempotent
# ---------------------------------------------------------------------------
class TestStorage:
    def test_settlements_are_appended_never_overwritten(self, tmp_path):
        out = tmp_path / "settlements"
        first = build_settlements(
            [prediction("a")], result_source=source_of(match()), settled_at=NOW, source=SOURCE
        )
        second = build_settlements(
            [prediction("b")], result_source=source_of(match()), settled_at=NOW, source=SOURCE
        )
        append_settlements(first, settled_at=NOW, settlement_dir=out)
        append_settlements(second, settled_at=NOW, settlement_dir=out)

        lines = (out / settlement_filename(NOW)).read_text().strip().split("\n")
        assert len(lines) == 2
        assert {json.loads(line)["prediction_id"] for line in lines} == {"a", "b"}

    def test_each_line_is_valid_json_on_its_own(self, tmp_path):
        """A missing trailing newline would corrupt both records on append."""
        out = tmp_path / "settlements"
        records = build_settlements(
            [prediction("a"), prediction("b", fixture_id="99")],
            result_source=source_of(match()),
            settled_at=NOW,
            source=SOURCE,
        )
        append_settlements(records, settled_at=NOW, settlement_dir=out)
        for line in (out / settlement_filename(NOW)).read_text().strip().split("\n"):
            json.loads(line)

    def test_a_settled_prediction_is_not_settled_again(self, tmp_path):
        """Idempotence: a second pass over the same ledger does no new work."""
        ledger_dir = write_ledger(tmp_path, prediction())
        out = tmp_path / "settlements"
        source = source_of(match())

        first = settle(
            result_source=source, source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        second = settle(
            result_source=source, source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        assert len(first) == 1
        assert second == [], "an already-settled prediction is not re-settled"
        assert len(load_settlements(out)) == 1

    def test_an_unresolved_prediction_is_retried(self, tmp_path):
        """
        NOT_YET_PLAYED must be revisited - that is the normal path from
        "predicted" to "settled".
        """
        ledger_dir = write_ledger(tmp_path, prediction())
        out = tmp_path / "settlements"

        settle(
            result_source=source_of(), source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        second = settle(
            result_source=source_of(match()), source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        assert len(second) == 1
        assert second[0].settlement_status is SettlementStatus.SETTLED

    def test_a_cancelled_prediction_is_not_retried(self, tmp_path):
        """
        A cancelled fixture never acquires a result at that event id, so an
        unbounded retry is unbounded work with a guaranteed answer.
        """
        ledger_dir = write_ledger(tmp_path, prediction())
        out = tmp_path / "settlements"
        cancelled = source_of(
            match(status="STATUS_CANCELED", completed=False, home=None, away=None)
        )

        settle(
            result_source=cancelled, source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        second = settle(
            result_source=cancelled, source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        assert second == []

    def test_a_correction_is_a_new_line_and_the_latest_wins(self, tmp_path):
        """
        Never mutate. The fact that we once believed otherwise is worth keeping,
        so a correction appends and the reader resolves to the latest.
        """
        out = tmp_path / "settlements"
        stale = build_settlements(
            [prediction("a")], result_source=source_of(), settled_at=NOW, source=SOURCE
        )
        fresh = build_settlements(
            [prediction("a")], result_source=source_of(match()), settled_at=NOW, source=SOURCE
        )
        append_settlements(stale, settled_at=NOW, settlement_dir=out)
        append_settlements(fresh, settled_at=NOW, settlement_dir=out)

        all_lines = load_settlements(out)
        assert len(all_lines) == 2, "both beliefs are kept"
        latest = latest_by_prediction(all_lines)["a"]
        assert latest["settlement_status"] == "SETTLED"

    def test_a_dry_run_writes_nothing(self, tmp_path):
        ledger_dir = write_ledger(tmp_path, prediction())
        out = tmp_path / "settlements"
        records = settle(
            result_source=source_of(match()), source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out, dry_run=True,
        )
        assert len(records) == 1
        assert not out.exists()

    def test_an_empty_ledger_produces_nothing(self, tmp_path):
        records = settle(
            result_source=source_of(match()), source=SOURCE, settled_at=NOW,
            ledger_dir=tmp_path / "missing", settlement_dir=tmp_path / "settlements",
        )
        assert records == []


# ---------------------------------------------------------------------------
# The ledger is read-only
# ---------------------------------------------------------------------------
class TestTheLedgerIsNeverModified:
    def test_the_ledger_bytes_are_unchanged_by_a_full_pass(self, tmp_path):
        ledger_dir = write_ledger(
            tmp_path,
            prediction("a", fixture_id="1"),
            prediction("b", fixture_id="2"),
            prediction("c", fixture_id="3", competition="esp.1"),
        )
        path = ledger_dir / "2026-08.jsonl"
        before = path.read_bytes()

        settle(
            result_source=source_of(match(event_id="1"), match(event_id="2")),
            source=SOURCE,
            settled_at=NOW,
            ledger_dir=ledger_dir,
            settlement_dir=tmp_path / "settlements",
        )
        assert path.read_bytes() == before

    def test_the_stored_probability_is_never_read_or_rewritten(self, tmp_path):
        """
        Settlement records what happened. The probability stays in the ledger
        untouched, and no settlement field carries it.
        """
        ledger_dir = write_ledger(tmp_path, prediction(probability=0.6123456789))
        out = tmp_path / "settlements"

        settle(
            result_source=source_of(match()), source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=out,
        )
        stored = json.loads((ledger_dir / "2026-08.jsonl").read_text().strip())
        assert stored["probability"] == 0.6123456789

        settlement = load_settlements(out)[0]
        assert "probability" not in settlement

    def test_no_settlement_file_lands_in_the_ledger_directory(self, tmp_path):
        ledger_dir = write_ledger(tmp_path, prediction())
        settle(
            result_source=source_of(match()), source=SOURCE, settled_at=NOW,
            ledger_dir=ledger_dir, settlement_dir=tmp_path / "settlements",
        )
        assert [p.name for p in ledger_dir.iterdir()] == ["2026-08.jsonl"]

    def test_a_refused_prediction_is_still_settled(self, tmp_path):
        """
        A prediction the model refused to score is still a fixture that happened.
        Settling it is what makes coverage measurable: NO_TEAM_STATS on a match
        that finished 2-1 is a missed opportunity, and only settlement shows it.
        """
        records = build_settlements(
            [prediction(status="NO_TEAM_STATS", probability=None)],
            result_source=source_of(match()),
            settled_at=NOW,
            source=SOURCE,
        )
        assert records[0].settlement_status is SettlementStatus.SETTLED


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------
class TestUnsettled:
    def test_a_prediction_with_no_settlement_is_pending(self):
        assert len(unsettled([prediction()], [])) == 1

    def test_selection_is_by_prediction_id_not_fixture_id(self):
        """
        Two predictions of one fixture: settling one must not silently mark the
        other done.
        """
        done = [{"prediction_id": "pred-1", "settlement_status": "SETTLED"}]
        todo = unsettled([prediction("pred-1"), prediction("pred-2")], done)
        assert [p["prediction_id"] for p in todo] == ["pred-2"]

    def test_a_record_without_a_prediction_id_is_skipped(self):
        """No join key, no settlement. Inventing one would fabricate identity."""
        assert unsettled([{"fixture_id": "1", "competition": "eng.1"}], []) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCli:
    def test_a_dry_run_over_an_empty_ledger_succeeds(self, tmp_path, capsys):
        code = main(
            [
                "--ledger-dir", str(tmp_path / "predictions"),
                "--out", str(tmp_path / "settlements"),
                "--dry-run",
            ]
        )
        assert code == 0
        assert "Nothing to settle" in capsys.readouterr().out

    def test_the_dataset_flag_does_not_touch_the_network(self, tmp_path, capsys):
        """
        `--dataset` settles from a local corpus. An empty dataset directory is a
        real, empty answer and must not raise.
        """
        dataset = tmp_path / "historical"
        dataset.mkdir()
        code = main(
            [
                "--ledger-dir", str(tmp_path / "predictions"),
                "--out", str(tmp_path / "settlements"),
                "--dataset", str(dataset),
                "--dry-run",
            ]
        )
        assert code == 0
        capsys.readouterr()
