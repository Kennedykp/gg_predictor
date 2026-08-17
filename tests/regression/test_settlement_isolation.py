"""
Epic 2H-2 - settlement is a recorder, enforced structurally.

Three failures would make settlement worse than useless, and none of them is
prevented by careful coding. Each is checked here against the import graph and
the source itself, so the guard holds even if no line of code exercises it yet.

1. SETTLEMENT RE-RUNS THE MODEL. `evaluation_harness.replay()` pairs fixtures
   with outcomes, which makes it look like exactly the right tool - while
   quietly recomputing the probability from today's data. A settlement job that
   used it would write a hindsight number that is indistinguishable in the file
   from the one that was actually published. This is the single most dangerous
   available shortcut, so the import is forbidden rather than discouraged.

2. SETTLEMENT WRITES TO THE LEDGER. A prediction is evidence of what was
   believed before kickoff. A settler that could edit one could rewrite history
   to agree with the result.

3. THE EVALUATION FIREWALL RE-OPENS. `test_evaluation_leakage.py` walls the
   evaluator off from prices and decisions, and `test_ledger_isolation.py` stops
   it reaching them transitively through the ledger. The settlement job imports
   the ledger, so if an evaluation module imported the job, the firewall would be
   breached through a module that names nothing forbidden.

The purity checks are asserted against the AST, not by observation: a module that
merely happens not to do IO today is not a module that cannot.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTRACT = REPO_ROOT / "domain" / "settlement.py"
JOB = REPO_ROOT / "settle_predictions.py"

# The contract is pure. It must not reach the model, the decision rules, a
# provider, the disk or the harness. `domain.evaluation` IS allowed and is the
# point: `btts_outcome` is the single chokepoint for deriving an outcome from a
# score, and re-deriving it here would reintroduce the None -> NO collapse.
FORBIDDEN_FOR_CONTRACT = {
    "poisson",
    "filters",
    "decision",
    "output",
    "espn",
    "odds_api",
    "evaluation_harness",
    "prediction_ledger",
    "historical_dataset",
    "requests",
    "json",
    "pathlib",
    "random",
    "uuid",
    "subprocess",
    "os",
}

# The job does IO by design, but must never run the model or grade anything.
FORBIDDEN_FOR_JOB = {
    "poisson",
    "filters",
    "decision",
    "evaluation_harness",
    "run_evaluation",
    "odds_api",
    "output",
}

# The evaluation layer, as `test_evaluation_leakage.py` defines it.
EVALUATION_MODULES = (
    REPO_ROOT / "domain" / "evaluation.py",
    REPO_ROOT / "evaluation_harness.py",
    REPO_ROOT / "run_evaluation.py",
)

SETTLEMENT_MODULE_NAMES = {"settle_predictions", "domain.settlement", "settlement"}

# Settlement lags prediction and must never be able to affect it.
PRODUCTION_ENTRY_POINTS = (
    REPO_ROOT / "main.py",
    REPO_ROOT / "analyze_all.py",
    REPO_ROOT / "prediction_ledger.py",
    REPO_ROOT / "domain" / "prediction_log.py",
)


def imported_modules(path: Path) -> set:
    """Every module name imported anywhere in a file, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.add(node.module.split(".")[0])
    return found


# ---------------------------------------------------------------------------
# The contract cannot reach the model, a provider or the disk
# ---------------------------------------------------------------------------
def test_the_contract_cannot_import_the_model_or_decision_rules():
    """
    No import path from `domain/settlement.py` to `poisson`, `filters` or
    `decision`. A settler that can call the model can re-run it.
    """
    leaked = imported_modules(CONTRACT) & FORBIDDEN_FOR_CONTRACT
    assert not leaked, (
        f"domain/settlement.py imports {sorted(leaked)}. The settlement contract must be "
        "pure: no model, no provider, no disk, no clock source. Those belong in "
        "settle_predictions.py."
    )


def test_the_contract_cannot_import_the_evaluation_harness():
    """
    Named separately because it is the specific trap (2H-F6).

    `evaluation_harness.replay()` recomputes probabilities from today's data. It
    looks like the right tool for settlement because it already pairs fixtures
    with outcomes, and using it would silently substitute a hindsight number for
    the published one.
    """
    assert "evaluation_harness" not in imported_modules(CONTRACT)
    assert "evaluation_harness" not in imported_modules(JOB)


def test_the_contract_uses_the_shared_outcome_derivation():
    """
    `btts_outcome` is the single chokepoint that refuses to read a missing score
    as NO. A second derivation anywhere would reintroduce that collapse, so the
    contract must import it rather than reimplement it.
    """
    assert "domain.evaluation" in imported_modules(CONTRACT)


def test_the_contract_performs_no_io():
    """
    No `open`, no `read_text`, no `write_text`, no `mkdir` in the contract.

    Asserted structurally rather than by behaviour: a pure module is one that
    CANNOT do IO, not one that happens not to today.

    Filesystem verbs only. `get` is deliberately NOT in this set: it is
    overwhelmingly `Mapping.get`, and a network `get` requires importing
    `requests` or `urllib`, which `FORBIDDEN_FOR_CONTRACT` already blocks. A
    check that flagged every `.get(` would fail on `prediction.get("season")`
    and would be silenced rather than fixed - a guard nobody trusts is worse
    than no guard.
    """
    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    forbidden = called & {
        "open",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "mkdir",
        "unlink",
    }
    assert not forbidden, f"domain/settlement.py performs filesystem IO: {sorted(forbidden)}"


def test_the_contract_cannot_reach_the_network_at_all():
    """
    The complement of the check above: no HTTP client is importable from the
    contract, so no `.get(` inside it can be a request.
    """
    imports = imported_modules(CONTRACT)
    assert not (imports & {"requests", "urllib", "http", "socket", "httpx"})


def test_the_contract_reads_no_clock():
    """
    `settled_at` is injected. A module that reads the clock cannot be pinned by a
    test, and a settlement timestamp that varies per run makes two identical
    passes produce different bytes.
    """
    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"))
    clock_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "utcnow", "today", "time"}
    ]
    assert not clock_calls, (
        "domain/settlement.py reads the clock. `settled_at` must be injected by "
        "the caller so every field is a function of the inputs."
    )


def test_the_contract_generates_no_identifier():
    """
    A random id inside the contract would make two settlements of the same facts
    differ, which would defeat the determinism the tests rely on.
    """
    imports = imported_modules(CONTRACT)
    assert "uuid" not in imports and "random" not in imports


# ---------------------------------------------------------------------------
# The job cannot run the model, and cannot write a prediction
# ---------------------------------------------------------------------------
def test_the_job_cannot_import_the_model_or_the_grader():
    leaked = imported_modules(JOB) & FORBIDDEN_FOR_JOB
    assert not leaked, (
        f"settle_predictions.py imports {sorted(leaked)}. Settlement records what "
        "happened; it must never compute a probability or grade one."
    )


def test_the_job_only_reads_the_ledger():
    """
    THE LEDGER IS IMMUTABLE.

    `prediction_ledger` exposes writers (`append_records`, `record_predictions`)
    and one reader (`load_records`). Settlement may use the reader only. Checked
    on the import list rather than on call sites, because importing a writer is
    already enough to make the guarantee unprovable.
    """
    tree = ast.parse(JOB.read_text(encoding="utf-8"))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "prediction_ledger":
            imported_names |= {alias.name for alias in node.names}

    writers = imported_names & {"append_records", "record_predictions", "build_records"}
    assert not writers, (
        f"settle_predictions.py imports ledger writers {sorted(writers)}. Settlement "
        "must read predictions and write only to data/settlements/."
    )
    assert "load_records" in imported_names


def test_every_open_in_the_job_appends_or_reads():
    """
    APPEND-ONLY, enforced on the source.

    Mode "w" would truncate a settlement history. A corrected settlement is a new
    line, because the fact that we once believed otherwise is itself worth
    keeping. Every `open()` must therefore be "a" or "r".
    """
    tree = ast.parse(JOB.read_text(encoding="utf-8"))
    modes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_open = (isinstance(node.func, ast.Name) and node.func.id == "open") or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "open"
        )
        if not is_open:
            continue
        mode = None
        if node.args and isinstance(node.args[0], ast.Constant):
            mode = node.args[0].value
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        modes.append(mode)

    assert modes, "no open() call found - the AST shape must have changed"
    for mode in modes:
        assert mode is not None, "an open() without an explicit mode defaults to read; be explicit"
        assert mode[0] in {"a", "r"}, (
            f"settle_predictions.py opens a file in mode {mode!r}. Settlement is "
            "append-only: 'w' would destroy settlement history."
        )


def test_the_job_writes_only_to_the_settlement_directory():
    """The default output path is `data/settlements`, never `data/predictions`."""
    source = JOB.read_text(encoding="utf-8")
    assert 'Path("data/settlements")' in source
    assert 'Path("data/predictions")' not in source


# ---------------------------------------------------------------------------
# The evaluation firewall stays shut
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", EVALUATION_MODULES, ids=lambda p: p.name)
def test_the_evaluation_layer_cannot_import_settlement(path):
    """
    The mirror of `test_the_evaluation_layer_cannot_import_the_ledger`.

    The settlement job imports `prediction_ledger`, which legitimately holds
    prices and the recommendation. So an evaluation module importing the job
    would reach the ledger transitively and defeat the odds firewall without
    naming a forbidden module. The evaluation layer consumes settlement OUTPUT
    (a JSONL file), never the settler.
    """
    leaked = imported_modules(path) & SETTLEMENT_MODULE_NAMES
    assert not leaked, (
        f"{path.name} imports {sorted(leaked)}. The settlement job reads the ledger, "
        "so importing it would hand the evaluator a price-bearing object "
        "transitively. Read the settlement file instead."
    )


@pytest.mark.parametrize("path", PRODUCTION_ENTRY_POINTS, ids=lambda p: p.name)
def test_production_never_imports_settlement(path):
    """
    Settlement lags prediction and must never be able to affect it.

    If `main.py` imported the settler, a provider failure during settlement could
    cost a prediction - and the recording of a result would sit on the critical
    path of producing one.
    """
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    leaked = imported_modules(path) & SETTLEMENT_MODULE_NAMES
    assert not leaked, (
        f"{path.name} imports {sorted(leaked)}. Settlement is an offline job; "
        "prediction must not depend on it."
    )


# ---------------------------------------------------------------------------
# The mirrored status names must not drift
# ---------------------------------------------------------------------------
def test_the_mirrored_status_names_still_match_the_provider():
    """
    `domain/settlement.py` mirrors ESPN's not-playable status names rather than
    importing `espn` (which would put a network client inside a pure contract).
    Mirroring risks drift, so this asserts the three sets still partition
    `espn._NOT_PLAYABLE` exactly.

    If ESPN adds a status, this test fails and names it - which is the point. A
    new not-playable status that no reason covers would otherwise be classified
    NOT_YET_PLAYED and retried forever.
    """
    import espn
    from domain.settlement import (
        _ABANDONED_NAMES,
        _CANCELLED_NAMES,
        _POSTPONED_NAMES,
    )

    mirrored = _POSTPONED_NAMES | _CANCELLED_NAMES | _ABANDONED_NAMES
    assert mirrored == set(espn._NOT_PLAYABLE), (
        "the mirrored status names have drifted from espn._NOT_PLAYABLE: "
        f"missing {sorted(set(espn._NOT_PLAYABLE) - mirrored)}, "
        f"extra {sorted(mirrored - set(espn._NOT_PLAYABLE))}"
    )


def test_every_not_playable_status_has_its_own_reason():
    """
    Each not-playable status maps to a distinct, non-default reason. A status that
    fell through to NOT_YET_PLAYED would be retried forever with no explanation.
    """
    from datetime import datetime, timezone

    import espn
    from domain.historical import HistoricalMatch
    from domain.settlement import SettlementStatus, UnresolvedReason, classify

    for status in espn._NOT_PLAYABLE:
        match = HistoricalMatch(
            event_id="1",
            competition="eng.1",
            season=2026,
            kickoff=datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc),
            home_team_id="H",
            away_team_id="A",
            completed=False,
            status=status,
        )
        settlement_status, reason = classify(match)
        assert settlement_status is SettlementStatus.UNRESOLVED
        assert reason is not UnresolvedReason.NOT_YET_PLAYED, (
            f"{status} falls through to NOT_YET_PLAYED and would be retried forever"
        )
