# Epic 1B.3 — GG Filter Wiring & Statistical Semantics (GG-002, GG-006)

## Objective

Make the existing GG hard filters actually evaluate the statistics they claim to
evaluate.

The filters were never mathematically wrong. Every threshold and every
comparison in `filters.py` was correct. The defect was upstream: callers handed
them **fabricated constants**, so the comparisons ran against numbers that did
not describe the teams involved. `clean_sheet_pct = 0` is not "no clean-sheet
data" — it asserts *this team has never kept a clean sheet*, and since
`0 > 0.40` is never true, the filter passed every fixture it ever saw.

**No threshold was changed.** This Epic changes what reaches them.

---

## Existing Filters

`filters.apply_filters` takes seven inputs. Five are documented in GG.md §9 as
mandatory hard filters; the sixth and seventh are a reliability flag and the
rejection-reason machinery.

| Filter | Statistic | Threshold | Previous Input | Previous Source | Correct Input | Correct Source | Status |
|---|---|---|---|---|---|---|---|
| Home goals average | Goals scored per home match | `< MIN_AVG_GOALS (1.0)` → reject | `main.py`: `total_goals_avg` (**scored + conceded**)<br>`analyze_all.py`: `home_goals_scored` | ESPN standings totals | `home_avg_goals_scored` | ESPN — DERIVED | ✅ FIXED (GG-006) |
| Away goals average | Goals scored per away match | `< MIN_AVG_GOALS (1.0)` → reject | same disagreement | ESPN standings totals | `away_avg_goals_scored` | ESPN — DERIVED | ✅ FIXED (GG-006) |
| Home clean sheet | Fraction of home matches conceding 0 | `> MAX_CLEAN_SHEET_PCT (0.40)` → reject | **hardcoded `0`** | `espn.py` literal | `home_clean_sheet_pct` | **UNAVAILABLE** | ✅ FIXED — now blocks |
| Away clean sheet | Fraction of away matches conceding 0 | `> MAX_CLEAN_SHEET_PCT (0.40)` → reject | **hardcoded `0`** | `espn.py` literal | `away_clean_sheet_pct` | **UNAVAILABLE** | ✅ FIXED — now blocks |
| Knockout first leg | Competition format | `True` → reject | **hardcoded `False`** | `main.py` literal | needs fixture metadata | **UNAVAILABLE** | ⚠️ OPEN (GG-002-B) |
| Heavy favourite mismatch | Market/strength gap | `True` → reject | **hardcoded `False`** | `main.py` literal | needs market data | **UNAVAILABLE** | ⚠️ OPEN (GG-002-B) |
| Reliable data | Are inputs present | `False` → reject | **hardcoded `True`** | `main.py` literal | computed from availability | Domain layer | ✅ FIXED |

Comparison semantics, pinned by test rather than by comment — the threshold
value itself **passes** (`<` and `>`, not `<=`/`>=`):

- `avg_goals == 1.0` → PASS; `0.99` → FAIL
- `clean_sheet == 0.40` → PASS; `0.41` → FAIL

`MIN_RECENT_GAMES`, `MIN_BOTH_SCORED_PCT` and `MAX_BOTH_SCORED_PCT` **do not
exist in `config.py`**. See *Recent-Games* and *BTTS Percentage* below — this
was a finding, not an omission.

---

## GG-002 Root Cause

Three distinct mechanisms, not one:

1. **Provider fabrication.** `espn.py` returned `"home_clean_sheet_pct": 0` and
   `"away_clean_sheet_pct": 0` as literals. ESPN's standings record contains no
   clean-sheet data whatsoever, so these were invented to fill the dict shape.
2. **Caller fabrication.** `main.py` passed `is_knockout_first_leg=False`,
   `is_heavy_favorite_mismatch=False`, `has_reliable_data=True` — three filters
   permanently disabled by literal.
3. **No way to say "unknown".** Before Epic 1B.1 the pipeline could not
   represent absence, so a shape-filling `0` was the only option available.

The result was structural, not incidental: **`passes_filters: true` in 39 of 39
committed fixtures.** The filters were not lenient — they were not running.

The deeper issue is that `0` and `False` are *plausible* values. A `None` would
have crashed and been fixed years ago; a fabricated `0` produced confident
output indefinitely.

---

## GG-006 Resolution

**Resolved.** The disagreement was real and the intended meaning was
recoverable from three independent sources that agree:

| Source | Evidence |
|---|---|
| `GG.md` §9 | "one team averages **< 1.0 goal**" — goals *scored* |
| `filters.py` docstring | parameter documented as "average goals per match" *for that team* |
| `analyze_all.py` | already passed `home_goals_scored` — the correct reading |

`main.py` was the outlier, passing `total_goals_avg` = `(GF + GA) / matches`.
That is a different statistic: it measures **how eventful a team's matches
are**, not how reliably it scores.

The practical consequence, now a regression test:

> A team with 5 scored / 30 conceded in 20 matches has a home scoring rate of
> **0.30** (fails) but `total_goals_avg` of **1.75** (passes).

A side that cannot score but leaks goals was being *approved* by the filter
designed to exclude exactly that profile.

Both entry points now call `domain.build_filter_stats`, the single place the
mapping is defined.

---

## ESPN Filter Data Availability

Verified live against `eng.1` on 2026-08-08 via `scripts/espn_diagnostic.py`.

| Statistic | Classification | Basis |
|---|---|---|
| `matches_played` | **DIRECT** | ESPN states `gamesPlayed` |
| `home_matches` / `away_matches` | **DIRECT** | `homeGamesPlayed` / `awayGamesPlayed` (GG-004) |
| `home_avg_goals_scored` | **DERIVED** | `homePointsFor / homeGamesPlayed` — exact division |
| `away_avg_goals_scored` | **DERIVED** | `awayPointsFor / awayGamesPlayed` — exact division |
| `home_clean_sheet_pct` | **UNAVAILABLE** | see proof below |
| `away_clean_sheet_pct` | **UNAVAILABLE** | see proof below |
| `both_scored_pct` | **UNAVAILABLE** | needs per-match scorelines |

### Why clean sheets are UNAVAILABLE, not DERIVED

This is a mathematical fact about the aggregate, not a parsing limitation.
ESPN's standings record supplies **goals against** and **matches played**. Both
of these histories produce `GA = 5` over 5 matches:

```
conceded 1, 1, 1, 1, 1  ->  0 clean sheets  (0%)
conceded 5, 0, 0, 0, 0  ->  4 clean sheets  (80%)
```

No function of `(GA, matches)` can distinguish them. Any mapping to a
clean-sheet rate is an **approximation**, and Task 4 forbids classifying an
approximation as DERIVED. The honest answer is UNAVAILABLE.

The same argument applies to BTTS: a team averaging 2 scored and 2 conceded per
match may have a BTTS rate anywhere from 0% to 100%.

Both statistics require **match-level records**. The per-team schedule endpoint
(`/teams/{id}/schedule`) carries them and is the verified next step.

---

## Clean-Sheet Semantics

Resolved from GG.md §9 and the existing parameter names:

- **Which team?** Both, independently. `filters.py` has always had two separate
  parameters and two separate comparisons — the "maximum of both" reading was
  never in the code.
- **Which perspective?** Home team's **home** matches; away team's **away**
  matches. Not invented: POISSON_V1 already uses the venue split (GG.md §6), and
  the parameters were already named `home_*`/`away_*`. Applying a venue-split λ
  alongside an overall-season filter would be the inconsistent choice.
- **Which period?** Season-to-date. No recency window exists anywhere in the
  project (see below).
- **Unit:** fraction in `[0.0, 1.0]`, **not** 0–100. Enforced at construction —
  a `40` meaning "40%" would clear `> 0.40` forever.

**Implementation:** `domain/match_records.py::clean_sheet_pct` derives it
exactly from completed match records, filtered by venue. It is fully tested but
**not yet wired to production**, because no current provider call returns match
records. Until one does, `espn.py` reports `None` and the fixture is refused.

---

## BTTS Percentage Semantics

**Finding: `MIN_BOTH_SCORED_PCT` and `MAX_BOTH_SCORED_PCT` do not exist.** They
are not in `config.py`, not in `filters.py`, and no caller passes them. The
Epic brief listed them among "existing thresholds"; verifying against source
rather than trusting the brief showed there is nothing to preserve.

Nothing was invented to fill the gap. Instead the **exact derivation** is
implemented and tested, so that if the filter is specified later it has a
correct, hand-verified statistic to consume rather than a fresh approximation:

`domain/match_records.py::both_teams_scored_pct` counts completed matches where
`goals_for > 0 AND goals_against > 0`. It is **never** inferred from aggregate
goals, for the reason given above.

---

## Recent-Games Semantics

**Finding: `MIN_RECENT_GAMES` does not exist either**, and — more importantly —
**no recency concept exists anywhere in this project.** `espn.get_team_stats`
reads season-long cumulative totals only (already tracked as GG-005).

Per Task 7, this ambiguity is **documented rather than resolved**. Silently
redefining "recent games" as "total season games" would produce a filter that
looks like form analysis while measuring something else entirely — precisely
the class of defect this Epic exists to remove.

`matches_played` is available and DIRECT if a games-played minimum is specified
later. A genuine *recency window* requires fixture-level history (GG-005).

---

## Average-Goals Semantics

Pinned in `FilterStats`, deliberately with self-describing names:

- **Statistic:** goals **scored** — not conceded, not scored + conceded
- **Perspective:** per-team, at the venue that team is actually playing
- **Unit:** goals per **match**, not per season
- **Threshold:** `MIN_AVG_GOALS = 1.0`, **unchanged**

The old parameter name `home_avg_goals` was vague enough that
`total_goals_avg` did not look wrong at the call site. `home_avg_goals_scored`
would have.

---

## Domain Changes

Three new modules, all additive:

| Module | Contents | Purpose |
|---|---|---|
| `domain/filter_stats.py` | `FilterStats`, `StatSource`, `build_filter_stats` | Typed filter inputs; the single mapping point |
| `domain/filter_evaluation.py` | `FilterResult`, `FilterOutcome`, `evaluate_filters` | The one evaluation boundary |
| `domain/match_records.py` | `MatchRecord`, `clean_sheet_pct`, `both_teams_scored_pct` | Exact derivations from completed matches |

`FilterStats` fields are `Optional[float]`, so **a genuine `0.0` remains valid
data** while `None` means unavailable. Construction rejects impossible values
(percentages outside `[0,1]`, negative goal averages) — a unit error is a wiring
bug and should fail loudly, not be compared against a real threshold.

`StatSource` keeps the DIRECT / DERIVED / UNAVAILABLE distinction as three enum
values. No confidence scores, no provenance graph.

---

## Filter Availability Policy

```
required filter statistic missing
    -> filter evaluation cannot be trusted
    -> FilterOutcome.UNEVALUATED
    -> reason "FILTER_DATA_UNAVAILABLE: <field names>"
    -> no recommendation
```

`evaluate_filters` returns UNEVALUATED **without calling `apply_filters` at
all**. There is no value to pass for an absent statistic: `0.0` lies about the
team, and `0.5` is a lie chosen because it passes.

`FilterResult.passed` is `False` for UNEVALUATED — the safe direction. An
unevaluated filter must never resemble a pass.

---

## Entry-Point Consistency

Both `main.py` and `analyze_all.py` now:

1. Build inputs via `build_filter_stats(home_stats, away_stats)`
2. Evaluate via `evaluate_filters(stats)`
3. Report the same reasons from the same `FilterResult`

Neither imports `filters` directly any more, and a structural test asserts they
never do again — if either starts assembling its own arguments, the mapping can
drift apart exactly as it did before.

---

## POISSON_V1 Verification

**Model data and filter data are separate concerns**, and the architecture now
preserves that distinction:

```
POISSON_V1 inputs complete  +  filter inputs incomplete
    =  probability IS calculated and displayed
       but NO RECOMMENDATION is made
```

A fixture with full model data reports its genuine BTTS probability while
`filter_outcome: UNEVALUATED` blocks the bet. Suppressing the probability would
discard a correctly-computed number; recommending on it would bet on an
unevaluated safety layer. Covered by
`test_probability_is_still_produced_by_both`.

---

## Early-Season Behaviour

It is **August 2026** — preseason. Live diagnostic output for `eng.1`:

```
scoreboard              : EMPTY - endpoint healthy, genuinely no fixtures
team endpoint           : PRESENT but ZERO (home=0.0, away=0.0)
get_league_avg_goals()  : UNAVAILABLE (None)
filter statistics       : UNAVAILABLE - provider returned None
```

**Zero predictions today, and that is the correct output.** Every team has
`gamesPlayed = 0`, so per-match rates are undefined — not zero. Nothing was
loosened to manufacture activity. Early-season priors are a separate,
deliberate piece of work.

---

## Tests Added/Changed

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_filter_evaluation.py` | 36 | Thresholds, boundaries, genuine zero, unavailability, unit validation, no fake constants |
| `tests/unit/test_match_record_derivations.py` | 28 | BTTS + clean-sheet derivations, venue perspective, incomplete matches |
| `tests/integration/test_entry_point_consistency.py` | 11 | GG-006, identical verdicts, probability preserved |

Boundary coverage is exhaustive per Task 14: exact threshold, ±0.01, genuine
`0.0`, `None`, and multiple simultaneous failures (all four reasons reported,
not just the first — an operator reviewing a rejection needs the full picture).

**Every threshold is imported from `config`, never retyped as a literal.** A
test hardcoding `1.0` would keep passing if someone edited `MIN_AVG_GOALS`,
defeating Task 19.

---

## Characterization Tests Transitioned

Per Task 20 — old test proves the defect, new test proves it cannot recur.
Evidence was preserved, not deleted.

| Test | Before | After |
|---|---|---|
| `test_espn_missing_data.py` — clean-sheet zeros | Asserted `== 0` (documenting fabrication) | Asserts `is None`, with the original defect explained in the docstring |
| `test_pipeline_missing_data.py` — filter wiring | Asserted filters always passed | Asserts unavailable data blocks recommendation |
| `test_spec_agreement.py::D3` | Skipped: "filters hardcoded off" | Skip reason narrowed to the two genuinely unsourced flags (GG-002-B) |
| `test_spec_agreement.py::D4` | Skipped: "entry points disagree" | **Unskipped** — now an active regression test |

---

## Live Diagnostic

`scripts/espn_diagnostic.py` gained **section 5**, reporting per statistic:
source classification, sample value, and sample size. Sample size is shown
because a rate computed from two matches should not be mistaken for a settled
one.

It remains **manual-only**: `scripts/` is outside `testpaths`, there is no
`__init__.py`, and all work is behind `if __name__ == "__main__"`. Never add it
to CI — a test depending on a third-party endpoint fails for reasons unrelated
to this repository.

One successful response proves a field exists for one team, in one league, at
one moment — not that it is universally available.

---

## Threshold Verification

Verified by hash comparison against `HEAD`, not by inspection:

| File | Status |
|---|---|
| `poisson.py` | **UNCHANGED** |
| `config.py` | **UNCHANGED** — every threshold identical |
| `filters.py` | **UNCHANGED** — no threshold, no comparison, no operator touched |
| `decision.py` | **UNCHANGED** |
| `run3/` | **UNCHANGED** — remains parked |

`git diff HEAD -- poisson.py config.py filters.py decision.py run3/` → empty.

Golden regression: **884 passed**. POISSON_V1 mathematics and outputs identical.

**Expected effect on volume:** far fewer fixtures will now pass. Clean-sheet
data is unavailable, so **no fixture currently reaches a recommendation**. This
is the correct consequence of removing a filter that never fired, and is **not**
grounds for loosening anything. Calibration belongs after historical
backtesting exists.

---

## Files Changed

**Production (4 modified, 3 added):**

| File | Change |
|---|---|
| `espn.py` | Clean-sheet literals `0` → `None` |
| `main.py` | Routed through the shared boundary; three hardcoded flags removed |
| `analyze_all.py` | Routed through the shared boundary |
| `domain/__init__.py` | Re-exports |
| `domain/filter_stats.py` | **NEW** |
| `domain/filter_evaluation.py` | **NEW** |
| `domain/match_records.py` | **NEW** |

**Tests:** 3 added, 3 updated. **Diagnostic:** section 5 added.

**Untouched:** `poisson.py`, `config.py`, `filters.py`, `decision.py`, `run3/`.

---

## Issues Closed

- **GG-002 — primary defect: CLOSED.** No fabricated value reaches any filter.
  Clean-sheet zeros removed; the three hardcoded flags removed from `main.py`;
  unavailable data blocks recommendations instead of silently passing.
  - **GG-002-B opened** for the residual: the knockout-first-leg and
    heavy-favourite filters have **no data source at all**. They are now
    explicitly `False` on the contract in one visible place rather than being
    literals buried at two call sites. They need a competition-format/market
    feed — not a wiring change.
- **GG-006 — CLOSED.** Semantics established from three agreeing sources; both
  entry points consistent; regression test in place.

## Issues Remaining

Explicitly, per Task 24:

- **LEAK-001 remains open.** Out of scope, untouched. Structural look-ahead bias
  makes backtesting invalid.
- **GG-024 remains open.** The ESPN team endpoint ignores `?season=`.
- **Historical backtesting remains unsafe.** LEAK-001 and GG-024 are both
  prerequisites.
- **No threshold calibration has occurred.** No constant was tuned.
- **Run-3 remains parked.** Not read, not modified.
- **GG-005** (no form/recency) — blocks a genuine recent-games filter.
- **GG-002-B** (new) — two filters with no data source.

---

## Recommended Next Step

**Wire the ESPN per-team schedule endpoint (`/teams/{id}/schedule`) to supply
`MatchRecord` lists.**

It is the single change that converts clean-sheet and BTTS percentage from
UNAVAILABLE to DERIVED, because it carries per-match scorelines and completion
status — exactly what the aggregates cannot provide. The derivations, the
contract and the tests already exist and are hand-verified; only the provider
call and its parsing are missing.

That would restore recommendation flow **with genuine data**, rather than by
relaxing anything.

Verify first that the endpoint returns completed-match scores across several
leagues before committing to it — one league in preseason is not evidence.
