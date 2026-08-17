"""
Epic 2G - the ledger is an observer, enforced structurally.

Two failures would make this Epic worse than useless, and neither is prevented by
careful coding:

1. CAPTURE CHANGES A PREDICTION. Recording is meant to be a read. If the ledger
   can alter or cost a published recommendation, then adding observability made
   the system less trustworthy - and the evidence it collects would be evidence
   about itself.

2. ODDS RE-ENTER THE EVALUATION LAYER. `test_evaluation_leakage.py` walls the
   evaluator off from prices, thresholds and decisions. The ledger deliberately
   holds all three. If any evaluation module imports the ledger, that firewall is
   breached transitively - the import graph would allow it even if no line of
   code used it yet.

The comparison tests are checked against a deliberately sabotaged capture to
prove they can fail. A guard that cannot fail is not a guard.
"""

import ast
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The ledger must not be able to reach the model or the decision rules. Reading
# `config` IS allowed and is the point of `config_fingerprint`: thresholds are
# measured, never written.
FORBIDDEN_FOR_LEDGER = {"poisson", "filters", "decision", "output"}

LEDGER_MODULES = (
    REPO_ROOT / "domain" / "prediction_log.py",
    REPO_ROOT / "prediction_ledger.py",
)

# The evaluation layer, as `test_evaluation_leakage.py` defines it.
EVALUATION_MODULES = (
    REPO_ROOT / "domain" / "evaluation.py",
    REPO_ROOT / "evaluation_harness.py",
    REPO_ROOT / "run_evaluation.py",
)

LEDGER_MODULE_NAMES = {"prediction_ledger", "domain.prediction_log", "prediction_log"}


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
# The ledger cannot reach the model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", LEDGER_MODULES, ids=lambda p: p.name)
def test_ledger_cannot_import_the_model_or_decision_rules(path):
    """
    No import path from the ledger to `poisson`, `filters` or `decision`.

    A recorder that can call the model can re-run it, and a second probability
    computed at capture time would be indistinguishable in the file from the one
    that was actually published.
    """
    offending = imported_modules(path) & FORBIDDEN_FOR_LEDGER
    assert not offending, (
        f"{path.name} imports {sorted(offending)}: the ledger must observe the "
        "prediction pipeline, never participate in it"
    )


def test_the_contract_is_pure_data():
    """
    `domain/prediction_log.py` performs no IO and reads no clock.

    Checked by BOTH the imports and the calls. Note the call list deliberately
    excludes `get`: `dict.get` is how the adapter reads a result mapping, and
    banning the bare name would have failed on ordinary dictionary access while
    saying nothing about IO. The import check is what actually closes the door -
    a module that cannot import `pathlib` or `requests` cannot reach a disk or a
    socket regardless of what it calls.

    Purity is what makes `created_at` injectable, and therefore what makes every
    field in this contract pinnable by a test.
    """
    path = REPO_ROOT / "domain" / "prediction_log.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    forbidden_imports = {"pathlib", "os", "subprocess", "requests", "uuid", "espn"}
    offending = imported_modules(path) & forbidden_imports
    assert not offending, (
        f"domain/prediction_log.py imports {sorted(offending)}: the contract must "
        "stay pure data so every field is a function of its inputs"
    )

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", None
            )
            if name:
                called.add(name)

    # `now`/`today`/`uuid4` would make the record unreproducible; the writer owns
    # the clock and the ids, and passes both in.
    for forbidden in ("open", "write_text", "write_bytes", "mkdir", "now", "today", "uuid4"):
        assert forbidden not in called, (
            f"domain/prediction_log.py calls {forbidden}(): the contract must stay "
            "pure so `created_at` and every other field can be pinned by a test"
        )



# ---------------------------------------------------------------------------
# The odds firewall holds in the other direction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", EVALUATION_MODULES, ids=lambda p: p.name)
def test_the_evaluation_layer_cannot_import_the_ledger(path):
    """
    The direction `test_evaluation_leakage.py` cannot see.

    That test forbids the evaluator from importing `odds_api`, `shared.odds`,
    `decision` and `filters` directly. The ledger legitimately records prices and
    the recommendation, so importing the ledger would hand the evaluator a
    price-bearing object and defeat the firewall without ever naming a forbidden
    module.
    """
    offending = imported_modules(path) & LEDGER_MODULE_NAMES
    assert not offending, (
        f"{path.name} imports {sorted(offending)}: the ledger carries odds and "
        "recommendations, so this would re-open the Epic 2B.3 firewall"
    )


def test_the_existing_odds_firewall_still_passes():
    """
    Epic 2B.3's own guard, re-run here. Epic 2G's answer to the price problem was
    a parallel record rather than a weakened firewall; if that test now fails, the
    justification for this whole module is gone.
    """
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/regression/test_evaluation_leakage.py", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"the Epic 2B.3 odds firewall broke:\n{completed.stdout[-2000:]}"
    )


# ---------------------------------------------------------------------------
# The production wiring
# ---------------------------------------------------------------------------
def test_process_fixture_does_not_mention_the_ledger():
    """
    The load-bearing structural fact of this Epic.

    Every probability, filter verdict and recommendation is decided inside
    `process_fixture`. Capture lives in `main()` and that function is untouched,
    so "capture cannot change a prediction" is a property of the call graph, not
    a promise about the implementation.
    """
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "process_fixture"
    )
    body = ast.dump(target)
    for name in ("prediction_ledger", "record_predictions", "prediction_log"):
        assert name not in body, f"process_fixture references {name}"


def test_the_ledger_is_not_imported_at_main_module_scope():
    """
    Imported inside `main()` on purpose: importing `main` must not import the
    ledger. The entry-point consistency tests drive `process_fixture` directly,
    and this keeps the ledger out of their import graph entirely.
    """
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    top_level = set()
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            top_level.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module)

    assert not top_level & LEDGER_MODULE_NAMES


def test_capture_is_the_last_thing_main_does():
    """
    Ordered after `write_csv` and `write_json`. The newest code on the path must
    never be able to cost the outputs the system already relies on.
    """
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    dumped = [ast.dump(statement) for statement in target.body]

    def index_of(needle):
        return next(i for i, text in enumerate(dumped) if needle in text)

    assert index_of("record_predictions") > index_of("write_json")
    assert index_of("record_predictions") > index_of("write_csv")


# ---------------------------------------------------------------------------
# A full run, with and without capture
# ---------------------------------------------------------------------------
def realistic_results():
    """One recommended fixture, one refused - what a live day looks like."""
    return [
        {
            "fixture_id": "740123",
            "datetime": "2026-08-17T14:00Z",
            "league_id": "eng.1",
            "league_name": "Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_team_id": "359",
            "away_team_id": "363",
            "lambda_home": 1.62,
            "lambda_away": 1.31,
            "gg_probability": 0.6234,
            "odds": 1.85,
            "implied_probability": 0.5405,
            "edge": 0.0829,
            "passes_filters": True,
            "filter_outcome": "PASSED",
            "decision": "BET",
            "rejection_reasons": [],
            "model_input_samples": {"home": 12, "away": 11, "league": 240},
        },
        {
            "fixture_id": "740999",
            "datetime": "2026-08-17T16:30Z",
            "league_id": "esp.1",
            "league_name": "La Liga",
            "home_team": "Getafe",
            "away_team": "Cadiz",
            "home_team_id": "2922",
            "away_team_id": "3842",
            "lambda_home": None,
            "lambda_away": None,
            "gg_probability": None,
            "odds": None,
            "implied_probability": None,
            "edge": None,
            "passes_filters": False,
            "decision": "NO BET",
            "rejection_reasons": ["Missing or unreliable team stats"],
        },
    ]


# Where the ledger lands under `monkeypatch.chdir`. `DEFAULT_LEDGER_DIR` is the
# RELATIVE path `data/predictions`, so a run inside `tmp_path` writes inside
# `tmp_path` - the real default path is exercised, with nothing patched.
LEDGER_SUBDIR = Path("data") / "predictions"


def run_main(tmp_path, monkeypatch, *, results=None, sabotage=None):
    """
    Drive `main.main()` for a fixed date, with the network replaced.

    `run_daily_workflow` is stubbed so the pipeline's own output is a constant:
    anything that then differs between two runs is attributable to capture and
    nothing else.

    In the normal case `record_predictions` is NOT patched. Only `sabotage`
    replaces it, which is how the comparison guards below are proven able to
    fail. Patching by default would have meant these tests never exercised the
    real writer.
    """
    import main as main_module

    payload = realistic_results() if results is None else results
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "2026-08-17"])
    monkeypatch.setattr(main_module, "run_daily_workflow", lambda target: payload)

    if sabotage is not None:
        import prediction_ledger

        monkeypatch.setattr(prediction_ledger, "record_predictions", sabotage)

    main_module.main()
    return payload


def published_predictions(directory):
    """
    The prediction payload from `output_*.json`, without the run timestamp.

    `output.py:164` stamps every file with `datetime.now().isoformat()`, so two
    runs are NEVER byte-identical regardless of what capture does. Comparing raw
    bytes would therefore have "passed" for a reason unrelated to the ledger -
    and would have gone on passing while capture silently corrupted every
    prediction. The timestamp is dropped and everything that describes a
    prediction is compared.
    """
    payload = json.loads(
        (Path(directory) / "output_2026-08-17.json").read_text(encoding="utf-8")
    )
    return {key: value for key, value in payload.items() if key != "generated_at"}



class TestCaptureCannotChangeARun:
    def test_the_published_output_is_unchanged(self, tmp_path, monkeypatch):
        """
        The whole Epic in one assertion: what a consumer reads must be exactly
        what it was before the ledger existed. Capture runs for real here.
        """
        import prediction_ledger

        with_capture = tmp_path / "with"
        run_main(with_capture, monkeypatch, results=realistic_results())

        without = tmp_path / "without"

        def disabled(*args, **kwargs):
            return prediction_ledger.CaptureReport(run_id="disabled")

        run_main(without, monkeypatch, results=realistic_results(), sabotage=disabled)

        assert published_predictions(with_capture) == published_predictions(without)

    def test_the_csv_is_byte_identical(self, tmp_path, monkeypatch):
        """
        `write_csv` stamps no timestamp, so this one CAN be compared byte for
        byte - the strictest available check on the published artifacts.
        """
        import prediction_ledger

        with_capture = tmp_path / "with"
        run_main(with_capture, monkeypatch, results=realistic_results())

        without = tmp_path / "without"
        run_main(
            without,
            monkeypatch,
            results=realistic_results(),
            sabotage=lambda *a, **k: prediction_ledger.CaptureReport(run_id="off"),
        )

        name = "output_2026-08-17.csv"
        assert (with_capture / name).read_bytes() == (without / name).read_bytes()

    def test_the_results_list_is_unchanged_by_a_real_run(self, tmp_path, monkeypatch):
        import copy

        payload = realistic_results()
        expected = copy.deepcopy(payload)
        run_main(tmp_path, monkeypatch, results=payload)
        assert payload == expected

    def test_the_comparison_instrument_detects_a_real_difference(self, tmp_path, monkeypatch):
        """
        MUTATION CHECK on the comparison itself.

        The equality tests above are worthless if `published_predictions` cannot
        tell two different runs apart. It earned this check: the version I wrote
        first compared raw JSON bytes, which differ on EVERY run because
        `output.py:164` stamps `datetime.now()`. That comparison would have
        "detected" a difference always, and its mutation check would have passed
        for a reason unrelated to the ledger - vacuous, in the 2F-P1-1 sense.

        Here a genuinely different prediction is published and the instrument
        must notice.
        """
        clean = tmp_path / "clean"
        run_main(clean, monkeypatch, results=realistic_results())

        altered = realistic_results()
        altered[0]["decision"] = "NO BET"
        altered[0]["gg_probability"] = 0.01

        other = tmp_path / "other"
        run_main(other, monkeypatch, results=altered)

        assert published_predictions(clean) != published_predictions(other), (
            "published_predictions cannot distinguish two different runs, so the "
            "equality assertions above prove nothing"
        )

        name = "output_2026-08-17.csv"
        assert (clean / name).read_bytes() != (other / name).read_bytes()

    def test_ordering_alone_protects_the_published_files(self, tmp_path, monkeypatch):
        """
        A FINDING, recorded as a test: a mutating recorder cannot corrupt the
        published artifacts at all.

        Discovered while trying to make the mutation check above fail. Capture is
        the last statement in `main()`, so `write_csv` and `write_json` have
        already closed their files by the time it runs - mutating `results`
        afterwards changes nothing a consumer will ever read. The position of the
        call is therefore a second, independent protection on top of the
        read-only implementation.

        What a mutating recorder CAN still corrupt is the in-memory list, which
        matters to any future in-process caller. That is what
        `test_a_mutating_capture_is_detectable_in_the_list` covers.
        """
        import prediction_ledger

        def malicious(results, target=None, **kwargs):
            results[0]["decision"] = "NO BET"
            results[0]["gg_probability"] = 0.01
            return prediction_ledger.CaptureReport(run_id="bad")

        clean = tmp_path / "clean"
        run_main(clean, monkeypatch, results=realistic_results())

        dirty = tmp_path / "dirty"
        run_main(dirty, monkeypatch, results=realistic_results(), sabotage=malicious)

        assert published_predictions(clean) == published_predictions(dirty), (
            "a post-write mutation reached the published output, which means "
            "capture is no longer ordered after the writers"
        )

    def test_a_mutating_capture_is_detectable_in_the_list(self, tmp_path, monkeypatch):
        """
        MUTATION CHECK for `test_the_results_list_is_unchanged_by_a_real_run`.

        That test is the guard that actually bites, since ordering already
        protects the files. Proven able to fail here: a sabotaged recorder mutates
        the list and the same comparison detects it.
        """
        import copy

        import prediction_ledger

        def malicious(results, target=None, **kwargs):
            results[0]["decision"] = "NO BET"
            return prediction_ledger.CaptureReport(run_id="bad")

        payload = realistic_results()
        expected = copy.deepcopy(payload)
        run_main(tmp_path, monkeypatch, results=payload, sabotage=malicious)

        assert payload != expected, (
            "the list-comparison guard cannot detect a mutating capture"
        )




class TestAFailingLedgerCannotBreakARun:
    def test_a_raising_writer_still_leaves_a_complete_run(self, tmp_path, monkeypatch):
        """
        A full disk must degrade to "predictions not recorded", never to "the
        matchday was lost". Observability is not worth an outage.
        """
        def explode(*args, **kwargs):
            raise OSError("No space left on device")

        run_main(tmp_path, monkeypatch, results=realistic_results(), sabotage=explode)

        published = published_predictions(tmp_path)
        assert published["total_matches"] == 2
        assert published["results"][0]["decision"] == "BET"
        assert published["results"][0]["gg_probability"] == 0.6234


    def test_the_failure_is_reported_not_silent(self, tmp_path, monkeypatch, capsys):
        """
        A silent capture failure is worse than no capture: the gap would later
        read as a day on which nothing was predicted.
        """
        def explode(*args, **kwargs):
            raise OSError("disk full")

        run_main(tmp_path, monkeypatch, results=realistic_results(), sabotage=explode)

        assert "ledger" in capsys.readouterr().out.lower()

    def test_an_import_failure_is_survivable(self, tmp_path, monkeypatch):
        """
        The import itself sits inside the guarded block, so a broken or absent
        ledger module cannot stop a run either.
        """
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "prediction_ledger":
                raise ImportError("ledger unavailable")
            return real_import(name, *args, **kwargs)

        import main as main_module

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(main_module.sys, "argv", ["main.py", "2026-08-17"])
        monkeypatch.setattr(
            main_module, "run_daily_workflow", lambda target: realistic_results()
        )
        monkeypatch.setattr(builtins, "__import__", blocked)

        main_module.main()  # must not raise

        assert (tmp_path / "output_2026-08-17.json").exists()


class TestTheLedgerRecordsTheRun:
    """
    The positive case, through the real default path. Everything above proves
    capture is harmless; without these it could be harmless by doing nothing.
    """

    def test_a_real_run_produces_a_ledger_file(self, tmp_path, monkeypatch):
        from prediction_ledger import load_records

        run_main(tmp_path, monkeypatch, results=realistic_results())

        rows = load_records(tmp_path / LEDGER_SUBDIR)
        assert len(rows) == 2
        assert {row["fixture_id"] for row in rows} == {"740123", "740999"}

    def test_both_the_recommendation_and_the_refusal_are_recorded(self, tmp_path, monkeypatch):
        from prediction_ledger import load_records

        run_main(tmp_path, monkeypatch, results=realistic_results())
        rows = {
            row["fixture_id"]: row for row in load_records(tmp_path / LEDGER_SUBDIR)
        }

        assert rows["740123"]["recommendation"] == "BET"
        assert rows["740123"]["odds"]["price"] == 1.85
        assert rows["740999"]["status"] == "NO_TEAM_STATS"
        assert rows["740999"]["probability"] is None

    def test_the_recorded_probability_matches_the_published_one(self, tmp_path, monkeypatch):
        """
        The ledger must agree with `output_*.json`. If these ever diverge, the
        archive is describing a prediction the system never made - and the ledger
        would be worse than absent, because it would look authoritative.
        """
        from prediction_ledger import load_records

        run_main(tmp_path, monkeypatch, results=realistic_results())

        published = published_predictions(tmp_path)["results"]
        recorded = {
            row["fixture_id"]: row for row in load_records(tmp_path / LEDGER_SUBDIR)
        }

        for row in published:
            mirror = recorded[str(row["fixture_id"])]
            assert mirror["probability"] == row["gg_probability"]
            assert mirror["recommendation"] == row["decision"]
            assert mirror["lambda_home"] == row["lambda_home"]
            assert mirror["odds"]["edge"] == row["edge"]

    def test_two_runs_of_the_same_date_both_survive(self, tmp_path, monkeypatch):
        """
        `output_2026-08-17.json` is overwritten by the second run; the ledger
        keeps both. That difference is the reason this Epic exists.
        """
        from prediction_ledger import load_records

        run_main(tmp_path, monkeypatch, results=realistic_results())
        run_main(tmp_path, monkeypatch, results=realistic_results())

        rows = load_records(tmp_path / LEDGER_SUBDIR)
        assert len(rows) == 4
        assert len({row["run_id"] for row in rows}) == 2

    def test_the_default_path_is_used_without_patching(self, tmp_path, monkeypatch):
        """
        The ledger lands under `data/predictions/` relative to the working
        directory - the real configured location, gitignored, exercised here with
        nothing stubbed but the network.
        """
        run_main(tmp_path, monkeypatch, results=realistic_results())
        assert (tmp_path / LEDGER_SUBDIR).exists()
        assert list((tmp_path / LEDGER_SUBDIR).glob("*.jsonl"))

