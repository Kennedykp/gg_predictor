# Epic 2D — Structured Goal Models and the Discrimination Ceiling

**Status:** COMPLETE — research only, nothing promoted to production
**Question:** does *structure* add discriminative power that better *estimation* cannot?
**Answer:** **No.** Structure changes almost nothing. The binding constraint is the
information content of goal counts, not the model form. Measured, not asserted.

---

## 1. Why this Epic exists

Epic 2C succeeded at what it set out to do — Gamma–Poisson shrinkage removed every
unjustified 0% BTTS prediction and improved Brier in every evidence bucket — but it
surfaced a problem that invalidates its own headline metric, recorded as **GG-029**:

> A **constant** predictor that ignores every fixture and always says 0.52 scores
> **Brier 0.2469**. Raw POISSON_V1 scores **0.2601**. The shrunk estimator scores
> **0.2528**.

So on the holdout the constant beats both. Brier rewards flattening toward the base
rate, which means "minimise Brier" drives prior strength toward infinity and selects
the model that has stopped saying anything. Epic 2C's Brier gains were therefore
*partly* a measure of how much signal it had discarded, and the Epic could not tell
how much.

That makes the open question **discrimination**, not calibration:

> Can any model here *rank* fixtures by BTTS likelihood?

AUC answers exactly that and is invariant to monotone flattening — the transformation
Brier rewards. That invariance is why AUC is this Epic's primary metric, and why
every parameter here is selected on **goal-count likelihood**, never on BTTS Brier.

---

## 2. Validation protocol (fixed before any number was inspected)

| Partition | Seasons | Use |
|---|---|---|
| Development | 2018, 2019 | parameter profiling, inspected repeatedly |
| Validation | 2021, 2022 | confirm the choice generalises, inspected once |
| **Holdout** | **2024** | run **once**, after parameters frozen |

**Why 2024 and not 2023.** Epic 2C searched on 2018–2019, validated on 2020 and used
2023 as its final test, reporting it by evidence bucket. 2020 and 2023 are therefore
**burned** and cannot honestly be called untouched. 2024 has never been a target in
any Epic and is fully cached (2,615 matches, 7 leagues, 0 gaps). The burned seasons
are recorded in code as `BURNED_SEASONS`, and the runner prints a warning if a target
season appears in it — so this cannot be quietly forgotten later.

Previous seasons are still loaded *as history* (2017 for 2018, 2023 for 2024). That
is not contamination: those matches occurred before the targets, which is the entire
point of a point-in-time prior. "Burned" means *inspected as a target*.

Rolling origin comes free: `replay` rebuilds each target's history from matches with
kickoff strictly `<` the target kickoff, so each season is already a rolling-origin
backtest. No second cutoff implementation was written.

**The holdout was run exactly once.** No parameter was revisited afterwards.

---

## 3. Candidates

All four reuse the *identical* BTTS mapping — `btts_independent` is asserted
bit-identical to `poisson.calculate_gg_probability` in
`tests/unit/test_goal_models.py`. Any AUC difference is therefore attributable to the
**rate estimates**, not to a different probability formula.

| ID | Structure | Status |
|---|---|---|
| `POISSON_V1_RAW` | production venue ratios | baseline |
| `C1_MAHER` | Maher attack/defence/home advantage, joint MLE | evaluated |
| `C2_MAHER_DECAY` | + Dixon–Coles exponential time decay ξ | **dropped, ξ̂ = 0** |
| `C3_DIXON_COLES` | + low-score dependence τ(ρ) | evaluated |
| `C4_BIVARIATE` | + shared component λ₃ | **dropped, not identifiable** |

`C1` replaces four of POISSON_V1's five inputs with a jointly fitted model: instead of
"this team's own home goals ÷ its own home matches", every match in the league
contributes to every parameter through opponent strength. A team with 2 home matches
is no longer estimated from 2 observations.

---

## 4. Parameter estimation — on goals, never on Brier

Profiled out-of-sample on development only (120 rolling origins), scoring the
likelihood of the goals actually scored under a model fitted strictly before kickoff.

**Time decay ξ** (mean log-likelihood per fixture):

| ξ | half-life | mean log-lik |
|---|---|---|
| **0.0000** | ∞ | **−2.95245** ← max |
| 0.0010 | 693 d | −2.95414 |
| 0.0050 | 139 d | −2.97977 |
| 0.0100 | 69 d | −3.02399 |
| 0.0200 | 35 d | −3.12216 |

**Monotone decreasing. ξ̂ = 0 at the boundary.** Within a season plus one season of
history, recency-weighting *reduces* predictive likelihood — every match is worth the
same. **C2 was dropped**: fitting a decay the data does not support would be inventing
structure. Note this contradicts the usual football-modelling assumption, which is
why it was profiled rather than assumed.

**Dixon–Coles ρ**: maximised at **ρ̂ ≈ −0.05** (−2.95132 vs −2.95245 at ρ = 0). The
sign matches the literature (excess low-score draws) but the magnitude is tiny — a
likelihood gain of 0.001 nats/fixture. Carried into C3 as a genuine estimate.

**Bivariate λ₃**: monotone decreasing, maximised at the **boundary λ₃ = 0**
(−2.95245 → −3.15850 at λ₃ = 0.5). The data cannot distinguish a shared component
from none. **C4 was dropped rather than constrained or given a stabilising prior** —
a boundary maximum means the parameter is not identifiable, and pinning it anywhere
else would be fabrication. Its diagnostic pmf deliberately stayed in the research
script rather than being promoted to `domain/goal_models.py`.

---

## 5. Results — fair intersections only

Every comparison below uses `domain.comparison.compare`, which computes the
intersection *before* summarising either arm. There is no code path that scores two
models over different fixture sets.

### Development (2018–2019, eng.1)

| | n | AUC | ΔAUC 95% CI | verdict |
|---|---|---|---|---|
| RAW → C1 | 736 | 0.5253 → 0.5374 | [−0.0210, +0.0435] | INDISTINGUISHABLE |
| RAW → C3 | 736 | 0.5253 → 0.5378 | [−0.0204, +0.0438] | INDISTINGUISHABLE |

### Validation (2021–2022, eng.1)

| | AUC | ΔAUC 95% CI | verdict |
|---|---|---|---|
| RAW → C1 | 0.5248 → 0.5462 | [−0.0040, +0.0494] | INDISTINGUISHABLE |
| RAW → C3 | 0.5248 → 0.5465 | [−0.0039, +0.0496] | INDISTINGUISHABLE |

C3 beats C1 by 0.0003 AUC. The ρ correction is real but negligible for BTTS.

### **Holdout — 2024, five leagues, run once**

Coverage: RAW 1,725/1,752 (98.5%); C1 and C3 1,698/1,752 (96.9%). Intersection 1,698.
C1 refuses more often, and **that is correct behaviour** — a team absent from the
fitting window has no estimated strength, and substituting "exactly average" would
present an assumption as a measurement.

| metric | POISSON_V1_RAW | C1_MAHER | note |
|---|---|---|---|
| AUC | 0.5368 | 0.5430 | ΔAUC +0.0062, CI [−0.0123, +0.0251] → **INDISTINGUISHABLE** |
| Brier | 0.2601 | 0.2528 | constant benchmark **0.2469 — still beats both** |
| Log loss | 0.8296 | 0.6998 | large improvement, driven by extremes |
| p ≤ 0.05 | 12 | **0** | |
| **p == 0 exactly** | **12** | **0** | GG-028 eliminated |
| sd of predictions | 0.1369 | 0.1060 | 22% less spread |

By evidence level (identical intersection, shared counts):

| prior venue matches | n | AUC RAW → C1 | Brier RAW → C1 |
|---|---|---|---|
| 0 | 69 | 0.421 → 0.472 | 0.2895 → 0.2641 |
| 1–2 | 165 | 0.577 → 0.593 | 0.2712 → 0.2495 |
| 3–5 | 288 | 0.558 → 0.549 | 0.2611 → 0.2553 |
| 6–9 | 384 | 0.535 → 0.557 | 0.2581 → 0.2484 |
| 10+ | 792 | 0.536 → 0.532 | 0.2559 → 0.2537 |

Brier improves everywhere. AUC does not move consistently in any direction. Note the
0-evidence bucket: RAW scores **AUC 0.421 — worse than random**, i.e. with no venue
history its ordering is actively *inverted*. C1 pulls it to 0.472, still ≤ 0.5.
Neither model can rank fixtures it knows nothing about, and RAW's confident 0%
predictions were anti-informative.

Calibration on the high-confidence tail is the sharpest single finding:

> RAW's `[0.00, 0.10)` bin: 13 fixtures, mean prediction **0.005**, observed BTTS rate
> **0.538** — a gap of **0.533**. Both teams scored in more than half of the fixtures
> RAW called essentially impossible.

C1 places nothing in that bin at all. Every remaining C1 gap is ≤ 0.089 apart from a
7-fixture bin. This is GG-028 measured on unseen data, and it is the mechanism behind
the log-loss improvement.

---

## 6. The ceiling probe — why the answer is decisive

A negative result invites the objection "your estimator was simply not good enough".
To close that off I built a deliberately **leaky** diagnostic, `ORACLE_LEAKY_CEILING`:
fitted on the **full dataset including the target season and the target fixture
itself**. It is not a model, it is quarantined, it is never registered in the harness
registry, and tests assert all of that.

It answers one question: if you knew every team's season-long strength *perfectly*,
how well could you rank BTTS?

| | AUC |
|---|---|
| POISSON_V1_RAW (honest) | 0.5229 |
| C1_MAHER (honest) | ~0.537–0.546 |
| **ORACLE_LEAKY (sees the future)** | **0.5679** |

**Perfect in-sample knowledge of team strength buys AUC 0.568.** The honest candidates
already reach 0.537–0.546. Better estimation of the same quantities can therefore
recover **at most ~0.02–0.03 AUC**, and no estimator can exceed the ceiling because
the ceiling already cheats.

The limit is the **model class and the information content of goal counts**, not
estimation error. That is the answer to Epic 2D's question, and it could not have been
obtained any other way.

---

## 7. Conclusions

1. **Structure adds nothing measurable.** All ΔAUC confidence intervals include zero,
   on all three partitions. Maher's joint estimation, time decay, Dixon–Coles and the
   bivariate model do not improve ranking.
2. **Two candidates were dropped on evidence**: ξ̂ = 0 and λ₃ non-identifiable, both
   boundary maxima. Neither was constrained into behaving.
3. **The ceiling is ~0.568 AUC and it leaks.** Goal counts alone cannot rank BTTS much
   better than a coin flip. This is a *data* limit.
4. **GG-028 is confirmed eliminated on unseen data**: 12 → 0 exact-zero predictions,
   and the `[0, 0.10)` bin that RAW filled with a 0.533 calibration gap is empty.
5. **GG-029 is confirmed and sharpened**: the constant predictor (0.2469) still beats
   every model on the holdout. Brier must never again be the selection objective.
6. **No model should be promoted.** C1's Brier and log-loss gains are real but come
   with a 1.6pp coverage cost and no discrimination gain. There is no case for
   changing production on this evidence.

**Recommendation for Epic 2E: stop adding structure to goal counts.** The ceiling
proves the information is not there. Either introduce genuinely new information
(shots, xG, lineups, in-play state) or establish whether the odds-derived market
probability discriminates better than 0.568 — which would tell us whether the ceiling
is a property of football or of *this feature set*. Recalibration is not worth
attempting while AUC sits near 0.54: a monotone recalibration cannot improve ranking,
so it would improve Brier while adding no skill, which is precisely the GG-029 trap.

---

## 8. Verification

- `pytest` — **1,654 passed, 3 skipped** (pre-existing GG-002-B / D1 skips)
- `ruff check .` — clean
- `mypy` — clean, 35 source files
- POISSON_V1 golden regression tests unchanged and passing
- **Zero production files modified.** All changes are new untracked files:
  `domain/goal_models.py`, `domain/discrimination.py`, `research/epic2d_experiment.py`,
  `research/epic2d_results/`, `tests/unit/test_goal_models.py`,
  `tests/unit/test_discrimination.py`, `tests/regression/test_epic2d_protocol.py`
- Unchanged and verified by `git diff`: `poisson.py`, `config.py`, `filters.py`,
  `decision.py`, `shared/odds.py`, `run3/`

## 9. Limitations

- eng.1 only for development/validation (speed); the holdout used five leagues.
- 120 rolling origins per profile, not every fixture.
- ρ̂ = −0.05 sits on a coarse grid; the likelihood surface is nearly flat there.
- The ceiling probe is in-sample, so it is an *estimate* of the ceiling, and if
  anything an optimistic one — which only strengthens the conclusion.
- Promotion status is still not directly available from ESPN (no promotion field exists in the
  payloads, as established in Epic 2C). C1 refuses teams absent from the fitting window rather than
  inferring promotion or substituting the league average, so promoted clubs reduce coverage instead of
  receiving a guessed prior. This is unchanged by 2D and is not tracked as a separate debt item.

