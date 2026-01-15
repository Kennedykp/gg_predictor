"""
Poisson GG Probability Calculator.

Core formula implementation - DO NOT MODIFY.

λ_home = (Home_GF_home × Away_GA_away) / League_Avg_Goals
λ_away = (Away_GF_away × Home_GA_home) / League_Avg_Goals
P(GG) = (1 − e^(−λ_home)) × (1 − e^(−λ_away))
"""

import math
from typing import Optional


def calculate_gg_probability(
    league_avg_goals: float,
    home_goals_scored_home: float,
    home_goals_conceded_home: float,
    away_goals_scored_away: float,
    away_goals_conceded_away: float,
) -> Optional[dict]:
    """
    Calculate GG (Both Teams To Score) probability using Poisson model.

    Args:
        league_avg_goals: League average goals per team per match
        home_goals_scored_home: Home team's average goals scored at home
        home_goals_conceded_home: Home team's average goals conceded at home
        away_goals_scored_away: Away team's average goals scored away
        away_goals_conceded_away: Away team's average goals conceded away

    Returns:
        dict with lambda_home, lambda_away, gg_probability
        None if any input is missing or invalid
    """
    # Validate inputs - if any are missing or invalid, return None (NO BET)
    inputs = [
        league_avg_goals,
        home_goals_scored_home,
        home_goals_conceded_home,
        away_goals_scored_away,
        away_goals_conceded_away,
    ]

    for val in inputs:
        if val is None or val < 0:
            return None

    # Avoid division by zero
    if league_avg_goals == 0:
        return None

    # Core formula - DO NOT MODIFY
    lambda_home = (home_goals_scored_home * away_goals_conceded_away) / league_avg_goals
    lambda_away = (away_goals_scored_away * home_goals_conceded_home) / league_avg_goals

    # P(team scores at least 1) = 1 - P(team scores 0) = 1 - e^(-λ)
    p_home_scores = 1 - math.exp(-lambda_home)
    p_away_scores = 1 - math.exp(-lambda_away)

    # P(GG) = P(home scores) × P(away scores)
    gg_probability = p_home_scores * p_away_scores

    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "gg_probability": gg_probability,
    }
