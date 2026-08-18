# Epic 2H-4 — Evaluation Operations

**Status:** complete
**Scope:** one operational entry point that settles, then evaluates, then reports
pipeline health — without ever touching the ledger or the model.

---

## 1. The problem this Epic closes

Epics 2H-1 → 2H-3 built the pieces:

| Epic | Artifact | Question it answers |
|---|---|---|
| 2G | `prediction_ledger.py` | what did we believe, and when? |
| 2H-1/2 | `settle_predictions.py` | what actually happened? |
| 2H-3 | `evaluate_settled.py` | how good was the belief? |

Each works. Operating them did not, for two reasons.

**Order.** Settlement and evaluation were separate commands, and evaluation read
only what settlement had already written. Run them in the wrong order and every
fixture that finished that afternoon was reported as pending — not an error, just
a quietly stale number. The operator had to know the order and never get it wrong.

**Silence.** A prediction that was never settled and a prediction that was settled
as postponed both showed up as "not scored". The first is *our* bug; the second is
football. Collapsing them meant a broken settlement job looked exactly like a wet
weekend, and coverage could decay for weeks without anyone noticing.

This Epic adds the layer that sequences the two and separates those states.

---

## 2. What was built

```
domain/lifecycle.py     pure    reconcile predictions against settlements
run_lifecycle.py        I/O     settle -> evaluate -> report, one command
```

Nothing else changed. `poisson.py`, `filters.py`, `decision.py`, `config.py`,
`domain/evaluation.py`, the ledger and the settlement writer are all untouched.

### 2.1 `domain/lifecycle.py` — the pure core

Six stages, chosen so that no two distinguishable situations share a name:

| Stage | Meaning | Whose problem |
|---|---|---|
| `AWAITING_KICKOFF` | kickoff is in the future | nobody's — normal |
| `IN_PLAY` | kicked off, inside the grace window | nobody's — normal |
| `AWAITING_SETTLEMENT` | due, and no settlement exists | **ours** — operational gap |
| `UNRESOLVED` | settled as postponed / cancelled / abandoned | football |
| `SETTLED` | a real result exists | done |
| `UNDATED` | no usable kickoff, cannot be judged | data quality |

The distinction that matters is `AWAITING_SETTLEMENT` vs `UNRESOLVED`. The first
means the pipeline failed to answer a question it could have answered. The second
means the question has no answer yet. `settlement_backlog` counts only the first,
which is why it is the number worth alerting on.

**Grace window (default 3h).** A prediction is not "missing a result" the moment
the whistle blows — the match takes ~2h and providers lag. Without the window,
every run during a matchday would report a backlog and the alert would be trained
out of the operator within a week. `IN_PLAY` exists to hold that period without
calling it a fault.

The module is pure: `now` is an injected keyword argument with no default, so the
same inputs always classify the same way. Two regression tests enforce this — one
rejects any clock read in the module, one rejects a default for `now` (an
import-time default would freeze the clock for the life of the process, a bug that
only appears in long-running jobs).

### 2.2 `run_lifecycle.py` — the orchestrator

```
ledger digest  ->  settle  ->  reload  ->  reconcile  ->  evaluate  ->  report  ->  ledger digest
```

The reload between settle and reconcile is the fix for the ordering problem: a
fixture settled at the start of the run is graded in the same run.

The clock is read **exactly once**, at the top of `main`, and threaded through.
A regression test asserts exactly one clock read in the file. Two reads would let
one fixture be `pending` in the lifecycle block and `settled` in the metrics block
of the same artifact — internally inconsistent, and genuinely nasty to debug.

---

## 3. The four proofs

The Epic's non-negotiables, each with tests and each verified on real files.

### 3.1 The ledger is byte-for-byte unchanged

Digested before and after every run; both digests go into the artifact as
`ledger_digest_before` / `ledger_digest_after` / `ledger_unchanged`.

Two tests prove the digest can *fail* (an edited line, a deleted month) — a guard
that cannot fail is not a guard. Structurally, `run_lifecycle.py` cannot import
`main` or `output`, so the prediction-writing path is unreachable.

### 3.2 Stored probabilities are graded verbatim

The model is never called. `probability_source: "ledger"` and
`replay_used: false` in the artifact are facts about the import graph, not claims
in a docstring: `test_lifecycle_isolation.py` rejects any import of `poisson`,
`decision`, `filters`, `domain.goal_models`, `domain.cold_start`, and of the
replay harnesses (`evaluation_harness`, `run_evaluation`, `analyze_all`).

`evaluation_harness` legitimately *recomputes* probabilities for research. That is
the right tool for a modelling question and the wrong one for grading a ledger —
mixing them would let a report labelled `probability_source: ledger` contain
replayed numbers. Hence the hard separation.

A test grades a deliberately awkward float (`0.6172839506172839`) and asserts
`brier == (1 - p)**2` exactly, so any rounding or re-derivation fails.

### 3.3 Reruns are idempotent

Verified live: second run appended **0** settlement lines, and Brier was identical
to 16 significant figures.

Idempotence is inherited from `unsettled()` in 2H-1, not re-implemented — the
tests prove the orchestrator does not defeat it. Terminally settled predictions
are not re-fetched, which also avoids inviting a provider to change its mind about
a settled result.

`UNRESOLVED` is deliberately **not** terminal: a postponed match is usually
replayed. When it is, the correction is a **new line**, never an edit — that is how
an append-only log represents a change of belief. Both lines survive on disk, and
an `UNRESOLVED -> SETTLED` progression is explicitly *not* a conflict (treating it
as one would fail a healthy pipeline every matchweek).

### 3.4 Conflicts fail loudly

| Situation | Exit | Why not resolve it |
|---|---|---|
| duplicate `prediction_id`, different probabilities | `1` | picking one silently changes every metric |
| two settlements, contradictory scores | `1` | ditto — and the wrong one is unfalsifiable later |
| ledger digest changed mid-run | `2` | the ledger is the one artifact we cannot reconstruct |
| due predictions unsettled | `3` *(opt-in)* | see below |

Exit `3` is **opt-in** via `--fail-on-backlog`. A backlog mid-matchday is normal;
paging on it by default trains the operator to ignore exit codes. The strict mode
is for a scheduled job that runs well after the last whistle.

---

## 4. Two join keys, deliberately

| Question | Key |
|---|---|
| did this *prediction* get settled? | `prediction_id` |
| what *happened* in this fixture? | `(competition, season, fixture_id)` |

Both are stated in the artifact. The lifecycle is per-prediction because that is
what a settlement answers; evaluation joins on the fixture triple because
`domain/settlement.py` establishes that a bare ESPN event id is not asserted
unique across competitions. Stating both prevents a future reader assuming the
lifecycle was keyed on the triple.

Settlements are read in **full**, ignoring `--month`, while the ledger is filtered.
An August prediction is routinely settled in September; filtering both sides would
report a real result as pending.

---

## 5. Usage

```bash
# nightly: settle from ESPN, evaluate, report
python run_lifecycle.py

# strict, for a job that runs after the last whistle
python run_lifecycle.py --fail-on-backlog

# offline replay of the pipeline from a local dataset
python run_lifecycle.py --dataset data/historical

# re-report without contacting any provider
python run_lifecycle.py --no-settle

# see what would happen, write nothing
python run_lifecycle.py --dry-run
```

Flags: `--month`, `--ledger-dir`, `--settlement-dir`, `--out`, `--bins`,
`--dataset`, `--grace-hours`, `--no-settle`, `--fail-on-backlog`, `--dry-run`.

Artifacts are written as `lifecycle_<timestamp>.json` — never overwritten, since
two runs are two observations of pipeline health and the second does not correct
the first. The `lifecycle_` prefix keeps them distinct from `evaluate_settled.py`
output.

### Verified run

```
lifecycle:  3 discovered, 1 settled, 1 unresolved, 1 pending, 0 awaiting settlement
evaluation: 3/3 joined, 1 settled, 1 unresolved, 1 awaiting settlement
POISSON_V1 1.0.0
  scored      1/3
  brier       0.2025
```

Three predictions: one settled 2-1, one postponed, one kicking off in 2099.
Brier `0.2025 == (1 - 0.55)²` from the single scored observation.
`accounted_for: true`, `ledger_unchanged: true`, `replay_used: false`.

---

## 6. `accounted_for`

Every discovered prediction lands in exactly one stage, and the stage counts must
sum back to `discovered`. `accounted_for` asserts that arithmetic in the artifact.

It guards the failure mode this Epic exists to prevent: a prediction falling
through the classification and simply never being counted. A silent undercount
would make coverage look better than it is, which is the one direction of error
nobody investigates.

---

## 7. Leakage firewall

`_summary_dict` is reused from `evaluate_settled.py` rather than reimplemented.
It is the single place deciding which metric keys reach an artifact, and that
choice is constrained by `tests/regression/test_evaluation_leakage.py`. A local
copy would be a second place for that list to drift, and the drift would be a leak.

`test_no_market_language_reaches_the_artifact` checks **key name components**
(`key.lower().split("_")`), matching the existing firewall, not raw substrings.
A substring scan flags `ledger_digest_before` as containing "edge" — and a firewall
that cries wolf on a legitimate field is one that gets deleted.

`config` is also refused: a threshold here would invite reporting "accuracy above
the recommendation line", which grades the *filter* rather than the probability and
quietly reintroduces the decision layer into evaluation.

---

## 8. Non-goals

- **No new metric.** Brier, log loss and calibration come from
  `domain/evaluation.py` unchanged.
- **No model change.** No probability is recomputed, ever.
- **No ledger write.** Structurally unreachable.
- **No scheduler.** Cron, CI or a human invoke the script; it does not daemonise.
- **No conflict resolution.** Contradictions are reported and the run fails.
- **No historical backfill.** Only ledger records can be settled — a prediction
  that was never recorded cannot be graded after the fact without inventing what
  we would have believed. (See `docs/EPIC_2H_SETTLEMENT_AUDIT.md`.)

---

## 9. Verification

```
2111 passed, 3 skipped        # 3 skips pre-existing, unrelated (D1, D3, 2D registry)
ruff check .   All checks passed!
mypy           Success: no issues found
```

New tests, 99 in total:
`tests/unit/test_lifecycle.py` (45),
`tests/unit/test_run_lifecycle.py` (41),
`tests/regression/test_lifecycle_isolation.py` (13).

`git status` reports the seven files below as **added**, and nothing as modified —
no frozen module was touched:

```
domain/lifecycle.py                            run_lifecycle.py
tests/unit/test_lifecycle.py                   tests/unit/test_run_lifecycle.py
tests/regression/test_lifecycle_isolation.py   tests/helpers/lifecycle_fixtures.py
docs/EPIC_2H4_EVALUATION_OPERATIONS.md
```

Every test in `test_run_lifecycle.py` drives the full path against `tmp_path`
with an injected result source — no network, no wall clock, no fixture left
behind.
