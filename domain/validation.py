"""
Required-input validation for POISSON_V1.

POISSON_V1 needs exactly five numbers:

    lambda_home = (home_goals_scored_home * away_goals_conceded_away) / league_avg_goals
    lambda_away = (away_goals_scored_away * home_goals_conceded_home) / league_avg_goals

    1. league_avg_goals
    2. home_goals_scored_home     <- HOME team, home split
    3. home_goals_conceded_home   <- HOME team, home split
    4. away_goals_scored_away     <- AWAY team, away split
    5. away_goals_conceded_away   <- AWAY team, away split

Validation happens HERE, before the model call. `poisson.py` is untouched: it is
the frozen POISSON_V1 baseline and its own guards still stand as a second line of
defence.

When a required value is unavailable this module reports that fact. It does not
substitute zero, does not substitute the 1.35 league fallback, does not
interpolate, and does not borrow another team's figures. Refusing to predict is
the correct outcome — `GG.md` §6 already says so ("if any of these are missing →
NO BET"); before Epic 1B.1 the code could not tell that data was missing.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from domain.availability import DataQuality
from domain.stats import LeagueStats, TeamStats

__all__ = ["PoissonInputs", "InputValidation", "validate_poisson_inputs", "REQUIRED_POISSON_INPUTS"]


# The five required inputs, in the order POISSON_V1 declares its parameters.
REQUIRED_POISSON_INPUTS: Tuple[str, ...] = (
    "league_avg_goals",
    "home_goals_scored_home",
    "home_goals_conceded_home",
    "away_goals_scored_away",
    "away_goals_conceded_away",
)


@dataclass(frozen=True)
class PoissonInputs:
    """
    Five validated, present values ready for POISSON_V1.

    Only constructed when validation succeeds, so every field is a real `float`,
    not `Optional[float]`. That is the point: code holding a `PoissonInputs` does
    not need to re-check for `None`, and the type system enforces it.
    """

    league_avg_goals: float
    home_goals_scored_home: float
    home_goals_conceded_home: float
    away_goals_scored_away: float
    away_goals_conceded_away: float


@dataclass(frozen=True)
class InputValidation:
    """
    Outcome of checking the five required inputs.

    Either `inputs` is populated and `missing` is empty, or the reverse.
    """

    quality: DataQuality
    missing: Tuple[str, ...] = ()
    inputs: Optional[PoissonInputs] = None

    @property
    def is_complete(self) -> bool:
        return self.quality.is_complete

    def reason(self) -> str:
        """
        A human-readable rejection reason naming the absent inputs.

        Named fields matter operationally: "Missing required data" sends someone
        to read code, while "Missing required model input(s): away_goals_scored_away"
        points straight at the provider gap.
        """
        if self.is_complete:
            return ""
        return "Missing required model input(s): " + ", ".join(self.missing)


def validate_poisson_inputs(
    league: LeagueStats,
    home_team: TeamStats,
    away_team: TeamStats,
) -> InputValidation:
    """
    Check the five POISSON_V1 inputs are all present.

    Note this validates *availability*, not plausibility. A genuine 0.0 passes,
    because zero goals is real data — POISSON_V1's own `val < 0` guard continues
    to handle negatives. Range and sanity checking is separate work.
    """
    candidates = (
        ("league_avg_goals", league.average_goals),
        ("home_goals_scored_home", home_team.home_goals_scored),
        ("home_goals_conceded_home", home_team.home_goals_conceded),
        ("away_goals_scored_away", away_team.away_goals_scored),
        ("away_goals_conceded_away", away_team.away_goals_conceded),
    )

    missing = tuple(name for name, value in candidates if value is None)

    if missing:
        return InputValidation(quality=DataQuality.INCOMPLETE, missing=missing)

    # Every value is present; the casts are safe and satisfy the type checker.
    values = {name: float(value) for name, value in candidates}  # type: ignore[arg-type]
    return InputValidation(
        quality=DataQuality.COMPLETE,
        missing=(),
        inputs=PoissonInputs(**values),
    )
