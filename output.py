"""
Output Formatters.

Output results to:
- Terminal
- CSV
- JSON
"""

import csv
import json
from typing import List, Dict, Any
from datetime import datetime


def format_result(result: Dict[str, Any]) -> str:
    """
    Format a single match result for terminal output.

    Matches specification format:
    Match: Team A vs Team B
    League: Premier League
    λ_home: 1.62
    λ_away: 1.18
    GG Probability: 0.56
    Odds: 1.80 (Implied: 0.56)
    Edge: +0.00
    Decision: NO BET
    """
    lines = []
    lines.append(f"Match: {result.get('home_team', 'N/A')} vs {result.get('away_team', 'N/A')}")
    lines.append(f"League: {result.get('league_name', 'N/A')}")
    lines.append("")

    lambda_home = result.get("lambda_home")
    lambda_away = result.get("lambda_away")
    gg_prob = result.get("gg_probability")
    odds = result.get("odds")
    implied = result.get("implied_probability")
    edge = result.get("edge")
    decision = result.get("decision", "NO BET")

    lines.append(f"λ_home: {lambda_home:.2f}" if lambda_home is not None else "λ_home: N/A")
    lines.append(f"λ_away: {lambda_away:.2f}" if lambda_away is not None else "λ_away: N/A")
    lines.append("")
    lines.append(f"GG Probability: {gg_prob:.2f}" if gg_prob is not None else "GG Probability: N/A")
    lines.append("")

    if odds is not None and implied is not None:
        lines.append(f"Odds: {odds:.2f} (Implied: {implied:.2f})")
    elif odds is not None:
        lines.append(f"Odds: {odds:.2f}")
    else:
        lines.append("Odds: N/A")

    if edge is not None:
        edge_sign = "+" if edge >= 0 else ""
        lines.append(f"Edge: {edge_sign}{edge:.2f}")
    else:
        lines.append("Edge: N/A")

    lines.append("")
    lines.append(f"Decision: {decision}")

    # Add rejection reasons if any
    reasons = result.get("rejection_reasons", [])
    if reasons:
        lines.append("")
        lines.append("Reasons:")
        for reason in reasons:
            lines.append(f"  - {reason}")

    return "\n".join(lines)


def print_result(result: Dict[str, Any]) -> None:
    """Print a single match result to terminal."""
    print(format_result(result))
    print("-" * 40)


def print_results(results: List[Dict[str, Any]]) -> None:
    """Print all results to terminal."""
    print("=" * 50)
    print("GG PREDICTION RESULTS")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print()

    if not results:
        print("No fixtures found for today in allowed leagues.")
        print()
        return

    flagged = [r for r in results if r.get("decision") == "FLAG GG"]
    no_bet = [r for r in results if r.get("decision") != "FLAG GG"]

    if flagged:
        print(f"*** FLAGGED MATCHES ({len(flagged)}) ***")
        print()
        for result in flagged:
            print_result(result)

    print(f"--- NO BET MATCHES ({len(no_bet)}) ---")
    print()
    for result in no_bet:
        print_result(result)

    print("=" * 50)
    print(f"Summary: {len(flagged)} flagged, {len(no_bet)} no bet")
    print("=" * 50)


def write_csv(results: List[Dict[str, Any]], filepath: str) -> None:
    """
    Write results to CSV file.

    Args:
        results: List of result dictionaries
        filepath: Path to output CSV file
    """
    if not results:
        return

    fieldnames = [
        "datetime",
        "league_name",
        "home_team",
        "away_team",
        "lambda_home",
        "lambda_away",
        "gg_probability",
        "odds",
        "implied_probability",
        "edge",
        "decision",
        "passes_filters",
        "rejection_reasons",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for result in results:
            row = result.copy()
            # Convert rejection reasons list to string
            if "rejection_reasons" in row and isinstance(row["rejection_reasons"], list):
                row["rejection_reasons"] = "; ".join(row["rejection_reasons"])
            writer.writerow(row)

    print(f"Results written to: {filepath}")


def write_json(results: List[Dict[str, Any]], filepath: str) -> None:
    """
    Write results to JSON file.

    Args:
        results: List of result dictionaries
        filepath: Path to output JSON file
    """
    output = {
        "generated_at": datetime.now().isoformat(),
        "total_matches": len(results),
        "flagged_count": len([r for r in results if r.get("decision") == "FLAG GG"]),
        "no_bet_count": len([r for r in results if r.get("decision") != "FLAG GG"]),
        "results": results,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Results written to: {filepath}")
