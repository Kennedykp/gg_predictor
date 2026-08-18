# Epic 2H-5 — Evaluation Reporting

**Status:** complete
**Scope:** deterministic, auditable breakdowns of already-settled predictions —
without recomputing a probability, contacting a provider, or touching the ledger.

---

## 1. The gap this Epic closes

The audit that opened this Epic found one thing worth building and a lot worth
leaving alone.

`evaluate_settled.py` (2H-3) grades the ledger and reports **one number per model
version**. After a bad week that answers "how good was the model" but not the
question an operator actually asks next: **where** was it bad?

The data needed to answer that has existed since 2H-3 —
`EvaluationInput.join_key` is `(competition, season, fixture_id)` — but nothing on
the ledger-graded path exposed it. `summarise_by_model` groups only by
`(model_id, model_version)`:

```python
key = (item.provenance.model_id, item.provenance.model_version)   # evaluate_settled.py
```

A per-competition breakdown *did* exist, on `run_evaluation.py --breakdown
competition`. That sits on the **research harness**, which recomputes
probabilities through today's model. So the only way to get a per-competition
Brier was the one route guaranteed **not** to describe what was actually
published.

This Epic adds the missing grouping on the honest path. It defines no new metric.

### Why it matters, from the verified run

```
overall                scored 3/4    brier 0.5433
  eng.1                scored 1/1    brier 0.0100
  esp.1                scored 2/2    brier 0.8100
  ger.1                scored 0/1    not yet measurable (0 unresolved, 1 awaiting)
```

`0.5433` describes neither competition. It is the average of an excellent one and
a badly mispriced one, and on the 2H-3 report it is all you would have seen.

---

## 2. What was built

```
domain/reporting.py     pure   grouping, rollup, deterministic ordering
report_evaluation.py    I/O    load -> regroup -> print -> write artifact
```

No existing file was modified. `git status` shows **6 added, 0 modified**.

### 2.1 `domain/reporting.py`

Five dimensions, all derived from fields the ledger already stores:
`overall`, `model`, `competition`, `season`, `competition_season`.

The metrics come from the frozen `summarise()`. This module decides only *which
records go together* — it cannot compute a probability, and it never re-derives
an outcome.

Three decisions carry the weight:

**`MIXED`.** A competition may span two model versions across a season.
`summarise` demands a single model id and version, so naming either one would
attribute that competition's Brier to a model that produced only part of it — a
wrong answer that looks precise. `MIXED` is deliberately unusable: it forces the
reader to cut by model before drawing a conclusion about a model.

**`UNKNOWN_SEASON`.** `JoinKey`'s season is `Optional[int]`, and `None` cannot be
compared with `int` in a sort key. Substituting `0` would file the record as a
real season and sort it before every genuine one. It is labelled and sorted last
instead.

**Unresolved records stay in the group.** They are passed to `summarise` exactly
as `summarise_by_model` does, so they count toward `targets` and `coverage` while
`is_scored` keeps them out of Brier. Filtering them here would report coverage as
a fraction of whatever happened to survive.

### 2.2 `report_evaluation.py`

Read-only by construction: it settles nothing and fetches nothing, so it is safe
to run at any time — including while a settlement job is running. `evaluate()` is
reused wholesale rather than reimplemented, because it owns the join and the
conflict detection, and a second loader would be a second place for the join key
to drift.

---

## 3. Report schema

```
schema_version, reporting_schema_version,
evaluation_schema_version, evaluation_input_schema_version
generated_at                    <- the ONLY time-dependent field
probability_source: "ledger"    replay_used: false
inputs   { ledger_dir, settlement_dir, month, bin_count }
join     { key, predictions, joined, evaluated, excluded_from_evaluation,
           settled, unresolved, missing_settlement, join_rate,
           settlement_coverage, unjoinable, settlement_conflicts }
dimensions [...]
breakdowns { <dimension>: [ { dimension, key, label, reportable,
                              counts  { total, settled, unresolved, missing,
                                        accounted_for },
                              metrics { ... _summary_dict, verbatim ... } } ] }
```

`counts` sits **beside** `metrics`, not inside it. The counts describe the
lifecycle — how much evidence exists and what is still missing — while the
metrics describe probability quality. Merging them invites reading `missing` as a
property of the model when it is an operational one.

All four schema versions are stamped because a report is evidence read months
later, and "which contract produced this" is not recoverable afterwards.

`metrics` is `_summary_dict` verbatim, imported from `evaluate_settled.py`
despite the underscore. It is the single place that decides which metric keys
reach an artifact, constrained by `tests/regression/test_evaluation_leakage.py`.
A local copy would be a second place for that list to drift, and the drift would
be a leak.

---

## 4. Determinism

Given the same ledger and settlement data, two runs produce byte-identical
reports apart from `generated_at`. A test asserts exactly that, by building two
reports at different instants and diffing them with the field removed.

- Group order is `sorted`, never dict insertion order.
- Grouping is pure — no clock, no filesystem, no network (AST-enforced).
- The clock is read **once**, in `main`. Two reads could stamp the artifact with
  one instant and name the file with another.
- Seasons sort numerically (`1999, 2009, 2010`), with `UNKNOWN` last.

Verified live: identical inputs, Brier identical to 16 significant figures.

---

## 5. Error semantics

| Situation | Behaviour | Exit |
|---|---|---|
| contradictory settlements for one fixture | reported, **no artifact written** | `1` |
| unrecognised `SettlementState` | `ValueError` | — |
| unsupported dimension | `ValueError` | — |
| unjoinable / malformed record | counted in `unjoinable`, never grouped | `0` |
| nothing evaluated | normal; artifact still written | `0` |
| nothing evaluated, `--fail-on-empty` | reported | `2` |

Nothing is silently repaired. A conflicting run writes **no** artifact, so a
misleading file is never left behind for someone to find later and trust.

`--fail-on-empty` is opt-in: silence before the first settled matchday is normal,
and failing by default would train the operator to ignore exit codes.

A group with nothing settled is reported as `reportable: false` with its counts
intact, rather than omitted — "12 predictions, nothing settled yet" is exactly
the operational signal worth surfacing, and hiding the row would make an
unsettled competition look like one that does not exist.

---

## 6. Relationship to the lifecycle runner

| Tool | Fetches? | Writes settlements? | Question |
|---|---|---|---|
| `run_lifecycle.py` (2H-4) | yes | yes | is the pipeline healthy *right now*? |
| `report_evaluation.py` (2H-5) | **no** | **no** | where is the model good or bad? |

Settlement stays the only layer allowed to contact a provider. A report that
fetched would return different numbers on a rerun with no change to any input,
which is precisely what the determinism contract forbids.

Typical use: `run_lifecycle.py` on a schedule; `report_evaluation.py` whenever a
number looks wrong.

---

## 7. What reporting may NOT do

Enforced structurally in `tests/regression/test_reporting_isolation.py`, on the
parsed AST so an alias or a function-local import cannot evade it:

- **no probability model** — `poisson`, `decision`, `filters`, `goal_models`,
  `cold_start`, …
- **no replay harness** — `evaluation_harness`, `run_evaluation`, `analyze_all`
- **no live provider** — `espn`, `api_football`, `sofascore`, `sportmonks`
- **no prediction writer** — `main`, `output`
- **no market data** — `odds_api`, `shared.odds` (LEAK-001)
- **no `config`** — a threshold here would invite "accuracy above the
  recommendation line", which grades the *filter*, not the probability, and
  quietly reintroduces the decision layer into evaluation

The replay ban is the sharp one. A breakdown invites the follow-up "esp.1 looks
bad — what would the *current* model say?" That is a legitimate research question
and an illegitimate thing for this module to answer: the moment reporting can
call the model, one artifact can mix published and recomputed probabilities and
no reader can tell the rows apart.

The guard was proven able to fail — a temporary `import poisson` in
`domain/reporting.py` failed `test_no_model_import`, and was reverted. A firewall
that cannot fail is not a firewall.

---

## 8. Usage

```bash
# default: overall, model, competition, season
python report_evaluation.py

# one axis
python report_evaluation.py --dimension competition

# finest cut, on request
python report_evaluation.py --dimension competition_season

# a single month
python report_evaluation.py --month 2026-08

# strict, for a scheduled job that expects data
python report_evaluation.py --fail-on-empty

# print without writing
python report_evaluation.py --dry-run
```

Flags: `--month`, `--ledger-dir`, `--settlement-dir`, `--out`, `--bins`,
`--dimension` (repeatable), `--fail-on-empty`, `--dry-run`.

`competition_season` is not a default: on a mature ledger it is the widest
breakdown by row count and mostly noise until a season has accumulated enough
settled fixtures to say anything.

Artifacts are `report_<timestamp>.json`, never overwritten — the `report_` prefix
keeps them distinct from `evaluate_settled.py` and 2H-4's `lifecycle_` files, so
three tools can share one directory without clobbering each other's history.

---

## 9. Verification

```
2214 passed, 3 skipped        # 3 skips pre-existing (D1, D3), unrelated
ruff check .   All checks passed!
mypy           Success: no issues found in 54 source files
```

New tests, 103 in total:
`tests/unit/test_reporting.py` (50),
`tests/unit/test_report_evaluation.py` (37),
`tests/regression/test_reporting_isolation.py` (16).

Files **added** (6) — nothing modified:

```
domain/reporting.py                             report_evaluation.py
tests/unit/test_reporting.py                    tests/unit/test_report_evaluation.py
tests/regression/test_reporting_isolation.py    docs/EPIC_2H5_EVALUATION_REPORTING.md
```

Frozen and confirmed untouched: `poisson.py`, `filters.py`, `decision.py`,
`config.py`, `domain/evaluation.py`, `domain/evaluation_input.py`,
`prediction_ledger.py`, `settle_predictions.py`, `domain/settlement.py`,
`domain/lifecycle.py`, `run_lifecycle.py`, `evaluate_settled.py`.

### A note on the test fixtures

`settlement()` takes `outcome` **separately** from the goals and defaults it to
`"YES"`. That faithfully mirrors production: `settle_predictions` derives the GG
outcome once, at settlement time, and evaluation reads the **stored** value
rather than re-deriving it from the score — correct for an immutable settlement.
It does mean a fixture built as `home=2, away=0` with the default outcome is
internally inconsistent, which is what three of my first-draft tests tripped
over. The tests derive the outcome from the score so every fixture stays
self-consistent; the production behaviour was left alone.

---

## 10. Non-goals

- **No new metric.** Brier, log loss and calibration come from
  `domain/evaluation.py` unchanged.
- **No model change**, and no probability recomputed — ever.
- **No ledger or settlement write.** Verified by digest on a real run.
- **No fetching.** Settlement owns that.
- **No conflict resolution.** Contradictions are reported and the run fails.
- **No new dimensions beyond stored fields.** A `team` or `evidence` cut would
  require widening the evaluation input contract, which is a different Epic.
- **No UI, no scheduler, no daemon.**
