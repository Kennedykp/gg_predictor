"""
LEAK-001 — the model may only see what existed before kickoff.

THE DEFECT
----------
Every POISSON_V1 input came from ESPN's aggregate team endpoint, which reports
the season AS IT STANDS TODAY. For an upcoming fixture that is merely stale. For
a fixture that has already been played it is circular:

    scoring Arsenal 3-1 Chelsea from 2026-02-08
      -> "Arsenal home goals scored" already contains those 3 goals
      -> the match is evidence for predicting itself

Any backtest built on that would look excellent and mean nothing, which is the
dangerous failure mode: it does not crash, it flatters.

WHAT THESE TESTS PIN
--------------------
The inputs are now derived from completed matches with `kickoff < target`. The
properties below are the ones that make that claim real rather than nominal:

    1. a match at or after kickoff cannot influence the numbers
    2. the target fixture cannot influence itself
    3. adding future matches to the feed changes nothing at all
    4. no cutoff means no probability, never a fallback to today's aggregates

Property 3 is the strongest of the four. The first two can be satisfied by
filtering one obvious record; invariance under arbitrary future data is what
demonstrates the cutoff is structural.

These tests are deliberately behavioural - they assert on derived values, not on
which functions were called - so the derivation can be reimplemented freely as
long as the leak stays closed.
"""

from datetime import timedelta
from typing import Any, Dict, List

import pytest
from conftest import espn_event, utc

import espn
from shared.match_history import build_fixture_poisson_inputs

LEAGUE = "eng.1"
HOME_ID = "359"
AWAY_ID = "360"
TARGET_ID = "555"
KICKOFF = utc(2026, 2, 8, 15, 0)

# Five completed home matches for the home side, all comfortably before kickoff.
# 2 scored / 1 conceded each time, so the expected averages are exactly 2.0 and
# 1.0 - round numbers, so a contaminating record is visible by inspection.
HOME_PRIOR = [espn_event(f"hp{i}", utc(2025, 10, i + 1), HOME_ID, "900", 2, 1) for i in range(5)]
AWAY_PRIOR = [espn_event(f"ap{i}", utc(2025, 10, i + 1), "901", AWAY_ID, 1, 2) for i in range(5)]
LEAGUE_PRIOR = HOME_PRIOR + AWAY_PRIOR

FIXTURE: Dict[str, Any] = {
    "fixture_id": TARGET_ID,
    "league_id": LEAGUE,
    "home_team_id": HOME_ID,
    "away_team_id": AWAY_ID,
    "kickoff_utc": KICKOFF,
}


def inputs_from(feed, home_events, away_events, league_events=None):
    """Install a schedule/scoreboard feed and assemble the five model inputs."""
    feed(
        team_events={HOME_ID: home_events, AWAY_ID: away_events},
        league_events=LEAGUE_PRIOR if league_events is None else league_events,
    )
    return build_fixture_poisson_inputs(
        FIXTURE,
        averages_provider=espn.get_team_venue_averages,
        baseline_provider=espn.get_league_baseline,
    )


@pytest.fixture
def baseline(espn_feed):
    """The uncontaminated reference every leak test is compared against."""
    return inputs_from(espn_feed, HOME_PRIOR, AWAY_PRIOR)


class TestBaselineIsMeaningful:
    """
    Establish the reference is real before asserting things cannot change it.
    Without this, every invariance test below could pass by returning None.
    """

    def test_baseline_is_complete_and_exact(self, baseline):
        assert baseline.is_complete
        assert baseline.home_goals_scored_home == 2.0
        assert baseline.home_goals_conceded_home == 1.0
        assert baseline.away_goals_scored_away == 2.0
        assert baseline.away_goals_conceded_away == 1.0


class TestTheTargetFixtureCannotPredictItself:
    """The circularity at the centre of LEAK-001."""

    def test_target_result_in_the_feed_is_ignored(self, espn_feed, baseline):
        """
        The exact leak: the fixture being scored appears in the feed with its
        own result. A 9-0 is chosen so that including it could not possibly be
        mistaken for noise - it would drag the 2.0 average to 3.17.
        """
        leaked = espn_event(TARGET_ID, KICKOFF, HOME_ID, AWAY_ID, 9, 0)

        contaminated = inputs_from(
            espn_feed,
            HOME_PRIOR + [leaked],
            AWAY_PRIOR + [leaked],
            LEAGUE_PRIOR + [leaked],
        )

        assert contaminated == baseline

    def test_target_excluded_by_id_even_when_timestamp_is_wrong(self, espn_feed, baseline):
        """
        Defence in depth. If a provider misreports the target's kickoff as
        earlier than it is, the timestamp cutoff alone would admit it. The
        event-ID exclusion is what still catches it.
        """
        misdated = espn_event(TARGET_ID, utc(2025, 10, 1), HOME_ID, AWAY_ID, 9, 0)

        contaminated = inputs_from(
            espn_feed,
            HOME_PRIOR + [misdated],
            AWAY_PRIOR + [misdated],
            LEAGUE_PRIOR + [misdated],
        )

        assert contaminated == baseline


class TestTheCutoffIsStrict:
    """`kickoff < target`, not `<=`."""

    def test_match_one_second_before_kickoff_is_included(self, espn_feed, baseline):
        """
        The boundary must not be so cautious it discards real evidence. This
        match genuinely finished before the target, so it must count - and it
        must move the average, or the test proves nothing.
        """
        just_before = espn_event("jb", KICKOFF - timedelta(seconds=1), HOME_ID, "902", 8, 0)

        result = inputs_from(espn_feed, HOME_PRIOR + [just_before], AWAY_PRIOR)

        assert result.home_goals_scored_home != baseline.home_goals_scored_home
        assert result.home_goals_scored_home == pytest.approx((2 * 5 + 8) / 6)

    def test_match_exactly_at_kickoff_is_excluded(self, espn_feed, baseline):
        """
        Simultaneous kickoff is not prior knowledge. A `<=` boundary would admit
        it, so this is the test that distinguishes the two.
        """
        simultaneous = espn_event("sim", KICKOFF, HOME_ID, "902", 9, 0)

        result = inputs_from(espn_feed, HOME_PRIOR + [simultaneous], AWAY_PRIOR)

        assert result == baseline

    def test_match_after_kickoff_is_excluded(self, espn_feed, baseline):
        later = espn_event("post", utc(2026, 3, 1), HOME_ID, "902", 9, 0)

        result = inputs_from(espn_feed, HOME_PRIOR + [later], AWAY_PRIOR)

        assert result == baseline


class TestInvarianceUnderFutureData:
    """
    The strongest property: what happens AFTER the fixture is irrelevant to it.

    A backtest replays fixtures against a feed containing the entire season. If
    later rounds could move an earlier fixture's inputs, every historical result
    would be contaminated by hindsight - and it would still look plausible.
    """

    def test_a_whole_future_season_changes_nothing(self, espn_feed, baseline):
        future: List[Dict[str, Any]] = [
            espn_event(f"f{i}", utc(2026, 3 + (i % 4), 1 + i), HOME_ID, "903", 7, 0)
            for i in range(30)
        ]

        result = inputs_from(espn_feed, HOME_PRIOR + future, AWAY_PRIOR, LEAGUE_PRIOR + future)

        assert result == baseline

    def test_league_baseline_is_also_point_in_time(self, espn_feed, baseline):
        """
        The baseline is the input most easily forgotten - it is league-wide, so
        it feels like a constant. It is not: computed over the full season it
        carries hindsight into every fixture scored against it.
        """
        future_league = [
            espn_event(f"lf{i}", utc(2026, 4, 1 + i), "904", "905", 6, 6) for i in range(20)
        ]

        result = inputs_from(espn_feed, HOME_PRIOR, AWAY_PRIOR, LEAGUE_PRIOR + future_league)

        assert result.league_avg_goals == baseline.league_avg_goals


class TestNoCutoffMeansNoProbability:
    """
    TASK 25. Without a kickoff there is no instant to be point-in-time relative
    to. The tempting fallback - today's aggregates - is exactly the leak, so the
    only safe answer is to refuse.
    """

    def test_missing_kickoff_yields_unavailable_inputs(self, espn_feed):
        espn_feed(
            team_events={HOME_ID: HOME_PRIOR, AWAY_ID: AWAY_PRIOR},
            league_events=LEAGUE_PRIOR,
        )
        without_kickoff = {k: v for k, v in FIXTURE.items() if k != "kickoff_utc"}

        result = build_fixture_poisson_inputs(
            without_kickoff,
            averages_provider=espn.get_team_venue_averages,
            baseline_provider=espn.get_league_baseline,
        )

        assert not result.is_complete
        assert result.home_goals_scored_home is None
        assert result.league_avg_goals is None

    def test_no_fallback_to_current_season_aggregates(self, espn_feed, monkeypatch):
        """
        The regression that would silently undo this Epic. If `get_team_stats`
        is ever consulted while assembling model inputs, the leak is back - so
        calling it is made an outright error rather than a wrong number.
        """

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "get_team_stats reached from the model-input path: current-season "
                "aggregates are not point-in-time (LEAK-001)"
            )

        monkeypatch.setattr(espn, "get_team_stats", forbidden)

        # Thin history is when a fallback would be most tempting to add.
        result = inputs_from(espn_feed, HOME_PRIOR[:1], [], league_events=[])

        assert result.home_goals_scored_home == 2.0
        assert result.away_goals_scored_away is None
        assert not result.is_complete
