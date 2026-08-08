"""
Match history through the full pipeline (Epic 1B.4, TASK 17 / 18 / 19 / 25).

These go end to end from a fixture dict to a filter verdict, through the same
`build_fixture_filter_stats` both entry points call. Offline: the history
provider is a local stub, so no socket is opened.

The question each test answers is not "does the code run" but "can a bad number
reach the decision layer". The two failure modes that matter:

  - a match that had not been played yet becoming evidence for a fixture
  - an ABSENT statistic arriving at the filter as 0.0, which passes
    MAX_CLEAN_SHEET_PCT and produces a recommendation from nothing
"""

from datetime import datetime, timedelta, timezone

import pytest

from domain.filter_evaluation import FilterOutcome, evaluate_filters
from domain.match_records import MatchRecord, Venue, derive_history
from shared.match_history import build_fixture_filter_stats

KICKOFF = datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)
LEAGUE = "eng.1"


FIXTURE = {
    "fixture_id": "TARGET",
    "league_id": LEAGUE,
    "home_team_id": "359",
    "away_team_id": "382",
    "home_team_name": "Arsenal",
    "away_team_name": "Chelsea",
    "kickoff_utc": KICKOFF,
}


def _stats(scored, conceded):
    """
    Aggregate team stats good enough to reach the filter layer.

    `home_clean_sheet_pct` / `away_clean_sheet_pct` are None on purpose: that is
    what ESPN's standings aggregates actually provide, and it is why this Epic
    exists. When history IS derived it overrides these; when it is not, the
    statistic stays unavailable rather than becoming zero.
    """
    return {
        "home_goals_scored": scored,
        "home_goals_conceded": conceded,
        "away_goals_scored": scored,
        "away_goals_conceded": conceded,
        "home_clean_sheet_pct": None,
        "away_clean_sheet_pct": None,
        "matches_played": 10,
    }



def _record(goals_for, goals_against, venue, days_before, event_id):
    return MatchRecord(
        venue=venue,
        goals_for=goals_for,
        goals_against=goals_against,
        completed=True,
        kickoff=KICKOFF - timedelta(days=days_before),
        event_id=event_id,
        competition=LEAGUE,
    )


def _provider(home_records=None, away_records=None, fail=False):
    """
    A stub `get_team_history`. Same signature as the real one, and it applies
    the real `derive_history`, so the cutoff under test is production code.
    """
    def provider(team_id, league_code, venue, target_kickoff, exclude_event_id=None, season=None):
        if fail:
            return None
        records = home_records if venue == Venue.HOME else away_records
        return derive_history(
            records or [],
            target_kickoff=target_kickoff,
            venue=venue,
            competition=league_code,
            exclude_event_id=exclude_event_id,
        )

    return provider


class TestDerivedStatsReachTheFilter:
    def test_clean_sheet_pct_is_derived_for_both_teams(self):
        """
        Home team: 1 clean sheet in 4 home games  = 0.25
        Away team: 2 clean sheets in 4 away games = 0.50
        """
        home = [
            _record(2, 0, Venue.HOME, 28, "h1"),
            _record(1, 1, Venue.HOME, 21, "h2"),
            _record(0, 2, Venue.HOME, 14, "h3"),
            _record(3, 1, Venue.HOME, 7, "h4"),
        ]
        away = [
            _record(1, 0, Venue.AWAY, 28, "a1"),
            _record(2, 2, Venue.AWAY, 21, "a2"),
            _record(0, 3, Venue.AWAY, 14, "a3"),
            _record(2, 0, Venue.AWAY, 7, "a4"),
        ]

        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(home, away)
        )

        assert stats.home_clean_sheet_pct == 0.25
        assert stats.away_clean_sheet_pct == 0.50
        assert stats.home_history_sample == 4
        assert stats.away_history_sample == 4

    def test_btts_pct_is_derived_but_no_filter_uses_it(self):
        """
        TASK 14 / 29. The statistic is exposed; it does NOT gain a threshold.

        Both teams have a 0% BTTS rate - the worst possible reading for a GG
        bet, and exactly what a hypothetical MIN_BOTH_SCORED_PCT would reject.
        The fixture must still PASS, because no such filter exists and this Epic
        does not invent one.

        The scorelines are chosen to keep every OTHER filter satisfied (0%
        clean-sheet rates), so a FAILED result here can only mean a new BTTS
        rule has appeared.
        """
        home = [
            _record(0, 1, Venue.HOME, 21, "h1"),
            _record(0, 2, Venue.HOME, 14, "h2"),
        ]
        away = [
            _record(0, 1, Venue.AWAY, 21, "a1"),
            _record(0, 2, Venue.AWAY, 14, "a2"),
        ]


        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(home, away)
        )

        assert stats.home_btts_pct == 0.0
        assert stats.away_btts_pct == 0.0

        result = evaluate_filters(stats)
        assert result.outcome == FilterOutcome.PASSED

    def test_venue_split_is_not_merged(self):
        """
        TASK 12. The home team's AWAY matches are in the feed and must not enter
        its HOME clean-sheet rate. Merging would read 3/4, not 1/2.
        """
        home_all = [
            _record(1, 1, Venue.HOME, 28, "h1"),
            _record(2, 0, Venue.HOME, 21, "h2"),
            _record(1, 0, Venue.AWAY, 14, "a1"),
            _record(3, 0, Venue.AWAY, 7, "a2"),
        ]

        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(home_all, [])
        )

        assert stats.home_clean_sheet_pct == 0.5
        assert stats.home_history_sample == 2


class TestPointInTimeSafety:
    def test_match_after_kickoff_cannot_affect_the_verdict(self):
        """
        The leak test. A future 4-0 clean sheet would push the rate from 0.0 to
        0.5 - and with a threshold in between, would flip the verdict.
        """
        with_future = [
            _record(1, 1, Venue.HOME, 7, "past"),
            _record(4, 0, Venue.HOME, -7, "future"),
        ]

        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(with_future, [])
        )

        assert stats.home_clean_sheet_pct == 0.0
        assert stats.home_history_sample == 1

    def test_target_fixture_is_excluded_from_its_own_history(self):
        """TASK 9. `fixture_id` is passed through as `exclude_event_id`."""
        contaminated = [
            _record(1, 1, Venue.HOME, 7, "h1"),
            _record(5, 0, Venue.HOME, 1, "TARGET"),  # the fixture being predicted
        ]

        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(contaminated, [])
        )

        assert stats.home_history_sample == 1
        assert stats.home_clean_sheet_pct == 0.0

    def test_fixture_without_kickoff_derives_no_history(self):
        """
        No cutoff means no safe derivation. The statistic stays UNAVAILABLE
        rather than being computed against an unbounded record set.
        """
        no_kickoff = {**FIXTURE, "kickoff_utc": None}

        stats = build_fixture_filter_stats(
            no_kickoff,
            _stats(1.5, 1.2),
            _stats(1.4, 1.3),
            _provider([_record(3, 0, Venue.HOME, 7, "h1")], []),
        )

        assert stats.home_clean_sheet_pct is None


class TestFailureBehavior:
    def test_provider_failure_blocks_recommendation_without_fabricating(self):
        """
        TASK 19. A failed feed must not become 0.0. The filter reports
        UNEVALUATED and no recommendation is allowed - but nothing is invented.
        """
        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(fail=True)
        )

        assert stats.home_clean_sheet_pct is None
        assert stats.away_clean_sheet_pct is None

        result = evaluate_filters(stats)
        assert result.outcome == FilterOutcome.UNEVALUATED
        assert result.allows_recommendation is False
        assert "home_clean_sheet_pct" in result.unavailable_fields

    def test_unavailable_is_not_the_same_as_a_genuine_zero(self):
        """
        The single most important distinction in this Epic. 0.0 is a team that
        never kept a clean sheet - a real, filter-passing fact. None is silence.
        Both block or allow differently, and conflating them is how a
        recommendation gets made from missing data.
        """
        genuine_zero = build_fixture_filter_stats(
            FIXTURE,
            _stats(1.5, 1.2),
            _stats(1.4, 1.3),
            _provider(
                [_record(1, 1, Venue.HOME, 7, "h1")],
                [_record(1, 2, Venue.AWAY, 7, "a1")],
            ),
        )
        unavailable = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(fail=True)
        )

        assert genuine_zero.home_clean_sheet_pct == 0.0
        assert unavailable.home_clean_sheet_pct is None

        assert evaluate_filters(genuine_zero).outcome == FilterOutcome.PASSED
        assert evaluate_filters(unavailable).outcome == FilterOutcome.UNEVALUATED

    def test_model_available_filter_data_unavailable_are_separable(self):
        """
        TASK 19. POISSON_V1 has everything it needs; only the FILTER input is
        missing. The two states must remain distinguishable, so a probability
        can still be shown while the recommendation is withheld.
        """
        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(fail=True)
        )
        result = evaluate_filters(stats)

        assert result.was_evaluated is False
        assert result.allows_recommendation is False
        # The goals filters still evaluated - only clean-sheet is missing.
        assert result.unavailable_fields == ("home_clean_sheet_pct", "away_clean_sheet_pct")


class TestAugust2026Behavior:
    """
    TASK 25. It is August 2026 and the 2026/27 season has barely started.
    """

    def test_zero_completed_matches_yields_unavailable_not_zero(self):
        """
        A team with no completed league matches has NO clean-sheet rate. The
        tempting bug is to report 0.0 - which passes MAX_CLEAN_SHEET_PCT and
        recommends a bet on a team that has not played.
        """
        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider([], [])
        )

        assert stats.home_clean_sheet_pct is None
        assert stats.home_history_sample == 0
        assert evaluate_filters(stats).outcome == FilterOutcome.UNEVALUATED

    def test_single_completed_match_is_reported_honestly(self):
        """
        TASK 15. n=1 is calculated and reported with its sample size. No hidden
        minimum-sample rejection is introduced here; calibration comes later.
        """
        stats = build_fixture_filter_stats(
            FIXTURE,
            _stats(1.5, 1.2),
            _stats(1.4, 1.3),
            _provider(
                [_record(2, 0, Venue.HOME, 3, "h1")],
                [_record(1, 1, Venue.AWAY, 3, "a1")],
            ),
        )

        assert stats.home_clean_sheet_pct == 1.0
        assert stats.home_history_sample == 1
        assert stats.away_clean_sheet_pct == 0.0
        assert stats.away_history_sample == 1

    def test_friendlies_are_not_used_to_pad_the_sample(self):
        """
        A preseason friendly is not a league match, even in August when it is
        the only thing available.
        """
        preseason = [
            MatchRecord(
                venue=Venue.HOME, goals_for=4, goals_against=0, completed=True,
                kickoff=KICKOFF - timedelta(days=20),
                event_id="friendly", competition="friendly",
            ),
        ]

        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(preseason, [])
        )

        assert stats.home_clean_sheet_pct is None
        assert stats.home_history_sample == 0


class TestEntryPointConsistency:
    """
    TASK 18. main.py and analyze_all.py must produce identical filter results
    from identical inputs.
    """

    def test_both_entry_points_call_the_same_composition_function(self):
        import analyze_all
        import main

        assert main.build_fixture_filter_stats is analyze_all.build_fixture_filter_stats

    def test_neither_entry_point_calls_build_filter_stats_directly(self):
        """
        The regression guard for GG-006. If either file starts assembling
        FilterStats itself again, the two can diverge; this fails immediately.
        """
        import inspect

        import analyze_all
        import main

        for module in (main, analyze_all):
            source = inspect.getsource(module)
            calls = [
                line for line in source.splitlines()
                if "build_filter_stats(" in line and "build_fixture_filter_stats(" not in line
            ]
            assert calls == [], f"{module.__name__} bypasses the shared boundary: {calls}"

    def test_identical_inputs_produce_identical_filter_stats(self):
        home = [
            _record(2, 0, Venue.HOME, 21, "h1"),
            _record(1, 1, Venue.HOME, 14, "h2"),
            _record(0, 0, Venue.HOME, 7, "h3"),
        ]
        away = [
            _record(1, 2, Venue.AWAY, 21, "a1"),
            _record(0, 1, Venue.AWAY, 14, "a2"),
        ]

        first = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(home, away)
        )
        second = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), _provider(home, away)
        )

        assert first == second
        assert evaluate_filters(first).outcome == evaluate_filters(second).outcome

    @pytest.mark.parametrize("fail", [True, False])
    def test_both_entry_points_agree_on_failure_and_success(self, fail):
        """The consistency must hold in the failure path too, not just the happy one."""
        provider = _provider([_record(1, 1, Venue.HOME, 7, "h1")], [], fail=fail)

        stats = build_fixture_filter_stats(
            FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3), provider
        )
        result = evaluate_filters(stats)

        if fail:
            assert result.outcome == FilterOutcome.UNEVALUATED
        else:
            assert stats.home_clean_sheet_pct == 0.0


class TestNoHistoryProviderFallsBackToAggregates:
    def test_omitting_the_provider_preserves_epic_1b3_behaviour(self):
        """
        `history_provider=None` is the pre-1B.4 path: aggregates only. ESPN's
        aggregates carry no clean-sheet figure, so it stays UNAVAILABLE - never
        silently zero.
        """
        stats = build_fixture_filter_stats(FIXTURE, _stats(1.5, 1.2), _stats(1.4, 1.3))

        assert stats.home_clean_sheet_pct is None
        assert stats.away_clean_sheet_pct is None
        assert evaluate_filters(stats).outcome == FilterOutcome.UNEVALUATED
