# Epic 2I — Prediction Capture Assurance

**Status:** complete
**Scope:** make a silent capture failure impossible to mistake for a healthy run
**Production files changed:** none
**Predecessor:** Epic 2H-5 (evaluation reporting)
**Successor obligation:** Epic 2J (2G-R5: AUC + constant-predictor benchmark + minimum-*n* gating)

---

## 1. The problem

Epic 2H-5 shipped a reporting command that prints a Brier score from the prediction
ledger. During the audit that preceded this Epic, one fact made every number that
tool could ever print provisional:

**the ledger write path had never run.**

The evidence:

| Observation | Source |
|---|---|
| `data/` does not exist; no `.jsonl` file anywhere in the repo | filesystem |
| The only run artifacts are `output_2026-01-16/17/18.json`, mtime **Aug 7** | `ls -lt` |
| The ledger was merged **Aug 17** (commit `c88169d`) | `git log` |

The outputs pre-date the ledger by ten days. Five epics and ~2,300 tests had been
built on synthetic fixtures, and no real prediction had ever been recorded.

That alone is a state of the world, not a defect. The defect is that **nothing
could tell the difference between that state and a healthy one.**

### Why the failure was invisible

Three behaviours compose into silence:

1. **Capture swallows every exception.** `main.py:313-318` catches `Exception`
   around the ledger append and continues. A run therefore prints its
   recommendations, writes `output_<date>.json`, and exits 0 whether or not a
   single record was written.

2. **"Nothing happened" and "capture broke" produce the same value.**
   `prediction_ledger.py:270` returns `CaptureReport(path=None, written=0)` for a
   day with no fixtures *and* for a day whose write failed. Per-fixture failures
   land in `CaptureReport.skipped`, which is only ever rendered inside a printed
   summary string — no caller inspects it.

3. **An absent ledger reads as success.** `load_records` on a missing directory
   returns `[]`. `run_lifecycle.py:460-462` maps that to
   `"Nothing to evaluate yet."` and `EXIT_OK`. The lifecycle runner starts from the
   ledger, so an empty ledger gives it nothing to complain about.

A human acting on printed recommendations would have no signal that the evidence
behind them was being discarded.

### Why this ranked above the known 2G-R5 gap

The audit also confirmed an outstanding compliance gap in 2H-5: no AUC and no
minimum-*n* gate on the ledger-graded path, though
`EPIC_2H_SETTLEMENT_AUDIT.md:326-330` requires both. That gap is real and is now
Epic 2J.

Capture went first on **reversibility**, not severity:

- 2G-R5's harm is recoverable. The ledger is immutable and metrics derive from it;
  adding AUC later and re-running the report yields correct numbers over the same
  history.
- A capture gap's harm is not. `EPIC_2H_SETTLEMENT_AUDIT.md:832` —
  *"Backfilling predictions | Impossible, not merely out of scope. A prediction not
  recorded when it was made cannot be reconstructed."*

Every day the pipeline runs unverified risks permanent, silent loss. And 2G-R5
cannot mislead anyone yet, because there are no predictions to misread.

---

## 2. Why `main.py` still swallows exceptions

Unchanged, deliberately. The reasoning in 2G is correct: a full disk, a permissions
error or a serialisation bug must degrade to *"predictions were not recorded"*, not
to *"the matchday run failed"*. Making capture fatal would trade a lost matchday
for observability, which is the wrong trade — the recommendations are still
correct when the writer fails.

So 2I does not change failure behaviour. It adds an **independent observer** that
can detect the failure after the fact. The pipeline stays fail-soft; the evidence
becomes auditable.

This is why capture assurance had to be a separate command rather than a flag on
`run_lifecycle.py`. The lifecycle runner reads the *ledger*; detecting a capture
gap requires starting from the *schedule* — the one input the ledger cannot supply.
Different data flow, different entry point.

---

## 3. The reconciliation design

```
FIXTURE SCHEDULE  ──┐
 (ESPN or dataset)  │
                    ├──►  reconcile()  ──►  per-day verdict  ──►  artifact
PREDICTION LEDGER ──┘        (pure)
 (read-only)
```

One question only:

> Was there **evidence** that a prediction was captured for this fixture?

Never:

> What **would** the model have predicted?

Answering the second requires importing `poisson`, at which point the verifier
manufactures the evidence it is supposed to be checking. That boundary is enforced
structurally (§6), not by convention.

### Why not a count comparison

`len(fixtures) != len(records)` would have been the obvious implementation and
would have been useless. `is_predictable()` is unwired (**GG-013**), so a fixture
legitimately goes unpredicted whenever its inputs are insufficient — frequently,
and correctly. A count check would fire most days.

A verifier that cries wolf daily gets switched off, and that is strictly worse
than no verifier, because it is a check someone *believes* they have.

So the reconciliation classifies rather than counts.

### Classification

Per fixture (`FixtureOutcome`):

| Outcome | Meaning |
|---|---|
| `CAPTURED` | ≥1 ledger record references this `fixture_id` |
| `NOT_PLAYABLE` | Schedule says postponed / cancelled / abandoned / suspended. No prediction expected |
| `UNACCOUNTED` | Playable, no record. **Cause unknown** |

Per day (`DayVerdict`):

| Verdict | Meaning | Alertable |
|---|---|---|
| `NO_FIXTURES` | Nothing scheduled | no |
| `NO_PLAYABLE_FIXTURES` | Everything postponed/cancelled | no |
| `COMPLETE` | Every playable fixture captured | no |
| `PARTIAL` | Some captured, some not | no |
| `ZERO_CAPTURE` | Playable fixtures existed, **zero** records | **yes** |

### Why only `ZERO_CAPTURE` alerts

`ZERO_CAPTURE` is the only state that cannot be explained by per-fixture data
gaps. GG-013 skips are a property of *individual* fixtures; it would take every
fixture on the card failing simultaneously to imitate this, and the overwhelmingly
likelier cause is that capture never ran or never wrote.

`PARTIAL` is reported and never alerted. Separating "lost record" from "legitimate
skip" inside a partial day would require recomputing what the model would have
said — the one thing this layer may not do. Rather than implying more precision
than exists, the tool reports the ambiguity and says so.

`UNACCOUNTED` is therefore explicitly **not** an error. It is a number to watch,
not a page to answer.

### Provider outage ≠ capture gap

If the schedule cannot be loaded, the honest answer is *unknown*. Reporting it as
missing evidence would blame this pipeline for someone else's downtime and destroy
trust in the single alert the tool raises. Days whose schedule is unavailable are
counted, named in the artifact, and excluded from gap detection entirely
(`EXIT_NO_SCHEDULE`).

`espn.get_fixtures` returns `[]` both for "quiet day" and for "fetch failed", so
the live source treats an empty list as **unavailable**. That is the conservative
direction: calling a genuinely empty day "unknown" costs one uninformative row,
whereas calling an outage "no fixtures" would let a real gap pass as a quiet day.

### Joining on `fixture_id`, not on the ledger month

Records are matched by `fixture_id` across the whole ledger. Filtering by monthly
file would be wrong: `prediction_ledger.ledger_filename` names the file from the
prediction's **creation** time, so a Saturday fixture predicted on the Friday of a
new month lands in the previous month's file. A month filter would report that
prediction as missing.

`fixture_id` is safe as the join key because **2H-F1** established that a live
`fixture_id` and a historical `event_id` are the same ESPN identifier from the same
endpoint. Team names are never used (**GG-008**).

---

## 4. CLI

```bash
# Yesterday (the default window)
python verify_capture.py

# One date
python verify_capture.py --date 2026-08-15

# A whole month, failing the job if a gap is found
python verify_capture.py --month 2026-08 --fail-on-gap

# A trailing window, fully offline
python verify_capture.py --since 7d --dataset data/historical

# Report without writing an artifact
python verify_capture.py --date 2026-08-15 --dry-run
```

`--date`, `--month` and `--since` are mutually exclusive. The default is
**yesterday**: fixtures later today may not have kicked off, so a missing
prediction for them is not yet evidence of anything. `--since Nd` ends yesterday
for the same reason.

### Exit codes

| Code | Constant | Meaning |
|---|---|---|
| 0 | `EXIT_OK` | Audit completed. **A gap alone still exits 0.** |
| 1 | `EXIT_GAP` | A gap was found **and** `--fail-on-gap` was passed |
| 2 | `EXIT_LEDGER_MUTATED` | The ledger changed mid-run (another process is writing) |
| 3 | `EXIT_NO_SCHEDULE` | No schedule could be loaded for any requested date |

The default is observational by design. An audit that fails a scheduled job on a
football-side quirk is one that gets removed from the schedule.

### Offline behaviour

`--dataset` reads the schedule from a local historical corpus via
`historical_dataset.load_dataset`, mirroring `settle_predictions.dataset_result_source`.
A corpus on disk always *answers*: absence of a date is a genuine "nothing
scheduled", not an outage, which is what makes offline runs safe to gate on.

The ESPN import is function-local, so `--dataset` runs — and the entire test suite —
never import a provider module. `tests/conftest.py` blocks sockets, so an
accidental fetch fails loudly rather than passing slowly on live data.

### Artifact

`data/evaluation/capture_<timestamp>.json`, never overwritten. The `capture_`
prefix keeps these distinct from `evaluate_settled.py`, 2H-4's `lifecycle_` and
2H-5's `report_` files, so four tools share one directory without clobbering each
other.

```json
{
  "schema_version": "2i.1",
  "capture_audit_schema_version": "2i.1",
  "schedule_source": "dataset",
  "probability_source": null,
  "replay_used": false,
  "inputs": {
    "days_requested": ["2026-08-15"],
    "days_resolved": ["2026-08-15"],
    "days_schedule_unavailable": []
  },
  "ledger_integrity": {
    "digest_before": "sha256:a30a5d…",
    "digest_after": "sha256:a30a5d…",
    "unchanged": true
  },
  "totals": { "expected": 3, "captured": 2, "unaccounted": 0,
              "not_playable": 1, "capture_gaps": 0, "duplicates": 0,
              "off_schedule_records": 0, "days": 1, "undated_expected": [] },
  "days": [ { "day": "2026-08-15", "verdict": "COMPLETE",
              "is_capture_gap": false, "counts": {…}, "fixtures": […] } ]
}
```

`probability_source: null` and `replay_used: false` are recorded permanently: an
artifact read months from now states on its face that no model was consulted.
`schedule_source` names where the schedule came from, because a figure derived
from a cached corpus and one from a live fetch are not interchangeable.

---

## 5. Ledger integrity

The verifier is strictly read-only, and this is **verified on every run** rather
than asserted in a docstring. `ledger_digest` hashes the ledger's raw bytes —
filenames included — before and after the audit; the result is written into the
artifact, and a mismatch returns `EXIT_LEDGER_MUTATED`.

Bytes rather than parsed records: re-serialising would normalise key order and
float formatting, so a rewritten file would hash as unchanged, hiding precisely
the mutation this exists to catch. Filenames are hashed too, so deleting a whole
month is caught as well as editing a line.

`ledger_digest` is duplicated from `run_lifecycle.py` rather than imported.
Importing it would pull settlement and evaluation into the verifier's import graph
and break the isolation this Epic guarantees; six lines of hashing is the cheaper
cost. Tests prove the digest detects an edited line, an appended line and a
deleted month — a digest that cannot detect a modification is decoration, and
every read-only claim here rests on it.

---

## 6. Isolation guarantees

`tests/regression/test_capture_audit_isolation.py` enforces the boundary on the
parsed **AST**, so an aliased import (`import poisson as p`) or a function-local
import cannot slip past a text search.

Banned from both `domain/capture_audit.py` and `verify_capture.py`:

| Category | Why |
|---|---|
| Model (`poisson`, `decision`, `filters`, `goal_models`, `cold_start`, `team_strength`, `poisson_inputs`) | Would let the verifier compute a probability, i.e. invent the evidence |
| Replay (`evaluation_harness`, `run_evaluation`, `analyze_all`, `main`) | Would let it re-derive predictions over old data (2H-F6's trap) |
| Market (`odds_api`, `shared.odds`, `output`) | LEAK-001. Capture is not a value question |
| Writers (`settle_predictions`, `domain.settlement`) | Would let a "fix" be written into immutable records |
| `config` | A threshold here invites "capture rate above the recommendation line", which measures the *filter*, not capture |

`domain/capture_audit.py` additionally imports no `pathlib`/`os`/`json`/socket
module, calls no file or clock function, and imports neither the ledger nor a
provider nor the evaluation layer. Its inputs arrive as arguments, which is what
makes it deterministic.

A further guard asserts that no string literal in either file names
`probability`, `lambda_home`, `lambda_away`, `recommendation`, `edge` or `price`.
The verifier asks whether a record **exists**, never what it says, so it cannot be
influenced by the value it is auditing.

### The firewall was proven able to fail

A guard that cannot fail is decoration. Both a top-level aliased import and a
function-local one were temporarily injected into `domain/capture_audit.py`:

```python
import poisson as _p                     # module level, aliased
from filters import evaluate_filters     # inside a function body
```

Result:

```
FAILED tests/regression/test_capture_audit_isolation.py::
       test_capture_layer_imports_no_forbidden_module[banned0-model-pure]
AssertionError: capture_audit.py must not import model code: ['filters', 'poisson']
1 failed, 18 passed
```

Both violations were caught. The injection was reverted and the file restored from
backup; the suite returned to 19 passed, and `grep` confirms no `TEMPORARY` marker
or forbidden import remains.

---

## 7. Verification results

```
ruff check .      All checks passed!
mypy              Success: no issues found in 57 source files
pytest -q         2321 passed, 3 skipped in 4.37s
```

The 3 skips are pre-existing and unrelated (`test_epic2d_protocol` MODELS
registry; `test_spec_agreement` D1 and D3, tracked as GG-002-B). No existing test
was modified.

New tests: **107** (46 pure reconciliation + 42 CLI + 19 isolation).

### End-to-end smoke, fully offline

A three-fixture corpus (`401`, `402` playable; `403` postponed) driven through
`--dataset`:

| Ledger state | Verdict | `--fail-on-gap` exit | Notes |
|---|---|---|---|
| empty | `ZERO_CAPTURE` | **1** | `captured 0/2` — the postponement is excluded from the denominator |
| 1 of 2 records | `PARTIAL` | **0** | GG-013 skip does not alarm |
| both records | `COMPLETE` | **0** | `captured 2/2`, `403` filed `NOT_PLAYABLE` |

Ledger SHA-256 measured externally with `shasum` around the complete run:

```
before = de6e9cb9345da3e59bee088fd169c76a2a9b0839ea686e24e04d6f9cde1c63d2
after  = de6e9cb9345da3e59bee088fd169c76a2a9b0839ea686e24e04d6f9cde1c63d2
RESULT: LEDGER BYTE-IDENTICAL
```

`--dry-run` produced no artifact (4 files before, 4 after). Two identical runs
produced artifacts identical apart from `generated_at`.

### Invariants proven

| # | Invariant | How |
|---|---|---|
| 1 | No probability recomputed | AST: no model import; no probability-field literal |
| 2 | No ledger or settlement write | AST writer ban; digest before/after; directory listing unchanged |
| 3 | No market data read | AST market-module and price-literal ban |
| 4 | No model/replay import | AST, proven able to fail |
| 5 | Deterministic for identical inputs | Equality of two reconciliations and two artifacts |
| 6 | Ledger byte-identical | Internal digest + external `shasum` |
| 7 | Works offline | `--dataset`; sockets blocked; provider import is function-local |
| 8 | Existing tests unchanged and passing | 2321 passed; no test file edited |
| 9 | Zero production files outside the six additions | `git status` |

---

## 8. Files

**Added (6):**

| File | Role |
|---|---|
| `domain/capture_audit.py` | Pure reconciliation. No IO, no network, no clock |
| `verify_capture.py` | Read-only CLI. Schedule + ledger → artifact |
| `tests/unit/test_capture_audit.py` | 46 tests |
| `tests/unit/test_verify_capture.py` | 42 tests |
| `tests/regression/test_capture_audit_isolation.py` | 19 AST/firewall tests |
| `docs/EPIC_2I_CAPTURE_ASSURANCE.md` | This document |

**Modified:** none.

**Confirmed untouched:** `poisson.py`, `filters.py`, `decision.py`, `config.py`,
`evaluation_harness.py`, `main.py`, `prediction_ledger.py`, `settle_predictions.py`,
`domain/settlement.py`, `domain/evaluation.py`, `domain/evaluation_input.py`,
`domain/lifecycle.py`, `domain/reporting.py`, `report_evaluation.py`,
`run_lifecycle.py`, `evaluate_settled.py`, and everything under `run3/`.

---

## 9. Judgement calls

1. **An empty live schedule is "unavailable", not "no fixtures".** `get_fixtures`
   cannot distinguish them. Chosen so an outage can never be reported as a quiet
   day; the cost is one uninformative row on a genuinely empty date.

2. **`PARTIAL` never alerts.** The alternative required recomputing predictions.
   Reporting the ambiguity honestly beats a check that fires daily and gets
   ignored.

3. **`ledger_digest` duplicated, not imported.** Importing from `run_lifecycle`
   would drag settlement and evaluation into this module's import graph.

4. **`expected_from_matches` is duck-typed.** Importing `domain.historical` would
   give the pure core a dataset dependency and, transitively, provider parsing.

5. **A naive `kickoff` is treated as UTC** rather than raising. Every stored
   timestamp is UTC; one hand-built dataset row must not be able to crash an audit
   whose purpose is to report anomalies.

6. **Default window is yesterday, and `--since` excludes today.** A fixture that
   has not kicked off cannot yet be judged uncaptured.

7. **`--fail-on-gap` is opt-in.** Keeps the tool useful as an observer without
   forcing operational behaviour into the prediction pipeline.

Two test-authoring errors were made and corrected while implementing this; in both
cases production behaviour was right and the test was wrong. A `kickoff=None`
default collided with the meaningful "undated" value (fixed with a sentinel), and
a malformed `--date` was asserted to raise `ValueError` when `parser.error`'s
`SystemExit(2)` is both argparse convention and what the sibling CLIs do.

---

## 10. Explicit non-goals

Not attempted here, deliberately:

- **Backfilling.** Impossible by construction, not merely out of scope.
- **Making `main.py` fail loudly.** §2.
- **A scheduler or daemon.** This is a command; when to run it is an operational
  choice.
- **Wiring `is_predictable()` (GG-013).** Would change production output.
- **AUC, the constant-predictor benchmark, and minimum-*n* gating.** These are the
  outstanding **2G-R5** obligation and are **Epic 2J**. They must land before any
  aggregate is shown to a human: `domain/discrimination.py` already provides
  `roc_auc` and is pure, so the work is wiring, not research.
- **Any change to reporting, settlement, evaluation, the model, or Run-3.**

---

## 11. What this does and does not buy

It buys one thing: **a missed capture day is now discoverable after the fact**, and
the tool that reports it cannot have invented the evidence it checked.

It does not buy correctness of the predictions, and it cannot recover a single
record that was never written. The first real question it should be pointed at is
whether capture works at all — run the pipeline for a matchday, then:

```bash
python verify_capture.py --date <that date>
```

Before 2I that question had no answer. `EXIT_OK` with an empty ledger meant
nothing at all.
