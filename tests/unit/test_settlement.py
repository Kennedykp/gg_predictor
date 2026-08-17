"""
The settlement contract (Epic 2H-2).

Every test here pins one of two things: that a real result is recorded exactly as
it happened, or that an ABSENCE is never converted into an observation. The
second group is the reason this module exists. A fabricated result is the one
defect that cannot be found later, because a fabricated record and a real one are
byte-identical in shape.

No network, no clock, no disk: `settled_at` is injected, so every field below is
a function of its inputs.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from domain.evaluation import BttsOutcome
from domain.historical import HistoricalMatch
from domain.settlement import (
    FIELD_ORDER,
    SETTLEMENT_SCHEMA_VERSION,
    TERMINAL_REASONS,
    SettlementRecord,
    SettlementStatus,
    UnresolvedReason,
    candidate_lookup_keys,
    classify,
    lookup_key,
    settle_one,
    to_json_dict,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
SOURCE = "espn/scoreboard"


def prediction(**overrides):
    """A ledger record as `load_records` returns it: a plain dict."""
    record = {
        "prediction_id": "pred-1",
        "fixture_id": "740123",
        "competition": "eng.1",
        "season": 2026,
        "probability": 0.55,
    }
    record.update(overrides)
    return record


def match(**overrides):
    """A completed 2-1 by default."""
    fields = {
        "event_id": "740123",
        "competition": "eng.1",
        "season": 2026,
        "kickoff": KICKOFF,
        "home_team_id": "H",
        "away_team_id": "A",
        "completed": True,
        "home_goals": 2,
        "away_goals": 1,
        "status": "STATUS_FULL_TIME",
    }
    fields.update(overrides)
    return HistoricalMatch(**fields)


# ---------------------------------------------------------------------------
# A real result is recorded as it happened
# ---------------------------------------------------------------------------
class TestASettledResult:
    def test_both_scored_is_yes(self):
        record = settle_one(prediction(), match(), settled_at=NOW, source=SOURCE)
        assert record.settlement_status is SettlementStatus.SETTLED
        assert (record.final_home_goals, record.final_away_goals) == (2, 1)
        assert record.gg_outcome is BttsOutcome.YES
        assert record.unresolved_reason is None

    def test_a_goalless_draw_is_no_not_unknown(self):
        """
        THE CASE THAT MUST NOT BE SWEPT UP WITH THE ABSENCES.

        0-0 is a real observation: both teams played and neither scored. Getting
        every unresolved state right by defaulting to UNKNOWN, and catching 0-0
        in the same net, would silently discard roughly a tenth of all evidence.
        """
        record = settle_one(
            prediction(), match(home_goals=0, away_goals=0), settled_at=NOW, source=SOURCE
        )
        assert record.settlement_status is SettlementStatus.SETTLED
        assert record.gg_outcome is BttsOutcome.NO
        assert (record.final_home_goals, record.final_away_goals) == (0, 0)

    def test_one_sided_is_no(self):
        record = settle_one(
            prediction(), match(home_goals=3, away_goals=0), settled_at=NOW, source=SOURCE
        )
        assert record.gg_outcome is BttsOutcome.NO

    def test_the_settlement_is_terminal(self):
        assert settle_one(prediction(), match(), settled_at=NOW, source=SOURCE).is_terminal

    def test_the_provider_status_is_kept_as_evidence(self):
        """The verdict is auditable only if the raw status behind it is stored."""
        record = settle_one(prediction(), match(), settled_at=NOW, source=SOURCE)
        assert record.provider_status == "STATUS_FULL_TIME"


# ---------------------------------------------------------------------------
# An absence is never an observation
# ---------------------------------------------------------------------------
class TestNoFabricatedResult:
    @pytest.mark.parametrize(
        "status, reason",
        [
            ("STATUS_POSTPONED", UnresolvedReason.POSTPONED),
            ("STATUS_CANCELED", UnresolvedReason.CANCELLED),
            ("STATUS_CANCELLED", UnresolvedReason.CANCELLED),
            ("STATUS_ABANDONED", UnresolvedReason.ABANDONED),
        ],
    )
    def test_a_not_playable_fixture_is_unresolved_with_no_score(self, status, reason):
        """
        Postponed, cancelled (both spellings) and abandoned each get their own
        reason, and NONE of them gets a score. `0-0` here would be a fabricated
        goalless draw that no later test could distinguish from a real one.
        """
        record = settle_one(
            prediction(),
            match(status=status, completed=False, home_goals=None, away_goals=None),
            settled_at=NOW,
            source=SOURCE,
        )
        assert record.settlement_status is SettlementStatus.UNRESOLVED
        assert record.unresolved_reason is reason
        assert record.final_home_goals is None
        assert record.final_away_goals is None
        assert record.gg_outcome is BttsOutcome.UNKNOWN

    def test_an_abandoned_match_partial_score_is_discarded(self):
        """
        ABANDONED PROTECTION.

        A match abandoned at 1-0 is not a 1-0 result: the remaining minutes could
        have produced the second goal. Grading it as NO would invent an
        observation from an interrupted one, so the partial score is dropped
        rather than stored.
        """
        abandoned = match(status="STATUS_ABANDONED", completed=False, home_goals=1, away_goals=0)
        assert abandoned.home_goals == 1  # the provider object still carries it

        record = settle_one(prediction(), abandoned, settled_at=NOW, source=SOURCE)
        assert record.unresolved_reason is UnresolvedReason.ABANDONED
        assert record.final_home_goals is None
        assert record.gg_outcome is BttsOutcome.UNKNOWN

    def test_a_postponed_fixture_is_not_a_goalless_draw(self):
        """POSTPONED PROTECTION, stated as the thing that must not happen."""
        record = settle_one(
            prediction(),
            match(status="STATUS_POSTPONED", completed=False, home_goals=None, away_goals=None),
            settled_at=NOW,
            source=SOURCE,
        )
        assert record.gg_outcome is not BttsOutcome.NO
        assert record.gg_outcome is BttsOutcome.UNKNOWN

    def test_an_unplayed_fixture_is_not_yet_played(self):
        record = settle_one(
            prediction(),
            match(completed=False, home_goals=None, away_goals=None, status="STATUS_SCHEDULED"),
            settled_at=NOW,
            source=SOURCE,
        )
        assert record.unresolved_reason is UnresolvedReason.NOT_YET_PLAYED

    def test_a_missing_fixture_is_not_found(self):
        record = settle_one(prediction(), None, settled_at=NOW, source=SOURCE)
        assert record.unresolved_reason is UnresolvedReason.FIXTURE_NOT_FOUND
        assert record.gg_outcome is BttsOutcome.UNKNOWN

    def test_an_outage_is_distinguishable_from_a_missing_fixture(self):
        """
        "We could not ask" and "we asked and it is not there" are different
        facts. The first is an outage; the second is usually a season-key bug.
        Merging them would make a systematic join failure look like bad luck.
        """
        outage = settle_one(
            prediction(), None, settled_at=NOW, source=SOURCE, provider_available=False
        )
        absent = settle_one(prediction(), None, settled_at=NOW, source=SOURCE)
        assert outage.unresolved_reason is UnresolvedReason.PROVIDER_UNAVAILABLE
        assert absent.unresolved_reason is UnresolvedReason.FIXTURE_NOT_FOUND
        assert outage.unresolved_reason is not absent.unresolved_reason

    def test_an_outage_is_reported_even_when_a_match_is_supplied(self):
        """Availability is checked first: a stale object cannot mask an outage."""
        record = settle_one(
            prediction(), match(), settled_at=NOW, source=SOURCE, provider_available=False
        )
        assert record.unresolved_reason is UnresolvedReason.PROVIDER_UNAVAILABLE
        assert record.final_home_goals is None

    def test_terminal_reasons_are_only_cancelled_and_abandoned(self):
        """
        A postponed fixture is retried; a cancelled one never resolves.
        Retrying a cancellation forever is unbounded work with a known answer.
        """
        assert TERMINAL_REASONS == {UnresolvedReason.CANCELLED, UnresolvedReason.ABANDONED}
        assert UnresolvedReason.POSTPONED not in TERMINAL_REASONS


# ---------------------------------------------------------------------------
# Invariants: the record refuses to express a fabrication
# ---------------------------------------------------------------------------
class TestInvariants:
    def base(self, **overrides):
        fields = {
            "prediction_id": "pred-1",
            "fixture_id": "740123",
            "competition": "eng.1",
            "season": 2026,
            "final_home_goals": 2,
            "final_away_goals": 1,
            "gg_outcome": BttsOutcome.YES,
            "settlement_status": SettlementStatus.SETTLED,
            "settled_at": NOW,
            "source": SOURCE,
        }
        fields.update(overrides)
        return fields

    def test_settled_without_a_score_is_refused(self):
        with pytest.raises(ValueError, match="missing a score"):
            SettlementRecord(**self.base(final_home_goals=None, gg_outcome=BttsOutcome.UNKNOWN))

    def test_unresolved_with_a_score_is_refused(self):
        with pytest.raises(ValueError, match="carries a score"):
            SettlementRecord(
                **self.base(
                    settlement_status=SettlementStatus.UNRESOLVED,
                    unresolved_reason=UnresolvedReason.ABANDONED,
                    gg_outcome=BttsOutcome.UNKNOWN,
                )
            )

    def test_unresolved_without_a_reason_is_refused(self):
        with pytest.raises(ValueError, match="names no reason"):
            SettlementRecord(
                **self.base(
                    settlement_status=SettlementStatus.UNRESOLVED,
                    final_home_goals=None,
                    final_away_goals=None,
                    gg_outcome=BttsOutcome.UNKNOWN,
                )
            )

    def test_settled_with_a_reason_is_refused(self):
        with pytest.raises(ValueError, match="also carries"):
            SettlementRecord(**self.base(unresolved_reason=UnresolvedReason.POSTPONED))

    def test_an_outcome_that_contradicts_the_score_is_refused(self):
        """2-1 cannot be NO. The outcome must be a derivation, not a claim."""
        with pytest.raises(ValueError, match="must follow from the score"):
            SettlementRecord(**self.base(gg_outcome=BttsOutcome.NO))

    def test_a_naive_timestamp_is_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            SettlementRecord(**self.base(settled_at=datetime(2026, 8, 17, 12, 0)))

    def test_a_negative_score_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            SettlementRecord(**self.base(final_away_goals=-1))

    def test_a_boolean_score_is_refused(self):
        """`True > 0` is True in Python, so a stray bool would read as 1-0."""
        with pytest.raises(ValueError, match="non-integer"):
            SettlementRecord(**self.base(final_home_goals=True, final_away_goals=1))

    @pytest.mark.parametrize("field", ["prediction_id", "fixture_id", "competition", "source"])
    def test_an_empty_identity_field_is_refused(self, field):
        with pytest.raises(ValueError):
            SettlementRecord(**self.base(**{field: ""}))

    def test_the_record_is_immutable(self):
        """
        A settled result cannot be edited after the fact. `FrozenInstanceError`
        specifically, not any exception: the record must refuse the write, not
        merely fail somewhere.
        """
        record = SettlementRecord(**self.base())
        with pytest.raises(FrozenInstanceError):
            record.final_home_goals = 9


# ---------------------------------------------------------------------------
# Deterministic matching, and no team-name matching
# ---------------------------------------------------------------------------
class TestDeterministicMatching:
    def test_the_key_is_competition_season_and_event_id(self):
        assert lookup_key("eng.1", 2026, "740123") == ("eng.1", 2026, "740123")

    def test_the_event_id_is_normalised_to_a_string(self):
        """The ledger stores a str; a provider could hand back an int."""
        assert lookup_key("eng.1", 2026, 740123) == ("eng.1", 2026, "740123")

    def test_the_same_id_in_two_competitions_is_two_keys(self):
        """
        DUPLICATE EVENT ID PROTECTION.

        Nothing in the repo asserts an ESPN event id is unique across
        competitions, and `domain/historical.py` keys duplicates on the same
        three-part composite. A bare-id lookup would work almost always and
        silently return the wrong match in the case that matters.
        """
        assert lookup_key("eng.1", 2026, "740123") != lookup_key("esp.1", 2026, "740123")

    def test_the_same_id_in_two_seasons_is_two_keys(self):
        assert lookup_key("eng.1", 2025, "740123") != lookup_key("eng.1", 2026, "740123")

    def test_matching_never_consults_a_team_name(self):
        """
        NO TEAM-NAME MATCHING (GG-008).

        A match whose team names are wholly unrelated to the prediction still
        settles, because identity is the event id alone. This is the property
        that keeps the substring-matching defect in the odds clients
        ("Athletic" vs "Athletic Club") out of settlement.
        """
        renamed = match(home_team_name="Totally Different FC", away_team_name="Nobody United")
        record = settle_one(prediction(), renamed, settled_at=NOW, source=SOURCE)
        assert record.settlement_status is SettlementStatus.SETTLED
        assert record.gg_outcome is BttsOutcome.YES

    def test_a_record_carries_no_team_name_at_all(self):
        """The settlement schema has no field a name comparison could use."""
        record = to_json_dict(settle_one(prediction(), match(), settled_at=NOW, source=SOURCE))
        assert not [key for key in record if "team" in key or "name" in key]

    def test_settlement_is_a_function_of_its_inputs(self):
        first = settle_one(prediction(), match(), settled_at=NOW, source=SOURCE)
        second = settle_one(prediction(), match(), settled_at=NOW, source=SOURCE)
        assert to_json_dict(first) == to_json_dict(second)


# ---------------------------------------------------------------------------
# The season mismatch, fixed at the lookup boundary only
# ---------------------------------------------------------------------------
class TestSeasonBoundary:
    def test_the_stored_season_is_tried_first(self):
        keys = candidate_lookup_keys("eng.1", 2026, "740123")
        assert keys[0] == ("eng.1", 2026, "740123")

    def test_the_adjacent_seasons_are_candidates(self):
        """
        VERIFIED MISMATCH (2H-F3): the ledger stores
        `espn.resolve_season`, which rolls over on 1 July, while history stores
        the event's own `season.year`. ESPN's eng.1 season 2025 ends 2026-06-01,
        so a 2026-06-15 fixture is ESPN season 2026 while `resolve_season`
        returns 2025. Measured, not assumed.

        The question is widened; the stored prediction is never rewritten.
        """
        keys = candidate_lookup_keys("eng.1", 2026, "740123")
        assert ("eng.1", 2027, "740123") in keys
        assert ("eng.1", 2025, "740123") in keys

    def test_a_missing_season_yields_one_candidate(self):
        assert candidate_lookup_keys("eng.1", None, "740123") == (("eng.1", None, "740123"),)

    def test_the_matched_season_is_recorded_when_it_differs(self):
        """
        Drift must be visible in the data, not hidden by the retry that tolerates
        it. `matched_season` is how a systematic disagreement gets noticed.
        """
        record = settle_one(
            prediction(season=2026), match(season=2027), settled_at=NOW, source=SOURCE
        )
        assert record.season == 2026
        assert record.matched_season == 2027

    def test_the_stored_season_is_never_altered(self):
        record = settle_one(prediction(season=2026), match(season=2027), settled_at=NOW, source=SOURCE)
        assert record.season == 2026, "the prediction's own season must be reported unchanged"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
class TestSerialisation:
    def test_the_key_order_is_fixed(self):
        record = settle_one(prediction(), match(), settled_at=NOW, source=SOURCE)
        assert tuple(to_json_dict(record)) == FIELD_ORDER

    def test_the_timestamp_is_normalised_to_utc(self):
        local = datetime(2026, 8, 17, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
        record = settle_one(prediction(), match(), settled_at=local, source=SOURCE)
        assert to_json_dict(record)["settled_at"] == "2026-08-17T12:00:00+00:00"

    def test_the_schema_version_is_its_own(self):
        record = settle_one(prediction(), match(), settled_at=NOW, source=SOURCE)
        assert record.schema_version == SETTLEMENT_SCHEMA_VERSION == "2h.1"

    def test_no_price_or_probability_is_carried(self):
        """
        The odds firewall. A settlement states what happened; it must not hand a
        price-bearing object to the evaluation layer.
        """
        payload = to_json_dict(
            settle_one(
                prediction(odds=1.85, probability=0.55, recommendation="BET"),
                match(),
                settled_at=NOW,
                source=SOURCE,
            )
        )
        for forbidden in ("odds", "probability", "edge", "recommendation", "stake", "roi"):
            assert forbidden not in payload

    def test_the_unresolved_reason_serialises_as_a_name(self):
        record = settle_one(prediction(), None, settled_at=NOW, source=SOURCE)
        assert to_json_dict(record)["unresolved_reason"] == "FIXTURE_NOT_FOUND"


# ---------------------------------------------------------------------------
# classify() in isolation
# ---------------------------------------------------------------------------
class TestClassify:
    def test_a_completed_match_is_settled(self):
        assert classify(match()) == (SettlementStatus.SETTLED, None)

    def test_an_outage_wins_over_everything(self):
        assert classify(match(), provider_available=False) == (
            SettlementStatus.UNRESOLVED,
            UnresolvedReason.PROVIDER_UNAVAILABLE,
        )

    def test_a_status_is_matched_case_insensitively(self):
        status, reason = classify(
            match(status="status_postponed", completed=False, home_goals=None, away_goals=None)
        )
        assert reason is UnresolvedReason.POSTPONED

    def test_a_completed_match_without_a_score_is_missing_result(self):
        """
        A provider contradiction, not football. Kept separate from ABANDONED
        because it is a data-quality signal about the feed.

        `HistoricalMatch` refuses this combination, so the object is built
        first and the field forced afterwards - the only way to reach the branch.
        """
        contradictory = match()
        object.__setattr__(contradictory, "home_goals", None)
        status, reason = classify(contradictory)
        assert status is SettlementStatus.UNRESOLVED
        assert reason is UnresolvedReason.MISSING_RESULT
