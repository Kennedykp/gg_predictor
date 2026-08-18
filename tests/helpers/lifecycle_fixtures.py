"""
Builders for the operational lifecycle tests (Epic 2H-4).

Separate from `settlement_fixtures` because these build the RESULT side - what a
provider returns - whereas that module builds the two stored logs. Imported as
`from helpers.lifecycle_fixtures import ...`, for the reason its sibling
documents.

The fake result source here is a plain function, which is the whole point:
`settle_predictions.ResultSource` is
`Callable[[str, Optional[int]], Tuple[Optional[List[HistoricalMatch]], bool]]`,
so a test can settle a full month with no network, no ESPN, and no clock. Every
operational test in this Epic runs offline because of it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from domain.historical import HistoricalMatch

__all__ = [
    "KICKOFF_AT",
    "historical_match",
    "result_source",
    "empty_result_source",
    "failing_result_source",
]

KICKOFF_AT = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)


def historical_match(
    event_id: str = "740123",
    competition: str = "eng.1",
    season: int = 2026,
    home_goals: Optional[int] = 2,
    away_goals: Optional[int] = 1,
    completed: bool = True,
    status: Optional[str] = "STATUS_FULL_TIME",
    kickoff: datetime = KICKOFF_AT,
    **extra: Any,
) -> HistoricalMatch:
    """
    One provider result. Defaults to a completed 2-1, so GG is YES.

    `completed` and the scores are independent parameters on purpose:
    `HistoricalMatch.__post_init__` refuses a completed match with no score, and
    a test that could not construct that pair could not prove the refusal.
    """
    return HistoricalMatch(
        event_id=event_id,
        competition=competition,
        season=season,
        kickoff=kickoff,
        home_team_id="359",
        away_team_id="360",
        completed=completed,
        home_goals=home_goals,
        away_goals=away_goals,
        status=status,
        **extra,
    )


def result_source(
    matches: Sequence[HistoricalMatch],
) -> Any:
    """
    A `ResultSource` serving a fixed list, filtered by league-season.

    Filtered rather than returning everything: settlement asks per league-season
    and a source that ignored the question would let a genuine season-key bug
    pass, which is precisely the failure `matched_season` exists to expose.
    """
    calls: List[Tuple[str, Optional[int]]] = []

    def source(competition: str, season: Optional[int]) -> Tuple[Optional[List[HistoricalMatch]], bool]:
        calls.append((competition, season))
        found = [
            match
            for match in matches
            if match.competition == competition and match.season == season
        ]
        return found, True

    source.calls = calls  # type: ignore[attr-defined]
    return source


def empty_result_source(competition: str, season: Optional[int]) -> Tuple[Optional[List[HistoricalMatch]], bool]:
    """A provider that answered, and has nothing. Reachable, but empty."""
    return [], True


def failing_result_source(competition: str, season: Optional[int]) -> Tuple[Optional[List[HistoricalMatch]], bool]:
    """
    A provider that could not be reached: `(None, False)`.

    Distinct from `empty_result_source`. "No data" and "no answer" must not settle
    to the same thing - one means the fixture is absent, the other means we do not
    know, and only the second should be retried.
    """
    return None, False


def counts_by_stage(rows: Sequence[Any]) -> Dict[str, int]:
    """Tally `LifecycleRow.stage` values, for asserting on a whole run at once."""
    out: Dict[str, int] = {}
    for row in rows:
        out[row.stage.value] = out.get(row.stage.value, 0) + 1
    return out
