"""
Fixture -> FilterStats composition (Epic 1B.4, TASK 17 / 18).

WHY THIS MODULE EXISTS
----------------------
Epic 1B.3 made `build_filter_stats` the one place a filter input is DEFINED.
This module is the one place the match history feeding it is RETRIEVED. Without
it, both entry points would each have to remember to:

    resolve the target kickoff
    -> ask for the home team's HOME history
    -> ask for the away team's AWAY history
    -> pass the target fixture id so it cannot appear in its own history
    -> pass both into build_filter_stats

Five chances each to get it subtly different - which is exactly how GG-006
happened, when main.py and analyze_all.py fed the same filter two different
statistics. One function, called by both, cannot drift.

The venue asymmetry below is the substantive rule, and it is not symmetric by
accident: the home team is judged on its HOME record and the away team on its
AWAY record, matching the venue split POISSON_V1 already uses for lambda and the
`home_*` / `away_*` naming the filters already carry.
"""

from typing import Any, Callable, Dict, Optional

from domain.filter_stats import FilterStats, build_filter_stats
from domain.match_records import DerivedHistory, Venue

__all__ = ["HistoryProvider", "build_fixture_filter_stats"]

# The provider seam. `espn.get_team_history` satisfies it, and so does a stub in
# a test - which is why this is a plain callable type and not an import of espn:
# it keeps this module, and every test that uses it, free of network code.
HistoryProvider = Callable[..., Optional[DerivedHistory]]


def build_fixture_filter_stats(
    fixture: Dict[str, Any],
    home_stats: Dict[str, Any],
    away_stats: Dict[str, Any],
    history_provider: Optional[HistoryProvider] = None,
) -> FilterStats:
    """
    Assemble the filter inputs for one fixture, including derived match history.

    `history_provider` is injected rather than imported at module scope so a
    caller can pass None (aggregates only, the Epic 1B.3 behaviour) and a test
    can pass a stub. When it is None, no history is derived and clean-sheet
    percentages fall back to the stat dicts - where ESPN's aggregates leave them
    None, so the filter still refuses to guess.

    THE CUTOFF. `fixture["kickoff_utc"]` is the target kickoff, and it is
    REQUIRED for history to be derived at all. If the fixture has no parseable
    kickoff there is no cutoff to enforce, and rather than derive a statistic
    from an unbounded record set - which could include the fixture's own result
    once it is played - this returns aggregates only, leaving clean-sheet
    UNAVAILABLE. Refusing to answer is the safe failure here.

    THE VENUE SPLIT (TASK 12):
        home team -> Venue.HOME records only
        away team -> Venue.AWAY records only
    Never merged. Merging would raise the sample size while silently changing
    which statistic is being measured.

    Provider failure returns None from the provider, which propagates as
    UNAVAILABLE - never as a 0% rate (TASK 19).
    """
    home_history: Optional[DerivedHistory] = None
    away_history: Optional[DerivedHistory] = None

    kickoff = fixture.get("kickoff_utc")
    league_code = fixture.get("league_id")

    if history_provider is not None and kickoff is not None and league_code:
        # The target fixture's own id, so it cannot enter its own history even
        # if a feed reported it as completed (TASK 9). Defence in depth: the
        # kickoff cutoff already excludes it, since a match cannot start
        # strictly before itself.
        target_event_id = fixture.get("fixture_id")
        target_event_id = str(target_event_id) if target_event_id is not None else None

        home_team_id = fixture.get("home_team_id")
        away_team_id = fixture.get("away_team_id")

        if home_team_id is not None:
            home_history = history_provider(
                team_id=str(home_team_id),
                league_code=league_code,
                venue=Venue.HOME,
                target_kickoff=kickoff,
                exclude_event_id=target_event_id,
            )
        if away_team_id is not None:
            away_history = history_provider(
                team_id=str(away_team_id),
                league_code=league_code,
                venue=Venue.AWAY,
                target_kickoff=kickoff,
                exclude_event_id=target_event_id,
            )

    return build_filter_stats(home_stats, away_stats, home_history, away_history)
