"""
Epic 2H-4 — the operational layer's firewall, enforced structurally.

Behavioural tests prove the code does the right thing today. These prove it
CANNOT do the wrong thing tomorrow, by refusing the imports that would make the
wrong thing reachable at all.

Two temptations are being locked out, both of which look reasonable at the time:

  1. "the settlement job may as well generate today's predictions too"
     -> one command, and predictions get written for fixtures whose results are
        already known. Look-ahead, straight into an immutable ledger.

  2. "recompute the probability, the stored one may be stale"
     -> the metric stops describing what was believed at prediction time and
        starts describing today's model on yesterday's fixtures. The ledger
        becomes decorative.

A grep would be fooled by an alias or a local import, so the check is on the
parsed AST.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The operational layer added by this Epic.
OPERATIONAL_MODULES = ["run_lifecycle.py", "domain/lifecycle.py"]

# Modules that compute a probability or a recommendation. An evaluator that can
# reach these can regenerate what it is supposed to be judging.
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

# The prediction-writing path. Settlement must never mint a prediction.
WRITER_MODULES = {"main", "output"}


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
                # `from domain import goal_models` names the module in the alias.
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


@pytest.mark.parametrize("module", OPERATIONAL_MODULES)
def test_no_model_import(module: str) -> None:
    """
    The operational layer cannot reach a probability model.

    This is what makes `probability_source: "ledger"` in the artifact a fact
    about the code rather than a claim in a docstring.
    """
    found = imported_modules(ROOT / module)
    banned = found & MODEL_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}. The operational layer grades stored "
        f"predictions; it must not be able to compute new ones."
    )


@pytest.mark.parametrize("module", OPERATIONAL_MODULES)
def test_no_market_import(module: str) -> None:
    found = imported_modules(ROOT / module)
    banned = found & MARKET_MODULES
    assert not banned, f"{module} imports {sorted(banned)}: LEAK-001."


@pytest.mark.parametrize("module", OPERATIONAL_MODULES)
def test_no_prediction_writer_import(module: str) -> None:
    """
    Cannot reach the prediction-writing path.

    Without this, a scheduled settlement run could append to the ledger. The
    ledger is the one artifact this system cannot reconstruct.
    """
    found = imported_modules(ROOT / module)
    banned = found & WRITER_MODULES
    assert not banned, (
        f"{module} imports {sorted(banned)}: settlement must never write a prediction."
    )


@pytest.mark.parametrize("module", OPERATIONAL_MODULES)
def test_no_config_threshold_import(module: str) -> None:
    """
    No recommendation threshold reaches the evaluator.

    A threshold here would invite reporting "accuracy above the recommendation
    line", which grades the FILTER rather than the probability and quietly
    reintroduces the decision layer into evaluation.
    """
    found = imported_modules(ROOT / module)
    assert "config" not in found, f"{module} imports config: thresholds do not belong in evaluation."


def test_the_pure_layer_does_no_io() -> None:
    """
    `domain/lifecycle.py` is pure: no filesystem, no network, no clock.

    `datetime` is importable (types and arithmetic) but `now` is a parameter, so
    the module cannot read the wall clock. That is what makes reconciliation
    deterministic and testable at an arbitrary instant.
    """
    found = imported_modules(ROOT / "domain/lifecycle.py")
    for banned in ("json", "pathlib", "requests", "urllib", "urllib.request", "os", "sqlite3"):
        assert banned not in found, f"domain/lifecycle.py must stay pure; found {banned!r}"


def test_the_pure_layer_never_reads_the_clock() -> None:
    """
    No `datetime.now()`, `utcnow()` or `time.time()` anywhere in the pure module.

    A hidden clock read would make the same inputs classify differently depending
    on when the job happened to run, and the drift would show up as a metric
    change with no code change behind it.
    """
    source = (ROOT / "domain/lifecycle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"now", "utcnow", "today", "time"}:
                calls.append(node.func.attr)
    assert not calls, f"domain/lifecycle.py reads the clock: {calls}. `now` must be injected."


def test_the_pure_layer_declares_no_default_now() -> None:
    """
    `reconcile` and `stage_of` must REQUIRE `now`.

    A default of `datetime.now(timezone.utc)` would be evaluated at import time
    and silently freeze the clock for the process's lifetime - a bug that only
    manifests in a long-running job.
    """
    tree = ast.parse((ROOT / "domain/lifecycle.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {"reconcile", "stage_of"}:
            names = [arg.arg for arg in node.args.kwonlyargs]
            assert "now" in names, f"{node.name} must take `now` as a keyword argument"
            index = names.index("now")
            assert node.args.kw_defaults[index] is None, (
                f"{node.name} must not default `now`: an import-time default freezes the clock."
            )


def test_the_operational_script_reads_the_clock_exactly_once() -> None:
    """
    One `datetime.now()` in `run_lifecycle.py`, at the top of `main`.

    Two clock reads in one run would let a fixture be pending in the lifecycle
    section and settled in the metrics section of the SAME artifact - an
    internally inconsistent report, and a genuinely confusing one to debug.
    """
    tree = ast.parse((ROOT / "run_lifecycle.py").read_text(encoding="utf-8"))
    reads = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"now", "utcnow"}:
                reads += 1
    assert reads == 1, f"expected exactly one clock read in run_lifecycle.py, found {reads}"


def test_replay_is_not_reachable_from_the_operational_layer() -> None:
    """
    No import of the research/replay harnesses.

    `evaluation_harness` legitimately RECOMPUTES probabilities for research. That
    is the correct tool for a modelling question and the wrong one for grading a
    ledger: mixing them would let a report labelled `probability_source: ledger`
    contain replayed numbers.
    """
    for module in OPERATIONAL_MODULES:
        found = imported_modules(ROOT / module)
        for banned in ("evaluation_harness", "analyze_all", "run_evaluation"):
            assert banned not in found, (
                f"{module} imports {banned!r}: replayed probabilities must not reach "
                f"a ledger-graded report."
            )
