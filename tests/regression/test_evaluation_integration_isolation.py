"""
Structural guards for the settlement/evaluation integration (Epic 2H-3).

These tests read SOURCE, not behaviour. A behavioural test proves the code did
the right thing on one input; these prove the wrong thing is not expressible.

Four rules:

  1. REPLAY IS FORBIDDEN. `evaluation_harness.replay()` regenerates a probability
     from today's data. Grading that instead of the stored value answers "what
     would the model say now?" while appearing to answer "how good was what we
     published?", and the two are indistinguishable in a report.

  2. NO MODEL IMPORTS. The adapter and the runner cannot reach `poisson`,
     `filters` or `decision`, so they cannot recompute anything.

  3. NO CYCLES. Prediction -> settlement -> evaluation flows one way, so no
     module may reach FORWARD to the layer that grades it. Note that depending
     BACKWARD on `domain/evaluation.py` is fine and intended - it is the frozen
     leaf that imports nothing from this repo, which is exactly why settlement
     can safely share its outcome derivation.

  4. NO WRITES TO EITHER LOG. Evaluation is a reader of the ledger and the
     settlement log. A grader that could edit its own inputs could make itself
     look good.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The two modules added by this Epic.
ADAPTER = REPO_ROOT / "domain" / "evaluation_input.py"
RUNNER = REPO_ROOT / "evaluate_settled.py"

# Anything that can produce a probability. Importing one of these into the
# integration layer is how a hindsight number gets in.
MODEL_MODULES = {"poisson", "filters", "decision", "run3", "main", "analyze_all"}


def imported_modules(path: Path) -> set:
    """
    Every module named by an import in one file, including inside functions.

    AST rather than text: a comment mentioning `replay` is harmless, an import of
    it is not, and only a parse can tell the two apart.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                names.add(node.module.split(".")[0])
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}")
    return names


def attribute_calls(path: Path) -> set:
    """Dotted call targets, e.g. `evaluation_harness.replay`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func
            if isinstance(target.value, ast.Name):
                out.add(f"{target.value.id}.{target.attr}")
            out.add(target.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


# ---------------------------------------------------------------------------
# 1. Replay is forbidden
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [ADAPTER, RUNNER], ids=lambda p: p.name)
def test_the_harness_is_never_imported(path):
    """
    THE CENTRAL RULE OF THIS EPIC.

    `evaluation_harness` owns `replay()`, which runs a model over a dataset and
    computes a probability. Evaluation of stored predictions must consume the
    ledger's number, so the module that could regenerate one is unreachable.
    """
    assert "evaluation_harness" not in imported_modules(path)


@pytest.mark.parametrize("path", [ADAPTER, RUNNER], ids=lambda p: p.name)
def test_replay_is_never_called(path):
    """Belt and braces: not imported, and not called under any alias."""
    calls = attribute_calls(path)
    assert "replay" not in calls
    assert "evaluation_harness.replay" not in calls


@pytest.mark.parametrize("path", [ADAPTER, RUNNER], ids=lambda p: p.name)
def test_no_model_module_is_imported(path):
    """
    No `poisson`, no `filters`, no `decision`. A module that cannot reach the
    model cannot regenerate a probability, whatever a future edit intends.
    """
    offending = imported_modules(path) & MODEL_MODULES
    assert not offending, f"{path.name} imports {sorted(offending)}"


def test_the_adapter_computes_no_probability():
    """
    The adapter must COPY the stored float, never derive one.

    A probability is produced by exponentials, factorials and products (see
    `poisson.py`). None of those operators may appear here: their presence would
    mean a number is being made rather than carried.
    """
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"exp", "factorial", "pow", "log"}, (
                f"{node.func.attr} in the adapter: a probability is being computed, "
                "not carried"
            )
        # `**` is how a Poisson term is written.
        assert not isinstance(node, ast.Pow), "exponentiation in the adapter"


def test_the_adapter_imports_no_math_module():
    """Nothing to compute with. `math` is absent from the adapter entirely."""
    assert "math" not in imported_modules(ADAPTER)


def test_the_probability_is_passed_through_untouched():
    """
    The single assignment that matters, asserted on the source.

    `probability` must be read straight from the record and handed to
    `PredictionRecord` unmodified. If a future edit wraps it in `round()`, or
    rescales it, this fails.
    """
    source = ADAPTER.read_text(encoding="utf-8")
    assert 'probability = prediction.get("probability")' in source
    assert "probability=probability," in source
    for forbidden in (
        "round(probability",
        "float(probability)",
        "probability *",
        "probability /",
        "min(probability",
        "max(probability",
    ):
        assert forbidden not in source, f"{forbidden!r} modifies the stored probability"


# ---------------------------------------------------------------------------
# 2. The join cannot be fuzzy
# ---------------------------------------------------------------------------
def test_the_adapter_never_compares_team_names():
    """
    GG-008. The odds clients match teams by substring and pair "Athletic" with
    "Athletic Club". A join that could do that would produce confident, wrong
    evidence, so the name fields are never read.
    """
    source = ADAPTER.read_text(encoding="utf-8")
    for field in ("home_team_name", "away_team_name", "competition_name"):
        assert f'get("{field}")' not in source, f"{field} is read by the join"


def test_the_adapter_uses_no_fuzzy_matching_helpers():
    source = ADAPTER.read_text(encoding="utf-8")
    for helper in (".lower()", "difflib", "SequenceMatcher", "startswith", "in name"):
        assert helper not in source, f"{helper!r} suggests a fuzzy join"


def test_the_join_key_is_the_documented_triple():
    """The key is asserted structurally, not just in a docstring."""
    from domain.evaluation_input import join_key_of_prediction, join_key_of_settlement

    record = {
        "competition": "eng.1",
        "season": 2026,
        "fixture_id": "740123",
        "home_team_name": "Arsenal",
        "kickoff": "2026-08-15T15:00:00+00:00",
    }
    assert join_key_of_prediction(record) == ("eng.1", 2026, "740123")
    assert join_key_of_settlement(record) == ("eng.1", 2026, "740123")


# ---------------------------------------------------------------------------
# 3. No cycles
# ---------------------------------------------------------------------------
def test_settlement_does_not_import_this_epics_modules():
    """
    One-way flow: prediction -> settlement -> evaluation.

    NOTE WHAT IS *NOT* ASSERTED. `domain/settlement.py` legitimately imports
    `domain.evaluation` for `btts_outcome`, so that the YES/NO rule has exactly
    one definition (pinned by `test_settlement_isolation.py::
    test_the_contract_uses_the_shared_outcome_derivation`). That is a dependency
    on the FROZEN LEAF and creates no cycle, because `domain/evaluation.py`
    imports nothing from this repo at all.

    What would create a cycle is settlement reaching FORWARD to the join or the
    runner - a settled result influenced by how it is graded. That is what is
    forbidden here.
    """
    for name in ("settle_predictions.py", "domain/settlement.py"):
        imports = imported_modules(REPO_ROOT / name)
        assert "domain.evaluation_input" not in imports, f"{name} reaches forward to the join"
        assert "evaluate_settled" not in imports, f"{name} reaches forward to the runner"


def test_the_ledger_does_not_import_the_evaluation_layer():
    for name in ("prediction_ledger.py", "domain/prediction_log.py"):
        imports = imported_modules(REPO_ROOT / name)
        assert "domain.evaluation_input" not in imports
        assert "evaluate_settled" not in imports


def test_the_frozen_evaluation_module_does_not_import_the_new_one():
    """
    `domain/evaluation.py` is frozen by this Epic and must stay the leaf of the
    dependency graph. The adapter depends on it, never the reverse - that is what
    makes the cycle impossible rather than merely absent.
    """
    imports = imported_modules(REPO_ROOT / "domain" / "evaluation.py")
    assert "domain.evaluation_input" not in imports
    assert "evaluate_settled" not in imports
    assert "settle_predictions" not in imports
    assert "prediction_ledger" not in imports


def test_the_adapter_depends_only_on_the_frozen_metrics():
    """
    The adapter is pure: it may import `domain.evaluation` and nothing else from
    the repo. No IO, so no `prediction_ledger`, no `settle_predictions`.
    """
    imports = imported_modules(ADAPTER)
    assert "domain.evaluation" in imports
    for forbidden in ("prediction_ledger", "settle_predictions", "espn", "historical_dataset"):
        assert forbidden not in imports, f"the pure adapter imports {forbidden}"


def test_the_import_graph_actually_loads():
    """
    A cycle the AST misses would still deadlock at import time, so the graph is
    also checked by really importing it.

    In a SUBPROCESS, for two reasons. The obvious approach - `importlib.reload()`
    in this process - is actively harmful: reloading rebuilds the module's enum
    CLASSES, so the `UnjoinableReason` member another test file imported at
    collection time is no longer the same object the reloaded module returns, and
    every `is` comparison against it starts failing. That is a test that breaks
    other tests, and it only shows up when the suite runs in one order.

    A subprocess also tests more: a genuinely cold interpreter, with nothing
    already resolved in `sys.modules` by an earlier import.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import evaluate_settled, domain.evaluation_input; print('ok')",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, f"importing the layer failed:\n{result.stderr}"
    assert "ok" in result.stdout


# ---------------------------------------------------------------------------
# 4. Neither log is written
# ---------------------------------------------------------------------------
def test_the_adapter_performs_no_io():
    """A pure join. No `open`, no `Path.write`, no file handling of any kind."""
    source = ADAPTER.read_text(encoding="utf-8")
    for forbidden in ("open(", "write_text", "mkdir", "json.dump", "requests"):
        assert forbidden not in source, f"{forbidden!r} in a pure module"


def test_the_adapter_reads_no_clock():
    """
    Timestamps arrive from storage. A module that reads the clock produces a
    different answer on every run over identical data.
    """
    source = ADAPTER.read_text(encoding="utf-8")
    assert "now(" not in source
    assert "utcnow" not in source


def test_the_runner_never_opens_a_file_for_writing():
    """
    The ONLY write in the runner is the report, via `Path.write_text` on the
    evaluation directory. No `open(..., "w")` and no `open(..., "a")` anywhere:
    a writer aimed at the ledger cannot be introduced by accident.
    """
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            modes = [a.value for a in node.args[1:] if isinstance(a, ast.Constant)]
            assert not any("w" in m or "a" in m for m in modes if isinstance(m, str)), (
                "the runner opens a file for writing"
            )


def test_the_runner_writes_only_to_the_evaluation_directory():
    source = RUNNER.read_text(encoding="utf-8")
    assert 'DEFAULT_EVALUATION_DIR = Path("data/evaluation")' in source
    # The report path is built from the evaluation directory alone.
    assert "directory = Path(evaluation_dir)" in source


def test_the_runner_imports_only_readers_from_the_two_logs():
    """
    `load_records` and `load_settlements` are read-only. `record_predictions`,
    `append_records`, `settle` and `append_settlements` all write, and none is
    imported here.
    """
    imports = imported_modules(RUNNER)
    assert "prediction_ledger.load_records" in imports
    assert "settle_predictions.load_settlements" in imports
    for writer in (
        "prediction_ledger.record_predictions",
        "prediction_ledger.append_records",
        "settle_predictions.settle",
        "settle_predictions.append_settlements",
    ):
        assert writer not in imports, f"{writer} can write to a log this layer must only read"


# ---------------------------------------------------------------------------
# The firewall the frozen module already had
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [ADAPTER, RUNNER], ids=lambda p: p.name)
def test_no_market_module_is_imported(path):
    """
    LEAK-001 extended to the new layer. The ledger stores a price; this layer
    reads ledger records and must not carry one into a metric.
    """
    imports = imported_modules(path)
    for forbidden in ("odds_api", "shared.odds", "sportmonks", "sofascore"):
        assert forbidden not in imports, f"{path.name} imports {forbidden}"


@pytest.mark.parametrize("path", [ADAPTER, RUNNER], ids=lambda p: p.name)
def test_no_threshold_constant_is_referenced(path):
    """
    A threshold decides a recommendation, not a probability's quality. Reading
    one here would make an evaluation metric move when a betting rule changed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for forbidden in ("EDGE_THRESHOLD", "MIN_ODDS", "MIN_AVG_GOALS", "MAX_CLEAN_SHEET_PCT"):
        assert forbidden not in referenced


def test_the_adapter_reads_no_odds_field_from_the_ledger():
    """
    Structural, not careful. The `odds` subtree is never indexed, so a price
    cannot reach the metrics even though it sits in every record the adapter
    reads.
    """
    source = ADAPTER.read_text(encoding="utf-8")
    for field in ("odds", "implied_probability", "edge", "recommendation"):
        assert f'get("{field}")' not in source, f"the adapter reads {field!r}"


def test_the_config_module_is_never_read():
    """
    Provenance comes from the ledger. Reading `config` here would stamp today's
    fingerprint onto a prediction made under an older one - exactly the
    discontinuity the fingerprint exists to expose.
    """
    for path in (ADAPTER, RUNNER):
        assert "config" not in imported_modules(path)
