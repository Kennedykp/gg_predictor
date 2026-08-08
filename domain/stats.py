"""
Team and league statistic contracts.

Only the statistics the current GG pipeline actually uses are represented here.
No xG, no player data, no recency weighting — those are future work and are
deliberately absent so this contract stays honest about what the system has.

Every field whose source can legitimately be unavailable is `Optional[float]`.
`None` means "not supplied"; `0.0` means "genuinely zero".
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Dict, Optional, Tuple

from domain.availability import DataQuality, missing_fields

__all__ = [
    "TeamStats",
    "LeagueStats",
    "LeagueAverageSource",
    "LEGACY_FALLBACK_LEAGUE_AVERAGE",
]


# ---------------------------------------------------------------------------
# Team statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamStats:
    """
    A team's scoring rates, as supplied by a provider.

    Rates are per match, matching what `espn.get_team_stats` produces today.
    Frozen because these are a record of what an API returned at a point in
    time; mutating them after the fact would make a prediction impossible to
    reproduce.
    """

    team_id: str
    league_id: str

    # Home/away splits. These four are the model-critical values: POISSON_V1
    # takes the home team's home figures and the away team's away figures.
    home_goals_scored: Optional[float] = None
    home_goals_conceded: Optional[float] = None
    away_goals_scored: Optional[float] = None
    away_goals_conceded: Optional[float] = None

    # Filter inputs, not model inputs.
    home_clean_sheet_pct: Optional[float] = None
    away_clean_sheet_pct: Optional[float] = None
    total_goals_avg: Optional[float] = None

    matches_played: Optional[int] = None

    # Which fields POISSON_V1 needs from a team, depending on the side it is
    # playing. A team appearing at home contributes only its home split.
    REQUIRED_AS_HOME: ClassVar[Tuple[str, ...]] = (
        "home_goals_scored",
        "home_goals_conceded",
    )
    REQUIRED_AS_AWAY: ClassVar[Tuple[str, ...]] = (
        "away_goals_scored",
        "away_goals_conceded",
    )

    # -- availability ------------------------------------------------------

    def missing_as_home(self) -> Tuple[str, ...]:
        return missing_fields(self, self.REQUIRED_AS_HOME)

    def missing_as_away(self) -> Tuple[str, ...]:
        return missing_fields(self, self.REQUIRED_AS_AWAY)

    def quality_as_home(self) -> DataQuality:
        return DataQuality.from_missing(self.missing_as_home())

    def quality_as_away(self) -> DataQuality:
        return DataQuality.from_missing(self.missing_as_away())

    # -- adapters ----------------------------------------------------------

    @classmethod
    def from_provider_dict(cls, data: Dict[str, Any]) -> "TeamStats":
        """
        Build from the dict shape the providers currently return.

        An adapter, not a rewrite. The existing pipeline passes dicts around and
        Epic 1B.1 does not restructure that; this lets validation and tests work
        against the typed contract without forcing a repo-wide change.

        Absent keys become `None` rather than `0` - the whole point of GG-001.
        """
        return cls(
            team_id=str(data.get("team_id", "")),
            league_id=str(data.get("league_id", "")),
            home_goals_scored=data.get("home_goals_scored"),
            home_goals_conceded=data.get("home_goals_conceded"),
            away_goals_scored=data.get("away_goals_scored"),
            away_goals_conceded=data.get("away_goals_conceded"),
            home_clean_sheet_pct=data.get("home_clean_sheet_pct"),
            away_clean_sheet_pct=data.get("away_clean_sheet_pct"),
            total_goals_avg=data.get("total_goals_avg"),
            matches_played=data.get("matches_played"),
        )


# ---------------------------------------------------------------------------
# League statistics
# ---------------------------------------------------------------------------


# The value `espn.get_league_avg_goals` returns whenever the standings request
# fails or is unparseable. Epic 0 found this is what production uses for every
# league, every time (GG-003). Named so it can never be mistaken for a measured
# figure, and so Epic 1B.2 can delete it by following the references.
LEGACY_FALLBACK_LEAGUE_AVERAGE = 1.35


class LeagueAverageSource(Enum):
    """
    Where a league average came from.

    POISSON_V1 divides both lambdas by this number, so it scales every
    probability the system produces. A hardcoded constant and a figure computed
    from real standings must therefore never look alike.
    """

    CALCULATED = "CALCULATED"
    """Computed from standings data actually returned by the provider."""

    LEGACY_FALLBACK = "LEGACY_FALLBACK"
    """The hardcoded 1.35. Retained for behavioural compatibility, NOT trusted."""

    UNATTRIBUTED = "UNATTRIBUTED"
    """
    A figure was obtained but its origin is unknown at this layer.

    This exists because of GG-003, which Epic 1B.1 does not fix.
    `espn.get_league_avg_goals` returns the hardcoded 1.35 internally on any
    failure, so by the time a caller receives a float it genuinely cannot tell
    a measured average from the fallback. Guessing from the value (`== 1.35`)
    would be wrong - a real league average can legitimately be 1.35.

    Not trustworthy, because it cannot be shown to be. Epic 1B.2 removes this
    state by making the provider report its own source.
    """

    UNAVAILABLE = "UNAVAILABLE"
    """No figure obtained. Callers must refuse to predict."""


@dataclass(frozen=True)
class LeagueStats:
    """
    League-level context required by POISSON_V1.

    `average_goals` is Optional so "unavailable" is representable, which is what
    Epic 1B.1 requires. Sourcing a real value is Epic 1B.2's job - this contract
    only makes the distinction expressible.
    """

    league_id: str
    average_goals: Optional[float] = None
    source: LeagueAverageSource = LeagueAverageSource.UNAVAILABLE

    REQUIRED: ClassVar[Tuple[str, ...]] = ("average_goals",)

    def missing(self) -> Tuple[str, ...]:
        return missing_fields(self, self.REQUIRED)

    def quality(self) -> DataQuality:
        return DataQuality.from_missing(self.missing())

    @property
    def is_trustworthy(self) -> bool:
        """
        True only for a genuinely measured average.

        The legacy 1.35 fallback is deliberately NOT trustworthy. It is still
        usable - the pipeline continues to run on it so Epic 1B.1 changes no
        published numbers - but it is labelled, so nothing downstream can treat
        it as evidence. Epic 1B.2 removes it.
        """
        return self.source is LeagueAverageSource.CALCULATED

    @classmethod
    def legacy_fallback(cls, league_id: str) -> "LeagueStats":
        """The hardcoded 1.35, explicitly tagged as such."""
        return cls(
            league_id=league_id,
            average_goals=LEGACY_FALLBACK_LEAGUE_AVERAGE,
            source=LeagueAverageSource.LEGACY_FALLBACK,
        )

    @classmethod
    def calculated(cls, league_id: str, average_goals: float) -> "LeagueStats":
        return cls(
            league_id=league_id,
            average_goals=average_goals,
            source=LeagueAverageSource.CALCULATED,
        )

    @classmethod
    def unattributed(cls, league_id: str, average_goals: Optional[float]) -> "LeagueStats":
        """
        Wrap a figure whose origin the caller cannot determine (GG-003).

        Used by the current pipeline, where `espn.get_league_avg_goals` hides
        whether it measured or fell back. `None` correctly becomes UNAVAILABLE.
        """
        if average_goals is None:
            return cls.unavailable(league_id)
        return cls(
            league_id=league_id,
            average_goals=average_goals,
            source=LeagueAverageSource.UNATTRIBUTED,
        )

    @classmethod
    def unavailable(cls, league_id: str) -> "LeagueStats":
        return cls(league_id=league_id, average_goals=None, source=LeagueAverageSource.UNAVAILABLE)
