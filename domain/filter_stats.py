"""
Filter input contract (Epic 1B.3, GG-002).

`domain/validation.py` already guards the five POISSON_V1 *model* inputs. This
module does the same job for the *filter* inputs, which is a genuinely separate
concern:

    model inputs complete    -> a probability may be calculated
    filter inputs complete   -> a recommendation may be made

Those two sets overlap but are not the same, and conflating them is what let
GG-002 survive. A fixture can legitimately have everything POISSON_V1 needs
while having nothing the clean-sheet filter needs.

The rule inherited from Epic 1B.1 is unchanged and is the whole point here:

    Unknown is not zero. Unknown is not neutral. Unknown is not pass.

`clean_sheet_pct = 0.0` is an assertion that a team has never kept a clean
sheet. That is real, usable data and it stays valid. `None` means ESPN did not
give us the statistic. Before this Epic both arrived at the filter as `0` and
the filter could not tell them apart, so it silently passed everything.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from domain.availability import missing_fields

__all__ = ["StatSource", "FilterStats", "REQUIRED_FILTER_FIELDS", "build_filter_stats"]



class StatSource(str, Enum):
    """
    Where a filter statistic came from.

    Kept deliberately small - three values, no confidence score. The purpose is
    to make "we measured this" and "we could not get this" different facts in
    the type system, not to build a provenance framework.
    """

    DIRECT = "DIRECT"            # the provider returned this statistic itself
    DERIVED = "DERIVED"          # calculated exactly from genuine provider data
    UNAVAILABLE = "UNAVAILABLE"  # the provider cannot supply it; do not invent it


# The statistics the current GG hard filters actually compare against a
# threshold. Ordered as `filters.apply_filters` evaluates them, so rejection
# reasons and unavailable-field reports read in the same order.
REQUIRED_FILTER_FIELDS: Tuple[str, ...] = (
    "home_avg_goals_scored",
    "away_avg_goals_scored",
    "home_clean_sheet_pct",
    "away_clean_sheet_pct",
)


@dataclass(frozen=True)
class FilterStats:
    """
    Validated inputs for the GG hard filters.

    Semantics are fixed here so both entry points cannot disagree again (GG-006).
    Each field name states its statistic, its perspective and its unit, because
    the previous parameter name (`home_avg_goals`) was vague enough to accept a
    combined scored+conceded figure without looking wrong at the call site.

    home_avg_goals_scored:
        Goals SCORED per match by the home team IN ITS HOME MATCHES.
        Not conceded. Not scored+conceded. Goals per match, not per season.
    away_avg_goals_scored:
        Goals SCORED per match by the away team IN ITS AWAY MATCHES.
    home_clean_sheet_pct:
        Fraction (0.0-1.0, NOT 0-100) of the home team's completed HOME matches
        in which it conceded zero.
    away_clean_sheet_pct:
        Fraction (0.0-1.0) of the away team's completed AWAY matches in which it
        conceded zero.

    The venue split is not a new invention: POISSON_V1 already uses
    home-team-at-home and away-team-away figures (GG.md section 6), and the
    existing filter parameters were already named `home_*` / `away_*`. The same
    convention is applied to every filter statistic rather than mixing a
    venue-split lambda with an overall-season filter.
    """

    home_avg_goals_scored: Optional[float]
    away_avg_goals_scored: Optional[float]
    home_clean_sheet_pct: Optional[float]
    away_clean_sheet_pct: Optional[float]

    # Non-statistical flags. Still hardcoded False by both callers - detecting a
    # knockout first leg or a defensive mismatch needs competition-format and
    # market data ESPN does not expose here. Carried on the contract so the
    # honest status is visible in one place instead of being buried as a literal
    # at two call sites (see docs/TECHNICAL_DEBT.md GG-002-B).
    is_knockout_first_leg: bool = False
    is_heavy_favorite_mismatch: bool = False

    # Provenance, for reporting and for the diagnostic. Defaults to DERIVED
    # because every statistic above is currently calculated from ESPN data
    # rather than read off a dedicated ESPN field.
    clean_sheet_source: StatSource = StatSource.DERIVED
    avg_goals_source: StatSource = StatSource.DERIVED

    def __post_init__(self) -> None:
        """
        Reject values that cannot be the statistic they claim to be.

        A percentage outside 0-1 is the classic unit error: 40 instead of 0.40
        would sail past `> 0.40` forever. A negative goal average is impossible.
        Loud failure is correct here - this is a provider/wiring bug, and the
        alternative is comparing a nonsense number against a real threshold.
        """
        for name in ("home_clean_sheet_pct", "away_clean_sheet_pct"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be a fraction in [0.0, 1.0], got {value!r}")

        for name in ("home_avg_goals_scored", "away_avg_goals_scored"):
            value = getattr(self, name)
            if value is not None and value < 0.0:
                raise ValueError(f"{name} must be >= 0, got {value!r}")

    def unavailable_fields(self) -> Tuple[str, ...]:
        """Required filter statistics ESPN did not supply, in evaluation order."""
        return missing_fields(self, REQUIRED_FILTER_FIELDS)

    @property
    def is_complete(self) -> bool:
        """True when every required filter statistic is present (a genuine 0.0 counts)."""
        return not self.unavailable_fields()


def build_filter_stats(
    home_stats: Dict[str, Any],
    away_stats: Dict[str, Any],
) -> FilterStats:
    """
    Build filter inputs from two provider stat dicts.

    THE ONE PLACE either entry point decides what a filter input means (GG-006).
    main.py and analyze_all.py both call this, so "the home team's goals average"
    resolves to the same key for both and cannot drift apart again.

    Mapping, and why:

      home_avg_goals_scored  <- home_stats["home_goals_scored"]
      away_avg_goals_scored  <- away_stats["away_goals_scored"]

    Goals SCORED, at the venue each team is actually playing at. NOT
    `total_goals_avg`, which main.py used and which is (scored + conceded) /
    matches - a different statistic that measures how eventful a team's matches
    are, not how reliably it scores. GG.md section 9 says "one team averages
    < 1.0 goal" and filters.py documents the parameter as "average goals per
    match" for that team, so the filter is about a team's own scoring.

      home_clean_sheet_pct   <- home_stats["home_clean_sheet_pct"]
      away_clean_sheet_pct   <- away_stats["away_clean_sheet_pct"]

    Each team's clean-sheet rate at its own venue. These currently arrive as
    None: ESPN's standings aggregates cannot yield a per-match clean-sheet count
    (see espn.get_team_stats). None flows through as UNAVAILABLE, which blocks a
    recommendation instead of quietly passing.

    `.get()` is used rather than `[...]` so a provider that omits a key entirely
    yields None (unavailable) rather than raising - absence is a data state this
    pipeline is designed to represent, not a crash.
    """
    home_clean_sheet = home_stats.get("home_clean_sheet_pct")
    away_clean_sheet = away_stats.get("away_clean_sheet_pct")

    return FilterStats(
        home_avg_goals_scored=home_stats.get("home_goals_scored"),
        away_avg_goals_scored=away_stats.get("away_goals_scored"),
        home_clean_sheet_pct=home_clean_sheet,
        away_clean_sheet_pct=away_clean_sheet,
        # The flags stay False: nothing in the current pipeline detects either
        # condition. Recorded honestly rather than being presented as a check
        # that ran and found nothing.
        is_knockout_first_leg=False,
        is_heavy_favorite_mismatch=False,
        clean_sheet_source=(
            StatSource.UNAVAILABLE
            if home_clean_sheet is None or away_clean_sheet is None
            else StatSource.DERIVED
        ),
        avg_goals_source=StatSource.DERIVED,
    )

