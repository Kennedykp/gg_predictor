"""
Run-3 Decision Logic.

PRIMARY MARKET — R3-NO
Flag R3-NO ONLY if ALL conditions are met:
- P_R3_NO >= 0.78
- 0.9 <= lambda_home <= 1.8
- 0.9 <= lambda_away <= 1.8
- 0.35 <= p_home <= 0.65
- Odds (if provided): >= 1.60

SECONDARY MARKET — R3-YES (RARE)
Flag R3-YES ONLY if ALL are true:
- P_R3_YES >= 0.30
- One team dominance: p >= 0.65 AND lambda >= 2.2
- lambda_home + lambda_away >= 2.8
- Odds (if provided): >= 2.80

If neither: Decision = SKIP
"""

from typing import Optional, Dict, Any, List


# Decision thresholds (DO NOT MODIFY)
R3_NO_MIN_PROB = 0.78
R3_NO_LAMBDA_MIN = 0.9
R3_NO_LAMBDA_MAX = 1.8
R3_NO_P_MIN = 0.35
R3_NO_P_MAX = 0.65
R3_NO_MIN_ODDS = 1.60
R3_NO_MIN_TOTAL_GOALS = 2.0
R3_NO_MAX_TOTAL_GOALS = 3.2
R3_NO_MIN_EDGE = 0.05  # 5% minimum edge required

R3_YES_MIN_PROB = 0.30
R3_YES_DOMINANCE_P = 0.65
R3_YES_DOMINANCE_LAMBDA = 2.2
R3_YES_MIN_TOTAL_LAMBDA = 2.8
R3_YES_MIN_ODDS = 2.80


def make_run3_decision(
    lambda_home: float,
    lambda_away: float,
    p_home: float,
    p_away: float,
    P_R3_YES: float,
    P_R3_NO: float,
    passes_filters: bool,
    odds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Make Run-3 bet decision.

    Args:
        lambda_home: Expected goals for home team
        lambda_away: Expected goals for away team
        p_home: Home team goal-share probability
        p_away: Away team goal-share probability
        P_R3_YES: Probability of Run-3 YES
        P_R3_NO: Probability of Run-3 NO
        passes_filters: Whether match passes hard filters
        odds: Optional odds for validation

    Returns:
        dict with decision details
    """
    result = {
        "decision": "SKIP",
        "reasons": [],
    }

    # If filters failed, immediate SKIP
    if not passes_filters:
        result["reasons"].append("Failed hard filters")
        return result

    # Check R3-NO conditions (PRIMARY)
    r3_no_reasons = []
    r3_no_valid = True

    if P_R3_NO < R3_NO_MIN_PROB:
        r3_no_reasons.append(f"P_R3_NO {P_R3_NO:.2f} < {R3_NO_MIN_PROB}")
        r3_no_valid = False

    if not (R3_NO_LAMBDA_MIN <= lambda_home <= R3_NO_LAMBDA_MAX):
        r3_no_reasons.append(
            f"lambda_home {lambda_home:.2f} not in [{R3_NO_LAMBDA_MIN}, {R3_NO_LAMBDA_MAX}]"
        )
        r3_no_valid = False

    if not (R3_NO_LAMBDA_MIN <= lambda_away <= R3_NO_LAMBDA_MAX):
        r3_no_reasons.append(
            f"lambda_away {lambda_away:.2f} not in [{R3_NO_LAMBDA_MIN}, {R3_NO_LAMBDA_MAX}]"
        )
        r3_no_valid = False

    if not (R3_NO_P_MIN <= p_home <= R3_NO_P_MAX):
        r3_no_reasons.append(
            f"p_home {p_home:.2f} not in [{R3_NO_P_MIN}, {R3_NO_P_MAX}]"
        )
        r3_no_valid = False

    if odds is not None and odds < R3_NO_MIN_ODDS:
        r3_no_reasons.append(f"Odds {odds:.2f} < {R3_NO_MIN_ODDS}")
        r3_no_valid = False

    # Check total goals range (2.0-3.2 sweet spot)
    total_lambda = lambda_home + lambda_away
    if total_lambda < R3_NO_MIN_TOTAL_GOALS:
        r3_no_reasons.append(
            f"Total goals {total_lambda:.2f} < {R3_NO_MIN_TOTAL_GOALS} (too few goals expected)"
        )
        r3_no_valid = False
    if total_lambda > R3_NO_MAX_TOTAL_GOALS:
        r3_no_reasons.append(
            f"Total goals {total_lambda:.2f} > {R3_NO_MAX_TOTAL_GOALS} (too many goals expected)"
        )
        r3_no_valid = False

    # Check edge (5% minimum required)
    if odds is not None:
        implied_prob = 1 / odds
        edge = P_R3_NO - implied_prob
        if edge < R3_NO_MIN_EDGE:
            r3_no_reasons.append(
                f"Edge {edge:.1%} < {R3_NO_MIN_EDGE:.0%} minimum"
            )
            r3_no_valid = False

    if r3_no_valid:
        result["decision"] = "R3-NO"
        return result

    # Check R3-YES conditions (SECONDARY - RARE)
    r3_yes_reasons = []
    r3_yes_valid = True

    if P_R3_YES < R3_YES_MIN_PROB:
        r3_yes_reasons.append(f"P_R3_YES {P_R3_YES:.2f} < {R3_YES_MIN_PROB}")
        r3_yes_valid = False

    # Check dominance condition
    home_dominant = (p_home >= R3_YES_DOMINANCE_P and lambda_home >= R3_YES_DOMINANCE_LAMBDA)
    away_dominant = (p_away >= R3_YES_DOMINANCE_P and lambda_away >= R3_YES_DOMINANCE_LAMBDA)
    
    if not (home_dominant or away_dominant):
        r3_yes_reasons.append("No team dominance (p >= 0.65 AND lambda >= 2.2)")
        r3_yes_valid = False

    total_lambda = lambda_home + lambda_away
    if total_lambda < R3_YES_MIN_TOTAL_LAMBDA:
        r3_yes_reasons.append(
            f"Total lambda {total_lambda:.2f} < {R3_YES_MIN_TOTAL_LAMBDA}"
        )
        r3_yes_valid = False

    if odds is not None and odds < R3_YES_MIN_ODDS:
        r3_yes_reasons.append(f"Odds {odds:.2f} < {R3_YES_MIN_ODDS}")
        r3_yes_valid = False

    if r3_yes_valid:
        result["decision"] = "R3-YES"
        return result

    # Neither condition met - SKIP
    result["reasons"] = r3_no_reasons + r3_yes_reasons
    return result
