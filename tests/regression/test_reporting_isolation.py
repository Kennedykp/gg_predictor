"""
Epic 2H-5 — the reporting layer's firewall, enforced structurally.

The 2H-4 argument applies unchanged, and one temptation is sharper here.

A breakdown invites a follow-up question: "esp.1 looks bad — what would the
CURRENT model have said about those fixtures?" That is a legitimate research
question and an illegitimate thing for this module to answer. The moment
reporting can call the model, a single artifact can mix probabilities that were
published with probabilities that were recomputed, and no reader can tell which
row is which. `evaluation_harness` exists for that question and is a different
tool with a different name.

The second temptation is `config`: a breakdown makes it tempting to report
"competitions where accuracy beat the recommendation threshold". That grades the
FILTER, not the probability, and reintroduces the decision layer into evaluation
through the back door.

A grep would be fooled by an alias or a function-local import, so the check is on
the parsed AST — the same helper shape `test_lifecycle_isolation.py` uses.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Set

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The reporting layer added by this Epic.
REPORTING_MODULES = ["report_evaluation.py", "domain/reporting.py"]

# Modules that compute a probability or a recommendation.
MODEL_MODULES = {
    "poisson",
    "decision",
    "filters",
    "domain.goal_models",
    "domain.poisson_inputs",
    "domain.team_strength",
    "domain.cold_start",
}

# Market data. LEAK-001: an evaluation artifact is about probability quality,
# never betting value.
MARKET_MODULES = {"odds_api", "shared.odds"}

# The prediction-writing path. Reporting must never mint or amend a prediction.
WRITER_MODULES = {"main", "output", "prediction_ledger.append"}

# Replay harnesses. These RECOMPUTE probabilities by design, which is correct for
# research and fatal for a report claiming `probability_source: "ledger"`.
REPLAY_MODULES = {"evaluation_harness", "run_evaluation", "analyze_all"}

# Live data sources. A report describes what is already on disk; reaching for a
# provider would make it non-deterministic and dependent on a network.
PROVIDER_MODULES = {
    "espn",
    "api_football",
    "sofascore",
    "sportmonks",
    "domain.availability",
}

# Thresholds. See the module docstring.
CONFIG_MODULES = {"config"}


def imported_modules(path: Path) -> Set[str]:
    """
    Every module named in an import anywhere in the file.

    Walks the whole tree, so a function-local import inside a helper is caught
    exactly like a top-level one - which is precisely where someone would put a
    "temporary" model call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


@pytest.mark.parametrize("module", REPORTING_MODULES)
def test_no_model_import(module: str) -> None:
    """
    Reporting cannot reach a probability model.

    This is what makes `probability_source: "ledger"` in the artifact a fact
    about the code rather than a claim in a docstring.
    """
    banned = imported_modules(ROOT / module) & MODEL_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}. Reporting summarises stored "
        f"predictions; it must not be able to compute new ones."
    )


@pytest.mark.parametrize("module", REPORTING_MODULES)
def test_no_market_import(module: str) -> None:
    banned = imported_modules(ROOT / module) & MARKET_MODULES
    assert not banned, f"{module} imports {sorted(banned)}: LEAK-001."


@pytest.mark.parametrize("module", REPORTING_MODULES)
def test_no_prediction_writer_import(module: str) -> None:
    """Cannot reach the prediction-writing path."""
    banned = imported_modules(ROOT / module) & WRITER_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}. A report is read-only; the ledger is "
        f"the one artifact this system cannot reconstruct."
    )


@pytest.mark.parametrize("module", REPORTING_MODULES)
def test_no_replay_import(module: str) -> None:
    """
    Cannot reach a replay harness.

    Without this, one artifact could mix published probabilities with recomputed
    ones and no reader could tell the rows apart.
    """
    banned = imported_modules(ROOT / module) & REPLAY_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}. Those recompute probabilities; a "
        f"report must grade what was published."
    )


@pytest.mark.parametrize("module", REPORTING_MODULES)
def test_no_live_provider_import(module: str) -> None:
    """
    Cannot fetch. Reporting is deterministic over data already on disk.

    Settlement is 2H-4's job and it is the only layer allowed to contact a
    provider. A report that fetched would produce different numbers on a rerun
    with no change to any input.
    """
    banned = imported_modules(ROOT / module) & PROVIDER_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}. Reporting reads settled data; "
        f"fetching belongs to settlement."
    )


@pytest.mark.parametrize("module", REPORTING_MODULES)
def test_no_config_import(module: str) -> None:
    """No threshold may enter a report. See the module docstring."""
    banned = imported_modules(ROOT / module) & CONFIG_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}. A threshold here grades the filter, "
        f"not the probability."
    )


def test_reporting_core_is_pure() -> None:
    """
    `domain/reporting.py` touches no filesystem, no network and no clock.

    Purity is what makes the ordering guarantee testable: the same inputs must
    group the same way on any machine, at any time.
    """
    found = imported_modules(ROOT / "domain/reporting.py")
    banned = found & {"json", "pathlib", "os", "sys", "urllib", "urllib.request", "requests"}
    assert not banned, f"domain/reporting.py imports {sorted(banned)}; it must stay pure."


def test_reporting_core_does_not_read_the_clock() -> None:
    """
    No `datetime.now` / `utcnow` / `time.time` in the pure core.

    A clock read inside grouping would make an identical set of inputs capable of
    producing two different reports, which is exactly the property the
    determinism contract promises it cannot do.
    """
    source = (ROOT / "domain/reporting.py").read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "utcnow", "time.time", "date.today"):
        assert forbidden not in source, (
            f"domain/reporting.py reads the clock via {forbidden}; grouping must "
            f"be a function of its inputs alone."
        )


def test_report_entry_point_reads_the_clock_once() -> None:
    """
    Exactly one clock read in `report_evaluation.py`.

    Two reads could stamp the artifact with one instant and name the file with
    another, so a directory listing would disagree with its own contents.
    """
    source = (ROOT / "report_evaluation.py").read_text(encoding="utf-8")
    assert source.count("datetime.now(") == 1, (
        "expected exactly one clock read in report_evaluation.py; "
        f"found {source.count('datetime.now(')}"
    )


def test_no_market_language_reaches_the_artifact() -> None:
    """
    No market vocabulary in any key the report emits.

    Checks key NAME COMPONENTS, matching the existing firewall in
    `test_evaluation_leakage.py`, rather than raw substrings: a substring scan
    flags `ledger_digest_before` for containing "edge", and a firewall that cries
    wolf on a legitimate field is one that gets deleted.
    """
    import json
    from datetime import datetime, timezone

    from report_evaluation import build_report, report

    banned = {"odds", "edge", "value", "stake", "bet", "bookmaker", "price", "market"}

    inputs, join, breakdowns = report(
        ledger_dir=ROOT / "does-not-exist",
        settlement_dir=ROOT / "does-not-exist",
    )
    payload = build_report(
        inputs,
        join,
        breakdowns,
        generated_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ledger_dir=ROOT,
        settlement_dir=ROOT,
        month=None,
        bin_count=10,
    )

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert isinstance(key, str)
                parts = set(key.lower().split("_"))
                assert not (parts & banned), f"market language in key {path}{key}"
                walk(value, f"{path}{key}.")
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(json.loads(json.dumps(payload)))
