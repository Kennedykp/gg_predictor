# Epic 2E — Can genuinely NEW information beat the goal-count ceiling?

**Status:** ✅ **APPROVED 2026-08-16. STAGE 0 ONLY authorised.**
Stage 1 remains unauthorised and must not be built until Stage 0's gate is reported and
separately approved.

### Approved decisions

| # | Decision | Ruling |
|---|---|---|
| 1 | Direction | **SHOTS-FIRST approved** — smallest rigorous next experiment |
| 2 | Holdout | **2025**; 2017 only if actual contamination is discovered |
| 3 | Stage 0 | **Hard stop-gate**; threshold **0.60 pre-registered and immovable**; judge on AUC + CI + n, never the point estimate alone |
| 4 | Odds logging | Approved as **data collection only** — no gating change, no decision change, not used in this experiment |
| 5 | `BURNED_SEASONS` | Define a **complete 2E-specific** constant incl. 2021, 2022, 2024; **do not edit Epic 2D's file** |
| 6 | `competitor.form` | **BANNED**; a regression test must *prove* research cannot consume it |
| 7 | Production | **Untouched**; `espn.py` may be studied but gains no shot parsing |

**Reporting obligation after Stage 0:** exact AUC and CI, comparison against the ~0.568
goal-count ceiling, explicit **PASS / FAIL / INCONCLUSIVE**, then **STOP** — no silent
continuation into a full model in either direction.

---

## STAGE 0 RESULT — **FAIL.** Stage 1 is not to be built.

Artifacts: `research/epic2e_results/stage0_eng1_replication_LEAKY.txt`,
`research/epic2e_results/stage0_all_leagues_LEAKY.txt`. Development seasons 2018–19 only;
**the 2025 holdout was never loaded** (`build_dataset` raises if a caller would pull it in).

| Arm (all deliberately leaky) | eng.1, n=754–760 | 5 leagues, n=3,530–3,656 |
|---|---|---|
| Goal-count ceiling (2D's probe, re-measured) | 0.5654 | 0.5838 |
| Shot-strength, in-sample | 0.5665 `[0.5277, 0.6073]` | 0.5737 |
| **Actual shots on target (raw)** | **0.7268** `[0.6900, 0.7640]` | **0.7244** `[0.7084, 0.7416]` |
| **Confound-controlled (non-scoring SOT)** | **0.5278** `[0.4852, 0.5689]` | **0.5121** `[0.4940, 0.5319]` |

**The raw 0.72 is not a discovery — it is a definitional artefact, and it is the reason this
epic needed a control arm.** Every goal *is* a shot on target, so `SOT == 0` implies that team
did not score, with certainty. The raw arm was reading part of the label: 182 exact-0.0
predictions on the five-league run are fixtures where it was told the answer.

Remove the scoring shots and ask only about chances *created but not converted* — the quantity a
pre-match model could actually forecast — and the ceiling falls to **0.5121–0.5278**, at or
**below** the goal-count ceiling it was supposed to beat. On five leagues it is a significant
**degradation** (ΔAUC −0.0718, CI `[−0.0963, −0.0475]`). Knowing every team's shot profile
perfectly, in-sample, is also indistinguishable from goal counts (ΔAUC −0.0101, CI straddling 0).

**Conclusion: H₁ is rejected and H₀ survives.** Epic 2D's suspicion is now measured rather than
suspected — the ~0.568 ceiling is not a limitation of *goal counts*; it is the irreducible
randomness of **conversion**. Shots cannot help because the noise is in the finishing, not in
the rate estimate. Since xG is a weighted function of shots, this result bounds xG too, which
retires that direction without buying a provider.

**What this does NOT claim.** Only the shot channel is closed. The market direction is untested
(§12) — a price aggregates lineups, injuries and motivation, which is information of a different
kind, not a better estimate of the same kind. It remains unmeasurable until odds are logged.

**Strategic question inherited from Epic 2D / GG-031:**

> Goal counts impose a discrimination ceiling of ≈0.568 AUC. Can genuinely new
> information discriminate beyond it — or is the ceiling a property of football?

---

## 0. What this design phase established (evidence, not assumption)

Every number below was measured **read-only** against the existing Epic 2A cache
(`research/.cache`, 140 files, 605 MB, all `retrieved_at = 2026-08-09`, zero network calls).
The probes were scratch scripts in `/tmp`, deliberately outside the repo.

Two findings change the plan materially, and one of them is a **leak**.

### Finding 1 — shot-level data already exists on disk, and is per-match

`docs/EPIC_2A_COLD_START_RESEARCH.md:1506` states shots/xG are *"not available from ESPN
endpoints in use"*. **That is incorrect for shots.** Every cached `scoreboard` payload carries a
per-competitor `statistics` block at
`payload.events[].competitions[0].competitors[].statistics`. It is simply **never parsed** —
`espn.py` contains no reference to `shot`, `possession` or `corner`.

Nine fields, present on 52,181 of 53,934 cached events:

| ESPN name | Meaning | Notes |
|---|---|---|
| `totalShots` | shots | the volume signal |
| `shotsOnTarget` | shots on target | the quality-filtered signal |
| `possessionPct` | possession % | also the availability sentinel (below) |
| `wonCorners` | corners won | territorial proxy |
| `foulsCommitted` | fouls | |
| `shotAssists` | key passes | chance creation |
| `goalAssists` | assists | |
| `totalGoals` | goals | **used only to validate the block, never as a feature** |
| `appearances` | players used | semantics unconfirmed — excluded |

**Validated as per-match, not cumulative.** This was the decisive check: if the block were a
season aggregate it would be a straight LEAK-001 repeat. Restricted to blocks with
`possessionPct > 0`, the block's own `totalGoals` equals that fixture's actual scoreline in
**68,393 of 68,568 competitor-blocks (99.74%)**. Corroborating internal consistency:

- possession sums to 100 across the two competitors in **34,259 of 34,264** pairs (5 sum to 99, rounding)
- `shotsOnTarget > totalShots` occurs **0** times
- the 175 disagreements are concentrated in **2006 ger.1**, outside any proposed partition

### Finding 2 — `competitor.form` is CONTAMINATED and must be banned

A 2025 fra.1 fixture played **2025-08-15** (matchday 1) carries:

```
form = 'LWLWW'          <-- five results, before five matches existed
records = [('All Splits', '1-0-0')]   <-- one match played
```

`form` is populated **as of cache retrieval (2026-08-09)**, i.e. end of season. It is
future information wearing a plausible name — the exact shape of LEAK-001. It is banned by
this protocol and the ban will be enforced by a regression test, not by intention.
`records` looks point-in-time at matchday 1 but is **not proven** across a season, so it is
also excluded until separately verified. Absence of proof is not proof.

### Finding 3 — the market direction cannot be measured at all today

| Source | Verdict |
|---|---|
| The Odds API (`shared/odds.py:38`, `odds_api.py:13`) | live pre-match only; **no historical endpoint**; never persisted to disk |
| ESPN `competition.odds` | key present on all 53,934 events, but **53,915 are literal `null`** |
| The 19 populated entries | **all eng.2 2015**, and all **1X2** (`homeTeamOdds`/`drawOdds`/`awayTeamOdds`) — **not BTTS** |

So there is **no point-in-time BTTS price anywhere in this project**, for any season. Direction 1
is not rejected on merit — it is **unmeasurable** until odds are logged going forward (LEAK-001)
or bought from a vendor. n=19, wrong market, wrong league, wrong season cannot answer a
strategic question. See §12.

### Finding 4 — xG and lineups are genuinely absent

Zero occurrences of `xG`/`expectedGoals` anywhere in the cache. No lineup field.
`competition.details` exists (goal/card events with a clock) but is **in-match**, so it is
usable only as history for *prior* fixtures, never for the target. xG would require a new
provider (Understat/FBref) — out of scope for a first experiment, and largely redundant:
xG is a weighted shot model, and shots + shots on target are its raw inputs.

---

## 1. The hypothesis

**H₁ (primary).** Pre-kickoff **shot-volume information** — shots and shots on target created
and conceded, aggregated point-in-time — ranks BTTS fixtures better than any model built on
goal counts alone, and **exceeds the 0.568 goal-count ceiling**.

**H₀ (the null Epic 2D's result makes plausible).** It does not. BTTS ranking is limited by the
**irreducible randomness of shot conversion**, so the ceiling is a property of football rather
than of the feature set, and no new pre-match feature moves it.

**Mechanism, stated so it can be wrong.** A team takes ~12 shots and scores ~1.4 goals per match.
Shots therefore supply roughly an order of magnitude more observations per match of the same
underlying chance-creation process. If AUC ≈0.54 is caused by **estimation noise in the rate**,
shots should reduce it and AUC should rise. If it is caused by **conversion variance**, shots
cannot help, because the noise is in the goal-generating step and not in the estimate.

Epic 2D's ceiling bounds only *goal-count* estimation. Shots are new information and are
**not bounded by 0.568** — which is precisely why this is the question worth asking, and why a
negative result here is far stronger than a negative result within the goal-count class.

---

## 2. Candidate data sources

| # | Source | Available? | Cost | Decision |
|---|---|---|---|---|
| 1 | **ESPN per-match shot statistics (cached)** | ✅ already on disk, validated per-match | zero network | **SELECTED** |
| 2 | The Odds API — BTTS price | ❌ live-only, no history | months of forward logging | deferred (§12) |
| 3 | ESPN embedded `odds` | ❌ 19 rows, 1X2, eng.2 2015 | — | rejected: cannot answer |
| 4 | xG (Understat/FBref) | ❌ not in repo | new provider + licence | deferred; shots are its inputs |
| 5 | Lineups / injuries | ❌ no field | new provider | out of scope |
| 6 | `competitor.form` | ⚠️ **contaminated** | — | **BANNED** (§0 Finding 2) |

---

## 3. Point-in-time availability

The cache is frozen at `retrieved_at = 2026-08-09`, so no refetch can inject future data.
Usable = **both** competitors have `possessionPct > 0` in a completed match.

| Season | Completed (5 leagues) | Usable stats | Coverage |
|---|---|---|---|
| 2018 | 1,828 | 1,822 | 99.7% |
| 2019 | 1,725 | 1,710 | 99.1% |
| 2021 | 1,826 | 1,739 | 95.2% |
| 2022 | 1,822 | 1,654 | 90.8% |
| 2023 | 1,752 | 1,699 | 97.0% |
| 2024 | 1,752 | 1,718 | 98.1% |
| **2025** | **1,751** | **1,751** | **100.0%** |

**Missingness rule (GG-001 discipline).** `possessionPct == 0` in a played match is physically
impossible, so it is the missing-as-zero signature — **not** an observation of zero possession.
Such a block is `UNAVAILABLE`; the fixture is **refused**, never imputed. Refusal costs coverage,
which the fair intersection then measures rather than hides.

`HistoricalMatch` (`domain/historical.py:213-226`) carries **no** shot fields. The new statistics
will therefore live in a **research-only sidecar keyed by `event_id`**, leaving the Epic 2B.2
contract and its schema version untouched.

---

## 4. Burned seasons

| Season | Burned as a TARGET by | Status |
|---|---|---|
| 2018, 2019 | 2B.3 baseline · 2C search · 2D development | 🔴 burned |
| 2020 | 2B.3 · 2C validation | 🔴 burned |
| 2021, 2022 | 2D validation | 🔴 burned |
| 2023 | 2B.3 · **2C final test** | 🔴 burned |
| 2024 | **2D holdout** | 🔴 burned |
| **2025** | never scored in any epic | 🟢 **untouched** |

⚠️ **Two integrity notes I am obliged to raise rather than quietly rely on.**

1. `BURNED_SEASONS` in `research/epic2d_experiment.py:121-126` lists only 2018/2019/2020/2023 —
   it **omits 2021, 2022 (2D's own validation) and 2024 (2D's own holdout)**. Epic 2E will define
   its own complete constant. **Epic 2D's file will not be edited.**
2. 2025 appears in `docs/EPIC_2A_COLD_START_RESEARCH.md:203` — Epic 2A recorded its
   *completeness* and *league-average goals* (eng.1 2025: 380/380, 1.375). That is **data-quality
   metadata, not BTTS outcome inspection**, so I judge 2025 still clean as a holdout. It is
   stated here so you can overrule me if you disagree. §11 offers a fallback if you do.

---

## 5. Untouched holdout

**2025 — the five production leagues, run exactly once.**

Chosen because it is the only season never scored as a target, and it happens to have the best
stat coverage in the entire cache (100.0%, 1,751/1,751). Its prior season 2024 is fully cached,
so point-in-time history is available from matchday 1. Loading 2024 **as history** is not
contamination — those matches precede every 2025 kickoff, which is the whole point of a
point-in-time prior. "Burned" means *inspected as a target*.

---

## 6. Baseline

Not a straw man. Three reference points, all reported together:

| Reference | Value on 2024 | Role |
|---|---|---|
| `POISSON_V1_RAW` | AUC 0.5368 | frozen production-equivalent baseline |
| **`C1_MAHER`** | **AUC 0.5430** | **2D's best honest arm — the bar to beat** |
| `ORACLE_LEAKY_CEILING` | AUC 0.5679 | the goal-count ceiling to exceed |
| constant predictor | Brier 0.2469 | **still beats every model** (GG-029) |

Beating `POISSON_V1_RAW` alone proves nothing; C1_MAHER already does that and was still not
promoted. The constant-predictor Brier is reported on every line so no arm can look acceptable
while losing to a model that says nothing.

---

## 7. Primary metric

**ΔAUC on the fair intersection, with a paired bootstrap 95% CI** — via the existing
`domain/discrimination.py` (`paired_auc_delta`, 2,000 iterations, seed 20260815) and
`domain/comparison.compare`, which computes the intersection *before* summarising either arm.

- **AUC is primary** because it depends only on ranking, and is invariant to the monotone
  flattening that Brier rewards (GG-029).
- **Brier and log loss are reported, never optimised.** Recalibration is explicitly out of
  scope: a monotone transformation cannot change ranking, so it would buy Brier and no skill.
- **Parameter selection is on out-of-sample predictive likelihood of GOALS**, on development
  only — the Epic 2D discipline. Never on BTTS Brier, never on AUC, never on the holdout.
  Selecting on the likelihood of *shots* would optimise the proxy instead of the target.

---

## 8. Evaluation protocol

| Partition | Seasons | Inspection |
|---|---|---|
| Development | 2018, 2019 | repeatedly (already burned) |
| Validation | 2021, 2022 | **once**, after dev is frozen |
| **Holdout** | **2025** | **exactly once**, after all parameters are frozen |

- **Rolling origin comes free**: `evaluation_harness.replay` rebuilds each target's history from
  matches with kickoff strictly `<` the target's own kickoff. No second cutoff is written.
- **Reuse, do not rebuild**: `load_season` (production ESPN parser + 2B.1 season integrity +
  2B.2 eligibility), `domain.comparison`, `domain.discrimination`, `domain.evaluation.summarise`,
  `domain.goal_models`. A private reader would let 2E score fixtures production would reject.
- **Same BTTS mapping for every arm** (`btts_independent`, asserted bit-identical to
  `poisson.calculate_gg_probability`), so any AUC difference is attributable to the
  **information**, not to a different probability formula.
- Results written to `research/epic2e_results/`, one file per stage, committed as evidence.

### Staged, so a dead end costs a day and not a fortnight

**Stage 0 — the shot-informed ceiling probe (do this FIRST; it can end the epic).**
Mirroring 2D's decisive move: a deliberately **leaky** probe fitted on the full dataset
*including the target season and the target fixture itself*, using shot profiles. It answers
"if we knew every team's shot profile perfectly, how well could we rank BTTS?"

> **If that leaky ceiling does not clear 0.568, H₁ is dead** — no honest estimator can exceed a
> ceiling that already cheats, and no amount of model quality can rescue it. The direction closes
> immediately, cheaply, and with the same force as Epic 2D's conclusion. This is the single
> highest-value number in the design and it is also the cheapest.

Quarantine is mandatory and test-enforced: `model_id` prefixed `ORACLE_LEAKY`, never registered
in the harness registry, `"NOT A MODEL"` in the docstring.

**Stage 1 — honest arms** (only if Stage 0 clears the ceiling): shot-based rate estimation fitted
strictly pre-kickoff, compared against `POISSON_V1_RAW` and `C1_MAHER` on fair intersections,
development → validation → holdout, in that order.

---

## 9. Leakage risks (and the control for each)

| # | Risk | Control |
|---|---|---|
| 1 | **`competitor.form` is end-of-season** (proven, §0) | **banned**; regression test asserts no arm reads it |
| 2 | `competitor.records` unverified point-in-time | excluded until proven; not assumed safe |
| 3 | Target's own statistics entering its own features | strict `kickoff <` cutoff in `replay`; test asserts no future match reaches the adapter |
| 4 | `details` (goal/card clock) is in-match | prior completed fixtures only |
| 5 | Same-matchday ordering (earlier result folded in) | strict `<`, not `<=` — mutation-tested in 2D |
| 6 | **Missing-as-zero** (`possessionPct == 0`) | treated as UNAVAILABLE → refuse, never impute (GG-001) |
| 7 | Odds reaching the evaluator | `tests/regression/test_evaluation_leakage.py` firewall stays intact |
| 8 | Private parser admitting rejected fixtures | production parser via `load_season` only |
| 9 | Holdout reuse / peeking | 2025 run once, after freeze; complete `BURNED_SEASONS` in 2E |
| 10 | Ceiling probe mistaken for a model | `ORACLE_LEAKY` prefix, unregistered, test-asserted |
| 11 | Refetch pulling post-hoc data | cache frozen at 2026-08-09; offline, zero network |
| 12 | Selecting on the holdout | selection on development goal-likelihood only |

---

## 10. Promotion / rejection criteria (pre-registered, before any number is seen)

**Close the direction (report a negative result, as 2D did) if:**
- Stage 0's **leaky** ceiling CI includes or falls below **0.568** → new information adds nothing
  even with perfect hindsight. Publish and stop.

**Continue to Stage 1 only if:** the leaky ceiling is materially above the goal-count ceiling
(pre-registered threshold **≥ 0.60**), leaving room an honest model could occupy.

**Label an honest arm PROMOTION-CANDIDATE only if ALL hold on the 2025 holdout:**
1. ΔAUC vs **C1_MAHER** positive with **95% CI excluding zero** on the fair intersection;
2. the same direction already seen on validation, before the holdout was touched;
3. coverage loss ≤ **2pp** vs C1_MAHER;
4. **zero** exact-0.0 / exact-1.0 predictions (GG-028 must not return);
5. Brier no worse than C1_MAHER's — improvement in ranking must not be bought with calibration.

**Explicitly disqualifying:** a Brier or log-loss improvement with ΔAUC CI containing zero. That
is the GG-029 trap and it will be reported as a negative result, not a win.

**Even if all five criteria pass, nothing is promoted in this epic.** 2E ends with an answer and
a recommendation. Production promotion is a separate epic with its own approval. `poisson.py`,
`config.py`, `filters.py`, `decision.py`, `shared/odds.py`, odds gating and `run3/` are **not
touched** — verified by `git diff` at the end, as in 2C and 2D.

---

## 11. Expected sample size — and is it enough?

Holdout 2025, five leagues, counting **availability only** (no outcome was computed — the design
is unapproved, so nothing that could constitute peeking was calculated):

| Requirement (within-season floor) | Fixtures |
|---|---|
| Completed | 1,751 |
| Usable stats (both teams) | **1,751 (100%)** |
| Both teams ≥3 prior usable matches | 1,606 |
| Both teams ≥5 prior usable matches | 1,510 |
| Both teams ≥8 prior usable matches | 1,367 |

These are conservative: they ignore 2024 history, which `replay` supplies and which lifts
early-season evidence.

**Is it sufficient? Partly — and the limit must be stated up front.** Epic 2D's ΔAUC CI at
n=1,698 was `[−0.0123, +0.0251]`, i.e. a half-width of **≈±0.019**. So at n≈1,500–1,750 this
design can resolve:

- ✅ a **material** effect (ΔAUC ≳ **0.02–0.025**) — which is the size that would actually matter,
  and the size implied by clearing 0.568 from 0.543;
- ❌ a **small** effect (ΔAUC ≈ 0.005–0.01) — this will correctly return INDISTINGUISHABLE.
  A shot model that buys 0.008 AUC is not worth promoting anyway.

**If more power is wanted**, two honest options, both to be fixed *before* the holdout is touched:
(a) add `eng.2`/`ger.2` as target leagues (≈+860 fixtures/season, already cached); (b) pool
development+validation for the *estimation* question while keeping 2025 as the single holdout.
Pooling burned seasons into the holdout is **not** an option.

**Fallback if you rule 2025 contaminated** (§4 note 2): use **2017** as the holdout — never a
target in any epic, 99.7% stat coverage, with 2016 cached as history. Weaker (older football,
2016 history coverage 79%) but honest.

---

## 12. Why the market direction is deferred, not dismissed

The market question — *does the BTTS price discriminate better than 0.568?* — is arguably the
more valuable one, because a market probability aggregates lineups, injuries, motivation and
money. GG-031 names it explicitly. It is deferred **only** because it cannot be measured today:
there is no historical BTTS price in this project, and none obtainable without a vendor or months
of forward logging.

**Recommended parallel action, cheap and non-blocking:** begin **persisting a pre-kickoff BTTS
odds snapshot** for upcoming fixtures — an append-only log of `(fixture, market, price,
captured_at)`, written before kickoff and never rewritten. It answers nothing this month, but it
is the only way the question becomes answerable at all, and it directly attacks the last open row
of LEAK-001. It changes **no** gating, no thresholds and no decision logic — it only records what
the market said, when.

---

## 13. What I need from you

1. **Approve the direction** — shots-first (measurable now) over market-first (blocked)?
2. **Approve 2025 as the holdout**, given §4 note 2 — or choose the 2017 fallback?
3. **Approve Stage 0 as a hard gate** — if the leaky shot ceiling misses 0.60, I stop and report
   a negative result rather than building anything?
4. **Approve the odds-logging side action** in §12 (recording only, no gating change)?

No code will be written until these are settled.
