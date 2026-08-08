"""
Exact derivations from completed match records (Epic 1B.3, Tasks 15-16).

These functions are the *definition* of the statistics - kept small and
hand-verifiable on purpose, and this file is the proof. ESPN's aggregate
standings cannot currently feed them (documented in docs/EPIC_1B3_FILTER_WIRING.md
as UNAVAILABLE), but they are the exact contract a future match-by-match source
must satisfy, and they will never be rebuilt ad hoc at a call site.

`MatchRecord` is a single team's perspective on one fixture: which venue it was
at, how many it scored and conceded, and whether the match actually finished.
Only completed, fully-scored records are usable.
"""

import pytest

from domain import (
    MatchRecord,
    Venue,
    both_teams_scored_pct,
    clean_sheet_pct,
    completed_matches,
)


def played(
    venue: str,
    goals_for: int,
    goals_against: int,
    completed: bool = True,
) -> MatchRecord:
    return MatchRecord(
        venue=venue,
        goals_for=goals_for,
        goals_against=goals_against,
        completed=completed,
    )


class TestBTTSFromCompletedMatches:
    """TASK 15. BTTS counts only completed matches where BOTH teams scored."""

    @pytest.mark.parametrize(
        "goals_for, goals_against",
        [
            (0, 0),  # neither team scored
            (1, 0),  # our team scored, opponent did not
            (0, 2),  # opponent scored, our team did not
            (5, 0),  # emphatic one-sided
        ],
    )
    def test_no(self, goals_for, goals_against):
        assert both_teams_scored_pct([played(Venue.HOME, goals_for, goals_against)]) == 0.0

    @pytest.mark.parametrize(
        "goals_for, goals_against",
        [
            (1, 1),  # the canonical GG scoreline
            (2, 1),
            (3, 4),
            (1, 5),  # lopsided but both scored
        ],
    )
    def test_yes(self, goals_for, goals_against):
        assert both_teams_scored_pct([played(Venue.HOME, goals_for, goals_against)]) == 1.0

    def test_hand_verifiable_mixed_batch(self):
        matches = [
            played(Venue.HOME, 0, 0),  # NO
            played(Venue.HOME, 1, 0),  # NO
            played(Venue.HOME, 0, 2),  # NO
            played(Venue.HOME, 1, 1),  # YES
            played(Venue.HOME, 2, 1),  # YES
            played(Venue.HOME, 3, 4),  # YES
        ]
        assert both_teams_scored_pct(matches) == pytest.approx(3 / 6)

    def test_empty_history_is_unavailable(self):
        assert both_teams_scored_pct([]) is None

    def test_no_completed_matches_is_unavailable(self):
        records = [played(Venue.HOME, 1, 1, completed=False)]
        assert completed_matches(records) == []
        assert both_teams_scored_pct(records) is None


class TestCompletedMatchesFilter:
    """Only matches that actually finished may be counted."""

    @pytest.mark.parametrize("completed", [False])
    def test_incomplete_fixtures_are_ignored(self, completed):
        records = [
            played(Venue.HOME, 1, 1, completed=completed),   # not completed
            played(Venue.HOME, 1, 1),                        # completed, BTTS
            played(Venue.HOME, 0, 0),                        # completed, no BTTS
        ]
        done = completed_matches(records)
        assert len(done) == 2
        assert both_teams_scored_pct(records) == pytest.approx(0.5)

    def test_uncompleted_match_must_not_be_counted_as_btts(self):
        """
        The specific danger: an unfinished 1-1 counts as YES if we forget to
        check completion, even though the match has not happened. `completed` is
        explicit rather than inferred from the presence of a score, because a
        postponed fixture can still carry a 0-0.
        """
        records = [played(Venue.HOME, 1, 1, completed=False)]
        assert both_teams_scored_pct(records) is None

    def test_venue_filter_counts_only_that_venue(self):
        records = [
            played(Venue.HOME, 1, 1),
            played(Venue.AWAY, 1, 1),
            played(Venue.HOME, 0, 0),
        ]
        assert both_teams_scored_pct(records, venue=Venue.HOME) == pytest.approx(0.5)
        assert both_teams_scored_pct(records, venue=Venue.AWAY) == pytest.approx(1.0)


class TestCleanSheetFromCompletedMatches:
    """TASK 16. A clean sheet is conceded 0, evaluated from the right venue."""

    @pytest.mark.parametrize("conceded", [0, 1, 2, 5])
    def test_conceding_zero_is_a_clean_sheet(self, conceded):
        records = [played(Venue.HOME, goals_for=3, goals_against=conceded)]
        assert clean_sheet_pct(records, venue=Venue.HOME) == (1.0 if conceded == 0 else 0.0)

    @pytest.mark.parametrize("conceded", [0, 1, 2, 5])
    def test_away_perspective_uses_away_matches(self, conceded):
        records = [played(Venue.AWAY, goals_for=3, goals_against=conceded)]
        assert clean_sheet_pct(records, venue=Venue.AWAY) == (1.0 if conceded == 0 else 0.0)

    def test_home_and_away_do_not_share_matches(self):
        """Each perspective uses only that team's actual matches at that venue."""
        records = [
            played(Venue.HOME, 1, 0),  # home, conceded 0 -> clean sheet
            played(Venue.HOME, 2, 0),  # home, conceded 0 -> clean sheet
            played(Venue.HOME, 0, 1),  # home, conceded 1 -> no
            played(Venue.AWAY, 1, 0),  # away, conceded 0 -> clean sheet
            played(Venue.AWAY, 0, 2),  # away, conceded 2 -> no
        ]
        # Home: 2 of 3. Away: 1 of 2. Pooling all five would give 3/5 - a
        # different number from either, which is exactly the error this guards.
        assert clean_sheet_pct(records, venue=Venue.HOME) == pytest.approx(2 / 3)
        assert clean_sheet_pct(records, venue=Venue.AWAY) == pytest.approx(1 / 2)
        assert clean_sheet_pct(records) == pytest.approx(3 / 5)


    def test_mixed_scores_hand_verifiable(self):
        """Clean sheet when the OPPONENT's score is 0; the team's own goals are irrelevant."""
        records = [
            played(Venue.HOME, 3, 0),  # clean sheet
            played(Venue.HOME, 0, 2),  # no
            played(Venue.HOME, 1, 1),  # no
            played(Venue.HOME, 4, 0),  # clean sheet
        ]
        assert clean_sheet_pct(records, venue=Venue.HOME) == pytest.approx(0.5)

    def test_own_goals_do_not_affect_clean_sheet(self):
        """Scoring 5 is still a clean sheet if the opponent scored 0."""
        records = [played(Venue.HOME, 5, 0)]
        assert clean_sheet_pct(records, venue=Venue.HOME) == 1.0

    def test_empty_history_is_unavailable(self):
        assert clean_sheet_pct([], venue=Venue.HOME) is None

    def test_no_completed_home_matches_is_unavailable(self):
        records = [played(Venue.AWAY, 1, 1)]  # no completed HOME matches
        assert clean_sheet_pct(records, venue=Venue.HOME) is None

    def test_genuine_zero_is_a_real_rate(self):
        """A team that has conceded in every home match has a genuine 0% rate."""
        records = [played(Venue.HOME, 0, 1), played(Venue.HOME, 2, 3)]
        assert clean_sheet_pct(records, venue=Venue.HOME) == 0.0
