# Epic 2C — Cold-Start & Team-Strength Estimation

**Status:** complete, not merged. **`poisson.py` unchanged.**

---

## 1. Headline answer

> **Did shrinkage actually solve the sparse-data problem?**

**Yes for the stated failure, and the mechanism is now impossible rather than
merely unlikely.** GG-028 is closed: exact-zero probabilities go **19 → 0**
(validation) and **17 → 0** (holdout), with no clipping anywhere in the code.
The 1–2 evidence bucket — the bucket the Epic exists for — improves on identical
fixtures from Brier 0.2884 → 0.2550 and log loss 2.0186 → 0.7039 on the holdout.
Mature buckets do not deteriorate. Coverage rises.

**But one finding materially qualifies the win, and it is the most important
thing in this document:**

A **constant predictor** at the base rate scores **Brier 0.2496** on the
development corpus. Raw POISSON_V1 scores **0.2615**. **The baseline model is
worse than a constant.** And AUC is **≈0.535 for every configuration tested,
including the raw baseline** — flat from k=2 to k=1000.

So the Brier and log-loss gains are almost entirely **calibration**: shrinkage
stops the model making confident wrong claims. It adds **no discrimination**.
POISSON_V1's venue-split inputs carry ~0.035 AUC of signal above coin-flipping,
and shrinkage cannot create signal that was never in the inputs.

**Epic 2C fixed the input estimator. It did not make the model predictive.**
Reporting the −0.0105 Brier improvement without that sentence would be
misleading, which is why Part 7's search was extended and the collapse
diagnostic written.

---

## 2. GG-028 failure mechanism

Verified in the repository, not taken from the brief.

`domain/poisson_inputs.py` derives venue scoring rates by division:

```
away_scoring_rate = away_goals_for / away_matches_played
```

With one prior away match that finished 0–2, `away_goals_for = 0`,
`away_matches_played = 1`, so the rate is **exactly 0.0**. `poisson.py` then
computes `lambda_away = 0.0 * opponent_factor * ... = 0.0`, and

```
P(away scores >= 1) = 1 - exp(-0.0) = 0.0
P(BTTS) = P(home scores) * 0.0 = 0.0
```

An **absolute claim of impossibility from a single observation**. Log loss is
unbounded here: one such fixture that ends 1–1 contributes `-log(0)`.

This is exactly what produced the 2.0186 log loss in the 1–2 bucket. The
baseline's headline log loss of 0.8944–0.9015 is dominated by ~17–19 fixtures.

**Reproduced permanently** in `tests/regression/test_gg028_sparse_sample.py`,
which asserts the *original* behaviour (`p == 0.0`) through the real
`poisson.py`. It is written to keep failing forever if someone "fixes"
POISSON_V1 — the test is evidence of the bug, not of the cure.

---

## 3. Statistical formulation

Full derivation in the `domain/team_strength.py` module docstring. Summary:

| Element | Form |
|---|---|
| Likelihood | `G_j ~ Poisson(λ)`, `Y = ΣG_j ~ Poisson(nλ)` |
| Prior | `λ ~ Gamma(α, β)`, β a **rate** not a scale |
| Posterior | `λ \| Y ~ Gamma(α + Y, β + n)` (conjugate) |
| Posterior mean | `(α + Y) / (β + n)` |
| Reparameterisation | `α = k·μ`, `β = k` ⟹ `E[λ] = μ` exactly |
| **Estimator** | **`λ̂ = (k·μ + Y) / (k + n)`** |
| Prior strength | `k` is a **count of matches** |
| Units | `μ` goals/match, `Y` goals, `k`/`n` matches ⟹ `λ̂` goals/match |
| `n → 0` | `λ̂ = μ` — the prior mean, no branch, nothing fabricated |
| `n → ∞` | `λ̂ → Y/n` — observation dominates; weight `k/(n+k)` → 0 |
| Shrinkage identity | `λ̂ = μ + (n/(n+k))·(Y/n − μ)`, reliability `r = n/(n+k)` |

**Why GG-028 becomes structurally impossible:** for `k > 0, μ > 0` the numerator
contains `k·μ > 0`, so `λ̂ > 0` for *any* `Y` including 0. A genuine 0 still pulls
the estimate down — it remains real evidence — but can no longer certify
impossibility. **No clipping, flooring or clamping exists in the codebase.**
That distinction is only verifiable because the second was never written.

The prompt's suggested equation was verified before adoption, not assumed: the
`(α=k·μ, β=k)` reparameterisation is what makes it a posterior mean rather than
a plausible-looking ratio, and is what gives `k` its units.

---

## 4. Two-level prior (Part 3)

Hierarchy implemented, narrowest defensible version:

```
previous-season team venue rate   (PREV_SEASON_TEAM)
            ↓ falls back to
league baseline                   (LEAGUE_BASELINE)
            ↓ if neither
UNAVAILABLE                       (never fabricated)
```

**Promoted clubs.** ESPN payloads carry **no promotion field**. Rather than
guess, promotion is inferred from *observed participation*: a club with no prior
season in that competition is flagged `NEW_TO_LEAGUE`. This is honest about what
it detects — promotion *and* first corpus appearance — so it is labelled
"new-to-league", not "promoted".

Second-tier rates are **not** treated as top-flight equivalent. Four handlings
were compared on development data:

| Handling | attack × | defence × | sparse Brier | overall Brier |
|---|---|---|---|---|
| **destination-league fallback (chosen)** | 1.00 | 1.00 | **0.2533** | **0.2553** |
| mild transform | 0.85 | 1.10 | 0.2532 | 0.2555 |
| moderate transform | 0.72 | 1.15 | 0.2534 | 0.2559 |
| Epic 2A's observed 0.605 ratio | 0.605 | 1.20 | 0.2540 | 0.2565 |

**Decision: fall back to the destination-league prior; apply no transform.**
Every transform was neutral-to-worse, and the 0.605 ratio was the *worst* of the
four. Epic 2A's ratio was correctly **not** promoted to a production constant —
the evidence does not support it, and one country's historical ratio cannot
justify a universal adjustment. The factors remain configurable and default to
1.0, so a future Epic with broader evidence can revisit without new code.

---

## 5. League baseline (Part 4)

Point-in-time safe and itself shrunk — the same estimator, applied one level up:

```
league_λ̂ = (k_league·μ_prev + Y_current_before_T) / (k_league + n_current_before_T)
```

`μ_prev` is the previous season's completed baseline (chronologically prior, so
legitimate at T); `Y/n` count only team-games with kickoff **strictly before** T.
The final current-season average is never used. No 70/30 weighting appears
anywhere: the weight is `n/(n+k_league)`, which *derives* from evidence volume.

`k_league` is in **team-games** (~760/season), an order of magnitude above team
`k` (~19 venue matches/season) — the sweep respects that scale.

`k_league` proved nearly inert (0.2552–0.2554 across 0→160). Reported as
measured; not tuned into a false precision.

---

## 6. Five POISSON_V1 inputs (Part 5)

The estimator emits **exactly** the five existing inputs. The model's external
mathematical meaning is untouched.

| Input | Estimated as | Prior used |
|---|---|---|
| `home_scoring_rate` | `(k_F·μ + Y)/(k_F + n)`, home venue | prev-season home-for → league home-for |
| `home_conceding_rate` | same form, goals against | prev-season home-against → league home-against |
| `away_scoring_rate` | same form, away venue | prev-season away-for → league away-for |
| `away_conceding_rate` | same form, goals against | prev-season away-against → league away-against |
| `league_avg_goals` | shrunk league baseline (§5) | prev-season league baseline |

**Provenance is structural, not commentary.** `ColdStartInputs` carries a
`ShrunkRate` per input, each recording `PriorSource`
(`PREV_SEASON_TEAM` / `NEW_TO_LEAGUE` / `LEAGUE_BASELINE` / `UNAVAILABLE`),
observed matches, observed total, prior mean, reliability weight and posterior
mean. Every value is attributable to current-season observation, previous-season
team prior, league prior, or the combination — and `UNAVAILABLE` is preserved
distinctly, so **missing data never becomes a silent zero**. No substitutions.

`league_avg_goals` remains **per-team-per-match**, matching the existing
convention. Getting this wrong would be wrong by a factor of two.

---

## 7. Point-in-time safety (Part 6)

- Strictly `kickoff < T.kickoff` — `>=` excludes, verified by mutation-style tests.
- Target fixture excluded by event id **as well as** by time, so a same-instant
  kickoff cannot leak the target into its own inputs.
- Previous-season data used **only** because it is chronologically prior.
- Never used: T itself, later fixtures, final current-season aggregates, future
  seasons, or promotion facts unavailable at T.
- **Reuses** the Epic 1B.5 / 2B.3 cutoff infrastructure; no competing
  implementation was created.

---

## 8. Validation protocol (Part 15) — stated before final numbers

| Partition | Seasons | Use |
|---|---|---|
| Development | 2018, 2019 | parameter search, repeated |
| Validation | 2020 | generalisation check |
| Final test | 2023 | run **once**, after freezing |

**Honest limitation.** 2023 is *not* pristine: Epic 2B.3's baseline corpus
included it as a target season, so its headline Brier has been seen. No Epic has
inspected 2023 by evidence bucket, by promotion status, or under any shrinkage
parameter. Correct description: **"held out for this Epic's parameter
selection"**, *not* "never observed". 2018–2020 were examined repeatedly by
Epics 2A/2B.3, so calling any of them untouched would be false.

Enforcement is mechanical: `--stage final` **refuses to run without an explicit
`--config`**, so a post-hoc parameter change cannot happen without appearing in
the command line. Parameters were frozen before the holdout ran; the holdout ran
once per frozen config; nothing was retuned afterwards.

Every prediction is already rolling-origin — `replay` rebuilds inputs per
fixture from strictly-prior matches — so a season's evaluation *is* a
rolling-origin backtest.

---

## 9. Parameter search (Part 7)

24 configurations on development seasons only. `k=5/8/10` were **not** assumed;
Epic 2A's `k≈8` was treated as a research bracket to test.

**Independent anchor — method of moments** (between-team variance, dev only):

| Split | k̂ |
|---|---|
| home for | 7.65 |
| home against | 13.99 |
| away for | 8.62 |
| away against | 19.11 |

Defence needs **~2× the shrinkage of attack** — an empirical finding, matching
Epic 2A's suspicion of a reliability asymmetry, and now measured.

**The search does not have an interior optimum.** Brier falls monotonically to
the edge of the grid:

| config | Brier | ΔBrier | log loss | ΔLL | sparse Brier | exact 0/1 |
|---|---|---|---|---|---|---|
| baseline (raw) | 0.2615 | — | 0.8739 | — | 0.2685 | 29 |
| null k=0 | 0.2877 | +0.0263 | 1.7728 | +0.8989 | 0.3929 | 209 |
| k=8 | 0.2538 | −0.0079 | 0.7015 | −0.1748 | 0.2516 | 0 |
| k=16 | 0.2508 | −0.0109 | 0.6950 | −0.1814 | 0.2487 | 0 |
| k=40 | **0.2486** | **−0.0130** | 0.6905 | −0.1859 | 0.2469 | 0 |

The grid was **extended from 12 to 40 after the first pass sat on the boundary** —
a boundary optimum is an unfinished search, not a result.

**The collapse diagnostic** (`research/epic2c_collapse_diagnostic.py`) explains
why there is no optimum:

| arm | mean | **sd** | min | max | **AUC** |
|---|---|---|---|---|---|
| baseline raw | 0.4700 | 0.1243 | 0.000 | 0.933 | **0.5354** |
| k=8 | 0.4955 | 0.0984 | 0.207 | 0.814 | 0.5383 |
| k=40 | 0.5264 | 0.0599 | 0.353 | 0.715 | 0.5405 |
| k=1000 | 0.5369 | **0.0470** | 0.430 | 0.650 | 0.5343 |

Observed base rate **0.5202**; **constant-predictor Brier 0.2496**.

Increasing `k` monotonically flattens predictions toward the base rate. AUC —
invariant to monotone flattening, hence the decisive statistic — is **flat at
≈0.535 everywhere, baseline included**. So minimising Brier drives `k` toward
"predict the base rate always". **The Brier-optimal configuration is
degenerate.**

**Therefore `k` was NOT selected by minimising Brier.** Two configurations were
frozen and both reported:

- **Config A (chosen): `kF=8, kA=16, kP=8, kL=40`** — the method-of-moments
  estimates, rounded. Selected by an *independent statistical criterion*, not by
  the objective it is scored against.
- **Config B (control): `k=100`** — deep in the degenerate region, reported to
  show what over-shrinkage buys and costs.

---

## 10. Fair comparison (Part 8)

Coverage differs, so comparing raw Brier across arms would be invalid.

| | Validation 2020 | Final 2023 |
|---|---|---|
| target fixtures | 1826 | 1752 |
| POISSON_V1_RAW scored | 1802 | 1726 |
| POISSON_V1_SHRUNK_V1 scored | **1826** | **1752** |
| **intersection** | **1802** | **1726** |

Shrinkage covers **every** fixture — 24/26 more than baseline — because a team
with zero prior venue matches now yields a prior-based estimate instead of an
unavailable input. Coverage improves *and* is explainable.

**All comparative claims below use the intersection.** `domain/comparison.py`
intersects on `(competition, season, event_id)` and drops any fixture either arm
could not score. Regression tests enforce it, including a mutation test that
makes the arms disagree and asserts the comparison refuses.

---

## 11. Evidence buckets — Part 9, the key table

**Final holdout 2023, Config A, identical 1726-fixture intersection:**

| bucket | N | base Brier | **shr Brier** | base LL | **shr LL** | base cal | **shr cal** | base cov | shr cov |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 75 | 0.2536 | **0.2463** | 0.6994 | **0.6853** | 0.114 | **0.041** | 75 | 75 |
| **1–2** | 207 | 0.2884 | **0.2550** | 2.0186 | **0.7039** | 0.166 | **0.058** | 207 | 207 |
| 3–5 | 293 | 0.2719 | **0.2495** | 0.9670 | **0.6927** | 0.100 | **0.071** | 293 | 293 |
| 6–9 | 375 | 0.2604 | **0.2534** | 0.7176 | **0.7008** | 0.081 | **0.043** | 375 | 375 |
| 10+ | 776 | 0.2468 | **0.2448** | 0.6871 | **0.6828** | 0.091 | **0.071** | 776 | 776 |

Validation 2020 (1802 fixtures) shows the same ordering: 1–2 bucket
0.2961 → 0.2504 Brier, 2.2056 → 0.6944 log loss.

**Bucket definition.** `min(home venue matches, away venue matches)` — BTTS is a
product over both sides, so the *worse-evidenced* side binds. A mean would let a
mature home side mask a debutant away side, which is precisely GG-028's case.
Counted once from the dataset and shared by both arms, so bucketing cannot
favour either.

**Reading it honestly.** Sparse buckets improve most (log loss 2.02 → 0.70 at
1–2) and mature buckets do not deteriorate — the hypothesis holds. But the
improvement is **error-removal, not skill**: it comes from deleting catastrophic
confident-wrong predictions, and per §9 the ordering is unchanged.

---

## 12. Extreme probabilities (Part 10)

| metric | validation base | validation shr | final base | final shr |
|---|---|---|---|---|
| p ≤ 0.05 | 19 | **0** | 17 | **0** |
| p ≥ 0.95 | 1 | **0** | 0 | **0** |
| **p == 0 exactly** | **19** | **0** | **17** | **0** |
| p == 1 exactly | 0 | 0 | 0 | 0 |

**GG-028 eliminated in both partitions.** Not by clipping — there is no clipping
in the codebase — but because `k·μ > 0` makes a zero posterior mean impossible.
Every eliminated case sat in the 0–2 evidence buckets, as the mechanism predicts.

---

## 13. Calibration (Part 11)

Existing Epic 2B.3 machinery, no recalibration layer fitted.

**Final 2023, Config A:**

| bin | base N | base pred | base obs | shr N | shr pred | shr obs |
|---|---|---|---|---|---|---|
| 0.0–0.1 | 18 | 0.005 | **0.556** | 0 | — | — |
| 0.1–0.2 | 17 | 0.158 | **0.588** | 0 | — | — |
| 0.2–0.3 | 77 | 0.265 | 0.584 | 20 | 0.273 | 0.500 |
| 0.4–0.5 | 571 | 0.451 | 0.543 | 619 | 0.454 | 0.527 |
| 0.5–0.6 | 494 | 0.547 | 0.593 | 631 | 0.544 | 0.577 |
| 0.7–0.8 | 33 | 0.731 | **0.515** | 20 | 0.722 | 0.550 |
| 0.8–0.9 | 9 | 0.839 | **0.444** | 0 | — | — |

**High-confidence predictions are where the baseline is worst, in both
directions.** It said 0.005 and BTTS happened **55.6%** of the time; said 0.839
and it happened **44.4%**. Those are not near-misses — they are inverted.
Shrinkage empties both tails: overconfidence is **reduced**, and the answer to
"does shrinkage reduce overconfidence" is yes, decisively.

Config B empties everything outside 0.4–0.7 — visibly degenerate, which is why
it is a control and not the recommendation.

---

## 14. Promoted / new-to-league clubs (Part 12)

| | Validation 2020 | Final 2023 |
|---|---|---|
| flagged target fixtures | 464 | 456 |
| in intersection | 440 | 430 |
| baseline Brier | 0.2784 | 0.2847 |
| **shrunk Brier (A)** | **0.2507** | **0.2509** |
| baseline log loss | 1.4886 | 1.5208 |
| **shrunk log loss (A)** | **0.6950** | **0.6954** |
| baseline calibration err | 0.111 | 0.149 |
| **shrunk calibration err (A)** | **0.040** | **0.065** |

Baseline log loss ≈1.5 confirms new-to-league clubs are where GG-028 concentrates.
No universal promotion adjustment is claimed: the chosen handling applies **no**
transform (§4), and the identification method is participation-based, stated
plainly. Where a previous season is absent entirely, fixtures are **excluded from
the flag rather than assumed** either way.

---

## 15. Model identity (Part 13)

| model_id | version | meaning |
|---|---|---|
| `POISSON_V1` | `1.0.0` | untouched baseline, still reproducible |
| `POISSON_V1_SHRUNK_V1` | `1.0.0+2c.1` | shrunk inputs → **same** `poisson.py` |

`POISSON_V1` was **not** overwritten; both run simultaneously, which is what
makes the intersection comparison possible. The `+2c.1` suffix marks a changed
input estimator with unchanged probability mathematics.

---

## 16. Tests (Part 14) — all 18 requirements

45 new tests across three files; suite total **1573 passed, 2 skipped**
(the 2 skips are pre-existing GG-002-B/D1 items, untouched).

| # | Requirement | Where |
|---|---|---|
| 1 | zero obs → prior, not fabricated rate | `test_team_strength.py` |
| 2 | one obs strongly shrunk | `test_team_strength.py` |
| 3 | large n → observed rate | `test_team_strength.py` |
| 4 | genuine zero still valid evidence | `test_team_strength.py` |
| 5 | missing ≠ zero | `test_team_strength.py`, `test_cold_start_leakage.py` |
| 6 | strict `<` cutoff | `test_cold_start_leakage.py` |
| 7 | target-match leakage | `test_cold_start_leakage.py` |
| 8 | future-match leakage | `test_cold_start_leakage.py` |
| 9 | prev-season only if prior | `test_cold_start_leakage.py` |
| 10 | league prior point-in-time | `test_cold_start_leakage.py` |
| 11 | promoted-club handling | `test_cold_start_leakage.py` |
| 12 | partition isolation | `test_cold_start_leakage.py` |
| 13 | identical-intersection comparison | `test_cold_start_leakage.py` |
| 14 | deterministic output | `test_team_strength.py` |
| 15 | provenance | `test_team_strength.py` |
| 16 | **baseline POISSON_V1 unchanged** | `test_poisson_v1_regression.py` (pre-existing, passing) |
| 17 | sparse no longer certain | `test_gg028_sparse_sample.py` |
| 18 | mature behaviour sensible | `test_gg028_sparse_sample.py` |

Mutation-style guards included: flipping `<` to `<=` fails; making arms disagree
fails the intersection guard.

---

## 17. Verification (Part 18)

```
pytest      1573 passed, 2 skipped (pre-existing)
ruff check . All checks passed!
mypy         Success: no issues found in 32 source files
```

**Files changed — complete list:**

| File | Status |
|---|---|
| `domain/team_strength.py` | new — Gamma-Poisson estimator |
| `domain/cold_start.py` | new — five-input construction, provenance |
| `domain/comparison.py` | new — intersection, buckets, extremes |
| `evaluation_harness.py` | **modified** — registers `PoissonV1ShrunkAdapter` |
| `research/epic2c_experiment.py` | new — search + holdout |
| `research/epic2c_collapse_diagnostic.py` | new — degeneracy/AUC |
| `research/probe_cache_coverage.py` | new — cache probe |
| `research/epic2c_results/*.txt` | new — captured outputs |
| `tests/regression/test_gg028_sparse_sample.py` | new |
| `tests/regression/test_cold_start_leakage.py` | new |
| `tests/unit/test_team_strength.py` | new |
| `docs/EPIC_2C_COLD_START_MODEL.md` | new — this file |

**Verified unchanged** (`git status` clean): `poisson.py`, `config.py`,
`filters.py`, `decision.py`, `odds_api.py`, `shared/odds.py`, `run3/`, `main.py`,
`GG.md`. No odds or decision logic was reached during evaluation; no ML; no
Dixon-Coles; no ROI optimisation; no threshold changes.

---

## 18. Success criteria (Part 16), judged honestly

| Criterion | Verdict |
|---|---|
| sparse Brier/log loss improve materially | **Yes** — 1–2 bucket LL 2.02 → 0.70 |
| calibration improves | **Yes** — inverted tails eliminated |
| unjustified extremes decrease | **Yes** — exact-zero 17→0, 19→0 |
| mature performance not harmed | **Yes** — 10+ bucket 0.2468 → 0.2448 |
| coverage improves or is understandable | **Yes** — full coverage, explained |

By the stated criteria the estimator is **promising**. The Epic was not tuned
until it won; the one configuration that "wins" hardest on Brier (k→∞) is
identified as **degenerate and rejected**.

---

## 19. Limitations

1. **The model has almost no discrimination.** AUC ≈0.535 for every arm
   *including baseline*; a constant predictor (0.2496) beats raw POISSON_V1
   (0.2615). Shrinkage fixed the estimator, **not** the model's predictive power.
2. **Brier is the wrong objective here.** Minimising it drives `k → ∞` and
   destroys spread. `k` was therefore chosen by method of moments, not by score.
3. **2023 is not pristine** — its headline Brier was seen in Epic 2B.3 (§8).
4. **`k_league` is nearly inert** (0.2552–0.2554); its value is weakly evidenced.
5. **"Promoted" means "not in this competition last season"** — participation-based,
   since ESPN exposes no promotion field.
6. Five leagues, top flights only; no cross-country promotion evidence.
7. `k` estimated on 2018–2019 only; no per-league or per-season `k`.
8. Defence/attack asymmetry (≈2×) is measured but not modelled beyond two `k`s.

---

## 20. Recommendation for Epic 2D

**Do not tune this estimator further.** The ceiling is not in the priors —
it is in the inputs. AUC ≈0.535 says venue-split rates barely separate BTTS from
no-BTTS, and no amount of shrinkage creates signal.

Priorities, in order:

1. **Establish a discrimination baseline first.** Report AUC alongside Brier
   permanently, and record the constant-predictor score in the harness. A model
   that loses to a constant should never have looked acceptable, and only the
   coverage-corrected intersection revealed it.
2. **Dixon-Coles / bivariate dependence** — now unblocked, and it attacks
   *discrimination*, which is the real deficit. Independent-Poisson BTTS is
   known to be biased; that is a modelling error shrinkage cannot touch.
3. **Team-level attack/defence parameters** (a proper hierarchical fit) rather
   than four independent venue rates — the ≈2× defence asymmetry suggests the
   current parameterisation is inefficient.
4. **Recalibration deliberately deferred** (Part 11 forbade it here). Note that
   shrinkage already captured most of the available calibration gain; a
   recalibration layer would now add little and could mask the real problem.

Adopt `POISSON_V1_SHRUNK_V1` as the **input estimator** — GG-028 is genuinely
fixed and coverage genuinely improves — but do **not** present it as a materially
better forecaster. It is a better-behaved one.
