# Epic 2B.3 — Point-in-Time Evaluation Harness

## Objective

Build the referee, not a model.

Epic 2B.2 produced a season-verified historical dataset. This Epic builds the machinery that
answers one question about it:

> Given a probability produced using only pre-kickoff information, how good was it once the
> real result became known?

Nothing here changes POISSON_V1, and nothing here decides what to do about the answer. The
measurement had to exist before the model work, because Epic 2C cannot demonstrate that
cold-start handling helps without a baseline it did not itself move.

## What This Epic Deliberately Does Not Do

| Not done | Why |
|---|---|
| Cold-start shrinkage, empirical Bayes, pseudo-matches | Epic 2C. Inventing one here would move the baseline 2C must beat. |
| Any change to `poisson.py` | The model under measurement must not move while being measured. |
| Odds, edges, thresholds, staking, profitability | Blocked by LEAK-001. Odds remain present-day only. |
| A "best model" verdict or parameter tuning | This Epic reports. It does not decide. |
| Dixon-Coles or any second model | Epic 2D. The registry is ready for it; the model is not in scope. |
| A train/validation/test split applied by default | A protocol decision, not a utility default. `SeasonPartition` exists but is never auto-applied. |

## Architecture

```
historical dataset (2B.2, season-verified)
        |
        v
  target fixture T
        |
        +--> history = { m : m.kickoff < T.kickoff }   <- ONE cutoff, shared with 2B.2
        |                       |
        |                       v
        |              model adapter (POISSON_V1 -> production functions)
        |                       |
        v                       v
  actual BTTS outcome  <--  P(BTTS YES) or explicit refusal
        |
        v
  PredictionRecord --> Brier / log loss / calibration / coverage
```

Four files, each with one job:

| File | Responsibility |
|---|---|
| `domain/evaluation.py` | Contracts and metrics. No model mathematics, no I/O, no odds. |
| `evaluation_harness.py` | Replay, model adapters, registry, artifacts. |
| `run_evaluation.py` | CLI entry point. |
| `research/evaluate_baseline.py` | Offline run over the Epic 2A payload cache. Research tooling. |

## The Point-in-Time Rule

```
history = { m in dataset : m.kickoff < T.kickoff }
```

Strictly `<`, enforced by `domain.historical.matches_before` — the same function the 2B.2
dataset layer uses. A second cutoff implementation in the harness could drift from the
first, and the drift would be invisible: both would produce plausible numbers.

Four consequences follow from the rule alone, with no special-casing:

- the target cannot see its own result (its kickoff is not `<` itself)
- no later fixture from the same season contributes
- no future season contributes
- a fixture kicking off at exactly `T` contributes nothing

Cross-competition leakage is a **separate** guard (`same_competition_only`, default on).
Season is deliberately *not* filtered: a March fixture may legitimately learn from the
previous September, and restricting history to the current season is a modelling choice, not
a leakage guard.

History is recomputed per target. No season-wide aggregate is built once and reused — that
is the specific mistake that makes an evaluation look excellent and mean nothing.

## Three Load-Bearing Separations

**Outcome vs unknown.** A fixture with no usable result is UNEVALUABLE. It never becomes
BTTS=0. `0-0` is a real goalless draw; "no score recorded" is an absence. Scoring the
absence as a goalless draw fabricates an observation and silently rewards models that
predict low probabilities.

**Quality vs coverage.** "How good were the predictions it made" and "how often could it
predict at all" are different questions, and `MetricSummary` always carries both. A superb
Brier score over 4% of fixtures is not a good model.

**Model vs reference.** `REFERENCE_BASE_RATE` is a yardstick, not a competitor. A Brier
score of 0.24 is good or bad only relative to what a naive predictor achieves on the same
fixtures. It is deliberately untuned — no recency weighting, no home/away split, no
shrinkage — because a tuned reference stops being a floor.

## No Zero Substitution, No Default Probability

A model returns a probability **or** a reason. Never both, never neither; `__post_init__`
enforces it rather than trusting callers.

The refusal reasons are counted per target and reported, never averaged away:

| Reason | Meaning |
|---|---|
| `INSUFFICIENT_HISTORY` | Model could not assemble its inputs from pre-kickoff history. |
| `NO_RESULT` | Fixture has no usable scoreline (postponed, abandoned, cancelled). |
| `NOT_MODEL_ELIGIBLE` | Target is not ordinary league play (playoff, per 2B.1/2B.2). |
| `MODEL_RETURNED_NONE` | The model itself rejected its inputs. |
| `MODEL_ERROR` | The adapter raised. Reported, not fatal to the run. |

`INSUFFICIENT_HISTORY` is the honest description of POISSON_V1 in August. Epic 2C exists to
change that number, so it must be measurable now rather than papered over with a league
average.

## Metrics

**Brier score** — `(1/N) Σ (p − y)²`. Range [0,1], lower better. Returns `None` for an empty
set: a mean of nothing is not 0.0, and 0.0 would read as perfect.

**Log loss** — `−(1/N) Σ [y·log p + (1−y)·log(1−p)]`. The `LOG_LOSS_EPSILON = 1e-15` clamp
applies **only inside the logarithm**. The stored and reported probability is always the
model's original value. Clipping the reported number would be editing a model's output to
improve its own score — the difference between a referee and an accomplice.

**Calibration** — bins are `[lower, upper)`, with the final bin closed at both ends
(`[0.90, 1.00]`). Without that clause `p=1.0` falls outside every bin and vanishes from the
table while still counting in the Brier score, so the two reports would silently disagree.
Empty bins are retained with `count=0`.

**`accuracy_at_half`** — diagnostic only, never for model selection. A league where 55% of
matches are BTTS is "predicted" at 55% accuracy by a constant, and accuracy cannot
distinguish a well-calibrated 0.51 from an overconfident 0.99.

## POISSON_V1 Is Called, Not Reimplemented

`PoissonV1Adapter` derives the five inputs with `domain.poisson_inputs` and calls
`poisson.calculate_gg_probability`. Both are the functions the live pipeline uses. A copied
formula would let the evaluated model and the shipped model diverge with no failing test —
the worst of both worlds, since the number reported would describe a model that was never
deployed.

Two bridges were needed, and each has a correctness argument:

- **`to_team_records`** re-expresses a fixture from one team's point of view. For the away
  side `goals_for` and `goals_against` swap. Getting that backwards would not raise; it
  would silently score every away team with its opponents' figures.
- **`to_league_records`** emits HOME perspective only. `derive_league_baseline` divides by
  `2 × fixtures` because it was written for per-team schedules where each fixture appears
  twice; the 2B.2 dataset stores each fixture once, so home-only reproduces exactly the
  input that function expects. The established 1.375 EPL cross-check still holds.

## Odds Firewall

`tests/regression/test_evaluation_leakage.py` asserts at import level that no evaluation
module reaches odds, prices, edges, thresholds or `decision.py`. This is enforced rather
than documented because the failure mode is attractive: a backtest of *recommendations*
would look like the most valuable output of the project, and would be invalid, since
`decision.py` compares against a market that did not exist at the historical kickoff.

Probability quality is a football question and is measurable today. Betting value is not,
and LEAK-001 stays open.

## Representative Offline Run

Zero network requests: `research/evaluate_baseline.py` replays the Epic 2A payload cache
through the production ESPN parser and the 2B.2 builder.

Scope: 5 production leagues × 4 seasons (2018 normal, 2019 COVID-extended, 2020
contamination boundary, 2023 recent normal) → **7,234 records from 20 league-seasons**,
40 cache reads, 0 network requests.

| | POISSON_V1 | REFERENCE_BASE_RATE |
|---|---|---|
| targets | 7,234 | 7,234 |
| scored | 6,955 | 7,025 |
| coverage | 0.9614 | 0.9711 |
| **Brier** | **0.2657** | **0.2479** |
| log loss | 1.0901 | 0.6890 |
| mean predicted | 0.4724 | 0.5356 |
| observed BTTS | 0.5431 | 0.5431 |

**POISSON_V1 scores worse than a naive base rate on both metrics.** That is the finding, and
it is reported rather than adjusted.

### Why — and it is not a harness bug

This was checked before being written down. `poisson.calculate_gg_probability` called
directly with `away_goals_scored_away=0.0` returns `lambda_away = 0.0` and
`gg_probability = 0.0` exactly. A team whose only prior away match was a 0-goal loss is
therefore assigned a **0% chance** of scoring. In eng.1 2019 alone, 17 predictions were
exactly 0.0 — and BTTS actually occurred in 11 of them. Under log loss those are punished at
the epsilon clamp, which is what inflates 1.09.

The Brier score by evidence bucket (prior home-venue matches for the home side) isolates it:

| Prior venue matches | n | Brier |
|---|---|---|
| 1–2 | 40 | **0.4241** |
| 3–5 | 60 | 0.2687 |
| 6–9 | 80 | 0.2611 |
| 10+ | 180 | **0.2555** |

*(eng.1 2019; the aggregate pattern holds across the wider run.)*

With 10+ matches of evidence, POISSON_V1 is roughly competitive with the base rate. With 1–2
matches it is catastrophic. The aggregate deficit is dominated by thin-evidence fixtures, not
by the Poisson formula being wrong where it has data.

### Calibration (POISSON_V1)

| bin | n | predicted | observed | gap |
|---|---|---|---|---|
| [0.00, 0.10) | 161 | 0.006 | 0.534 | **+0.528** |
| [0.10, 0.20) | 120 | 0.157 | 0.525 | +0.368 |
| [0.20, 0.30) | 379 | 0.261 | 0.520 | +0.258 |
| [0.30, 0.40) | 1135 | 0.356 | 0.497 | +0.141 |
| [0.40, 0.50) | 1990 | 0.452 | 0.528 | +0.077 |
| [0.50, 0.60) | 2022 | 0.546 | 0.553 | +0.007 |
| [0.60, 0.70) | 936 | 0.638 | 0.613 | −0.025 |
| [0.70, 0.80) | 158 | 0.735 | 0.589 | −0.146 |
| [0.80, 0.90) | 46 | 0.834 | 0.565 | −0.269 |
| [0.90, 1.00] | 8 | 0.927 | 0.500 | −0.427 |

Textbook overconfidence at both extremes: the model is well calibrated in the 0.50–0.60 band
where most of its mass sits, and increasingly wrong the more certain it becomes. The extreme
low bins are the zero-lambda mechanism above.

**This is a measurement, not a mandate.** Whether to shrink small samples, add a prior, or
floor lambda is Epic 2C's decision, made deliberately — not as a side effect of this Epic.

## Mutation Testing

Two guards were deliberately broken to confirm the tests protect the invariant rather than
describe the implementation.

| Mutation | Result |
|---|---|
| Replace the strict cutoff with whole-season history (`m.season == target.season`) | **4 tests failed**: `test_exactly_at_kickoff_is_excluded`, `test_after_kickoff_is_excluded`, `test_history_is_recomputed_per_target_not_reused`, `test_does_not_see_the_season_final_rate` |
| Make `btts_outcome` return `NO` for a missing result instead of `UNKNOWN` | **5 tests failed**, incl. `test_missing_score_is_unknown_not_no[None-None]`, `test_incomplete_fixture_is_unknown`, `test_no_result_target_is_unevaluable_not_scored_as_no` |

Both mutations were reverted; the suite is green at the reported numbers.

## Validation

| Check | Result |
|---|---|
| `pytest` | **1528 passed, 2 skipped** |
| `ruff check .` | **All checks passed** |
| `mypy` | **Success: no issues found in 29 source files** |
| POISSON_V1 golden regression + point-in-time | **48 passed** |
| Live network calls | **0** (evaluation is entirely offline) |

The 2 skips are pre-existing spec-agreement decisions (D1 data-source authority, D3
unwireable filters / GG-002-B), untouched by this Epic.

### Production safety

`git status` shows only new files. No existing production file was modified:

| File | State |
|---|---|
| `poisson.py` | unchanged — `3cf9f2a19604cac4…` |
| `config.py` (thresholds) | unchanged — `29f9d03627c29e05…` |
| `filters.py` | unchanged — `fd96f4eaacb9650b…` |
| `decision.py` | unchanged — `69e241abf05a92aa…` |
| `odds_api.py`, `shared/odds.py` | unchanged |
| `espn.py`, `main.py`, `analyze_all.py` | unchanged |
| `run3/` | untouched |

No cold-start mathematics exists in this branch.

## Files Created

| File | Purpose |
|---|---|
| `domain/evaluation.py` | Outcome/reason contracts, `PredictionRecord`, Brier, log loss, calibration, summaries |
| `evaluation_harness.py` | Replay engine, model adapters, registry, artifact writer |
| `run_evaluation.py` | CLI entry point |
| `research/evaluate_baseline.py` | Offline cache-replay evaluation (research tooling) |
| `tests/unit/test_evaluation_harness.py` | Contracts, metrics, adapters, cutoff behaviour |
| `tests/regression/test_evaluation_leakage.py` | Look-ahead and odds-firewall regressions |

## Remaining Risks

- **LEAK-001 stays open.** Odds are still present-day. Probability quality is measured;
  betting value is not, and a recommendation backtest remains invalid.
- **Coverage is not uniform.** ~4% of targets are unevaluable, concentrated in early-season
  fixtures. Model and reference therefore score slightly different fixture sets (6,955 vs
  7,025); the reference's lower bar admits more. Comparisons on the intersection would be
  stricter, and Epic 2C should do that when it claims an improvement.
- **Scope is 4 seasons × 5 leagues**, chosen as the 2B.2-verified boundary set. Wider history
  is available in cache but was not needed to establish the baseline.
- **No train/test split was applied.** Every number above is in-sample in the sense that no
  partition was held out. Epic 2D must state its own partition for model comparison.

## Recommended Next Epic

**Epic 2C — cold-start and small-sample handling**, with a specific target: the 1–2 match
evidence bucket at Brier 0.4241, and the zero-lambda mechanism that assigns 0% to teams
with a single goalless away match. The baseline to beat is now recorded and reproducible:
POISSON_V1 at Brier 0.2657 / coverage 0.9614, against a naive reference at 0.2479.

Two policy questions belong to 2C and are deliberately left open here:

1. Should lambda be floored, shrunk toward a prior, or should thin-evidence fixtures remain
   unevaluable? Each is a different product stance on refusing to predict.
2. Should coverage be bought at the cost of calibration? Raising coverage by predicting on
   thin evidence would, on this evidence, make the aggregate Brier worse.
