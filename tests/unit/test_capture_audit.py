"""
Epic 2I — the reconciliation core.

The tests that matter most are the ones about what is NOT a gap. A verifier that
over-reports gets ignored, and an ignored verifier is worse than none, so the
false-alarm cases (postponement, legitimate skip, schedule outage) are pinned as
hard as the true positive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from domain.capture_audit import (
    CAPTURE_AUDIT_SCHEMA_VERSION,
    DayVerdict,
    ExpectedFixture,
    FixtureOutcome,
    expected_from_matches,
    index_records,
    reconcile,
    reconcile_day,
)

DAY = date(2026, 8, 15)
NOON = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)

# A sentinel, because `kickoff=None` is a MEANINGFUL value here (an undated
# fixture) and must be distinguishable from "the caller did not say". Using None
# as the default silently substituted the default kickoff for an explicit None,
# which made the undated-fixture test fail against correct production code.
_UNSET = object()


def fixture(
    fixture_id: str,
    *,
    competition: str = "eng.1",
    season: Optional[int] = 2026,
    kickoff: Any = _UNSET,
    status: Optional[str] = None,
) -> ExpectedFixture:
    return ExpectedFixture(
        fixture_id=fixture_id,
        competition=competition,
        season=season,
        kickoff=NOON if kickoff is _UNSET else kickoff,
        status=status,
    )



def record(fixture_id: str, *, prediction_id: str = "p1", **extra: Any) -> Dict[str, Any]:
    """A ledger line, keyed as `domain/prediction_log.to_json_dict` writes it."""
    row: Dict[str, Any] = {
        "prediction_id": prediction_id,
        "fixture_id": fixture_id,
        "competition": "eng.1",
        "season": 2026,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Ledger states: absent, empty, populated
# ---------------------------------------------------------------------------


def test_absent_ledger_with_fixtures_is_a_capture_gap() -> None:
    # The headline case: the ledger directory never existed, so `load_records`
    # returned []. Before 2I this was reported as a healthy day.
    audit = reconcile([fixture("1"), fixture("2")], [])
    assert audit.days[0].verdict is DayVerdict.ZERO_CAPTURE
    assert audit.has_gap is True
    assert audit.captured == 0
    assert audit.unaccounted == 2


def test_empty_ledger_is_indistinguishable_from_absent_and_both_are_gaps() -> None:
    # An empty file and no file at all are the same evidence: nothing recorded.
    assert reconcile([fixture("1")], []).has_gap is True


def test_no_fixtures_is_not_a_gap() -> None:
    # Nothing was scheduled, so nothing should have been captured.
    audit = reconcile([], [], days=[DAY])
    assert audit.days[0].verdict is DayVerdict.NO_FIXTURES
    assert audit.has_gap is False


def test_no_fixtures_and_no_forced_day_yields_no_days_at_all() -> None:
    assert reconcile([], []).days == ()


def test_complete_capture() -> None:
    expected = [fixture("1"), fixture("2")]
    records = [record("1"), record("2", prediction_id="p2")]
    audit = reconcile(expected, records)
    assert audit.days[0].verdict is DayVerdict.COMPLETE
    assert audit.has_gap is False
    assert audit.captured == 2


def test_partial_capture_is_reported_but_is_not_a_gap() -> None:
    # THE false-alarm rule. GG-013 is unwired, so a fixture may legitimately go
    # unpredicted. Alerting here would fire most days and get the tool switched
    # off; separating a legitimate skip from a lost record would require asking
    # the model what it would have predicted, which 2I forbids.
    audit = reconcile([fixture("1"), fixture("2"), fixture("3")], [record("1")])
    day = audit.days[0]
    assert day.verdict is DayVerdict.PARTIAL
    assert day.is_gap is False
    assert audit.has_gap is False
    assert (day.captured, day.unaccounted) == (1, 2)


# ---------------------------------------------------------------------------
# Not-playable fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["postponed", "POSTPONED", "STATUS_POSTPONED", "cancelled", "canceled", "abandoned", "suspended"],
)
def test_not_playable_fixtures_are_never_unaccounted(status: str) -> None:
    # Football, not a defect. Case and provider-prefix variants must agree, or a
    # postponement would be reported as missing evidence depending on its source.
    audit = reconcile([fixture("1", status=status)], [])
    day = audit.days[0]
    assert day.rows[0].outcome is FixtureOutcome.NOT_PLAYABLE
    assert day.verdict is DayVerdict.NO_PLAYABLE_FIXTURES
    assert day.is_gap is False
    assert day.unaccounted == 0


def test_a_card_of_only_postponements_is_not_a_gap() -> None:
    expected = [fixture("1", status="postponed"), fixture("2", status="cancelled")]
    assert reconcile(expected, []).has_gap is False


def test_postponed_fixtures_do_not_mask_a_gap_on_the_playable_ones() -> None:
    # One postponement plus two uncaptured playable fixtures is still zero capture.
    expected = [fixture("1", status="postponed"), fixture("2"), fixture("3")]
    day = reconcile(expected, []).days[0]
    assert day.verdict is DayVerdict.ZERO_CAPTURE
    assert (day.not_playable, day.unaccounted, day.playable) == (1, 2, 2)


def test_a_postponed_fixture_that_was_predicted_counts_as_captured() -> None:
    # Predicted, then postponed. The evidence exists; settlement handles the rest.
    day = reconcile([fixture("1", status="postponed")], [record("1")]).days[0]
    assert day.rows[0].outcome is FixtureOutcome.CAPTURED
    assert day.verdict is DayVerdict.COMPLETE


def test_unknown_status_is_treated_as_playable() -> None:
    # Conservative: an unrecognised status must not silently excuse a missing
    # prediction, or a provider wording change would quietly disable the check.
    day = reconcile([fixture("1", status="STATUS_SCHEDULED")], []).days[0]
    assert day.rows[0].outcome is FixtureOutcome.UNACCOUNTED
    assert day.verdict is DayVerdict.ZERO_CAPTURE


def test_none_status_is_treated_as_playable() -> None:
    assert fixture("1", status=None).playable is True


# ---------------------------------------------------------------------------
# Duplicates and off-schedule records
# ---------------------------------------------------------------------------


def test_duplicate_prediction_ids_for_one_fixture_count_once() -> None:
    # A re-run is legitimate (2G made them distinguishable), but must not let a
    # day report more captures than it had fixtures.
    expected = [fixture("1"), fixture("2")]
    records = [record("1", prediction_id="p1"), record("1", prediction_id="p2"), record("2")]
    day = reconcile(expected, records).days[0]
    assert day.captured == 2
    assert day.duplicates == 1
    assert day.rows[0].duplicated is True
    assert day.accounted_for is True


def test_the_same_prediction_id_twice_is_not_a_duplicate() -> None:
    # An interrupted append can leave a line twice. That is one prediction.
    records = [record("1", prediction_id="p1"), record("1", prediction_id="p1")]
    day = reconcile([fixture("1")], records).days[0]
    assert day.duplicates == 0
    assert day.captured == 1


def test_records_for_fixtures_not_on_the_schedule_are_counted_separately() -> None:
    # Usually a schedule window narrower than the ledger. Reported, never a gap.
    audit = reconcile([fixture("1")], [record("1"), record("99")])
    assert audit.unknown_fixture_records == 1
    assert audit.has_gap is False


def test_records_without_a_fixture_id_are_ignored_not_crashed_on() -> None:
    records: List[Dict[str, Any]] = [{"prediction_id": "p1"}, {"fixture_id": ""}, record("1")]
    assert reconcile([fixture("1")], records).captured == 1


def test_non_mapping_records_are_ignored() -> None:
    # A malformed line must be skipped rather than take the whole audit down.
    assert index_records(["nonsense", 42, None]) == {}  # type: ignore[list-item]


def test_records_are_matched_on_fixture_id_across_types() -> None:
    # The ledger stores fixture_id as a string; a dataset may yield an int.
    assert reconcile([fixture("1")], [record(1)]).captured == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Grouping, dates, timezones
# ---------------------------------------------------------------------------


def test_fixtures_are_grouped_by_utc_kickoff_date() -> None:
    expected = [
        fixture("1", kickoff=datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)),
        fixture("2", kickoff=datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc)),
    ]
    audit = reconcile(expected, [record("1")])
    assert [day.day for day in audit.days] == [date(2026, 8, 15), date(2026, 8, 16)]
    assert audit.days[0].verdict is DayVerdict.COMPLETE
    assert audit.days[1].verdict is DayVerdict.ZERO_CAPTURE


def test_a_late_kickoff_is_filed_on_its_utc_date_not_its_local_one() -> None:
    # 22:30 in Lagos (UTC+1) on the 15th is 21:30 UTC on the 15th; 00:30 on the
    # 16th local is the 15th UTC. Every stored timestamp is UTC, so the audit must
    # group in UTC or a late fixture would be sought on the wrong day.
    lagos = timezone(timedelta(hours=1))
    late = fixture("1", kickoff=datetime(2026, 8, 16, 0, 30, tzinfo=lagos))
    assert late.kickoff_date == date(2026, 8, 15)


def test_a_naive_kickoff_is_treated_as_utc_rather_than_raising() -> None:
    # Comparing naive and aware datetimes raises TypeError. One hand-built dataset
    # row must not be able to crash the audit.
    naive = fixture("1", kickoff=datetime(2026, 8, 15, 14, 0))
    assert naive.kickoff_date == DAY


def test_undated_fixtures_are_reported_and_sorted_last() -> None:
    expected = [fixture("2"), fixture("1", kickoff=None)]
    audit = reconcile(expected, [])
    assert audit.days[-1].day is None
    assert audit.undated_expected == ("1",)


def test_forced_days_appear_even_when_the_schedule_is_empty_for_them() -> None:
    # "No fixtures that day" is a different statement from silence.
    audit = reconcile([], [], days=[date(2026, 8, 14), DAY])
    assert [day.day for day in audit.days] == [date(2026, 8, 14), DAY]
    assert all(day.verdict is DayVerdict.NO_FIXTURES for day in audit.days)


def test_a_forced_day_that_also_has_fixtures_is_not_duplicated() -> None:
    audit = reconcile([fixture("1")], [record("1")], days=[DAY])
    assert len(audit.days) == 1


def test_days_are_sorted_chronologically_regardless_of_input_order() -> None:
    expected = [
        fixture("3", kickoff=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)),
        fixture("1", kickoff=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)),
        fixture("2", kickoff=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)),
    ]
    days = [day.day for day in reconcile(expected, []).days]
    assert days == sorted(d for d in days if d is not None)


# ---------------------------------------------------------------------------
# Multiple competitions and seasons
# ---------------------------------------------------------------------------


def test_one_competition_capturing_and_another_not_is_partial_not_a_gap() -> None:
    # A single day mixes leagues. Zero capture is a property of the DAY: if one
    # league recorded, the writer worked, so the cause is per-fixture.
    expected = [fixture("1", competition="eng.1"), fixture("2", competition="esp.1")]
    day = reconcile(expected, [record("1")]).days[0]
    assert day.verdict is DayVerdict.PARTIAL


def test_rows_are_sorted_by_competition_then_season_then_fixture_id() -> None:
    expected = [
        fixture("9", competition="esp.1"),
        fixture("2", competition="eng.1", season=2025),
        fixture("1", competition="eng.1", season=2026),
    ]
    rows = reconcile(expected, []).days[0].rows
    assert [(r.competition, r.season) for r in rows] == [
        ("eng.1", 2025),
        ("eng.1", 2026),
        ("esp.1", 2026),
    ]


def test_a_missing_season_sorts_without_raising() -> None:
    # Optional[int]: None cannot be compared with int, and 0 would sort an unknown
    # season before every real one.
    expected = [fixture("1", season=None), fixture("2", season=2026)]
    rows = reconcile(expected, []).days[0].rows
    assert [row.season for row in rows] == [None, 2026]


# ---------------------------------------------------------------------------
# Invariants and totals
# ---------------------------------------------------------------------------


def test_every_fixture_lands_in_exactly_one_bucket() -> None:
    expected = [fixture("1"), fixture("2"), fixture("3", status="postponed")]
    day = reconcile(expected, [record("1")]).days[0]
    assert day.captured + day.unaccounted + day.not_playable == day.expected
    assert day.accounted_for is True


def test_totals_are_the_sum_of_the_days() -> None:
    expected = [
        fixture("1", kickoff=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)),
        fixture("2", kickoff=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)),
    ]
    audit = reconcile(expected, [record("1")])
    assert audit.expected == sum(day.expected for day in audit.days) == 2
    assert audit.captured == 1
    assert len(audit.gap_days) == 1


def test_reconciliation_is_deterministic_for_identical_inputs() -> None:
    expected = [fixture("2"), fixture("1"), fixture("3", status="postponed")]
    records = [record("1"), record("3")]
    first = reconcile(expected, records)
    second = reconcile(expected, records)
    assert first == second
    assert first.summary() == second.summary()


def test_summary_names_the_gap_count() -> None:
    assert "1 capture gap(s)" in reconcile([fixture("1")], []).summary()
    assert "no capture gap" in reconcile([fixture("1")], [record("1")]).summary()


def test_schema_version_is_stamped() -> None:
    assert CAPTURE_AUDIT_SCHEMA_VERSION == "2i.1"


def test_a_fixture_without_an_id_is_refused() -> None:
    # An id-less fixture cannot be reconciled with anything; accepting one would
    # silently drop it from the count it belongs in.
    with pytest.raises(ValueError):
        ExpectedFixture(fixture_id="")


def test_reconcile_day_accepts_an_explicit_date() -> None:
    day = reconcile_day(DAY, [fixture("1")], [record("1")])
    assert day.day == DAY
    assert day.verdict is DayVerdict.COMPLETE


# ---------------------------------------------------------------------------
# The HistoricalMatch adapter
# ---------------------------------------------------------------------------


@dataclass
class FakeMatch:
    """Shaped like `domain.historical.HistoricalMatch`, which stays unimported."""

    event_id: str
    competition: str = "eng.1"
    season: int = 2026
    kickoff: Any = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)
    status: Optional[str] = None


def test_expected_from_matches_reads_event_id_as_the_fixture_id() -> None:
    # 2H-F1: a live fixture_id and a historical event_id are the same identifier.
    fixtures = expected_from_matches([FakeMatch(event_id="401")])
    assert fixtures[0].fixture_id == "401"
    assert fixtures[0].competition == "eng.1"


def test_expected_from_matches_parses_an_iso_string_kickoff() -> None:
    fixtures = expected_from_matches([FakeMatch(event_id="1", kickoff="2026-08-15T14:00:00Z")])
    assert fixtures[0].kickoff_date == DAY


def test_expected_from_matches_survives_an_unparseable_kickoff() -> None:
    # Reported as undated rather than dropped: a dropped fixture is one the tool
    # silently stops checking.
    fixtures = expected_from_matches([FakeMatch(event_id="1", kickoff="not-a-date")])
    assert fixtures[0].kickoff is None


def test_expected_from_matches_skips_a_match_with_no_id() -> None:
    assert expected_from_matches([FakeMatch(event_id="")]) == []


def test_expected_from_matches_coerces_a_numeric_id_to_string() -> None:
    fixtures = expected_from_matches([FakeMatch(event_id=401)])  # type: ignore[arg-type]
    assert fixtures[0].fixture_id == "401"
