"""
Epic 2B.3 - the odds firewall, enforced structurally.

This Epic measures PROBABILITY QUALITY. Betting value is a separate question,
still blocked by LEAK-001, and the two must not be able to merge by accident.

The risk is specific and easy to walk into: someone adds "expected value" to the
evaluator, it needs a price, `odds_api` is right there, and now the harness's
Brier score silently depends on a bookmaker's margin. These tests fail the moment
an odds import or threshold reference appears in the evaluation modules, so the
firewall is a build error rather than a code-review hope.

Enforced by reading the module source, not by mocking: an import added inside a
function body would evade a runtime check but not this one.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The modules Epic 2B.3 introduced. Each must be evaluable with odds absent
# entirely.
EVALUATION_MODULES = (
    REPO_ROOT / "domain" / "evaluation.py",
    REPO_ROOT / "evaluation_harness.py",
    REPO_ROOT / "run_evaluation.py",
)

FORBIDDEN_MODULES = {
    "odds_api",
    "shared.odds",
    "decision",
    "filters",
    "sportmonks",
    "api_football",
    "sofascore",
}

FORBIDDEN_NAMES = {
    "EDGE_THRESHOLD",
    "MIN_ODDS",
    "implied_probability",
    "calculate_edge",
    "expected_value",
}


def _imported_modules(path: Path) -> set:
    """Every module named by an import anywhere in the file, nested included."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


@pytest.mark.parametrize("path", EVALUATION_MODULES, ids=lambda p: p.name)
def test_no_odds_or_decision_imports(path):
    """
    No import path from the evaluator to odds, prices, decisions or filters.

    `decision` and `filters` are included because both consume thresholds:
    reaching either from here would pull EDGE_THRESHOLD and MIN_ODDS into a
    probability metric by transitive dependency.
    """
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    imported = _imported_modules(path)
    offending = imported & FORBIDDEN_MODULES
    assert not offending, (
        f"{path.name} imports {sorted(offending)}. The evaluation harness measures "
        "probability quality only; betting value remains blocked by LEAK-001."
    )


@pytest.mark.parametrize("path", EVALUATION_MODULES, ids=lambda p: p.name)
def test_no_threshold_or_price_identifiers(path):
    """
    No threshold or price identifier is referenced, even without an import.

    Copying the value of EDGE_THRESHOLD into the evaluator would be worse than
    importing it: the coupling would exist and be invisible.
    """
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    offending = referenced & FORBIDDEN_NAMES
    assert not offending, f"{path.name} references betting identifiers {sorted(offending)}"


def test_harness_runs_with_odds_module_unavailable(monkeypatch):
    """
    The harness must work with odds absent, not merely unused.

    Blocking the import proves the dependency does not exist, where a passing
    test with odds importable would only prove it was not needed today.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in {"odds_api", "shared.odds"}:
            raise ImportError(f"{name} is firewalled during evaluation")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    from datetime import datetime, timedelta, timezone

    from domain.historical import HistoricalMatch
    from evaluation_harness import PoissonV1Adapter, replay

    base = datetime(2020, 9, 1, 15, 0, tzinfo=timezone.utc)
    dataset = [
        HistoricalMatch(
            event_id=f"e{i}",
            competition="eng.1",
            season=2020,
            kickoff=base + timedelta(days=i),
            home_team_id=str(i % 2 + 1),
            away_team_id=str((i + 1) % 2 + 1),
            completed=True,
            home_goals=1,
            away_goals=1,
            season_phase="regular-season",
        )
        for i in range(10)
    ]

    records = replay(dataset, PoissonV1Adapter())
    assert records, "harness produced nothing with odds unavailable"


def test_prediction_artifacts_carry_no_price_fields():
    """
    Serialised predictions contain no odds, price, edge or stake field.

    An artifact schema is a contract: once a price column exists, a later Epic
    will fill it, and profitability claims would then be one aggregation away.
    """
    from datetime import datetime, timezone

    from domain.evaluation import BttsOutcome, PredictionRecord, to_json_dict

    record = PredictionRecord(
        model_id="POISSON_V1",
        model_version="1.0.0",
        competition="eng.1",
        season=2020,
        event_id="e1",
        kickoff=datetime(2021, 1, 1, tzinfo=timezone.utc),
        home_team_id="1",
        away_team_id="2",
        outcome=BttsOutcome.YES,
        probability=0.6,
    )
    keys = set(to_json_dict(record))
    # Matched against whole name COMPONENTS, not substrings: `ev` inside
    # `event_id` is not a betting field, and a substring test would ban the
    # provenance the artifact exists to carry.
    components = {part for key in keys for part in key.lower().split("_")}
    banned = {"odds", "price", "edge", "stake", "value", "bookmaker", "ev", "roi", "profit"}
    offending = components & banned
    assert not offending, f"prediction artifact exposes betting field(s) {sorted(offending)}"
