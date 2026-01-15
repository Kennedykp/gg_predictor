"""
Hard Filters (Safety Rules).

GG is NOT allowed if any of the following are true:
- One team averages < 1.0 goal
- One team keeps > 40% clean sheets
- First-leg knockout match
- Heavy favorite vs deep-defending underdog
- Missing or unreliable data

Filters are mandatory. They protect the bankroll.
"""

from typing import Tuple, List
from config import MIN_AVG_GOALS, MAX_CLEAN_SHEET_PCT


def apply_filters(
    home_avg_goals: float,
    away_avg_goals: float,
    home_clean_sheet_pct: float,
    away_clean_sheet_pct: float,
    is_knockout_first_leg: bool = False,
    is_heavy_favorite_mismatch: bool = False,
    has_reliable_data: bool = True,
) -> Tuple[bool, List[str]]:
    """
    Apply hard safety filters to determine if GG bet is allowed.

    Args:
        home_avg_goals: Home team's average goals per match
        away_avg_goals: Away team's average goals per match
        home_clean_sheet_pct: Home team's clean sheet percentage (0-1)
        away_clean_sheet_pct: Away team's clean sheet percentage (0-1)
        is_knockout_first_leg: True if this is a first-leg knockout match
        is_heavy_favorite_mismatch: True if heavy favorite vs deep-defending underdog
        has_reliable_data: True if data is complete and reliable

    Returns:
        Tuple of (passes_filters: bool, rejection_reasons: list[str])
    """
    rejection_reasons = []

    # Check minimum average goals
    if home_avg_goals < MIN_AVG_GOALS:
        rejection_reasons.append(
            f"Home team averages < {MIN_AVG_GOALS} goals ({home_avg_goals:.2f})"
        )

    if away_avg_goals < MIN_AVG_GOALS:
        rejection_reasons.append(
            f"Away team averages < {MIN_AVG_GOALS} goals ({away_avg_goals:.2f})"
        )

    # Check clean sheet percentage
    if home_clean_sheet_pct > MAX_CLEAN_SHEET_PCT:
        rejection_reasons.append(
            f"Home team keeps > {MAX_CLEAN_SHEET_PCT * 100:.0f}% clean sheets ({home_clean_sheet_pct * 100:.1f}%)"
        )

    if away_clean_sheet_pct > MAX_CLEAN_SHEET_PCT:
        rejection_reasons.append(
            f"Away team keeps > {MAX_CLEAN_SHEET_PCT * 100:.0f}% clean sheets ({away_clean_sheet_pct * 100:.1f}%)"
        )

    # Check knockout first leg
    if is_knockout_first_leg:
        rejection_reasons.append("First-leg knockout match")

    # Check heavy favorite mismatch
    if is_heavy_favorite_mismatch:
        rejection_reasons.append("Heavy favorite vs deep-defending underdog")

    # Check data reliability
    if not has_reliable_data:
        rejection_reasons.append("Missing or unreliable data")

    passes_filters = len(rejection_reasons) == 0

    return passes_filters, rejection_reasons
