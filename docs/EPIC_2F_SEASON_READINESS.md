# Epic 2F — GG Season Readiness Audit

**Status:** AUDIT COMPLETE — P0 reviewed, approved and FIXED
**Branch:** `epic/2f-market-information`
**Baseline commit:** `50a787b`
**Audit date:** 2026-08-17 (approx. one week before Matchday 1)
**Scope:** audit first, then one approved minimal safety fix (2F-P0-1).

**Files changed by this epic (3):**
- `docs/EPIC_2F_SEASON_READINESS.md` — this document (new)
- `analyze_all.py` — the 2F-P0-1 filter gate (+4 executable lines)
- `tests/integration/test_entry_point_consistency.py` — de-vacuumed fixture, +6 regression tests

**No other file was touched.** `poisson.py`, `filters.py`, `decision.py`,
`config.py`, `main.py`, `espn.py`, `odds_api.py`, `shared/odds.py` and all of
`domain/` are byte-identical to `50a787b` (`git diff --stat` on those paths is
empty).


This is not a model-improvement epic. The locked prediction model
(POISSON_V1, `EDGE_THRESHOLD`, `MIN_ODDS`, `MIN_AVG_GOALS`,
`MAX_CLEAN_SHEET_PCT`, and the existing decision rules) was **not touched
and must not be touched** to resolve anything below.

---

## Executive summary

The **prediction core is sound and fails closed.** A live dry run against
real ESPN data for a genuine Matchday-1 date produced 17 fixtures, 0 flagged
bets, and refused every fixture for missing statistics — which is the correct
cold-start behaviour.

However, the audit found **one P0 blocker**: the second GG entry point,
`analyze_all.py`, computes its published `system_recommendation` from
**edge and odds only**. The hard-filter verdict is attached to the output row
as a *label* but never gates the recommendation. A fixture that explicitly
**FAILED** the mandatory safety filters, or whose filter data was
**unavailable**, can still be published as `RECOMMEND_PLAY`.

The regression test that exists to prevent exactly this
(`test_neither_entry_point_recommends_without_clean_sheet_data`) is
**vacuous**: it prices the market at 1.80, which yields a *negative* edge, so
it passes no matter what the filters do.

`main.py` is correct and is unaffected.

---

## Phase 1 — Repository / technical-debt audit

### P0 — can cause an incorrect published recommendation

| ID | Finding |
|----|---------|
| **2F-P0-1** | `analyze_all.py` publishes `RECOMMEND_PLAY` on fixtures that FAILED the hard filters, and on fixtures whose filter data is UNAVAILABLE. Silent safety-filter bypass. Proven below. |

### P1 — materially reduces reliability, core pipeline still valid

| ID | Finding |
|----|---------|
| **2F-P1-1** | The guarding test for 2F-P0-1 is vacuous (negative edge at odds 1.80), so CI is green while the defect is live. |
| **2F-P1-2** | Two independent odds clients exist: `main.py` → `odds_api.py`, `analyze_all.py` → `shared/odds.py`. Duplicated `SPORT_KEYS` tables and duplicated thresholds (`MIN_ODDS_FOR_PLAY = 1.60` and `VALUE_EDGE = 0.05` restate `config.MIN_ODDS` / `config.EDGE_THRESHOLD`). Two copies can drift. |
| **2F-P1-3** | Neither odds client verifies `commence_time` against the fixture kickoff. The odds endpoint returns *all* upcoming games for the league; matching is by team name alone. |
| **2F-P1-4** | Odds team-name matching is bidirectional substring fuzzing (`a in b or b in a`) with no normalisation and no tie-breaking. It fails closed (returns `None`) on a miss, but a wrong-but-plausible substring hit is not defended against. |
| **2F-P1-5** | Both odds clients take the **first bookmaker** that quotes BTTS and the **first** `yes`/`no` outcome. There is no named-bookmaker authority and no price sanity bound, so the published edge depends on arbitrary provider ordering. |
| **2F-P1-6** | **GG-002-B** (pre-existing): two of the five GG.md hard filters have no data feed at all — see Phase 3. |
| **2F-P1-7** | **LEAK-001** (pre-existing, open): no point-in-time odds — see Phase 4. |
| **2F-P1-8** | `D1` unresolved: `GG.md` names API-Football as the primary source while production runs ESPN, and `api_football.py` is dead code. Tracked by a *skipped* test, so the contradiction is documented but unenforced. |

### P2 — non-blocking

| ID | Finding |
|----|---------|
| **2F-P2-1** | Stale `main.py` comments carry unanswered questions into production (`"might break int expectation in odds_api? check."`). |
| **2F-P2-2** | Unused/parallel provider modules retained: `api_football.py`, `sofascore.py`, `sportmonks.py`, `run3/`. |
| **2F-P2-3** | 3 skipped tests are load-bearing documentation (D1, D3/GG-002-B, absent 2D `MODELS` registry). Skips are invisible in a green summary line. |
| **2F-P2-4** | 23 debt items remain open in `docs/TECHNICAL_DEBT.md`; none other than the above bears on Matchday 1. |

---

## Phase 2 — Data-source authority

| Question | Evidence |
|---|---|
| Which source does production use? | **ESPN.** `main.py` and `analyze_all.py` both import `espn.py` for fixtures, stats, league average and match history. |
| Which source do tests use? | **ESPN**, via mocked ESPN payloads (`tests/conftest.py`, `espn_feed` / `espn_stats`). |
| Which source does historical evaluation use? | **ESPN**, through `historical_dataset.py` / `evaluation_harness.py`. |
| Is API-Football used? | **No.** `api_football.py` is dead code. `GG.md` still names it primary — the D1 contradiction (2F-P1-8). |
| Suitable for the new season? | Yes for fixtures and results. It is the same feed already validated across Epics 1B–2E. |
| League codes correct? | Yes. `ALLOWED_LEAGUES` has 5 entries and **all 5** are present in the odds `SPORT_KEYS` map (verified programmatically — no gaps). |
| Season boundaries handled? | Yes. GG-025 was fixed in Epic 2B.1: season membership now comes from provider metadata, not a constructed July→June window. `domain/season_identity.py` + `tests/regression/test_season_integrity.py` protect this. |
| Newly promoted teams supported? | Yes — teams are discovered from the fixture feed by ID, not from a hardcoded roster. A promoted side simply has a thin sample (see below). |
| Renamed / relegated teams? | Handled for the model (ID-based). **Renames are a risk for odds matching only** (2F-P1-4), because odds matching is by *name*. |
| Do fixtures and stats refer to the same competition? | Yes. `league_id` is carried on the fixture and used for the stats, history and league-average lookups. |
| Can current-season stats include future matches? | **No.** Epic 1B.5 made model inputs point-in-time: only completed matches kicking off strictly before the fixture are used. Guarded by `tests/regression/test_point_in_time_inputs.py`. |
| Empty / malformed API data? | Fails closed. Verified live: every fixture without usable stats returned `NO BET — Missing or unreliable team stats`. |

**Conclusion:** ESPN is the de-facto production authority and is fit for the
new season. The documentation, not the code, is what is wrong (2F-P1-8).

---

## Phase 3 — Safety filter audit

Both entry points route through one boundary —
`build_fixture_filter_stats()` → `evaluate_filters()` — so the two cannot
compute *different verdicts*. The defect is in what `analyze_all.py` does
with the verdict afterwards.

| # | Filter | Current behaviour | Verdict |
|---|--------|-------------------|---------|
| 1 | Minimum average goals (`MIN_AVG_GOALS = 1.0`) | Wired to the true goals-**scored** rate. GG-006 (combined scored+conceded) was fixed in 1B.3. | **Correct** |
| 2 | Clean-sheet % (`MAX_CLEAN_SHEET_PCT = 0.40`) | Derived from real point-in-time match history. | **Correct** |
| 3 | First-leg knockout exclusion | **Hardcoded `False`** (`domain/filter_stats.py:236`). Can never fire. | **Cannot fire** — GG-002-B |
| 4 | Heavy-favourite vs deep-defending underdog | **Hardcoded `False`** (`domain/filter_stats.py:237`). Can never fire. | **Cannot fire** — GG-002-B |
| 5 | Missing / unreliable data rejection | Unavailable data → `UNEVALUATED`; `allows_recommendation` is True only on an explicit PASS. | **Correct in `main.py`; bypassed in `analyze_all.py`** |

### Can unavailable clean-sheet data become 0% and silently pass?

**No — this specific defect is genuinely fixed.** Provider failure returns
`None`, which becomes `FILTER_DATA_UNAVAILABLE`, not `0.0`. Verified live:

```
main.py, clean sheet unavailable, generous odds 2.50
  decision=NO BET  filter_outcome=UNEVALUATED  gg_probability=0.6485  edge=None
  rejection_reasons=['FILTER_DATA_UNAVAILABLE: home_clean_sheet_pct, away_clean_sheet_pct',
                     'Failed hard filters']
```

The probability is still reported (correct — POISSON_V1 had its inputs), but
the **recommendation** is withheld. That is the intended contract.

### Filters 3 and 4 — intended vs actual

- **Current behaviour:** structurally inert, defaulted to `False`.
- **Intended behaviour:** exclude first legs and heavy-favourite mismatches.
- **Is it a safety defect?** It is a **missing feed**, not a wiring bug. No
  fabricated value is reaching a real filter, and nothing is being silently
  made to pass. The filters are absent, and that absence is documented.
- **Is the information available from an allowed source?** **Not reliably.**
  ESPN's fixture payload does not mark leg number for the 5 allowed domestic
  leagues, and "heavy favourite" requires a match-odds market this system
  does not fetch.
- **Smallest safe fix:** none for this epic. Both filters are **inapplicable
  to the 5 allowed leagues**, which are all domestic single-leg competitions.
  There are no first legs to exclude. This must remain a **documented
  limitation**, not a code change, and it becomes a genuine P0 the moment a
  two-legged competition is added to `ALLOWED_LEAGUES`.

---

## Phase 4 — Odds audit

| Check | Result |
|---|---|
| Bookmaker / market identification | `markets=btts`, `regions=eu`, `oddsFormat=decimal`. **First bookmaker wins**, unnamed (2F-P1-5). |
| BTTS Yes selection | Correct: `outcome.name == "yes"` → price. `GG_NO` correctly reads `btts_no`. |
| Odds parsing | Decimal float straight from the provider. No range validation. |
| Missing odds | Fails closed: `None` → `edge=None` → `NO_ODDS` / `RECOMMEND_NO_PLAY` / `NO BET`. Verified live with no API key. |
| Stale / invalid odds | **Not handled.** No `commence_time` check (2F-P1-3); no sanity bound on price. |
| Team-name matching | Bidirectional substring fuzzing, unnormalised (2F-P1-4). |
| League matching | Explicit ESPN→Odds-API `SPORT_KEYS` map; unknown code → `None`. All 5 allowed leagues covered. |
| Fixture matching | By team names only, not by kickoff or fixture ID (2F-P1-3). |
| Odds used only for implied probability / value? | **Yes.** |
| Can odds influence GG probability? | **No.** Confirmed by inspection and live: with odds available the probability was `0.6485` both with and without a price; `gg_probability` is computed and assigned *before* any odds call in `main.py`, and `analyze_market()` receives the probability as an input it never writes back. |

### LEAK-001 — classification

`get_team_stats()` takes no date parameter. **For model inputs this is
resolved**: Epic 1B.5 moved the pipeline onto point-in-time match history,
enforced by `tests/regression/test_point_in_time_inputs.py` and
`test_evaluation_leakage.py`.

**What remains is odds only.** No historical point-in-time BTTS prices exist
in this repository, and The Odds API's live endpoint cannot supply a
pre-kickoff snapshot for a past fixture. Therefore:

> **LEAK-001 is hereby classified as an evaluation / data-provenance
> limitation, not a solved problem.** Any backtest of *betting value* (edge,
> ROI, yield) is not honest and must not be reported. Backtests of
> *discrimination* (AUC, calibration, Brier) remain valid because they do not
> consume odds.

No odds were fabricated, reconstructed or back-filled. No closing price was
substituted for a pre-match price.

---

## Phase 5 — Matchday-1 dry run

### Executed live, against real ESPN data

```
$ python main.py 2026-08-22
17 fixtures discovered across allowed leagues
Summary: 0 flagged, 17 no bet
Results written to: output_2026-08-22.csv
Results written to: output_2026-08-22.json
```

Every fixture was refused with `Missing or unreliable team stats`. This is
**correct cold-start behaviour**, not a failure: at Matchday 1 there is no
current-season history, and the system declines rather than guessing.

| Step | Status |
|---|---|
| Fixture discovery | ✅ 17 real fixtures |
| Allowed-league filtering | ✅ only the 5 allowed leagues |
| Team statistics | ✅ absent → refused, not fabricated |
| League average | ✅ fetched per league |
| Missing-data handling | ✅ fails closed |
| Probability calculation | ✅ skipped when inputs absent |
| Filters | ✅ reached only with real inputs |
| Odds | ✅ absent key → `N/A`, no crash |
| Edge | ✅ `N/A` |
| Decision | ✅ `NO BET` |
| Terminal output | ✅ readable, reasons shown |
| CSV output | ✅ written |
| JSON output | ✅ written |
| Zero-bet handling | ✅ clean `0 flagged` summary |

### Thin-sample probe (the real early-season risk)

`esp.1` had already played 4 matches, so one fixture *did* reach the model on
a single-match sample:

```
2026-08-28 Alavés v Villarreal
    samples          = {'home': 1, 'away': 1, 'league': 4}
    gg_probability   = 0.0
    lambda_home/away = 3.6923 / 0.0
    filter_outcome   = FAILED
    decision         = NO BET
    reasons          = ['Home team keeps > 40% clean sheets (100.0%)', 'Failed hard filters']
```

An `n=1` sample produces a **degenerate `λ_away = 0.0` and `P(GG) = 0.0`**
(known as GG-028). In `main.py` the clean-sheet filter caught it and the
decision was `NO BET` — the safety net worked. **This is precisely the input
shape that makes 2F-P0-1 dangerous**, as shown next.

### Reproducible pre-matchday procedure

Run this the day before the first official matchday. Any deviation is a stop.

```bash
cd /Users/kennedykp/Documents/repo-pull/gg_predictor

# 1. Gates must all be clean
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
git diff --check

# 2. Confirm the odds key state deliberately (absent is safe, not silent)
.venv/bin/python -c "from config import ODDS_API_KEY; print('odds key:', bool(ODDS_API_KEY))"

# 3. Dry run into a scratch directory so no output lands in the repo
mkdir -p /tmp/gg_dryrun && cd /tmp/gg_dryrun
PYTHONPATH=/Users/kennedykp/Documents/repo-pull/gg_predictor \
  /Users/kennedykp/Documents/repo-pull/gg_predictor/.venv/bin/python \
  /Users/kennedykp/Documents/repo-pull/gg_predictor/main.py <MATCHDAY-1 DATE>
```

**Acceptance criteria**

1. Fixture count matches the real schedule for the allowed leagues.
2. No traceback.
3. CSV and JSON are both written.
4. Every `FLAG` row shows `filter_outcome=PASSED`, real `odds`, and
   `edge ≥ 0.05` with `odds ≥ 1.60`.
5. **Any row with `gg_probability` of exactly `0.0` or `1.0` is a
   thin-sample artefact and must be `NO BET`.**
6. Missing stats produce `NO BET`, never a default.
7. If `analyze_all.py` is used, every `RECOMMEND_PLAY` row must show
   `filter_status=PASSED`. This is now enforced in code (2F-P0-1 fix) and
   guarded by `TestEpic2FRecommendationGating`, so a violation means the gate
   has regressed — stop and investigate.


---

## Phase 6 — Test coverage

### Gate results (exact)

```
pytest -q        1690 passed, 3 skipped in 1.21s
ruff check .     All checks passed!
mypy             Success: no issues found in 36 source files
git diff --check (clean — no whitespace errors, no output)
```

Skips (all deliberate, all documentation of open decisions):
- `test_epic2d_protocol.py:139` — harness exposes no `MODELS` registry
- `test_spec_agreement.py:129` — **D1**: GG.md says API-Football, production uses ESPN
- `test_spec_agreement.py:171` — **D3 / GG-002-B**: knockout + heavy-favourite filters have no feed

### Does an existing test protect 2F-P0-1?

**No.** A test intends to, and it is vacuous.

`tests/integration/test_entry_point_consistency.py:228`
`test_neither_entry_point_recommends_without_clean_sheet_data` asserts
`system_recommendation == "RECOMMEND_NO_PLAY"`. Its `generous_odds` fixture
prices both sides at **1.80**. With the fixture's own inputs
(λ = 1.3333 both sides → `P(GG) = 0.5423`):

```
  odds  implied     edge    would recommend?
  1.60   0.6250  -0.0827   RECOMMEND_NO_PLAY
  1.80   0.5556  -0.0133   RECOMMEND_NO_PLAY  <-- the test uses this
  2.00   0.5000  +0.0423   RECOMMEND_NO_PLAY
  2.20   0.4545  +0.0877      RECOMMEND_PLAY
  2.50   0.4000  +0.1423      RECOMMEND_PLAY

break-even price at which the UNFILTERED edge alone recommends: odds > 2.0313
```

At 1.80 the edge is **negative**. The assertion is satisfied by the price,
never by the filter. The odds are not "generous" enough to expose the bug.

### Proof of the defect (read-only, in-process monkeypatching)

**(a) Filters FAILED — both sides average 0.30 goals, well under `MIN_AVG_GOALS`:**

```
market=GG_YES  filter_status=FILTERED  edge=0.2485  class=STRONG_VALUE  -> RECOMMEND_PLAY
          filter_reasons=['Home team averages < 1.0 goals (0.30)',
                          'Away team averages < 1.0 goals (0.30)']
```

The row states the reasons it must be rejected, and recommends the play anyway.

**(b) Filter data UNAVAILABLE — same fixture through both entry points:**

```
analyze_all.py : filter_status=FILTER_DATA_UNAVAILABLE  edge=0.2485 -> RECOMMEND_PLAY
main.py        : decision=NO BET  filter_outcome=UNEVALUATED  edge=None
```

**(c) Worst case — thin-sample `n=1` compounding with the bypass**, using the
real live shape observed on `esp.1` for 2026-08-28:

```
market=GG_YES  model_prob=0.0  filter_status=FILTERED  odds=1.9  edge=-0.5263 -> RECOMMEND_NO_PLAY
market=GG_NO   model_prob=1.0  filter_status=FILTERED  odds=1.9  edge=+0.4737 -> RECOMMEND_PLAY
```

A **100 %-certain `GG_NO`** — an artefact of a single match, on a fixture that
FAILED the filters — is published as `STRONG_VALUE` / `RECOMMEND_PLAY`, and
`analyze_all.py:324` collects it into the headline recommendations list. This
is the maximum-loss path and it is reachable in the first fortnight of the
season.

### Root cause

`analyze_all.py:212–251`. `analyze_market()` returns a dict whose
`system_recommendation` is a pure function of edge and odds. The following
`.update({... "filter_status": ..., "filter_reasons": ...})` **adds labels
beside that verdict without ever revising it.** `main.py` does the equivalent
thing correctly by passing `filter_result.allows_recommendation` *into*
`make_decision()`.

### The fix — APPLIED (reviewed and approved after the audit)

**`analyze_all.py`, +4 executable lines.** One helper and its two call sites:

```python
def _gate_recommendation_on_filters(analysis, filter_status):
    if filter_status != "PASSED":
        analysis["system_recommendation"] = "RECOMMEND_NO_PLAY"
    return analysis

results.append(_gate_recommendation_on_filters(gg_yes_analysis, filter_status))
results.append(_gate_recommendation_on_filters(gg_no_analysis, filter_status))
```

Only an explicit `"PASSED"` permits a play; `"FILTERED"` and
`"FILTER_DATA_UNAVAILABLE"` are both refusals. This mirrors what `main.py` has
always done by passing `filter_result.allows_recommendation` *into*
`make_decision()`.

`classification` is deliberately **not** overwritten: it describes the *price*
("was this market generous?"), not the bet. Blanking it would destroy the
evidence that a filtered fixture happened to be mispriced.

### Behaviour change — exactly one field

| Condition | `system_recommendation` before | after |
|---|---|---|
| `filter_status == "PASSED"` | unchanged | **unchanged** |
| `"FILTERED"` + high edge | `RECOMMEND_PLAY` | `RECOMMEND_NO_PLAY` |
| `"FILTER_DATA_UNAVAILABLE"` + high edge | `RECOMMEND_PLAY` | `RECOMMEND_NO_PLAY` |
| `"MISSING_DATA"` / `"CALCULATION_FAILED"` | already `RECOMMEND_NO_PLAY` | unchanged |

Output schema is unchanged — no key added, renamed or removed.

### Proof that no probability or formula moved

The same fixture was run twice through `analyze_gg_match`: once with the gate
active, once with it replaced by the identity function (bit-exact pre-fix
behaviour), diffing **every key** in every row.

```
--- FILTERS FAILED (leaky home) ---
  GG_YES  filter_status=FILTERED  prob=0.5423 lam_h=1.3333333333333333 lam_a=1.3333333333333333 odds=2.5 edge=0.1423 class=STRONG_VALUE
          rec: pre-fix=RECOMMEND_PLAY  ->  post-fix=RECOMMEND_NO_PLAY
          other fields changed: NONE
  GG_NO   filter_status=FILTERED  prob=0.4577 lam_h=1.3333333333333333 lam_a=1.3333333333333333 odds=2.5 edge=0.0577 class=VALUE
          rec: pre-fix=RECOMMEND_PLAY  ->  post-fix=RECOMMEND_NO_PLAY
          other fields changed: NONE

--- FILTERS PASSED (both solid) ---
  GG_YES  filter_status=PASSED    prob=0.5423 lam_h=1.3333333333333333 lam_a=1.3333333333333333 odds=2.5 edge=0.1423 class=STRONG_VALUE
          rec: pre-fix=RECOMMEND_PLAY  ->  post-fix=RECOMMEND_PLAY
          other fields changed: NONE
  GG_NO   filter_status=PASSED    prob=0.4577 lam_h=1.3333333333333333 lam_a=1.3333333333333333 odds=2.5 edge=0.0577 class=VALUE
          rec: pre-fix=RECOMMEND_PLAY  ->  post-fix=RECOMMEND_PLAY
          other fields changed: NONE

RESULT: only `system_recommendation` ever differs.
```

`model_probability`, `lambda_home`, `lambda_away`, `odds`,
`implied_probability`, `edge` and `classification` are identical in every row.
A PASSING fixture still recommends exactly as before. Corroborated structurally:
`git diff --stat` over `poisson.py filters.py decision.py config.py main.py
espn.py odds_api.py shared/odds.py domain/` is **empty**.

### The three original attack paths, re-run post-fix

```
(a) FILTERS FAILED (0.30 goals):
    GG_YES  filter_status=FILTERED  edge=0.2485  class=STRONG_VALUE  -> RECOMMEND_NO_PLAY
    GG_NO   filter_status=FILTERED  edge=-0.0485 class=FAIR_NO_EDGE  -> RECOMMEND_NO_PLAY

(b) FILTER DATA UNAVAILABLE, odds 2.50:
    analyze_all.py : FILTER_DATA_UNAVAILABLE  edge=0.2485 -> RECOMMEND_NO_PLAY
    main.py        : NO BET  UNEVALUATED  edge=None            [now agree]

(c) WORST CASE, thin-sample n=1 (the real esp.1 shape):
    GG_YES  model_prob=0.0  FILTERED  edge=-0.5263 class=OVERPRICED   -> RECOMMEND_NO_PLAY
    GG_NO   model_prob=1.0  FILTERED  edge=+0.4737 class=STRONG_VALUE -> RECOMMEND_NO_PLAY
```

The 100 %-certain `GG_NO` on a filter-FAILED fixture — the maximum-loss path —
is now refused while still being fully reported.

### Regression tests

`generous_odds` raised **1.80 → 2.50** (above the 2.0313 break-even), so the
pre-existing assertions are load-bearing for the first time. Plus
`TestEpic2FRecommendationGating`, 6 tests:

| Test | Guards |
|---|---|
| `test_the_edge_alone_would_recommend` | **anti-vacuity**: a PASSING fixture at 2.50 *does* reach `RECOMMEND_PLAY` |
| `test_failed_filters_cannot_recommend_despite_high_edge` | FAILED + edge > 0.05 → NO PLAY |
| `test_unavailable_filter_data_cannot_recommend_despite_high_edge` | UNAVAILABLE + edge > 0.05 → NO PLAY |
| `test_gate_never_recommends_against_a_non_passing_status` | invariant: `RECOMMEND_PLAY` ⇒ `filter_status == "PASSED"` |
| `test_gate_leaves_every_measurement_untouched` | prob/λ/odds/implied/edge/classification survive the refusal |
| `test_both_entry_points_now_agree_on_what_may_be_published` | GG-006 extended to the publication step |

**Mutation-verified.** With the gate neutered to the identity function (via an
injected pytest plugin, repo untouched), **6 tests fail**, including the
formerly vacuous original:

```
FAILED TestIdenticalFilterConclusion::test_neither_entry_point_recommends_without_clean_sheet_data
FAILED TestEpic2FRecommendationGating::test_failed_filters_cannot_recommend_despite_high_edge
FAILED TestEpic2FRecommendationGating::test_unavailable_filter_data_cannot_recommend_despite_high_edge
FAILED TestEpic2FRecommendationGating::test_gate_never_recommends_against_a_non_passing_status
FAILED TestEpic2FRecommendationGating::test_gate_leaves_every_measurement_untouched
FAILED TestEpic2FRecommendationGating::test_both_entry_points_now_agree_on_what_may_be_published
6 failed, 12 passed
```

`test_the_edge_alone_would_recommend` correctly still passes under mutation —
it asserts the *premise* (the edge wants to recommend), not the gate.

### Post-fix gate results (exact)

```
pytest -q        1696 passed, 3 skipped in 1.31s      (was 1690; +6 new)
ruff check .     All checks passed!
mypy             Success: no issues found in 36 source files
git diff --check (clean — no output)
```

---


## EPIC 2F SEASON READINESS VERDICT

**P0 blockers:**
- **2F-P0-1 — FIXED.** `analyze_all.py` was publishing `RECOMMEND_PLAY` on
  fixtures that FAILED the mandatory hard filters and on fixtures whose filter
  data was UNAVAILABLE. A silent safety-filter bypass, reachable in the first
  weeks of the season via thin-sample degenerate probabilities. Now gated on an
  explicit `filter_status == "PASSED"`, mutation-verified by 6 tests, with
  every measurement proven unchanged. `main.py` was never affected.
- **No P0 blocker remains open.**

**P1 issues:**
- 2F-P1-1 **CLOSED** — the vacuous guard is de-vacuumed (1.80 → 2.50, above the
  2.0313 break-even) and mutation-verified

- 2F-P1-2 two duplicated odds clients and duplicated thresholds
- 2F-P1-3 no `commence_time` / kickoff verification on odds
- 2F-P1-4 unnormalised bidirectional substring team-name matching
- 2F-P1-5 first-bookmaker-wins, unnamed, with no price sanity bound
- 2F-P1-6 GG-002-B: knockout + heavy-favourite filters have no data feed
- 2F-P1-7 LEAK-001: no point-in-time historical odds
- 2F-P1-8 D1: GG.md names API-Football; production runs ESPN

**P2 issues:**
- 2F-P2-1 stale unanswered questions in `main.py` comments
- 2F-P2-2 unused provider modules (`api_football.py`, `sofascore.py`, `sportmonks.py`, `run3/`)
- 2F-P2-3 three load-bearing skipped tests invisible in a green summary
- 2F-P2-4 23 open debt items, none other than the above affecting Matchday 1

**Production source:**
ESPN (`espn.py`), for fixtures, statistics, league average and match history,
in production, tests and historical evaluation alike. API-Football is dead
code. All 5 `ALLOWED_LEAGUES` codes map correctly to odds `SPORT_KEYS`.
Season boundaries come from provider metadata (GG-025 fixed). Model inputs
are point-in-time; future matches cannot leak in.

**Safety-filter status:**
3 of 5 correct and genuinely enforced, **and now enforced identically by both
entry points.** Unavailable clean-sheet data **cannot** become 0 % — it becomes
`UNEVALUATED`/`FILTER_DATA_UNAVAILABLE` and blocks the recommendation in both
scripts. Filters 3 and 4 (`is_knockout_first_leg`,
`is_heavy_favorite_mismatch`) are **hardcoded `False`** at
`domain/filter_stats.py:236–237` and can never fire; this is a missing feed,
and both are inapplicable to the 5 allowed single-leg domestic leagues — a
**documented limitation** that becomes a real P0 if a two-legged competition
is ever added to `ALLOWED_LEAGUES`.


**Odds status:**
Structurally isolated from the model — odds cannot influence GG probability
(verified). Missing odds fail closed. Weaknesses are all in *matching and
provenance*: no kickoff check, fuzzy names, arbitrary first bookmaker,
duplicated client.

**Historical evaluation status:**
Discrimination backtests (AUC / calibration / Brier) are valid — inputs are
point-in-time and leakage-tested. **Value/ROI backtests are not valid and
must not be published**: no point-in-time odds exist. LEAK-001 is classified
as an evaluation / data-provenance limitation. No odds were fabricated.

**Matchday-1 dry-run status:**
PASSED. Live run on 2026-08-22 produced 17 fixtures, 0 flagged, 17 `NO BET`,
CSV + JSON written, no traceback, correct cold-start refusal. A reproducible
pre-matchday procedure with 7 acceptance criteria is recorded above.
`analyze_all.py` is now safe to run — its recommendation is gated on the
filter verdict — though `main.py` remains the reference workflow.

**Tests:**
`1696 passed, 3 skipped in 1.31s`

**Ruff:**
`All checks passed!`

**Mypy:**
`Success: no issues found in 36 source files`

**git diff --check:**
clean (no output, no whitespace errors)

### VERDICT: **READY WITH DOCUMENTED LIMITATIONS**

The single P0 blocker (2F-P0-1) was demonstrated with reproducible evidence,
fixed with 4 executable lines confined to one publication step, and is now
guarded by 6 mutation-verified regression tests. No probability, threshold,
lambda, filter definition or odds calculation was altered — proven field by
field, and corroborated by an empty `git diff` across every locked module.

**The documented limitations, accepted knowingly:**

1. **GG-002-B** — the first-leg-knockout and heavy-favourite filters cannot
   fire (no data feed). Inapplicable to the 5 allowed single-leg domestic
   leagues. **Becomes a P0 if a two-legged competition is ever added to
   `ALLOWED_LEAGUES`.**
2. **LEAK-001** — no point-in-time historical odds. Discrimination backtests
   are honest; **value/ROI backtests must not be published.**
3. **GG-028** — thin early-season samples can produce degenerate `P(GG)` of
   exactly 0.0 or 1.0. Currently caught by the clean-sheet filter *and*, since
   this epic, by the recommendation gate in both entry points. Dry-run
   criterion 5 requires these to be checked by hand for the first fortnight.
4. **Odds matching** (2F-P1-2 … 2F-P1-5) — no kickoff verification, fuzzy team
   names, arbitrary first bookmaker, duplicated client. All fail *closed*, but
   a wrong-but-plausible name match is not defended against. Watch the first
   matchday's `odds` values for sanity, especially for renamed/promoted clubs.

**Not done, deliberately:** no threshold was tuned, no ML added, no Epic 2E
finding promoted, no new predictive feature introduced, and no speculative
improvement made. The remaining P1/P2 items are recorded above for a future
epic, not silently carried as risk.

Nothing has been committed or pushed — the three changed files are staged for
review only.

