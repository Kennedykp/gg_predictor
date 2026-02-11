"""
Value Calculation and Bet Decision Logic.

Value & Decision Rule:
- P_book = 1 / Odds (implied probability)
- Edge = P(GG) − P_book

Bet Rule - FLAG GG if ALL are true:
- Edge ≥ 0.05 (5%)
- Odds ≥ 1.60
- Match passes filters

Otherwise: NO BET
"""

from typing import Optional
from config import EDGE_THRESHOLD, MIN_ODDS


def calculate_implied_probability(odds: float) -> float:
    """
    Calculate implied probability from odds.

    P_book = 1 / Odds
    """
    if odds <= 0:
        return 0.0
    return 1 / odds


def calculate_edge(gg_probability: float, odds: float) -> float:
    """
    Calculate edge (value).

    Edge = P(GG) − P_book
    """
    implied_probability = calculate_implied_probability(odds)
    return gg_probability - implied_probability


def make_decision(
    gg_probability: float,
    odds: Optional[float],
    passes_filters: bool,
) -> dict:
    """
    Make bet decision based on rules.

    Bet Rule - FLAG GG if ALL are true:
    - Edge ≥ 0.05 (5%)
    - Odds ≥ 1.60
    - Match passes filters

    Otherwise: NO BET

    Args:
        gg_probability: Calculated GG probability from Poisson model
        odds: BTTS Yes odds (None if not available)
        passes_filters: Whether match passes all hard filters

    Returns:
        dict with decision details
    """
    result = {
        "gg_probability": gg_probability,
        "odds": odds,
        "implied_probability": None,
        "edge": None,
        "passes_filters": passes_filters,
        "decision": "NO BET",
        "reasons": [],
    }

    # If filters failed, immediate NO BET
    if not passes_filters:
        result["reasons"].append("Failed hard filters")
        return result

    # If no odds available, cannot calculate edge
    if odds is None:
        result["reasons"].append("No odds available")
        return result

    # Calculate value
    implied_probability = calculate_implied_probability(odds)
    edge = calculate_edge(gg_probability, odds)

    result["implied_probability"] = implied_probability
    result["edge"] = edge

    # Check all conditions for FLAG GG
    conditions_met = True

    if edge < EDGE_THRESHOLD:
        result["reasons"].append(
            f"Edge {edge:.2%} < {EDGE_THRESHOLD:.0%} threshold"
        )
        conditions_met = False

    if odds < MIN_ODDS:
        result["reasons"].append(f"Odds {odds:.2f} < {MIN_ODDS:.2f} minimum")
        conditions_met = False

    if conditions_met:
        result["decision"] = "FLAG GG"
    else:
        result["decision"] = "NO BET"

    return result
