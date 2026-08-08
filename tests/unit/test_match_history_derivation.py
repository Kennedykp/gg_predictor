"""
Hand-calculated history derivations (Epic 1B.4, TASK 23).

Every expected value here was worked out by hand from an explicit list of
scorelines and is written as a literal fraction, not as a re-implementation of
the production formula. A test that computes its own expectation the same way
the code does cannot detect a wrong formula - it only detects a crash.

Pure domain: no ESPN, no network, no monkeypatching.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from domain.match_records import (
    DerivedHistory,
    MatchRecord,
    Venue,
    derive_history,
    eligible_history,
)

TARGET = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
LEAGUE = "eng.1"


def _record(
    goals_for,
    goals_against,
    venue=Venue.HOME,
    days_before=10,
    event_id=None,
    competition=LEAGUE,
    completed=True,
):
    """A completed league record, `days_before` days before the target kickoff."""
    return MatchRecord(
        venue=venue,
        goals_for=goals_for,
        goals_against=goals_against,
        completed=completed,
        kickoff=TARGET - timedelta(days=days_before),
        event_id=event_id,
        competition=competition,
    )


class TestHomeTeamHomeHistory:
    """
    TASK 23, worked example. Home team's HOME matches:

        2-0   GF=2 GA=0   clean sheet   BTTS no
        1-1   GF=1 GA=1                 BTTS yes
        3-0   GF=3 GA=0   clean sheet   BTTS no
        0-2   GF=0 GA=2                 BTTS no
        2-0   GF=2 GA=0   clean sheet   BTTS no

    clean sheets = 3/5 = 0.60
    BTTS         = 1/5 = 0.20
    """

    HISTORY = [
        _record(2, 0, days_before=35, event_id="1"),
        _record(1, 1, days_before=28, event_id="2"),
        _record(3, 0, days_before=21, event_id="3"),
        _record(0, 2, days_before=14, event_id="4"),
        _record(2, 0, days_before=7, event_id="5"),
    ]

    def test_clean_sheet_pct_is_three_fifths(self):
        history = derive_history(self.HISTORY, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.clean_sheet_pct == 3 / 5
        assert history.clean_sheet_pct == 0.60

    def test_btts_pct_is_one_fifth(self):
        history = derive_history(self.HISTORY, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.both_teams_scored_pct == 1 / 5
        assert history.both_teams_scored_pct == 0.20

    def test_sample_size_is_five(self):
        history = derive_history(self.HISTORY, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.sample_size == 5


class TestAwayTeamAwayHistory:
    """
    TASK 23, worked example. The away team's AWAY matches, written as the home
    side saw them and then inverted to this team's perspective:

        Home 0-1 Team   GF=1 GA=0   clean sheet   BTTS no
        Home 2-2 Team   GF=2 GA=2                 BTTS yes
        Home 3-0 Team   GF=0 GA=3                 BTTS no
        Home 0-2 Team   GF=2 GA=0   clean sheet   BTTS no

    GA sequence  = 0, 2, 3, 0
    clean sheets = 2/4 = 0.50
    BTTS         = 1/4 = 0.25
    """

    HISTORY = [
        _record(1, 0, venue=Venue.AWAY, days_before=28, event_id="1"),
        _record(2, 2, venue=Venue.AWAY, days_before=21, event_id="2"),
        _record(0, 3, venue=Venue.AWAY, days_before=14, event_id="3"),
        _record(2, 0, venue=Venue.AWAY, days_before=7, event_id="4"),
    ]

    def test_clean_sheet_pct_is_one_half(self):
        history = derive_history(self.HISTORY, target_kickoff=TARGET, venue=Venue.AWAY)

        assert history.clean_sheet_pct == 2 / 4
        assert history.clean_sheet_pct == 0.50

    def test_btts_pct_is_one_quarter(self):
        history = derive_history(self.HISTORY, target_kickoff=TARGET, venue=Venue.AWAY)

        assert history.both_teams_scored_pct == 1 / 4
        assert history.both_teams_scored_pct == 0.25

    def test_sample_size_is_four(self):
        history = derive_history(self.HISTORY, target_kickoff=TARGET, venue=Venue.AWAY)

        assert history.sample_size == 4


class TestBttsScorelines:
    """TASK 14. BTTS is `goals_for > 0 AND goals_against > 0`, nothing else."""

    @pytest.mark.parametrize(
        "goals_for,goals_against,expected",
        [
            (0, 0, False),   # goalless
            (1, 0, False),   # only we scored
            (0, 2, False),   # only they scored
            (1, 1, True),
            (3, 1, True),
            (2, 4, True),
        ],
    )
    def test_btts_classification(self, goals_for, goals_against, expected):
        history = derive_history(
            [_record(goals_for, goals_against)], target_kickoff=TARGET, venue=Venue.HOME
        )

        assert history.both_teams_scored_pct == (1.0 if expected else 0.0)


class TestCleanSheetExamples:
    """TASK 13. Worked GA sequences."""

    def test_ga_sequence_0_1_0_2_0_gives_zero_point_six(self):
        records = [
            _record(1, 0, event_id="1", days_before=50),
            _record(1, 1, event_id="2", days_before=40),
            _record(2, 0, event_id="3", days_before=30),
            _record(1, 2, event_id="4", days_before=20),
            _record(3, 0, event_id="5", days_before=10),
        ]

        history = derive_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.clean_sheet_pct == 3 / 5

    def test_ga_sequence_1_1_1_is_a_genuine_zero(self):
        """
        0.0 is a MEASUREMENT: this team played three times and never kept a
        clean sheet. It must stay distinguishable from None.
        """
        records = [
            _record(2, 1, event_id="1", days_before=30),
            _record(1, 1, event_id="2", days_before=20),
            _record(0, 1, event_id="3", days_before=10),
        ]

        history = derive_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.clean_sheet_pct == 0.0
        assert history.clean_sheet_pct is not None
        assert history.sample_size == 3
        assert history.is_available is True

    def test_no_eligible_matches_is_unavailable_not_zero(self):
        history = derive_history([], target_kickoff=TARGET, venue=Venue.HOME)

        assert history.clean_sheet_pct is None
        assert history.both_teams_scored_pct is None
        assert history.sample_size == 0
        assert history.is_available is False


class TestCutoffBoundary:
    """TASK 8. Strict `<`, tested one second either side."""

    def test_one_second_before_target_is_included(self):
        record = MatchRecord(
            venue=Venue.HOME, goals_for=1, goals_against=0, completed=True,
            kickoff=TARGET - timedelta(seconds=1), event_id="1", competition=LEAGUE,
        )

        assert len(eligible_history([record], target_kickoff=TARGET, venue=Venue.HOME)) == 1

    def test_exactly_at_target_is_excluded(self):
        record = MatchRecord(
            venue=Venue.HOME, goals_for=1, goals_against=0, completed=True,
            kickoff=TARGET, event_id="1", competition=LEAGUE,
        )

        assert eligible_history([record], target_kickoff=TARGET, venue=Venue.HOME) == []

    def test_one_second_after_target_is_excluded(self):
        record = MatchRecord(
            venue=Venue.HOME, goals_for=1, goals_against=0, completed=True,
            kickoff=TARGET + timedelta(seconds=1), event_id="1", competition=LEAGUE,
        )

        assert eligible_history([record], target_kickoff=TARGET, venue=Venue.HOME) == []

    def test_record_without_kickoff_is_excluded(self):
        """It cannot prove it happened first, so it is not evidence."""
        record = MatchRecord(
            venue=Venue.HOME, goals_for=1, goals_against=0, completed=True, competition=LEAGUE,
        )

        assert eligible_history([record], target_kickoff=TARGET, venue=Venue.HOME) == []

    def test_future_match_cannot_inflate_a_clean_sheet_rate(self):
        """
        The leak this Epic exists to prevent: a match played AFTER the fixture
        would move the rate from 0/1 to 1/2 while looking perfectly normal.
        """
        records = [
            _record(1, 1, days_before=7, event_id="past"),
            _record(3, 0, days_before=-7, event_id="future"),
        ]

        history = derive_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.sample_size == 1
        assert history.clean_sheet_pct == 0.0


class TestTimezoneSafety:
    """TASK 10."""

    def test_naive_target_kickoff_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            eligible_history([], target_kickoff=datetime(2026, 9, 1, 15, 0), venue=Venue.HOME)

    def test_naive_record_kickoff_is_rejected_at_construction(self):
        """
        Rejected when built, not when compared. A naive datetime that survives
        into the cutoff either raises TypeError deep in a loop or, if someone
        "fixes" it by stripping tzinfo, silently misorders records.
        """
        with pytest.raises(ValueError, match="timezone-aware"):
            MatchRecord(
                venue=Venue.HOME, goals_for=1, goals_against=0, completed=True,
                kickoff=datetime(2026, 8, 1, 12, 0),
            )

    def test_equivalent_instants_in_different_offsets_compare_equally(self):
        """
        14:00Z and 15:00+01:00 are the same moment, so both must fall on the
        same side of a cutoff at 14:30Z.
        """
        utc = MatchRecord(
            venue=Venue.HOME, goals_for=1, goals_against=0, completed=True,
            kickoff=datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc),
            event_id="1", competition=LEAGUE,
        )
        offset = MatchRecord(
            venue=Venue.HOME, goals_for=1, goals_against=0, completed=True,
            kickoff=datetime(2026, 8, 1, 15, 0, tzinfo=timezone(timedelta(hours=1))),
            event_id="2", competition=LEAGUE,
        )
        cutoff = datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc)

        assert len(eligible_history([utc, offset], target_kickoff=cutoff, venue=Venue.HOME)) == 2


class TestExclusionRules:
    def test_target_event_id_is_excluded(self):
        records = [
            _record(1, 0, days_before=7, event_id="TARGET"),
            _record(1, 1, days_before=14, event_id="OTHER"),
        ]

        eligible = eligible_history(
            records, target_kickoff=TARGET, venue=Venue.HOME, exclude_event_id="TARGET"
        )

        assert [r.event_id for r in eligible] == ["OTHER"]

    def test_duplicate_event_ids_are_deduplicated(self):
        records = [
            _record(1, 0, days_before=7, event_id="A"),
            _record(1, 0, days_before=7, event_id="A"),
            _record(1, 1, days_before=14, event_id="B"),
        ]

        eligible = eligible_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert len(eligible) == 2

    def test_identical_scores_with_different_ids_are_both_kept(self):
        """
        TASK 11: dedup on event ID only. Two genuine 1-0 home wins are two
        matches, and collapsing them by scoreline would understate the sample.
        """
        records = [
            _record(1, 0, days_before=7, event_id="A"),
            _record(1, 0, days_before=14, event_id="B"),
        ]

        eligible = eligible_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert len(eligible) == 2

    def test_other_competition_is_excluded_when_requested(self):
        records = [
            _record(1, 1, days_before=7, event_id="league"),
            _record(4, 0, days_before=14, event_id="cup", competition="eng.fa"),
        ]

        history = derive_history(
            records, target_kickoff=TARGET, venue=Venue.HOME, competition=LEAGUE
        )

        assert history.sample_size == 1
        assert history.clean_sheet_pct == 0.0

    def test_unknown_competition_is_excluded_not_assumed(self):
        records = [_record(3, 0, days_before=7, event_id="x", competition=None)]

        history = derive_history(
            records, target_kickoff=TARGET, venue=Venue.HOME, competition=LEAGUE
        )

        assert history.sample_size == 0

    def test_incomplete_match_is_excluded(self):
        records = [
            _record(1, 1, days_before=7, event_id="played"),
            _record(0, 0, days_before=14, event_id="abandoned", completed=False),
        ]

        history = derive_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.sample_size == 1

    def test_wrong_venue_is_excluded(self):
        records = [
            _record(1, 1, venue=Venue.HOME, days_before=7, event_id="h"),
            _record(2, 0, venue=Venue.AWAY, days_before=14, event_id="a"),
        ]

        history = derive_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.sample_size == 1
        assert history.clean_sheet_pct == 0.0


class TestDerivedHistoryContract:
    def test_both_rates_share_one_sample_size(self):
        """
        TASK 15. Both percentages come from the same eligible set, so a single
        `sample_size` describes both - documented here as an assertion rather
        than only in prose.
        """
        records = [
            _record(1, 0, days_before=7, event_id="1"),
            _record(1, 1, days_before=14, event_id="2"),
            _record(0, 2, days_before=21, event_id="3"),
        ]

        history = derive_history(records, target_kickoff=TARGET, venue=Venue.HOME)

        assert history.sample_size == 3
        assert history.clean_sheet_pct == 1 / 3
        assert history.both_teams_scored_pct == 1 / 3

    def test_venue_and_competition_are_recorded_on_the_result(self):
        """Provenance: which slice of history produced these numbers."""
        history = derive_history(
            [_record(1, 0, days_before=7, event_id="1")],
            target_kickoff=TARGET,
            venue=Venue.HOME,
            competition=LEAGUE,
        )

        assert history.venue == Venue.HOME
        assert history.competition == LEAGUE

    def test_derived_history_is_immutable(self):
        history = DerivedHistory(
            clean_sheet_pct=0.5, both_teams_scored_pct=0.5, sample_size=2, venue=Venue.HOME
        )

        # FrozenInstanceError specifically, not a bare Exception: the point is
        # that the dataclass is frozen, not merely that some error occurred.
        with pytest.raises(FrozenInstanceError):
            history.clean_sheet_pct = 0.9

