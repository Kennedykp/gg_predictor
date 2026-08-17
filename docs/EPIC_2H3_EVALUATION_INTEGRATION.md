# Epic 2H-3 — Settlement Evaluation Integration

**Status:** complete
**Depends on:** Epic 2G (prediction ledger), Epic 2H-2 (settlement engine)
**Modifies:** nothing. Two new modules, three new test files, one test helper.

---

## 1. What this connects

```
  main.py / analyze_all.py
        |  writes (append-only)
        v
  data/predictions/YYYY-MM.jsonl        <-- Epic 2G, immutable
        |
        |  read-only
        v
  settle_predictions.py                  <-- Epic 2H-2
        |  writes (append-only)
        v
  data/settlements/YYYY-MM.jsonl         <-- Epic 2H-2, immutable
        |
        |  read-only              read-only
        v                              v
  domain/evaluation_input.py  <-- join on (competition, season, fixture_id)
        |
        |  EvaluationInput[]  (carries the STORED probability)
        v
  domain/evaluation.py         <-- FROZEN. Brier, log loss, calibration, coverage
        |
        v
  data/evaluation/evaluation_<ts>.json
```

Two modules were added:

| File | Role | IO | Clock | Network |
|---|---|---|---|---|
| `domain/evaluation_input.py` | the join and the adapter | none | none | none |
| `evaluate_settled.py` | the job: read, grade, report | reads two logs, writes one report | injected | none |

The split is the same one Epic 2H-2 used, for the same reason: a pure contract
can be tested exhaustively against every edge case without a filesystem, and the
IO layer stays thin enough to read in one sitting.

---

## 2. Why `replay()` is forbidden

This is the single most important rule of the Epic, so it is worth being exact
about the failure it prevents.

`evaluation_harness.replay()` takes a dataset and a **model adapter**, and calls
the model to produce a probability. It is the right tool for its own job —
offline research, where the question is *"how would this model have done?"*

Used here it would answer a different question while looking identical:

| Question | Correct source | What replay gives you |
|---|---|---|
| How good was what we **published**? | the ledger's stored float | a **new** number |
| How would the model do **today**? | replay | replay |

Three specific ways the replayed number diverges from the published one:

1. **Data has changed.** ESPN backfills. A team's history at replay time
   includes matches that had not been played when the prediction was made — the
   very leakage `tests/regression/test_point_in_time_inputs.py` exists to
   prevent. The replayed probability is a hindsight number.
2. **Code has changed.** The ledger records `model_version`, `filter_version`,
   `decision_version` and a `config_fingerprint` precisely because all four can
   move. Replay uses today's code for yesterday's prediction.
3. **The divergence is invisible.** Both are floats in `[0, 1]`. Once written to
   a report, nothing distinguishes them.

Point 3 is what makes this a firewall rather than a preference. A wrong answer
that announces itself is a bug; a wrong answer that looks exactly like the right
one is a **measurement you cannot trust again**. The enforcement is therefore
structural, not documentary:

- `test_the_harness_is_never_imported` — `evaluation_harness` is unreachable from
  either new module.
- `test_replay_is_never_called` — not called under any alias.
- `test_no_model_module_is_imported` — no `poisson`, `filters`, `decision`.
- `test_the_adapter_imports_no_math_module` — the adapter has nothing to compute
  *with*.
- `test_the_adapter_computes_no_probability` — no `exp`, `factorial`, `log`, `**`.
- `test_the_probability_is_passed_through_untouched` — pins the exact
  read-and-pass source lines and bans `round(`, rescaling, `min(`/`max(`.

And the artifact says so on its face:

```json
"probability_source": "ledger",
"replay_used": false
```

A reader in six months can tell a ledger-graded report from a replayed one
without reading any code.

---

## 3. The join contract

```
key = (competition, season, fixture_id)
```

Exact equality on all three parts. Never a team name, never a date, never a
fuzzy or partial comparison.

**Why three parts.** An ESPN event id is not documented as unique across
competitions and demonstrably repeats across seasons. `domain/historical.py`
keys its own records the same way. A bare-id join would attach a La Liga result
to a Premier League prediction — and score it with total confidence.

**Why never a name.** GG-008: the odds clients match teams by substring and pair
"Athletic" with "Athletic Club". A join that could compare names would
manufacture confident, wrong evidence. `EvaluationInput` has no field a name
comparison could use, and `test_the_adapter_never_compares_team_names` asserts
the name fields are never read at all.

**Why never a date.** A postponed fixture is settled weeks after its original
kickoff. Any date component would break exactly the cases that matter most.

### The `season` vs `matched_season` subtlety

The settlement side joins on `season`, **not** `matched_season`.

Epic 2H-2 finding 2H-F3: ESPN files fixtures around the 1 July rollover under
either adjacent season, so settlement retries the neighbouring season and
records where the fixture was *actually found* in `matched_season`. That drift
has already been absorbed. Joining on `matched_season` here would re-introduce
the mismatch settlement just resolved. `matched_season` is carried through for
diagnostics and plays no part in the key.

### Normalisation

`fixture_id` is coerced to `str` on both sides. The ledger stores a string; a
provider may hand back an int; `"740123" != 740123` would silently turn a
correct join into a miss.

---

## 4. Settlement states

Three states, deliberately not two:

| State | Meaning | Scored? | In coverage? |
|---|---|---|---|
| `SETTLED` | a real result exists | **yes** | yes |
| `UNRESOLVED` | settlement ran; postponed/cancelled/abandoned/no result | no | **yes** |
| `MISSING` | no settlement record exists at all | no | **yes** |

**`MISSING` is not `UNRESOLVED`.** Unresolved is a fact about football or a
provider. Missing is a fact about *our pipeline* — most often that the
settlement job has not run for that day yet. Merging them would make an
operational gap look like a wave of postponements, and the two demand completely
different responses.

### Unresolved keeps its probability

A `SCORED` prediction whose fixture is unresolved retains its stored probability
and takes an `UNKNOWN` outcome. It is not scoreable, but it *is* coverage.
Dropping the probability would misreport a postponement as a model refusal —
conflating "the model declined to speak" with "the result is unknown", which is
exactly the distinction `LivePredictionStatus` and `UnevaluableReason` were kept
separate to preserve.

### A real 0-0 is `NO`, not `UNKNOWN`

Inherited from `domain/evaluation.btts_outcome` and re-asserted here. A goalless
draw is a genuine observation; "no score recorded" is an absence. Sweeping the
first in with the second would silently discard roughly a tenth of all settled
fixtures — and would systematically flatter models that predict low.

The outcome is **read** from the settlement record, never re-derived from the
score. `domain/settlement.py` already refuses to record an outcome that
contradicts its own score; a second derivation here would be a second place for
that rule to drift.

---

## 5. Invariants enforced

| # | Invariant | Enforced by |
|---|---|---|
| 1 | The graded probability is the stored probability, bit-for-bit | `test_the_ledger_probability_reaches_the_metrics_unchanged`, `test_the_probability_is_passed_through_untouched` |
| 2 | No model is ever called during evaluation | `test_no_model_module_is_imported`, `test_the_adapter_computes_no_probability` |
| 3 | `replay()` is unreachable | `test_the_harness_is_never_imported`, `test_replay_is_never_called` |
| 4 | The join is exact on the documented triple | `test_the_join_key_is_the_documented_triple`, `test_the_same_fixture_id_in_another_competition_does_not_join` |
| 5 | Unresolved is excluded from Brier and log loss | `test_an_unresolved_fixture_is_excluded_from_brier_and_log_loss` |
| 6 | Unresolved is counted in coverage | `test_an_unresolved_fixture_is_counted_in_coverage` |
| 7 | The ledger is never modified | `test_the_ledger_bytes_are_unchanged`, `test_no_file_is_added_to_either_directory` |
| 8 | Settlement records are never modified | `test_the_settlement_bytes_are_unchanged` |
| 9 | No import cycle exists | `test_settlement_does_not_import_this_epics_modules`, `test_the_frozen_evaluation_module_does_not_import_the_new_one`, `test_the_import_graph_actually_loads` |
| 10 | No price, edge or stake reaches a metric or artifact | `test_no_market_module_is_imported`, `test_it_carries_no_price_or_edge_anywhere`, `test_no_price_value_reaches_the_artifact` |
| 11 | Provenance comes from storage, never today's config | `test_the_stored_provenance_is_used_verbatim`, `test_the_config_module_is_never_read` |
| 12 | Every prediction is accounted for | `test_every_prediction_is_accounted_for` |
| 13 | Two runs over unchanged data agree | `test_the_join_is_deterministic_across_input_orderings` |
| 14 | Absence is never a score (`None`, never `0.0`) | `test_rates_are_none_for_an_empty_ledger_not_one` |

### On the dependency direction (invariant 9)

`domain/settlement.py` **does** import `domain.evaluation`, deliberately, to
reuse `btts_outcome` so the YES/NO rule has exactly one definition. That is not
a cycle: `domain/evaluation.py` imports nothing from this repository, so it is
the leaf of the graph. What would be a cycle — and is forbidden — is settlement
or the ledger reaching *forward* to the join or the runner, which would let the
way a result is graded influence the result itself.

---

## 6. Example flow

```bash
# 1. Predictions are made and recorded (Epic 2G, unchanged)
python main.py

# 2. After the matches finish, settle them (Epic 2H-2, unchanged)
python settle_predictions.py

# 3. Grade the stored predictions against the stored results (this Epic)
python evaluate_settled.py --month 2026-08
```

Output:

```
join: 47/50 joined, 41 settled, 4 unresolved, 2 awaiting settlement, 3 unjoinable

POISSON_V1 1.0.0
  scored      41/47  (coverage 87.2%)
  brier       0.2413
  log loss    0.6702
  predicted   0.548 vs observed 0.561
    NO_RESULT              4
    INSUFFICIENT_HISTORY   2
```

Read it as four separate facts, none of which can be recovered from the others:

- **41 settled** carry a real result and are graded.
- **4 unresolved** were postponed, cancelled or abandoned — counted, not scored.
- **2 awaiting settlement** means run `settle_predictions.py` again later.
- **3 unjoinable** are malformed ledger rows and a **data-quality alarm**, not a
  model limitation.

`--month` filters the **ledger** only; settlements are always read in full,
because an August prediction is routinely settled in September and filtering
both sides identically would report a real result as still pending.

### Exit codes

`0` on success. `1` when two settlement records state **different final scores**
for one fixture. That is not a correction to apply quietly — two sources
described the same fixture differently, every metric in the report would inherit
whichever was picked, so the run fails and the conflict is printed. An
`UNRESOLVED → SETTLED` progression is *not* a conflict; it is the normal life of
a fixture that has since been played.

---

## 7. Files changed

**Added (7):**

```
domain/evaluation_input.py                                  the pure join + adapter
evaluate_settled.py                                         the job + CLI
tests/helpers/__init__.py                                   makes helpers a package
tests/helpers/settlement_fixtures.py                        shared builders
tests/unit/test_evaluation_input.py                         54 tests
tests/unit/test_evaluate_settled.py                         22 tests
tests/regression/test_evaluation_integration_isolation.py   28 tests
```

### Why the record builders are a module and not a fixture file

Both test files need the same `prediction()` / `settlement()` builders. The
tempting shortcut - importing them from the other test file - makes mypy see one
source file under two module names (`test_evaluation_input` AND
`tests.unit.test_evaluation_input`) and fail the whole run before checking
anything else.

They are imported as `from helpers.settlement_fixtures import ...`, NOT
`tests.helpers...`. `tests/` is on `sys.path` (see `pythonpath` in
`pyproject.toml`) and has no `__init__.py`, so `helpers` is the one name both
pytest and mypy resolve. This follows the existing precedent in the suite, where
three files already do `from conftest import espn_event, utc`.

**`tests/__init__.py` must stay absent.** Adding one makes pytest resolve the
root conftest as `tests.conftest`, and those three `from conftest import ...`
files stop importing - 3 collection errors, verified.

**Modified: none.** `poisson.py`, `filters.py`, `decision.py`, `config.py`,
`domain/evaluation.py`, `evaluation_harness.py`, `prediction_ledger.py`,
`domain/prediction_log.py`, `domain/settlement.py` and `settle_predictions.py`
are byte-for-byte unchanged.

---

## 8. Known limitations

**A lossy status mapping, kept recoverable.** `domain/evaluation.py` is frozen,
so its five `UnevaluableReason` members cannot be extended. Two distinct ledger
statuses — `NO_TEAM_STATS` ("the provider gave nothing") and
`NO_POINT_IN_TIME_INPUTS` ("the history was too thin") — both map to
`INSUFFICIENT_HISTORY`. The exact original status is preserved verbatim in
`PredictionRecord.detail` (`ledger_status=NO_TEAM_STATS`), so the collapse is a
presentation detail and never a loss of evidence. Widening the enum is a
candidate for a future Epic.

**`season` defaults to `0` when the ledger has none.** `PredictionRecord.season`
is typed `int`; `LedgerRecord.season` is `Optional[int]` because
`espn.resolve_season` is best-effort. Such a record is graded with `season=0`
rather than discarded — but note its **join key keeps `season=None`**, so it can
only ever match a settlement that also has no season. It cannot be
mis-joined to a real season.

**`history_matches` is always `0`.** The live ledger does not record how many
prior matches fed a prediction; it records the three sample counts, which are
carried faithfully. Not inferred, because a guessed provenance number is worse
than an absent one.

---

## 9. Non-goals

Explicitly **not** done here:

- **No profit, ROI, yield or staking metric.** LEAK-001 and the
  `test_evaluation_leakage.py` firewall. Probability quality is a football
  question; betting value is a different one, and mixing them makes a
  calibration number move when a threshold changes.
- **No model change of any kind.** No retuning, no recalibration, no
  threshold adjustment informed by these metrics.
- **No modification to any stored record.** Ledger and settlement records are
  immutable; this layer only reads them.
- **No historical backfill.** Only ledger-era predictions can be evaluated —
  a pre-ledger prediction has no stored probability, and inventing one by
  running the model over old data is precisely the replay this Epic forbids.
- **No automatic scheduling.** `evaluate_settled.py` is run deliberately.
- **No `evaluation_harness.py` change.** `replay()` remains correct and useful
  for offline research; it is simply the wrong tool for grading published
  predictions.
