# EPIC 2G — Prediction Ledger

**Status:** Implemented (Phase 1 of the audit's roadmap)
**Scope:** `main.py` entry point only
**Preceded by:** `docs/EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md`

---

## 1. The problem this solves

The audit established one finding above all others:

> **No prediction the system has ever made still exists.**

`main.py` wrote `output_{date}.csv` / `output_{date}.json` and exited. Both are
opened with mode `"w"` (`output.py:142`, `output.py:171`), keyed on the fixture
date, so the second run of any given date destroyed the first. Nothing recorded
the thresholds in force, the model that ran, or the price a recommendation was
made against.

The consequence was not that evaluation was hard. It was that evaluation was
**impossible**: after Epic 2B built a scoring harness, and 2C/2D/2E measured the
model offline, the live system still could not answer *"was yesterday's
recommendation correct?"* — because yesterday's recommendation no longer existed.

Epic 2G makes every prediction durable and self-explaining. It does not grade
anything yet.

---

## 2. What was built

| File | Role |
|---|---|
| `domain/prediction_log.py` | **Contract.** `LedgerRecord`, `PredictionProvenance`, `OddsSnapshot`, `LivePredictionStatus`, `config_fingerprint()`. Pure data — no IO, no clock, no network. |
| `prediction_ledger.py` | **Writer.** Append-only JSONL → `data/predictions/YYYY-MM.jsonl`. `record_predictions()` is the single entry point. |
| `main.py` | **+38 lines, 0 deletions**, entirely inside `main()`. |
| `.gitignore` | `+data/` — operational evidence, not source. |
| `tests/unit/test_prediction_log.py` | 54 tests: contract invariants. |
| `tests/unit/test_prediction_ledger.py` | 35 tests: append-only, partial failure, layout. |
| `tests/regression/test_ledger_isolation.py` | 24 tests: firewall + capture-cannot-alter-a-run. |

**Result:** 1809 passed, 3 skipped (the same 3 pre-existing skips). Production
diff confined to `main.py` and `.gitignore`.

Untouched: `poisson.py`, `filters.py`, `decision.py`, `config.py`, `output.py`,
`espn.py`, `odds_api.py`, `shared/`, `domain/evaluation.py`, `run3/`, and —
critically — **`process_fixture`**.

---

## 3. Four decisions that shaped the design

### 3.1 A separate record, not an extended `PredictionRecord`

The audit (§5.3) proposed extending `domain/evaluation.PredictionRecord`. **That
was wrong**, and inspecting the guard rather than trusting the plan is what
caught it.

`tests/regression/test_evaluation_leakage.py:177` bans the name components
`{odds, price, edge, stake, bookmaker, roi, profit}` from that record's
serialised form, and `:32-40` bans the module from importing `odds_api`,
`shared.odds`, `decision`, `filters`. Requirement 8 of this Epic — *store the
odds snapshot* — is therefore **incompatible** with reusing that class.

The firewall exists because a scoring harness with access to prices will
eventually be asked for a profitability number. That reasoning is sound, so the
firewall stayed and the ledger became a **parallel record**. What was reused is
the *discipline*, not the class: frozen dataclasses, invariants enforced in
`__post_init__`, timezone-aware datetimes only, fixed key order, and
probability-XOR-refusal.

A new guard closes the direction the original could not see:
`test_the_evaluation_layer_cannot_import_the_ledger` — because importing the
ledger would hand the evaluator a price-bearing object without ever naming a
forbidden module.

### 3.2 Capture lives in `main()`, not `process_fixture`

Every probability, filter verdict and recommendation is decided inside
`process_fixture`. That function was **not edited**. Capture is one guarded call
at the end of `main()`, reading an already-final list.

This makes *"capture cannot change a prediction"* a property of the call graph
rather than a promise about the implementation, and it is enforced by
`test_process_fixture_does_not_mention_the_ledger`.

It also preserves the existing safety net: `tests/integration/test_entry_point_consistency.py:171`
drives `main.process_fixture(...)` directly. All ~19 of those tests still prove
identical verdicts, reasons and probabilities, and never touch the new code.

**A finding emerged from this ordering.** While trying to make a mutation check
fail, I found that a *deliberately malicious* recorder still cannot corrupt
`output_*.json` or `output_*.csv` — because both files are written and closed
before capture runs. The position of the call is a second, independent
protection on top of the read-only implementation. This is recorded as
`test_ordering_alone_protects_the_published_files`, and the guard that does bite
(in-memory mutation) is separately mutation-checked.

### 3.3 Three filter states, never a boolean

`filter_outcome` is stored as `PASSED` / `FAILED` / `UNEVALUATED`, and a boolean
is **rejected** by the contract.

GG-002 survived as long as it did because "passed the filters" and "the filters
never ran" were the same `False`. Storing a bool would rebuild that ambiguity in
the archive, where it would be permanent and unrecoverable.

The string set is pinned against the real enum
(`test_the_string_set_matches_the_real_enum`), so a fourth state cannot be added
to `FilterOutcome` without this failing.

### 3.4 Configuration is measured, not declared

Four version strings are recorded (`model`, `filter`, `decision`,
`data_source`), because four independent things can change what the system
publishes. But version strings are promises a human must remember to keep.

`config_fingerprint` is a **measurement**: a sha256 over the live values of
`EDGE_THRESHOLD`, `MIN_ODDS`, `MIN_AVG_GOALS`, `MAX_CLEAN_SHEET_PCT` and the
sorted `ALLOWED_LEAGUES` keys, read from `config.py` on every call and never
written. Edit a threshold and forget the version bump, and the ledger still
shows a discontinuity. Same reasoning as `dataset_checksum` in the evaluation
harness.

---

## 4. Honesty about what is recorded

Two fields deliberately state their own limitations rather than implying
completeness:

**`odds.provenance = "PARTIAL_NO_BOOKMAKER"`.** `odds_api.get_btts_odds` returns
a bare `Optional[float]`; which bookmaker quoted the price, and when, are
discarded before any caller sees them. Changing that signature was out of scope,
so the record names the gap instead of leaving two nulls that look like an
omission. When a future Epic captures the book and timestamp, `COMPLETE` already
exists.

**`code_revision`** is best-effort: the short commit plus a `-dirty` marker, or
`None`. Verified as `e43b86e-dirty`. An unknown revision recorded as unknown is
useful; a fabricated one is not.

Also deliberate: **there is no `outcome` field.** A prediction is immutable, and
grading is a later fact about the same fixture. Writing a result back into the
record would erase what was actually claimed at prediction time — the one thing
the ledger exists to preserve. `test_the_record_carries_no_outcome_field` pins
this.

`GG-007` is not inherited: `edge` and `implied_probability` use `is not None`,
so a genuine `0.0` edge survives instead of being serialised as null and made
indistinguishable from "no odds".

Refusals are **recorded, not skipped**, with a named `LivePredictionStatus`
(`NO_TEAM_STATS`, `NO_POINT_IN_TIME_INPUTS`, `MODEL_RETURNED_NONE`). "The system
was asked and declined" is the only evidence that distinguishes a quiet matchday
from a broken provider feed.

---

## 5. Two bugs my own tests caught

Both were in the tests, and both would have produced green CI over a real
weakness — the 2F-P1-1 failure mode.

**A prose scan masquerading as a structural check.** The append-only guard began
as `assert '"w"' not in source`, which failed on my own docstring describing the
rule, while a real truncating write hidden behind a variable would have passed.
Replaced with an AST walk over every `open()` call, asserting each mode literal
begins with `a` or `r` and contains no `+`.

**A comparison that could never have failed.** The "capture changes nothing"
guard originally compared raw JSON bytes. `output.py:164` stamps every file with
`datetime.now().isoformat()`, so two runs are *never* byte-identical — the
assertion would have "detected a difference" always, and its mutation check
would have passed for a reason unrelated to the ledger. Now the run timestamp is
excluded, the CSV (which carries no timestamp) is compared byte-for-byte, and
both comparisons are mutation-checked against a genuinely different run.

---

## 6. Verified behaviour

Two runs of the same date, end-to-end through the real writer:

```
ledger: 1 prediction(s) -> data/predictions/2026-08.jsonl
ledger: 1 prediction(s) -> data/predictions/2026-08.jsonl
$ wc -l < data/predictions/2026-08.jsonl
       2
```

`output_2026-08-17.json` was overwritten. **Both predictions survived**, with
distinct `prediction_id`s and distinct `run_id`s. That difference is the Epic.

Also verified: a raising writer still leaves a complete, correct run (a full disk
degrades to "predictions not recorded", never to "the matchday was lost"); the
failure is printed rather than silent, because a silent gap would later read as a
day on which nothing was predicted; and an `ImportError` on the ledger module is
survivable, since the import itself sits inside the guarded block.

---

## 7. What was NOT built, and why

| Not built | Reason |
|---|---|
| Result settlement / grading | Needs the ledger to exist first. Phase 2. |
| Brier / ROI / calibration reporting | Needs graded outcomes. Phase 3. |
| A database | ~10 fixtures/day across 5 leagues. A schema migration is a cost to pay when volume demands it. |
| `analyze_all.py` capture | Second entry point, two rows per fixture, different schema. Follow-up, by explicit decision. |
| GG-013 / GG-014 fixes | Both change *which fixtures are processed* — i.e. production output. Out of scope for an observability layer. |
| Bookmaker + observation time | Requires changing `odds_api.get_btts_odds`. `OddsProvenance.COMPLETE` is reserved for it. |

**Build next:** settlement (`espn` already exposes completed results, and
`fixture_id` is the same identity space as `domain.historical.HistoricalMatch.event_id`,
so grading is a lookup and never a team-name match — GG-008).

**Do not build until settlement exists:** anything that reports accuracy, Brier
or ROI. The ledger currently proves only that predictions are durable. A metric
computed before grading is trustworthy would be a number without evidence behind
it, which is worse than no number at all.
