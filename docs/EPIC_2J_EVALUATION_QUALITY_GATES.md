# Epic 2J — Evaluation Quality Gates

**Status:** complete
**Requirement closed:** 2G-R5 — *"Evaluation reporting must not present Brier score by itself as evidence of model quality."*
**Preceded by:** 2H-2 (settlement), 2H-3 (evaluation integration), 2H-4 (operations), 2H-5 (reporting), 2I (capture assurance)

---

## 1. The defect

Before this Epic, the evaluation report could emit exactly this and stop:

```text
brier 0.0100
```

One settled fixture. A prediction of `p=0.90` on a match that finished 2–1. A Brier
of 0.01 is, in isolation, a spectacular score — it is what a near-perfect
forecaster looks like. It was one observation, and the naive constant predictor
scored **0.0000** on the same observation, which is better.

Nothing in the report said so. `is_reportable` was `True`, because a metric
existed. That is the whole defect: the system had no way to distinguish *"a
number can be computed"* from *"this number may be believed."*

The failure mode is not a wrong calculation. Every arithmetic result was correct.
The failure is **false precision** — four decimal places of unearned confidence,
which is the specific way a measurement system loses its usefulness. A model that
cannot beat a coin flip could have looked excellent for an entire season, and the
report would have agreed.

---

## 2. What "performing well" now requires

The report may only present a metric as evidence of quality when **all** of the
following hold, per group:

| Condition | Why it is necessary |
|---|---|
| `scored >= minimum_n` | A Brier over n=1 is noise with a decimal point. |
| Both outcome classes present | AUC — discrimination — is undefined otherwise, and a single-class sample makes any calibration-free score meaningless. |
| AUC computed and reported | Epic 2D measured POISSON_V1 at **AUC ≈ 0.54**: it barely ranks. Brier alone concealed this; AUC is what exposed it. |
| Constant-predictor Brier computed on the same observations | The naive baseline is the floor. A score that does not beat it is not skill. |
| Delta vs baseline stated with direction named | "0.2525" is unremarkable. "0.0025 **worse** than baseline" is a finding. |
| Probability is the published one | See §6. Grading a probability recomputed today is hindsight, not track record. |

Any single condition failing means the group is reported with a status and
**without** a quality claim. The metrics remain visible — see §5.

---

## 3. Architecture

One new pure module. No metric logic was duplicated.

```text
prediction ledger (immutable)
        │
        ▼
settle_predictions ──> settlements (immutable)
        │
        ▼
domain/evaluation_input.join_for_evaluation      [2H-3, frozen]
        │  EvaluationInput records
        ▼
domain/evaluation.summarise                      [frozen]  Brier, log loss, calibration
        │  MetricSummary
        ▼
domain/quality.assess  ◄── domain/discrimination.auc  [frozen]
        │  QualityAssessment: + AUC, baseline, delta, status
        ▼
domain/reporting.summarise_dimension             Group.quality per dimension
        │
        ▼
report_evaluation.py                             JSON artifact + console
```

`domain/quality.py` computes **no** metric that already existed. It calls
`summarise()` for the proper scores and `discrimination.auc()` for ranking, then
adds only the two things that were genuinely missing — the constant-predictor
baseline and the gating decision — and returns them in one frozen object.

### Why `domain/discrimination.py` was reused unchanged

It was audited first and found already correct for this purpose: pure, no I/O,
no clock, deterministic, `n=0` and single-class both return `None` rather than a
misleading `0.5`, and ties receive proper midpoint credit (the tie handling was
verified against a hand-computed rank-sum). It needed no modification, so it
received none.

### The `Group` contract

`Group` gained one field, `quality`, and two delegating properties:

- `is_reportable` — **unchanged meaning.** A metric exists. Preserved so no 2H-5
  behaviour shifts underneath an existing caller.
- `is_quality_claim` — **new.** The metric may be believed.

Keeping both is deliberate. Redefining `is_reportable` would have silently
changed the meaning of existing report fields; the two properties disagreeing on
an n=1 group *is* the fix, and there is a test asserting exactly that
disagreement.

---

## 4. The minimum-n policy

`MINIMUM_REPORTABLE_N = 100`, living in `domain/quality.py` — **not** in
`config.py`.

That placement is the substantive design decision in this Epic. `config.py` holds
thresholds that change what the model *predicts* or *recommends*. A reporting
threshold changes what we are willing to *claim*. Putting the two in one file
invites a future edit that loosens an evidence standard while believing it is
tuning a model — the exact class of mistake that makes a measurement system
untrustworthy. Reporting policy therefore lives with the reporting code, and
`config.py` was not touched.

**Derivation of 100.** For a proportion, the standard error is
`sqrt(p(1-p)/n)`, maximised at `p=0.5`, giving `0.5/sqrt(n)`. At n=100 that is
**0.05** — a ±5pp band, tight enough that a genuine difference between the model
and the baseline is distinguishable from sampling noise. At n=25 it is 0.10,
which is wider than the effect we are trying to detect: a model at AUC 0.54 sits
well inside that band and could not be told apart from a coin.

The number is pinned by a test that names the derivation, so changing it requires
editing an assertion that states the reasoning — a deliberate act, not a drift.
It is a **default**, and callers may pass a stricter `minimum_n`; the API cannot
loosen it silently because the default applies unless overridden.

The gate applies **per group, per dimension**. A season can clear the threshold
overall while a competition inside it has eleven settled fixtures. Judging the
slice by the parent's sample size would reintroduce the original defect one level
down, so each group is judged only on its own evidence — including its own
baseline, computed from its own base rate.

---

## 5. Gated, not hidden

Below the threshold the report withholds the *claim*, never the *evidence*:

```json
{
  "metrics": { "brier": 0.0100, "log_loss": 0.1054, "calibration": [...] },
  "quality": {
    "scored": 1, "shortfall": 99,
    "brier": 0.0100,
    "auc": null,
    "auc_undefined_reason": "single-class sample (1 YES, 0 NO)",
    "constant_predictor_brier": 0.0,
    "brier_delta_vs_baseline": 0.01,
    "baseline_verdict": "WORSE",
    "status": "INSUFFICIENT_SAMPLE",
    "is_quality_claim": false
  }
}
```

Suppressing the numbers would be the opposite failure: an operator investigating a
thin sample needs to see what is there. Two rules follow, both tested:

- **AUC is never defaulted.** `0.5` means "no discrimination", which is a finding.
  An absent AUC stays `null` and carries a reason naming the class counts, so no
  reader can mistake absence for mediocrity.
- **`auc` and `auc_undefined_reason` are mutually exclusive.** Exactly one is
  present, always.

### Status vocabulary

| Status | Meaning |
|---|---|
| `REPORTABLE` | All conditions met. The only status that licenses a claim. |
| `INSUFFICIENT_SAMPLE` | Metrics exist; too few observations. |
| `AUC_UNDEFINED` | Enough observations, but one class only — discrimination unmeasurable. |
| `NOT_MEASURABLE` | Nothing settled yet. An **operational** state, not a model verdict. |

The last distinction matters: unresolved fixtures are football's problem (a
postponement), not a model failure, and must never read as a poor score. Sample
size is checked **before** class diversity, so a thin single-class group reports
`INSUFFICIENT_SAMPLE` — reporting `AUC_UNDEFINED` would imply n was the only
thing missing.

---

## 6. The ledger-probability firewall

The evaluation path grades the probability that was **published**, never one
recomputed today. The realistic threat is not malice but convenience: AUC over a
sparse group looks unstable, someone reasons that a fresh probability from the
current model would be "more accurate", and imports `poisson` or calls
`evaluation_harness.replay()`.

That would grade a model that never made the prediction, against outcomes already
known. The resulting metrics would look **better**, which is what makes it
dangerous rather than merely wrong — and no runtime assertion would catch it,
because a replay-contaminated evaluation returns entirely plausible floats.

`tests/regression/test_quality_gates_isolation.py` therefore checks the
**source**: it parses the import graph with `ast` and asserts as a structural fact
that no probability-computing, data-fetching or replay module is reachable.

- `domain/quality.py` and `domain/reporting.py` — strict **transitive** standard.
  Nothing forbidden at any depth. `domain.quality`'s entire first-party
  dependency set is asserted to be exactly `{evaluation, discrimination,
  validation}`, so a new dependency fails until reviewed.
- `report_evaluation.py` (CLI) — direct-import standard, plus an **exact**
  transitive allow-list (§8).
- `ast` rather than text search, so the extensive prose about replay in these
  files does not trip the test that forbids it.
- The detector is itself verified: one test points it at `run_evaluation.py`,
  which legitimately uses the harness, and asserts it finds the violation. A
  silently-empty checker would otherwise pass everything.

**Verified by injection.** Adding a single `import poisson` to `domain/quality.py`
failed 5 tests — including the propagation into `domain/reporting.py` and the
entry point. Removing it returned the suite to green.

### Research code: what may and may not be reused

| Reusable | Not reusable |
|---|---|
| `domain/discrimination.py` — pure metric over supplied pairs | `evaluation_harness.replay()` — produces a probability never published |
| `domain/evaluation.summarise` — pure scoring | `research/evaluate_baseline.py` — replays the model to construct its comparison |
| The base-rate *formula* | Any live provider fetch |

`research/evaluate_baseline.py` is correct research code and answers a legitimate
question ("how would today's model have done?"). It is unusable here because its
baseline is built from a replayed population rather than from settled published
predictions. 2J therefore computes the constant predictor **from the same settled
observations the model is scored on** — 15 lines, no replay, no contamination —
rather than importing a function that would silently import hindsight.

---

## 7. Tests

**102 new tests. Full suite: 2423 passed, 3 skipped** (baseline 2321 / 3 — no
regressions). `ruff check .` clean. `mypy` clean, 60 source files.

| File | Count | Covers |
|---|---|---|
| `tests/unit/test_quality.py` | 57 | The defect; n=0/1/2/tiny/boundary/above; exact-threshold inclusivity; all-positive; all-negative; all-ties → 0.5; AUC never defaulted; baseline better/worse/equal/undefined; float-noise tolerance; published probability used verbatim; no mutation or reordering; determinism; immutability |
| `tests/regression/test_quality_gates_isolation.py` | 18 | Transitive import firewall; forbidden calls; allow-list integrity; detector self-check; 2G-R5 end-to-end through the real reporting path |
| `tests/unit/test_report_quality_output.py` | 27 | JSON artifact shape; all eight required facts present; serialisability; console rendering; gated markers; per-dimension gating; per-group baselines |

Two test-authoring notes, recorded because both were caught by the suite itself:

- A test asserting `BETTER` for a flat `p=0.55` over a 50/50 population was
  **wrong, and the code was right**: 0.2525 vs the baseline's 0.25 is genuinely
  worse. The assertion was corrected to state that arithmetic explicitly — it is
  now one of the clearest demonstrations of why the baseline column exists.
- One test guessed an `UnevaluableReason` member that does not exist. Corrected
  to the real `INSUFFICIENT_HISTORY`.

The schema pin in `tests/unit/test_reporting.py` was **updated, not relaxed**,
from `2h5.1` to `2j.1`, with a comment explaining the bump. A pin's purpose is to
make an artifact-shape change a deliberate edit; this was one. It was the only
such pin in the suite — the existing 2H-5 reporting and evaluation tests otherwise
passed unmodified, which is the evidence that `Group`'s existing contract was
extended rather than altered.


---

## 8. Known limitation

`report_evaluation.py` imports `DEFAULT_SETTLEMENT_DIR` from `settle_predictions`,
so that the reporting and settlement CLIs cannot disagree about where settlements
live. `settle_predictions` needs a provider for its own work, which makes `espn`,
`historical_dataset` and `domain.poisson_inputs` **transitively** reachable from
the reporting entry point.

This predates 2J and is not a probability leak: what crosses the boundary is a
directory name (`Path("data/settlements")`), and `domain/reporting.py` — where the
metrics are actually assembled — has no such path at any depth. The 2H-5 firewall
checks direct imports only, which is why it had never surfaced.

It is **pinned rather than filtered**. `ENTRY_POINT_KNOWN_TRANSITIVE` enumerates
exactly those three modules; a fourth fails the build, and a separate test asserts
`poisson`, `evaluation_harness` and `run_evaluation` remain unreachable regardless
of what the allow-list contains. The clean fix is a shared paths module, which is
a refactor of files frozen for this Epic and is left as tracked debt.

---

## 9. Files

**Added**

- `domain/quality.py` — the gate
- `tests/unit/test_quality.py`
- `tests/unit/test_report_quality_output.py`
- `tests/regression/test_quality_gates_isolation.py`
- `docs/EPIC_2J_EVALUATION_QUALITY_GATES.md`

**Changed**

- `domain/reporting.py` — `Group.quality` + two delegating properties; schema `2j.1`
- `report_evaluation.py` — `_quality_dict`, quality block in the artifact, gated console lines
- `tests/unit/test_reporting.py` — schema pin updated `2h5.1` → `2j.1`


**Frozen — not touched**

`poisson.py`, `filters.py`, `decision.py`, `config.py`, `evaluation_harness.py`,
`domain/evaluation.py`, `domain/evaluation_input.py`, `domain/discrimination.py`,
`domain/settlement.py`, `domain/lifecycle.py`, `prediction_ledger.py`,
`settle_predictions.py`, `evaluate_settled.py`, `run_lifecycle.py`, `run3/`.

No prediction probability, recommendation, threshold or ledger record was altered.
The model was not retrained and no historical prediction was recomputed.

---

## 10. What this Epic does not do

It does not make the model good. POISSON_V1 at AUC ≈ 0.54 is a weak ranker, and
with the baseline column now present the reports will say so plainly and often —
including cases where the model loses to a constant predictor.

That is the intended outcome. 2J is not about adding AUC; it is about ensuring
that when this system eventually says *"the model is performing well,"* the
statement is backed by enough observations, discrimination evidence, proper
scoring, a naive-baseline comparison, the probability that was actually published,
transparent coverage, and no hindsight.

Until that bar is met, the report now says so instead of showing four decimal
places.
