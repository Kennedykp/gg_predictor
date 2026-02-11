"""
Run-3 Probability Calculator.

Core formula implementation - DO NOT MODIFY.

Step 1: Goal-share probabilities
    p_home = lambda_home / (lambda_home + lambda_away)
    p_away = lambda_away / (lambda_home + lambda_away)

Step 2: Probability a team scores 3 in a row
    P_home_run3 ≈ p_home³
    P_away_run3 ≈ p_away³

Step 3: Probability ANY team scores 3 in a row
    P_R3_YES = 1 - (1 - P_home_run3) * (1 - P_away_run3)

Step 4: Probability of interest (NO)
    P_R3_NO = 1 - P_R3_YES
"""

from typing import Optional, Dict, Any


def calculate_run3_probability(
    lambda_home: float,
    lambda_away: float,
) -> Optional[Dict[str, Any]]:
    """
    Calculate Run-3 (unanswered goals) probabilities.

    Args:
        lambda_home: Expected goals for home team
        lambda_away: Expected goals for away team

    Returns:
        dict with p_home, p_away, P_R3_YES, P_R3_NO
        None if inputs invalid
    """
    # Validate inputs
    if lambda_home is None or lambda_away is None:
        return None
    if lambda_home < 0 or lambda_away < 0:
        return None

    # Avoid division by zero
    total_lambda = lambda_home + lambda_away
    if total_lambda == 0:
        return None

    # Step 1: Goal-share probabilities
    p_home = lambda_home / total_lambda
    p_away = lambda_away / total_lambda

    # Step 2: Probability a team scores 3 in a row
    P_home_run3 = p_home ** 3
    P_away_run3 = p_away ** 3

    # Step 3: Probability ANY team scores 3 in a row
    P_R3_YES = 1 - (1 - P_home_run3) * (1 - P_away_run3)

    # Step 4: Probability of interest (NO)
    P_R3_NO = 1 - P_R3_YES

    return {
        "p_home": p_home,
        "p_away": p_away,
        "P_home_run3": P_home_run3,
        "P_away_run3": P_away_run3,
        "P_R3_YES": P_R3_YES,
        "P_R3_NO": P_R3_NO,
    }
