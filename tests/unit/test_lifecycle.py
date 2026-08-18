"""
Epic 2H-4 — the pure lifecycle contract.

The distinction under test throughout is between a fact about FOOTBALL (a match
was postponed) and a fact about OUR PIPELINE (a finished match has no result
recorded). `domain/evaluation_input.py` reports both as `missing_settlement`;
these tests pin the split, because the two demand opposite responses - wait
versus investigate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from helpers.settlement_fixtures import prediction, settlement, unresolved

from domain.lifecycle import (
    DEFAULT_SETTLEMENT_GRACE,
    LifecycleReport,
    Stage,
    ledger_conflicts,
    reconcile,
    stage_of,
)

KICKOFF = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
BEFORE = KICKOFF - timedelta(hours=2)
DURING = KICKOFF + timedelta(minutes=45)
JUST_AFTER = KICKOFF + timedelta(hours=2, minutes=59)
LONG_AFTER = KICKOFF + timedelta(hours=6)


class TestStageOf:
    """One prediction, one clock reading, one answer."""

    def test_a_settled_record_is_settled(self) -> None:
        assert stage_of(prediction(), settlement(), now=LONG_AFTER) is Stage.SETTLED

    def test_a_postponed_record_is_unresolved(self) -> None:
        assert stage_of(prediction(), unresolved("POSTPONED"), now=LONG_AFTER) is Stage.UNRESOLVED

    def test_before_kickoff_is_awaiting_kickoff(self) -> None:
        assert stage_of(prediction(), None, now=BEFORE) is Stage.AWAITING_KICKOFF

    def test_during_the_match_is_in_play(self) -> None:
        assert stage_of(prediction(), None, now=DURING) is Stage.IN_PLAY

    def test_inside_the_grace_window_is_still_in_play(self) -> None:
        """
        The boundary matters: at 2h59 a final score may legitimately not be
        published yet, so this must not be reported as a fault.
        """
        assert stage_of(prediction(), None, now=JUST_AFTER) is Stage.IN_PLAY

    def test_past_the_grace_window_is_awaiting_settlement(self) -> None:
        """The one operational alarm: finished long ago, no result recorded."""
        assert stage_of(prediction(), None, now=LONG_AFTER) is Stage.AWAITING_SETTLEMENT

    def test_the_grace_boundary_is_exclusive(self) -> None:
        exactly = KICKOFF + DEFAULT_SETTLEMENT_GRACE
        assert stage_of(prediction(), None, now=exactly) is Stage.AWAITING_SETTLEMENT

    def test_a_custom_grace_is_honoured(self) -> None:
        assert (
            stage_of(prediction(), None, now=LONG_AFTER, grace=timedelta(hours=12))
            is Stage.IN_PLAY
        )

    def test_a_settlement_record_beats_the_clock(self) -> None:
        """
        A stored result is a statement of fact and is never re-judged against the
        clock. Settled before kickoff would be strange data, but it is still what
        settlement recorded, and this module does not get to overrule it.
        """
        assert stage_of(prediction(), settlement(), now=BEFORE) is Stage.SETTLED

    def test_a_missing_kickoff_is_undated_not_pending(self) -> None:
        """
        A malformed kickoff is a data-quality problem. Folding it into a benign
        "pending" would hide it forever, since pending is expected to be large.
        """
        assert stage_of(prediction(kickoff=None), None, now=LONG_AFTER) is Stage.UNDATED

    def test_a_naive_kickoff_is_undated(self) -> None:
        """
        GG-014. A naive timestamp cannot be compared to an aware `now`, and
        assuming UTC would misplace a late kickoff by a day. Refused, not guessed.
        """
        record = prediction(kickoff="2026-08-15T15:00:00")
        assert stage_of(record, None, now=LONG_AFTER) is Stage.UNDATED

    def test_an_unparseable_kickoff_is_undated(self) -> None:
        assert stage_of(prediction(kickoff="not-a-date"), None, now=LONG_AFTER) is Stage.UNDATED

    def test_any_non_settled_status_is_unresolved(self) -> None:
        """
        Whatever new unresolved reason a future provider brings, it is football's
        business and never an operational gap.
        """
        for reason in ("POSTPONED", "CANCELLED", "ABANDONED", "FIXTURE_NOT_FOUND", "NO_RESULT"):
            assert stage_of(prediction(), unresolved(reason), now=LONG_AFTER) is Stage.UNRESOLVED

    def test_the_score_is_never_re_derived(self) -> None:
        """
        A settled 0-0 stays SETTLED. This module reads `settlement_status` and
        never looks at the goals, so it cannot disagree with `domain/settlement.py`
        about what a goalless draw means.
        """
        goalless = settlement(home=0, away=0, outcome="NO")
        assert stage_of(prediction(), goalless, now=LONG_AFTER) is Stage.SETTLED


class TestReconcile:
    """Whole-ledger reconciliation."""

    def test_every_prediction_lands_in_exactly_one_stage(self) -> None:
        predictions = [
            prediction("a"),
            prediction("b"),
            prediction("c", kickoff=None),
        ]
        rows, report = reconcile(predictions, [settlement("a")], now=LONG_AFTER)
        assert len(rows) == 3
        assert report.discovered == 3
        assert report.accounted_for is True
        assert sum(report.by_stage.values()) == 3

    def test_the_discovered_count_is_the_ledger_size(self) -> None:
        """
        The denominator can only be the number of records read. A shrinking
        denominator would make a broken pipeline look like an improving model.
        """
        predictions = [prediction(f"p{i}") for i in range(7)]
        _, report = reconcile(predictions, [], now=LONG_AFTER)
        assert report.discovered == 7

    def test_settlements_are_matched_by_prediction_id(self) -> None:
        """
        Not by fixture id. Two predictions of one fixture are two independent
        things to settle - the same rule `settle_predictions.unsettled()` follows.
        """
        predictions = [prediction("a"), prediction("b")]
        rows, report = reconcile(predictions, [settlement("a")], now=LONG_AFTER)
        assert rows[0].stage is Stage.SETTLED
        assert rows[1].stage is Stage.AWAITING_SETTLEMENT
        assert report.settled == 1

    def test_a_later_settlement_line_wins(self) -> None:
        """
        The log is append-only, so a correction is a new line and the last belief
        is the current one.
        """
        log = [unresolved("POSTPONED", prediction_id="a"), settlement("a")]
        rows, report = reconcile([prediction("a")], log, now=LONG_AFTER)
        assert rows[0].stage is Stage.SETTLED
        assert report.unresolved == 0

    def test_rows_follow_ledger_order(self) -> None:
        predictions = [prediction("c"), prediction("a"), prediction("b")]
        rows, _ = reconcile(predictions, [], now=LONG_AFTER)
        assert [row.prediction_id for row in rows] == ["c", "a", "b"]

    def test_unresolved_never_counts_as_awaiting_settlement(self) -> None:
        """The central separation of the Epic, asserted on the report itself."""
        rows, report = reconcile([prediction("a")], [unresolved(prediction_id="a")], now=LONG_AFTER)
        assert report.unresolved == 1
        assert report.awaiting_settlement == 0

    def test_awaiting_settlement_never_counts_as_unresolved(self) -> None:
        _, report = reconcile([prediction("a")], [], now=LONG_AFTER)
        assert report.awaiting_settlement == 1
        assert report.unresolved == 0

    def test_pending_is_not_a_fault(self) -> None:
        _, report = reconcile([prediction("a"), prediction("b")], [], now=BEFORE)
        assert report.pending == 2
        assert report.awaiting_settlement == 0
        assert report.settlement_backlog is None

    def test_provenance_is_carried_onto_the_row(self) -> None:
        """Enough on each row to chase a gap without re-reading both logs."""
        rows, _ = reconcile([prediction("a")], [settlement("a")], now=LONG_AFTER)
        row = rows[0]
        assert row.settlement_status == "SETTLED"
        assert row.settlement_source == "espn/scoreboard"
        assert row.competition == "eng.1"
        assert row.season == 2026
        assert row.fixture_id == "740123"

    def test_the_unresolved_reason_is_carried(self) -> None:
        rows, _ = reconcile([prediction("a")], [unresolved("ABANDONED", prediction_id="a")], now=LONG_AFTER)
        assert rows[0].unresolved_reason == "ABANDONED"

    def test_an_empty_ledger_reconciles_to_nothing(self) -> None:
        rows, report = reconcile([], [], now=LONG_AFTER)
        assert rows == []
        assert report.discovered == 0
        assert report.accounted_for is True
        assert report.settlement_backlog is None

    def test_a_settlement_for_an_unknown_prediction_is_ignored(self) -> None:
        """
        The ledger is the denominator. A settlement with no matching prediction
        cannot invent one, or the report would count more lives than predictions.
        """
        _, report = reconcile([prediction("a")], [settlement("zzz")], now=LONG_AFTER)
        assert report.discovered == 1
        assert report.settled == 0

    def test_settlements_are_never_mutated(self) -> None:
        before = settlement("a")
        snapshot = dict(before)
        reconcile([prediction("a")], [before], now=LONG_AFTER)
        assert before == snapshot

    def test_predictions_are_never_mutated(self) -> None:
        before = prediction("a")
        snapshot = dict(before)
        reconcile([before], [], now=LONG_AFTER)
        assert before == snapshot

    def test_the_probability_is_never_read(self) -> None:
        """
        This module answers "where is it?", never "was it good?". A record with no
        probability at all must reconcile exactly like one that has it.
        """
        without = prediction("a", probability=None, status="NO_TEAM_STATS")
        rows, report = reconcile([without], [settlement("a")], now=LONG_AFTER)
        assert rows[0].stage is Stage.SETTLED
        assert report.settled == 1


class TestBacklog:
    """The one number worth alerting on."""

    def test_backlog_excludes_pending_from_the_denominator(self) -> None:
        """
        Otherwise the figure tracks the fixture calendar rather than pipeline
        health: a big Saturday slate would look like a regression.
        """
        predictions = [
            prediction("done", kickoff="2026-08-15T15:00:00+00:00"),
            prediction("late", kickoff="2026-08-15T15:00:00+00:00"),
            prediction("future", kickoff="2026-08-20T15:00:00+00:00"),
        ]
        _, report = reconcile(predictions, [settlement("done")], now=LONG_AFTER)
        assert report.pending == 1
        # Two are due; one of those is unsettled.
        assert report.settlement_backlog == 0.5

    def test_backlog_is_none_when_nothing_is_due(self) -> None:
        """
        Not 0.0. A rate over zero cases is unknown, and 0.0 would read as perfect
        health at the exact moment there is no evidence either way.
        """
        _, report = reconcile([prediction("a")], [], now=BEFORE)
        assert report.settlement_backlog is None

    def test_backlog_is_zero_when_everything_due_is_settled(self) -> None:
        _, report = reconcile([prediction("a")], [settlement("a")], now=LONG_AFTER)
        assert report.settlement_backlog == 0.0

    def test_undated_records_are_excluded_from_the_denominator(self) -> None:
        """An undated record cannot be judged late, so it cannot be a backlog."""
        _, report = reconcile([prediction("a", kickoff=None)], [], now=LONG_AFTER)
        assert report.undated == 1
        assert report.settlement_backlog is None


class TestLedgerConflicts:
    """A repeated prediction_id is corruption, not a re-run."""

    def test_unique_ids_are_no_conflict(self) -> None:
        assert ledger_conflicts([prediction("a"), prediction("b")]) == ()

    def test_two_predictions_for_one_fixture_are_not_a_conflict(self) -> None:
        """
        Legitimate and common: re-running the pipeline produces a second, equally
        valid prediction for the same match. Only a repeated ID is a conflict.
        """
        first = prediction("a", fixture_id="740123")
        second = prediction("b", fixture_id="740123")
        assert ledger_conflicts([first, second]) == ()

    def test_a_repeated_id_is_a_conflict(self) -> None:
        conflicts = ledger_conflicts([prediction("a"), prediction("a")])
        assert len(conflicts) == 1
        assert "a" in conflicts[0]

    def test_the_conflict_names_the_disagreeing_field(self) -> None:
        """
        So an operator can tell a duplicated line from two contradictory records
        without diffing the files by hand.
        """
        conflicts = ledger_conflicts([prediction("a", probability=0.55), prediction("a", probability=0.61)])
        assert "probability" in conflicts[0]

    def test_an_identical_duplicate_is_still_reported(self) -> None:
        """
        Harmless to grade, but it double-counts in every metric. Reported, with
        wording that distinguishes it from a genuine disagreement.
        """
        conflicts = ledger_conflicts([prediction("a"), prediction("a")])
        assert "disagree" not in conflicts[0]

    def test_records_without_an_id_are_skipped_not_merged(self) -> None:
        """
        Two id-less records are not "the same record twice"; they are unusable
        rows, and the join reports them as unjoinable. Treating them as one
        conflict would raise a false alarm.
        """
        assert ledger_conflicts([prediction(""), prediction("")]) == ()

    def test_conflicts_reach_the_report(self) -> None:
        _, report = reconcile([prediction("a"), prediction("a")], [], now=LONG_AFTER)
        assert len(report.ledger_conflicts) == 1


class TestReportShape:
    def test_summary_names_pending_and_backlog_separately(self) -> None:
        predictions = [prediction("a"), prediction("b", kickoff="2026-08-20T15:00:00+00:00")]
        _, report = reconcile(predictions, [], now=LONG_AFTER)
        text = report.summary()
        assert "1 awaiting settlement" in text
        assert "1 pending" in text

    def test_summary_flags_conflicts_loudly(self) -> None:
        _, report = reconcile([prediction("a"), prediction("a")], [], now=LONG_AFTER)
        assert "LEDGER CONFLICTS" in report.summary()

    def test_count_of_an_absent_stage_is_zero(self) -> None:
        report = LifecycleReport(discovered=0, by_stage={})
        assert report.count(Stage.SETTLED) == 0

    def test_the_report_is_frozen(self) -> None:
        _, report = reconcile([prediction("a")], [], now=LONG_AFTER)
        try:
            report.discovered = 99  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("LifecycleReport must be immutable")

    def test_reconcile_is_deterministic(self) -> None:
        """
        Same inputs, same answer, every time. `now` is injected precisely so this
        is testable rather than a matter of when the suite happens to run.
        """
        predictions = [prediction("a"), prediction("b"), prediction("c", kickoff=None)]
        settlements = [settlement("a")]
        first_rows, first = reconcile(predictions, settlements, now=LONG_AFTER)
        second_rows, second = reconcile(predictions, settlements, now=LONG_AFTER)
        assert first == second
        assert first_rows == second_rows
