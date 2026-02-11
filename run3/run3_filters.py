"""
Run-3 Hard Filters.

DISALLOW ALL BETS if ANY are true:
- lambda_home + lambda_away >= 3.5
- p_home >= 0.65 OR p_away >= 0.65
- lambda_home >= 2.2 OR lambda_away >= 2.2
- Missing or unreliable data

These filters exist to eliminate dominance and chaos games.
"""

from typing import Tuple, List


# Filter thresholds (DO NOT MODIFY)
MAX_TOTAL_LAMBDA = 3.5
MAX_GOAL_SHARE = 0.65
MAX_SINGLE_LAMBDA = 2.2


def apply_run3_filters(
    lambda_home: float,
    lambda_away: float,
    p_home: float,
    p_away: float,
    has_reliable_data: bool = True,
) -> Tuple[bool, List[str]]:
    """
    Apply Run-3 hard filters.

    Args:
        lambda_home: Expected goals for home team
        lambda_away: Expected goals for away team
        p_home: Home team goal-share probability
        p_away: Away team goal-share probability
        has_reliable_data: Whether data is complete and reliable

    Returns:
        Tuple of (passes_filters: bool, rejection_reasons: list[str])
    """
    rejection_reasons = []

    # Check total lambda (chaos filter)
    total_lambda = lambda_home + lambda_away
    if total_lambda >= MAX_TOTAL_LAMBDA:
        rejection_reasons.append(
            f"Total lambda {total_lambda:.2f} >= {MAX_TOTAL_LAMBDA} (chaos game)"
        )

    # Check goal-share dominance
    if p_home >= MAX_GOAL_SHARE:
        rejection_reasons.append(
            f"p_home {p_home:.2f} >= {MAX_GOAL_SHARE} (home dominance)"
        )
    if p_away >= MAX_GOAL_SHARE:
        rejection_reasons.append(
            f"p_away {p_away:.2f} >= {MAX_GOAL_SHARE} (away dominance)"
        )

    # Check single team lambda dominance
    if lambda_home >= MAX_SINGLE_LAMBDA:
        rejection_reasons.append(
            f"lambda_home {lambda_home:.2f} >= {MAX_SINGLE_LAMBDA} (home too strong)"
        )
    if lambda_away >= MAX_SINGLE_LAMBDA:
        rejection_reasons.append(
            f"lambda_away {lambda_away:.2f} >= {MAX_SINGLE_LAMBDA} (away too strong)"
        )

    # Check data reliability
    if not has_reliable_data:
        rejection_reasons.append("Missing or unreliable data")

    passes_filters = len(rejection_reasons) == 0

    return passes_filters, rejection_reasons
