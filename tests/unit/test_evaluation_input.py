"""
The evaluation input adapter (Epic 2H-3).

The central claim under test: THE PROBABILITY THAT IS GRADED IS THE PROBABILITY
THAT WAS PUBLISHED. Every other property here supports that one.

  - the stored float reaches the metrics bit-for-bit, and no model is consulted
  - the join is exact on (competition, season, fixture_id) - never a name or date
  - an unresolved fixture is excluded from Brier/log loss and counted in coverage
  - a real 0-0 is scored NO, because it is evidence, not an absence
"""

import dataclasses
import math
from datetime import datetime, timezone

import pytest
from helpers.settlement_fixtures import (
    SETTLED_AT,
    prediction,
    settlement,
    unresolved,
)

from domain.evaluation import (
    BttsOutcome,
    UnevaluableReason,
    brier_score,
    log_loss,
    summarise,
)
from domain.evaluation_input import (
    EvaluationInput,
    SettlementState,
    StoredProvenance,
    UnjoinableReason,
    adapt_one,
    index_settlements,
    join_for_evaluation,
    join_key_of_prediction,
    join_key_of_settlement,
    scoreable,
    to_prediction_records,
)


# ---------------------------------------------------------------------------
# The stored probability
# ---------------------------------------------------------------------------
class TestTheStoredProbabilityIsWhatIsGraded:
    def test_the_ledger_probability_reaches_the_metrics_unchanged(self):
        """
        Bit-for-bit. Not rounded, not rescaled, not recomputed.

        A deliberately awkward float: any rounding, any float32 round-trip, any
        "clean up the number" step shows up as an exact-equality failure here.
        """
        stored = 0.6123456789012345
        adapted, _ = adapt_one(prediction(probability=stored), settlement())
        assert adapted is not None
        assert adapted.prediction.probability == stored
        assert adapted.stored_probability == stored

    def test_the_brier_score_is_computed_from_the_stored_value(self):
        """
        The arithmetic is pinned to the STORED number, so a regenerated
        probability could not produce this result by coincidence.
        """
        adapted, _ = adapt_one(prediction(probability=0.25), settlement(outcome="YES"))
        assert adapted is not None
        # (0.25 - 1)^2 = 0.5625
        assert brier_score([adapted.prediction]) == pytest.approx(0.5625)

    def test_a_probability_of_zero_is_kept_and_not_treated_as_absent(self):
        """
        GG-007. `if probability:` would read a confident 0.0 as "no prediction"
        and silently drop the most confidently wrong record in the ledger.
        """
        adapted, reason = adapt_one(prediction(probability=0.0), settlement(outcome="YES"))
        assert reason is None
        assert adapted is not None
        assert adapted.prediction.probability == 0.0
        assert adapted.is_scored, "a 0.0 prediction against a YES is scoreable"
        # log loss must be finite: the epsilon clamp lives inside the logarithm.
        assert math.isfinite(log_loss([adapted.prediction]) or math.inf)

    def test_a_probability_of_one_is_kept(self):
        adapted, _ = adapt_one(prediction(probability=1.0), settlement(outcome="NO"))
        assert adapted is not None
        assert adapted.prediction.probability == 1.0
        assert math.isfinite(log_loss([adapted.prediction]) or math.inf)

    def test_an_out_of_range_probability_is_refused_not_clipped(self):
        """
        A corrupt row is reported, never repaired. Clipping 1.5 to 1.0 would
        invent a prediction nobody made.
        """
        adapted, reason = adapt_one(prediction(probability=1.5), settlement())
        assert adapted is None
        assert reason is UnjoinableReason.MALFORMED

    def test_the_stored_provenance_is_used_verbatim(self):
        """
        The model identity comes from the ledger, not from today's constants. A
        record written by 0.9.0 is graded as 0.9.0.
        """
        adapted, _ = adapt_one(
            prediction(provenance={"model_id": "POISSON_V1", "model_version": "0.9.0"}),
            settlement(),
        )
        assert adapted is not None
        assert adapted.prediction.model_version == "0.9.0"

    def test_a_record_without_provenance_is_still_evaluable(self):
        adapted, _ = adapt_one(prediction(provenance=None), settlement())
        assert adapted is not None
        assert adapted.provenance.model_id == "UNKNOWN"


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------
class TestTheJoinIsExact:
    def test_the_key_is_competition_season_and_fixture(self):
        assert join_key_of_prediction(prediction()) == ("eng.1", 2026, "740123")
        assert join_key_of_settlement(settlement()) == ("eng.1", 2026, "740123")

    def test_the_same_fixture_id_in_another_competition_does_not_join(self):
        """
        A bare-id join would attach La Liga's result to a Premier League
        prediction and score it with total confidence.
        """
        inputs, report = join_for_evaluation(
            [prediction(competition="eng.1")],
            [settlement(competition="esp.1")],
        )
        assert report.joined == 1
        assert inputs[0].settlement_state is SettlementState.MISSING
        assert not inputs[0].is_scored

    def test_the_same_fixture_id_in_another_season_does_not_join(self):
        inputs, _ = join_for_evaluation(
            [prediction(season=2026)], [settlement(season=2025)]
        )
        assert inputs[0].settlement_state is SettlementState.MISSING

    def test_team_names_are_never_consulted(self):
        """
        GG-008. Identical ids with wildly different names still join; the join
        key does not contain a name, so a name cannot break or make it.
        """
        inputs, _ = join_for_evaluation(
            [prediction(home_team_name="Athletic", away_team_name="Betis")],
            [settlement()],
        )
        assert inputs[0].settlement_state is SettlementState.SETTLED

    def test_dates_are_never_consulted(self):
        """
        A settlement dated months later still joins. Matching on date would
        break every postponed fixture.
        """
        inputs, _ = join_for_evaluation(
            [prediction()], [settlement(settled_at="2027-01-01T00:00:00+00:00")]
        )
        assert inputs[0].settlement_state is SettlementState.SETTLED

    def test_an_integer_fixture_id_still_joins(self):
        """`"740123" != 740123` would turn a correct join into a silent miss."""
        inputs, _ = join_for_evaluation(
            [prediction(fixture_id=740123)], [settlement(fixture_id="740123")]
        )
        assert inputs[0].settlement_state is SettlementState.SETTLED

    def test_the_join_is_deterministic_across_input_orderings(self):
        """
        Same data, same answer. The settlement list is shuffled; only
        `settled_at` may decide precedence, never file order.
        """
        predictions = [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")]
        settlements = [settlement("a", fixture_id="1"), settlement("b", fixture_id="2", outcome="NO", home=0, away=0)]

        first, _ = join_for_evaluation(predictions, settlements)
        second, _ = join_for_evaluation(predictions, list(reversed(settlements)))
        assert [i.prediction.outcome for i in first] == [i.prediction.outcome for i in second]

    def test_ledger_order_is_preserved(self):
        """Two runs over an unchanged ledger produce comparable artifacts."""
        predictions = [prediction(f"p{n}", fixture_id=str(n)) for n in range(5)]
        inputs, _ = join_for_evaluation(predictions, [])
        assert [i.prediction_id for i in inputs] == ["p0", "p1", "p2", "p3", "p4"]

    def test_two_predictions_of_one_fixture_both_join_to_the_one_result(self):
        """
        A re-run produces two predictions; the pitch produced one result. Both
        must be graded against it - dropping either would discard evidence.
        """
        inputs, report = join_for_evaluation(
            [prediction("pred-1"), prediction("pred-2")], [settlement("pred-1")]
        )
        assert report.joined == 2
        assert all(i.settlement_state is SettlementState.SETTLED for i in inputs)
        assert {i.prediction_id for i in inputs} == {"pred-1", "pred-2"}

    def test_the_join_uses_season_not_matched_season(self):
        """
        2H-F3. Settlement already absorbed the rollover drift and recorded it in
        `matched_season`; joining on that field would re-introduce the mismatch.
        """
        inputs, _ = join_for_evaluation(
            [prediction(season=2026)], [settlement(season=2026, matched_season=2027)]
        )
        assert inputs[0].settlement_state is SettlementState.SETTLED
        assert inputs[0].matched_season == 2027


# ---------------------------------------------------------------------------
# Settlement states
# ---------------------------------------------------------------------------
class TestSettlementStates:
    def test_a_settled_fixture_is_scored(self):
        inputs, report = join_for_evaluation([prediction()], [settlement()])
        assert inputs[0].settlement_state is SettlementState.SETTLED
        assert inputs[0].is_scored
        assert report.scored == 1

    def test_an_unresolved_fixture_is_excluded_from_brier_and_log_loss(self):
        """
        The requirement, asserted on the metrics themselves rather than on an
        intermediate flag.
        """
        inputs, _ = join_for_evaluation([prediction()], [unresolved()])
        assert inputs[0].settlement_state is SettlementState.UNRESOLVED
        assert not inputs[0].is_scored
        records = to_prediction_records(inputs)
        assert brier_score(records) is None, "no scoreable records means no score, not 0.0"
        assert log_loss(records) is None

    def test_an_unresolved_fixture_is_counted_in_coverage(self):
        """
        Excluded from quality, INCLUDED in coverage. It is a fixture the system
        predicted and cannot yet be graded on, and that is the number Epic 2C
        exists to move.
        """
        inputs, report = join_for_evaluation(
            [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")],
            [settlement("a", fixture_id="1"), unresolved("b", fixture_id="2")],
        )
        assert report.settled == 1
        assert report.unresolved == 1
        assert report.joined == 2

        summary = summarise(
            to_prediction_records(inputs), model_id="POISSON_V1", model_version="1.0.0"
        )
        assert summary.targets == 2, "both are targets"
        assert summary.scored == 1, "only one is scored"
        assert summary.coverage == 0.5
        assert summary.unevaluable == {UnevaluableReason.NO_RESULT.value: 1}

    def test_an_unresolved_prediction_keeps_its_probability(self):
        """
        The model DID speak; the fixture has no result yet. Dropping the
        probability would misreport a postponement as a model refusal.
        """
        adapted, _ = adapt_one(prediction(probability=0.7), unresolved())
        assert adapted is not None
        assert adapted.prediction.probability == 0.7
        assert adapted.prediction.outcome is BttsOutcome.UNKNOWN
        assert adapted.prediction.unevaluable_reason is None

    def test_a_missing_settlement_is_not_the_same_as_unresolved(self):
        """
        MISSING means the settlement job has not run. UNRESOLVED means it ran and
        football happened. Merging them would hide an operational gap.
        """
        inputs, report = join_for_evaluation([prediction()], [])
        assert inputs[0].settlement_state is SettlementState.MISSING
        assert report.missing_settlement == 1
        assert report.unresolved == 0

    def test_a_real_goalless_draw_is_scored_as_no(self):
        """
        0-0 is evidence, not an absence. Sweeping it in with the unresolved
        records would discard roughly a tenth of all settled fixtures.
        """
        adapted, _ = adapt_one(
            prediction(), settlement(home=0, away=0, outcome="NO")
        )
        assert adapted is not None
        assert adapted.prediction.outcome is BttsOutcome.NO
        assert adapted.is_scored

    def test_the_outcome_is_read_from_settlement_not_re_derived(self):
        """
        Settlement owns the derivation and already refuses to record an outcome
        that contradicts its own score. A second derivation here would be a
        second place for that rule to drift.
        """
        adapted, _ = adapt_one(prediction(), settlement(home=None, away=None, outcome="YES"))
        assert adapted is not None
        assert adapted.prediction.outcome is BttsOutcome.YES

    def test_an_unrecognised_outcome_is_unknown_not_no(self):
        adapted, _ = adapt_one(prediction(), settlement(outcome="PROBABLY"))
        assert adapted is not None
        assert adapted.prediction.outcome is BttsOutcome.UNKNOWN


# ---------------------------------------------------------------------------
# Refused predictions
# ---------------------------------------------------------------------------
class TestRefusedPredictions:
    def test_a_refused_prediction_becomes_coverage_information(self):
        adapted, _ = adapt_one(
            prediction(probability=None, status="NO_TEAM_STATS"), settlement()
        )
        assert adapted is not None
        assert adapted.prediction.probability is None
        assert adapted.prediction.unevaluable_reason is UnevaluableReason.INSUFFICIENT_HISTORY
        assert not adapted.is_scored

    def test_the_original_ledger_status_survives_the_lossy_mapping(self):
        """
        Two ledger statuses collapse onto INSUFFICIENT_HISTORY because
        `domain/evaluation.py` is frozen. The exact status is kept in `detail`,
        so the collapse is presentation and never a loss of evidence.
        """
        thin, _ = adapt_one(
            prediction(probability=None, status="NO_POINT_IN_TIME_INPUTS"), settlement()
        )
        empty, _ = adapt_one(
            prediction(probability=None, status="NO_TEAM_STATS"), settlement()
        )
        assert thin is not None and empty is not None
        assert thin.prediction.unevaluable_reason is empty.prediction.unevaluable_reason
        assert thin.prediction.detail == "ledger_status=NO_POINT_IN_TIME_INPUTS"
        assert empty.prediction.detail == "ledger_status=NO_TEAM_STATS"
        assert thin.ledger_status != empty.ledger_status

    def test_model_returned_none_maps_to_its_own_reason(self):
        adapted, _ = adapt_one(
            prediction(probability=None, status="MODEL_RETURNED_NONE"), settlement()
        )
        assert adapted is not None
        assert adapted.prediction.unevaluable_reason is UnevaluableReason.MODEL_RETURNED_NONE

    def test_an_unknown_status_without_a_probability_is_refused(self):
        """
        Guessing a reason would fabricate provenance. A new ledger status must be
        mapped deliberately, not absorbed silently.
        """
        adapted, reason = adapt_one(
            prediction(probability=None, status="SOMETHING_NEW"), settlement()
        )
        assert adapted is None
        assert reason is UnjoinableReason.UNKNOWN_STATUS


# ---------------------------------------------------------------------------
# Malformed rows
# ---------------------------------------------------------------------------
class TestMalformedRows:
    @pytest.mark.parametrize(
        "override,expected",
        [
            ({"prediction_id": None}, UnjoinableReason.NO_PREDICTION_ID),
            ({"fixture_id": None}, UnjoinableReason.NO_FIXTURE_ID),
            ({"competition": None}, UnjoinableReason.NO_COMPETITION),
            ({"kickoff": None}, UnjoinableReason.NO_KICKOFF),
            ({"kickoff": "not-a-date"}, UnjoinableReason.NO_KICKOFF),
            ({"home_team_id": None}, UnjoinableReason.NO_TEAM_IDS),
        ],
    )
    def test_a_missing_field_is_reported_never_guessed(self, override, expected):
        adapted, reason = adapt_one(prediction(**override), settlement())
        assert adapted is None
        assert reason is expected

    def test_a_naive_kickoff_is_refused(self):
        """
        GG-014. A naive 23:30 compared against local time lands on the wrong
        matchday, so an unusable timestamp is refused rather than assumed UTC.
        """
        adapted, reason = adapt_one(
            prediction(kickoff="2026-08-15T15:00:00"), settlement()
        )
        assert adapted is None
        assert reason is UnjoinableReason.NO_KICKOFF

    def test_one_bad_row_costs_one_row(self):
        good = [prediction(f"p{n}", fixture_id=str(n)) for n in range(3)]
        bad = prediction("bad", fixture_id="9", kickoff=None)
        inputs, report = join_for_evaluation([*good, bad], [])
        assert report.joined == 3
        assert report.unjoinable == {UnjoinableReason.NO_KICKOFF.value: 1}

    def test_every_prediction_is_accounted_for(self):
        """
        A silently shrinking denominator would make a failing pipeline look like
        an improving model.
        """
        inputs, report = join_for_evaluation(
            [prediction("a"), prediction("b", kickoff=None), prediction("c", fixture_id=None)],
            [],
        )
        assert report.predictions == 3
        assert report.joined + sum(report.unjoinable.values()) == 3


# ---------------------------------------------------------------------------
# Corrections and conflicts
# ---------------------------------------------------------------------------
class TestCorrectionsAndConflicts:
    def test_the_latest_settlement_wins(self):
        """
        Settlement is append-only, so a correction is a new line. Unresolved then
        settled is the normal progression of a fixture that has since finished.
        """
        index, conflicts = index_settlements(
            [
                unresolved(settled_at="2026-08-16T12:00:00+00:00"),
                settlement(settled_at="2026-08-18T12:00:00+00:00"),
            ]
        )
        assert conflicts == ()
        assert index[("eng.1", 2026, "740123")]["settlement_status"] == "SETTLED"

    def test_precedence_is_by_settled_at_not_file_order(self):
        index, _ = index_settlements(
            [
                settlement(settled_at="2026-08-18T12:00:00+00:00", home=3, away=3),
                unresolved(settled_at="2026-08-16T12:00:00+00:00"),
            ]
        )
        assert index[("eng.1", 2026, "740123")]["final_home_goals"] == 3

    def test_two_settlements_disagreeing_on_the_score_are_reported(self):
        """
        Not a correction to apply quietly: two sources described one fixture
        differently and every metric downstream would inherit the choice.
        """
        _, conflicts = index_settlements(
            [
                settlement(home=2, away=1, settled_at="2026-08-17T12:00:00+00:00"),
                settlement(home=3, away=0, outcome="NO", settled_at="2026-08-18T12:00:00+00:00"),
            ]
        )
        assert len(conflicts) == 1
        assert "740123" in conflicts[0]

    def test_a_conflict_is_surfaced_by_the_join(self):
        _, report = join_for_evaluation(
            [prediction()],
            [
                settlement(home=2, away=1, settled_at="2026-08-17T12:00:00+00:00"),
                settlement(home=0, away=0, outcome="NO", settled_at="2026-08-18T12:00:00+00:00"),
            ],
        )
        assert report.settlement_conflicts

    def test_an_unresolved_record_never_conflicts_with_a_settled_one(self):
        _, conflicts = index_settlements([unresolved(), settlement()])
        assert conflicts == ()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
class TestJoinReport:
    def test_the_counts_are_all_reported_separately(self):
        inputs, report = join_for_evaluation(
            [
                prediction("a", fixture_id="1"),
                prediction("b", fixture_id="2"),
                prediction("c", fixture_id="3"),
                prediction("d", fixture_id="4", kickoff=None),
            ],
            [settlement("a", fixture_id="1"), unresolved("b", fixture_id="2")],
        )
        assert (report.predictions, report.joined) == (4, 3)
        assert (report.settled, report.unresolved, report.missing_settlement) == (1, 1, 1)
        assert sum(report.unjoinable.values()) == 1

    def test_rates_are_none_for_an_empty_ledger_not_one(self):
        _, report = join_for_evaluation([], [])
        assert report.join_rate is None
        assert report.settlement_coverage is None

    def test_settlement_coverage_is_settled_over_joined(self):
        _, report = join_for_evaluation(
            [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")],
            [settlement("a", fixture_id="1")],
        )
        assert report.settlement_coverage == 0.5

    def test_scoreable_filters_to_gradeable_records_only(self):
        inputs, _ = join_for_evaluation(
            [
                prediction("a", fixture_id="1"),
                prediction("b", fixture_id="2"),
                prediction("c", fixture_id="3", probability=None, status="NO_TEAM_STATS"),
            ],
            [settlement("a", fixture_id="1"), unresolved("b", fixture_id="2")],
        )
        assert [i.prediction_id for i in scoreable(inputs)] == ["a"]

    def test_to_prediction_records_keeps_every_input(self):
        """Coverage needs the unresolved records; filtering here would inflate it."""
        inputs, _ = join_for_evaluation(
            [prediction("a", fixture_id="1"), prediction("b", fixture_id="2")],
            [settlement("a", fixture_id="1")],
        )
        assert len(to_prediction_records(inputs)) == 2


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------
class TestPurity:
    def test_the_input_dicts_are_never_mutated(self):
        """
        The adapter reads storage. A reader that edits its input could rewrite
        history to agree with the result.
        """
        led, settle = prediction(), settlement()
        before_led, before_settle = dict(led), dict(settle)
        adapt_one(led, settle)
        assert led == before_led
        assert settle == before_settle

    def test_the_adapted_record_is_frozen(self) -> None:
        """
        `FrozenInstanceError` specifically, not `Exception`: a bare `Exception`
        would also pass if the attribute name were misspelled, which proves
        nothing about immutability.

        The `-> None` is load-bearing. mypy skips the bodies of UNANNOTATED
        functions, so without it the `type: ignore` below is never exercised and
        `warn_unused_ignores` correctly flags it as dead. Annotated, the body is
        checked, mypy sees the write to a frozen field, and the ignore documents
        that the error is the point of the test - immutability is enforced at
        BOTH type-check and run time.
        """
        adapted, _ = adapt_one(prediction(), settlement())
        assert adapted is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            adapted.prediction_id = "changed"  # type: ignore[misc]

    def test_stored_provenance_reads_the_block_it_is_given(self):
        block = {
            "model_id": "M",
            "model_version": "2",
            "config_fingerprint": "abc123",
            "code_revision": "deadbeef",
        }
        provenance = StoredProvenance.from_ledger(block)
        assert provenance.config_fingerprint == "abc123"
        assert provenance.code_revision == "deadbeef"

    def test_created_at_is_carried_from_the_ledger(self):
        adapted, _ = adapt_one(prediction(), settlement())
        assert adapted is not None
        assert adapted.created_at == datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)

    def test_the_settlement_source_is_carried(self):
        adapted, _ = adapt_one(prediction(), settlement())
        assert adapted is not None
        assert adapted.settlement_source == "espn/scoreboard"
        assert adapted.settled_at == SETTLED_AT

    def test_an_evaluation_input_carries_no_price_field(self):
        """
        LEAK-001. The ledger legitimately stores a price; the evaluation layer is
        walled off from it. This adapter sits on that boundary.
        """
        adapted, _ = adapt_one(
            prediction(odds={"provenance": "PARTIAL_NO_BOOKMAKER", "price": 1.85, "edge": 0.07}),
            settlement(),
        )
        assert adapted is not None
        fields = set(EvaluationInput.__dataclass_fields__)
        banned = {"odds", "price", "edge", "stake", "bookmaker", "roi", "profit", "value"}
        assert not (fields & banned)
        assert not (set(type(adapted.prediction).__dataclass_fields__) & banned)
