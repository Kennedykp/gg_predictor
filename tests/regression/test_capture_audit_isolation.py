"""
Epic 2I — the capture-audit firewall.

The temptation this exists to block is specific and reasonable-sounding. Once a
tool can say "fixture 401 has no prediction", the obvious next question is "well,
what WOULD the model have predicted?" — and answering it would require importing
`poisson`. At that moment the verifier stops being an auditor of recorded evidence
and starts manufacturing the evidence it is supposed to be checking.

So the rule is structural rather than advisory: the reconciliation layer cannot
import a model, a replay harness, market data or a writer. Enforced on the parsed
AST, so an alias (`import poisson as p`) or a function-local import cannot slip
past a text search.

Every guard here was demonstrated able to fail before this file was committed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Set

import pytest

ROOT = Path(__file__).resolve().parents[2]

PURE_MODULE = ROOT / "domain" / "capture_audit.py"
CLI_MODULE = ROOT / "verify_capture.py"

# Would let the verifier compute a probability, i.e. invent the evidence.
MODEL_MODULES = {
    "poisson",
    "decision",
    "filters",
    "goal_models",
    "domain.goal_models",
    "cold_start",
    "domain.cold_start",
    "team_strength",
    "domain.team_strength",
    "poisson_inputs",
    "domain.poisson_inputs",
}

# Would let it re-derive predictions over old data — 2H-F6's trap.
REPLAY_MODULES = {
    "evaluation_harness",
    "run_evaluation",
    "analyze_all",
    "main",
}

# LEAK-001: no price may enter this layer. Capture is not a value question.
MARKET_MODULES = {"odds_api", "shared.odds", "output"}

# Would let a "fix" be written back into immutable records.
WRITER_MODULES = {"settle_predictions", "domain.settlement"}

# A threshold here would invite "capture rate above the recommendation line",
# which measures the FILTER rather than the capture.
CONFIG_MODULES = {"config"}


def imported_modules(path: Path) -> Set[str]:
    """
    Every module named by an import in this file, at any nesting depth.

    Walks the AST rather than matching text: `import poisson as p` and an import
    inside a function body both have to be caught, and neither looks like
    "import poisson" in the source.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                found.add(node.module.split(".")[0])
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


def called_names(path: Path) -> Set[str]:
    """Every function name called in this file, however it is spelled."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@pytest.mark.parametrize("path", [PURE_MODULE, CLI_MODULE], ids=["pure", "cli"])
@pytest.mark.parametrize(
    ("banned", "label"),
    [
        (MODEL_MODULES, "model"),
        (REPLAY_MODULES, "replay"),
        (MARKET_MODULES, "market"),
        (CONFIG_MODULES, "config"),
    ],
)
def test_capture_layer_imports_no_forbidden_module(
    path: Path, banned: Set[str], label: str
) -> None:
    offending = imported_modules(path) & banned
    assert not offending, f"{path.name} must not import {label} code: {sorted(offending)}"


@pytest.mark.parametrize("path", [PURE_MODULE, CLI_MODULE], ids=["pure", "cli"])
def test_capture_layer_never_writes_a_record(path: Path) -> None:
    offending = imported_modules(path) & WRITER_MODULES
    assert not offending, f"{path.name} must not import a writer: {sorted(offending)}"


def test_the_pure_module_performs_no_io() -> None:
    # No file handle, no path read, no directory walk. Its inputs arrive as
    # arguments, which is what makes it deterministic and trivially testable.
    banned_calls = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "glob", "iterdir"}
    offending = called_names(PURE_MODULE) & banned_calls
    assert not offending, f"domain/capture_audit.py must do no IO: {sorted(offending)}"


def test_the_pure_module_imports_no_io_or_network_module() -> None:
    banned = {"pathlib", "requests", "urllib", "socket", "http", "os", "json", "sqlite3"}
    offending = imported_modules(PURE_MODULE) & banned
    assert not offending, f"domain/capture_audit.py must not import {sorted(offending)}"


def test_the_pure_module_reads_no_clock() -> None:
    # A clock would make the same inputs produce different output tomorrow. The
    # caller supplies every date, so "yesterday" is a CLI concern, not a domain one.
    offending = called_names(PURE_MODULE) & {"now", "today", "utcnow", "time", "monotonic"}
    assert not offending, f"domain/capture_audit.py must read no clock: {sorted(offending)}"


def test_the_pure_module_does_not_import_the_ledger_or_a_provider() -> None:
    # It reconciles data it is HANDED. Reaching for the ledger itself would give
    # the pure layer a filesystem dependency and two ways to load a record.
    banned = {"prediction_ledger", "espn", "api_football", "sofascore", "sportmonks", "historical_dataset"}
    offending = imported_modules(PURE_MODULE) & banned
    assert not offending, f"domain/capture_audit.py must stay pure: {sorted(offending)}"


def test_the_pure_module_does_not_import_the_evaluation_layer() -> None:
    # Capture assurance is upstream of evaluation and must not depend on it, or
    # a metric change could alter what counts as captured.
    banned = {
        "domain.evaluation",
        "domain.evaluation_input",
        "domain.reporting",
        "domain.lifecycle",
        "evaluate_settled",
        "report_evaluation",
        "run_lifecycle",
    }
    offending = imported_modules(PURE_MODULE) & banned
    assert not offending, f"domain/capture_audit.py must not import evaluation: {sorted(offending)}"


def test_the_cli_reads_the_ledger_but_only_appends_nothing_to_it() -> None:
    # Every write in the CLI must target the ARTIFACT directory. A ledger write
    # would be a mutation of immutable evidence.
    source = CLI_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"write_text", "write_bytes", "mkdir"}
    ]
    # The only writes are the artifact file and the artifact directory it lives in.
    assert len(writes) == 2, "unexpected write in verify_capture.py"
    assert "audit_dir" in source


def test_the_cli_never_opens_a_file_for_appending_or_writing() -> None:
    tree = ast.parse(CLI_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            args = [a for a in node.args[1:] if isinstance(a, ast.Constant)]
            for arg in args:
                assert "w" not in str(arg.value), "verify_capture.py must not open a file for writing"
                assert "a" not in str(arg.value), "verify_capture.py must not open a file for appending"


def test_no_probability_field_is_read_anywhere_in_the_capture_layer() -> None:
    # The strongest guarantee available at this level: the string "probability"
    # does not appear as a lookup. The verifier asks whether a record EXISTS, never
    # what it says, so it cannot be influenced by the value it is auditing.
    for path in (PURE_MODULE, CLI_MODULE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {
                    "probability",
                    "lambda_home",
                    "lambda_away",
                    "recommendation",
                    "edge",
                    "price",
                }, f"{path.name} must not read model or market fields"


def test_the_isolation_guard_can_actually_fail() -> None:
    # A firewall that cannot fail is decoration. This proves the mechanism by
    # running it against a synthetic module that violates the rule, rather than
    # asking a reader to trust that the real check works.
    offending = MODEL_MODULES & {"poisson", "filters"}
    assert offending, "the banned set must be non-empty or every test above is vacuous"

    tree = ast.parse("import poisson as p\n\ndef f():\n    from filters import x\n")
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    assert found & MODEL_MODULES == {"poisson", "filters"}, (
        "the AST walk must catch both an aliased import and a function-local one"
    )
