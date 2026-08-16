# GG Predictor — Technical Debt Register (as of `be67223`)

Prioritised. Items marked ✅ RESOLVED were fixed in the Epic named on the heading; everything else is
still open and the recommendation stands. Original problem text is kept verbatim under each resolved
item — resolutions are **appended, not substituted**, so the history stays readable.

**Counts:** 5 CRITICAL (4 RESOLVED) · 12 HIGH (3 RESOLVED) · 9 MEDIUM (3 RESOLVED) · 6 LOW (1 RESOLVED)
· 1 leakage risk (counted within CRITICAL, open)

**Status: 34 items tracked · 11 RESOLVED · 23 open.**




| Epic | Resolved | Items |
|---|---|---|
| 1B.1 — data contracts | 1 | GG-001 |
| 1B.2 — ESPN provider | 6 | GG-003, GG-004, GG-012, GG-013, GG-014, GG-020 |
| 1B.3 — filter wiring | 2 | GG-002, GG-006 |
| 1B.4 — match history | 0 | GG-002-B narrowed (clean-sheet feed built); nothing closed |
| 1B.5 — point-in-time model inputs | 0 | LEAK-001 narrowed to odds only; GG-024 superseded in practice; nothing closed |
| 2A — cold-start research | 0 | Read-only audit; **found GG-025**, fixed in 2B.1 |
| 2B.1 — season integrity | 1 | GG-025; opened GG-026, GG-027 |
| 2B.2 — historical dataset | 1 | GG-026 (playoff policy decided at dataset level) |
| 2B.3 — evaluation harness | 0 | Measurement only; **opened GG-028**; LEAK-001 narrowed no further |
| 2C — cold-start estimator | 0 | GG-028 addressed via inputs (raw model unchanged); **opened GG-029** |
| 2D — discrimination research | 0 | Research only, nothing promoted; GG-029 confirmed on clean holdout; **opened GG-031** |
| 2E — new-information research | 0 | Stage 0 only, **FAILED its own pre-registered gate**; nothing built, nothing promoted; **opened GG-032** |
| **Total** | **11** | 23 remain open, incl. LEAK-001 and R3-001 |






New in Epic 1B.2: **GG-024** (HIGH) — the ESPN team endpoint ignores `?season=`.

New in Epic 1B.3: **GG-002-B** (HIGH) — two of the five GG.md hard filters
(knockout-first-leg, heavy-favourite mismatch) have **no data source at all**. Split out of GG-002 so
the resolved part is not held open by a distinct problem: GG-002 was a *wiring* defect (fabricated
values reaching real filters), whereas GG-002-B is a *missing feed*.

New in Epic 1B.4: **no new issues, nothing closed.** GG-002-B is **narrowed** — the clean-sheet feed
now exists (derived from ESPN match-level schedule records), leaving knockout-first-leg and
heavy-favourite mismatch still without a source. GG-024 gains a finding: the **schedule** endpoint
does honour `season=`, though the **team-statistics** endpoint still does not, so GG-024 stays open.
LEAK-001 is **explicitly not** closed — see its entry.

Found in Epic 2A (read-only audit), fixed in Epic 2B.1: **GG-025** (CRITICAL) — historical season
membership was defined by a constructed July→June date window rather than by provider season
metadata, which deleted 221 real fixtures and injected the same 221 into the following season.

New in Epic 2B.1: **GG-026** (HIGH, open) — whether promotion/relegation playoff fixtures belong in
a regular-season team-strength dataset is an undecided modelling policy, deliberately not settled
inside provider parsing. **GG-027** (MEDIUM, open) — ESPN's eng.1 2009/10 metadata is
self-contradictory and the season is refused rather than imported.




---

## CRITICAL

### GG-001 — Missing statistics are silently converted to `0` — ✅ RESOLVED (Epic 1B.1)
- **Component:** ESPN provider
- **Problem:** `get_stat()` returns `0` for any statistic ESPN omits. `poisson.py` guards `None` and
  negatives but accepts `0.0` as valid. Missing data and genuine zero are indistinguishable everywhere.
- **Evidence:** `espn.py:101`
  `return next((s.get("value", 0) for s in stats_list if s.get("name") == name), 0)`.
  `poisson.py:44` guard is `if val is None or val < 0`. Same pattern in `main_run3.py`.
- **Impact:** The model produces confident-looking probabilities from data that was never received.
  Directly contradicts `GG.md` §6 ("if any of these are missing → NO BET"). This is the single most
  consequential defect: every other data problem is amplified because nothing can report "unavailable".
- **Future action:** Introduce an explicit `DATA_UNAVAILABLE` sentinel or `Optional` propagation.
  Provider returns absence as absence; the model refuses to score rather than substituting.
- **RESOLVED — Epic 1B.1.** Absence is now represented as `None` and refused before the model runs.
  - `espn.get_stat()` distinguishes three cases: entry absent → `None`; entry present with no `"value"`
    key → `None`; entry present with value `0` → `0` (genuine zero, real data).
  - `domain/` adds typed contracts (`TeamStats`, `LeagueStats`, `DataQuality`) where every optional
    statistic is `Optional[float]`, plus `is_available()` — an explicit `is not None` check, since
    truthiness treats a genuine `0.0` as absent.
  - `validate_poisson_inputs()` checks the five required model inputs and returns **no inputs at all**
    when any is unavailable. Nothing is substituted: not `0`, not the league average, not another
    team's figures.
  - Both entry points refuse instead of predicting. In `analyze_all.py` this closes the worst path:
    a missing statistic previously drove `P(GG_YES)` to `0.0`, so `1 - 0.0` published a **100%-confident
    `GG_NO`** that could be classified `STRONG_VALUE` / `RECOMMEND_PLAY` against real odds.
  - Filter inputs are guarded too — they can now be `None`, and `None < 1.0` raises `TypeError`.
    An unevaluable filter rejects the fixture rather than comparing against an invented number.
  - **`poisson.py` was not touched.** POISSON_V1 remains the frozen baseline and still accepts `0.0`
    as valid, which is correct — a genuine `0.0` *is* valid. The fix was to stop fabricating one.
  - Spec disagreement **D2 is resolved**: `GG.md` §6 ("if any of these are missing → NO BET") now holds.
  - **Verification:** 946 tests pass, ruff and mypy clean; the 51-test golden regression suite is
    unchanged, confirming identical output for complete data.
    New coverage: `tests/unit/test_domain_contracts.py`, `tests/unit/test_espn_missing_data.py`,
    `tests/integration/test_pipeline_missing_data.py`. Detail: `docs/EPIC_1B1_DATA_CONTRACTS.md`.
- **Still open (deliberately out of scope):** GG-002 (clean-sheet rates still hardcoded `0`),
  GG-003 (league average still fabricated inside the provider, so callers cannot attribute it — hence
  the `UNATTRIBUTED` source), GG-004 (home/away counts still halved). Each changes production output
  and needs its own sub-epic.
  - **Update (Epic 1B.2):** GG-003 and GG-004 are now resolved — see below. **GG-002 remains open:**
    ESPN supplies no clean-sheet data at all, so the hardcoded `0`s are still in `espn.py`. They were
    left deliberately: the domain contract can already represent them as unavailable, but switching
    them would make every fixture fail the clean-sheet filter and change production output, which is
    GG-002's job, not this sub-epic's.

### GG-002 — Four of five documented hard filters cannot fire — ✅ RESOLVED (Epic 1B.3)

- **Component:** Filters + provider + entry points
- **Problem:** Three filter flags are hardcoded by callers; the clean-sheet input is hardcoded `0` by
  the provider; the one remaining filter receives the wrong quantity.
- **Evidence:**
  - `espn.py:127-128` → `home_clean_sheet_pct = 0`, `away_clean_sheet_pct = 0`. Filter fires only when
    `> 0.40`; verified `0 > 0.40` is never true.
  - `main.py:104-106` → `is_knockout_first_leg=False`, `is_heavy_favorite_mismatch=False`,
    `has_reliable_data=True`.
  - Simulated with the repo's own constants: a side with 5 GF / 30 GA in 20 games yields
    `total_goals_avg = 1.75 > MIN_AVG_GOALS 1.0` → passes.
  - **Empirical:** `passes_filters: true` in **39 of 39** committed fixtures across two dates.
- **Impact:** `GG.md` §9 calls these "mandatory… they protect the bankroll". In practice there is
  effectively no safety layer between the model and a bet recommendation.
- **Future action:** Source real clean-sheet data (`api_football.py` and `sportmonks.py` already
  compute it), derive the three flags from fixture metadata, and correct the goals-average input.
- **RESOLVED — Epic 1B.3.** No fabricated value reaches any filter. The thresholds were never wrong;
  the inputs were. `filters.py` and `config.py` are **byte-identical** — the fix was entirely upstream.
  - **The clean-sheet literals are gone.** `espn.py` returns `None`, not `0`. This is the crux: `0`
    was not a harmless placeholder, it was the *assertion* "this team has never kept a clean sheet",
    and since `0 > 0.40` is never true the filter approved **every fixture it ever saw**.
  - **The three hardcoded flags are gone from `main.py`.** `has_reliable_data` is now computed from
    actual availability rather than asserted `True`.
  - **Unavailable no longer means pass.** `domain/filter_evaluation.py` returns `UNEVALUATED` and
    **does not call `apply_filters` at all** when a required statistic is missing. There is no honest
    value to pass: `0.0` lies about the team and `0.5` is a lie chosen because it passes.
  - **FAILED and UNEVALUATED are now different facts.** Conflating them is what made this invisible —
    "passed filters" was indistinguishable from "filters never ran".
  - **Clean-sheet data is UNAVAILABLE, and that is a mathematical finding, not a parsing gap.** ESPN's
    standings give aggregate goals-against only. `GA = 5` over 5 matches is consistent with **0** clean
    sheets (conceded 1,1,1,1,1) and with **4** (conceded 5,0,0,0,0). No function of `(GA, matches)`
    distinguishes them, so any clean-sheet rate derived from the aggregate is an approximation — and an
    approximation presented as a measurement is the exact defect being closed. The correct derivation
    is implemented and tested in `domain/match_records.py` against match-level records, ready for a
    provider that supplies them.
  - **Consequence, stated plainly:** with clean-sheet data unavailable, **no fixture currently reaches
    a recommendation**. That is the correct behaviour of a safety layer that was previously disabled,
    and is **not** grounds for loosening anything. The probability is still calculated and displayed
    when the five POISSON_V1 inputs are present — model completeness and filter completeness are
    separate questions.
  - **Verification:** 1206 tests pass (2 skipped), ruff and mypy clean; golden regression 884 passed,
    outputs identical. `git diff HEAD -- poisson.py config.py filters.py decision.py run3/` is empty.
    New coverage: `tests/unit/test_filter_evaluation.py` (36), `tests/unit/test_match_record_derivations.py`
    (28), `tests/integration/test_entry_point_consistency.py` (11).
    Detail: `docs/EPIC_1B3_FILTER_WIRING.md`.
- **Still open — split out as GG-002-B (HIGH):** the knockout-first-leg and heavy-favourite-mismatch
  filters have **no data source at all**. They are now `False` in one visible place on the contract
  rather than as literals buried at two call sites, but they still cannot fire. Closing them needs a
  competition-format/market feed, not a wiring change. Spec disagreement **D3 remains partially open**
  for this reason.
  - **Update (Epic 1B.4) — GG-002-B narrowed, still open.** The clean-sheet feed anticipated above now
    exists: `espn.py` converts team-schedule events into `MatchRecord`s and
    `domain/match_records.derive_history()` derives the rate exactly from goals-against, so the
    derivation written in 1B.3 is no longer waiting on a provider. **Two of the three gaps remain
    unresolved and were not faked:** knockout-first-leg still needs a competition-format feed, and
    heavy-favourite mismatch still needs a market/favouritism signal. Both are still `False` on the
    contract and still cannot fire, so **D3 stays partially open**.



### GG-003 — League average goals is always the hardcoded `1.35` — ✅ RESOLVED (Epic 1B.2)
- **Component:** ESPN provider
- **Problem:** The `/standings` endpoint returns HTTP 200 with an empty body, so every call falls
  through to the fallback constant. Because the status code is 200, nothing errors or logs.
- **Evidence:** Live read-only verification, 2026-08-07:
  ```
  eng.1 -> HTTP 200, bytes=2, body: {}
  ger.1 -> HTTP 200, bytes=2, body: {}
  esp.1 -> HTTP 200, bytes=2, body: {}
  bra.1 -> HTTP 200, bytes=2, body: {}   (in-season, still empty)
  ```
  `espn.py:139-142` → `if not data or "children" not in data: return 1.35`.
  Two further `1.35` fallbacks at `espn.py:157,162`; more at `main.py:171`, `analyze_all.py:182`.
- **Impact:** `league_avg_goals` is the **denominator of both λ values**, so a single fabricated
  constant scales every prediction the system makes. Corroborated by the committed output: λ_home 4.36
  for Strasbourg vs Metz is not a plausible expected-goals figure.
- **Future action:** Compute the league average from a working endpoint or a stored historical table.
  On failure, return an explicit unavailable state — never a plausible constant.
- **RESOLVED — Epic 1B.2.** The root cause was a **wrong endpoint path**, not merely a bad fallback.
  - **Wrong address, right question.** `/apis/site/v2/sports/soccer/{league}/standings` answers
    **HTTP 200 with a 2-byte body `{}`** (verified live). Because the status was 200 nothing raised,
    so every call fell silently through to the constant — no error, no log, no signal.
  - The working path is `/apis/v2/sports/soccer/{league}/standings` (note: **no `/site` segment**),
    verified live returning ~68KB of real standings. Added as `config.ESPN_STANDINGS_BASE_URL`.
  - The `1.35` fallback is removed from `espn.get_league_avg_goals()` **and from both callers**
    (`main.py`, `analyze_all.py`). `analyze_all.py`'s `or 1.35` was **doubly wrong**: `or` is a
    truthiness test, so it also replaced a genuine `0.0` with the constant.
  - Unavailable now returns `None`, which `validate_poisson_inputs()` refuses (the Epic 1B.1
    contract). The fixture yields **no prediction** rather than one scaled by an invented denominator.
  - **The constant was not merely unsourced — it was wrong.** Measured EPL 2025-26: **1.3750**
    (1045 goals / 760 team-games). Being close enough to look plausible is precisely why it survived
    undetected through every previous review.
  - Units are pinned in the docstring: goals per **team** per match. A standings table counts each
    fixture twice, so summing `gamesPlayed` gives team-games; using the per-fixture figure (2.7500)
    would have halved every λ.
  - **Integrity checks added:** league-wide goals scored must equal goals conceded (every goal is
    both — a mismatch means a truncated or inconsistent table; verified live EPL 1045 == 1045), and a
    partial table (any team missing `pointsFor`/`gamesPlayed`) is refused rather than averaged. A
    preseason table with zero matches played returns `None`, not `0`.
  - Spec disagreement **D5 is resolved**; covered by a regression test.
  - **Verification:** 1129 tests pass (3 skipped — the unresolved D1/D3/D4 spec decisions), ruff and
    mypy clean. The frozen POISSON_V1 golden regression file is untouched.
    New coverage: `tests/unit/test_espn_league_average.py`, `tests/unit/test_espn_transport.py`,
    `tests/unit/test_espn_provider.py`. Live re-verification: `scripts/espn_diagnostic.py`, which
    probes both paths side by side so the 200-with-`{}` failure is reproducible on demand.

### GG-025 — Season membership was defined by a date window, not by season identity — ✅ RESOLVED (Epic 2B.1)
- **Component:** ESPN provider (historical retrieval) · **Found:** Epic 2A · **Fixed:** Epic 2B.1
- **Problem:** `espn._season_date_range(season)` built a fixed `{season}0701-{season+1}0630` window and
  every event the window returned was treated as belonging to that season. Date-range membership was
  the *definition* of season membership; no event metadata was consulted.
- **Evidence:** Measured across 5 production leagues × 4 seasons from the Epic 2A cache. Seasons that
  ran past 30 June were truncated, and the same fixtures were admitted into the following season:

  | league | season | true | old rule | lost | wrongly admitted |
  |---|---|---|---|---|---|
  | eng.1 | 2019 | 380 | 314 | 66 | 0 |
  | eng.1 | 2020 | 380 | 446 | 0 | 66 |
  | ita.1 | 2019 | 380 | 282 | 98 | 0 |
  | ita.1 | 2020 | 380 | 478 | 0 | 98 |
  | esp.1 | 2019 | 380 | 323 | 57 | 0 |
  | esp.1 | 2020 | 380 | 437 | 0 | 57 |

  **221 real fixtures deleted; the same 221 injected into the wrong season.** Three clubs relegated
  after 2019/20 (Bournemouth 349, Norwich 381, Watford 395) appeared inside 2020/21 results.
- **Impact:** Every historical dataset, backtest, calibration and cold-start measurement built on this
  retrieval would have been contaminated — and invisibly so, because 14 of the 20 audited
  league-seasons were unaffected and returned exactly the expected 380/306.
- **RESOLVED — Epic 2B.1.** Season identity is now taken from the **event**, not the calendar.
  - **Discovery and validation are separate.** `_season_discovery_windows()` returns a deliberately
    broad candidate range (the season's window plus the following one); membership is decided solely by
    `domain/season_identity.classify_event_season()`, the single chokepoint.
  - **A wider window was explicitly rejected as the fix.** It cannot work: eng.1 2019/20 ended
    2020-07-26 and eng.1 2020/21 began 2020-09-12, so no boundary separates them for all leagues in
    all seasons — and widening the season's own window necessarily widens the next one, which is what
    caused the contamination. ESPN also refuses ranges over 366 days (`dates` +1 day → **HTTP 400**),
    so the option does not exist even if it were correct.
  - **`season.year` is corroborated, not trusted blindly.** Where `season.slug` encodes a season and
    contradicts `season.year`, the event is **refused**. This is not hypothetical: eng.1's 2009 window
    carries 380 events labelled `season.year = 2009` with slug `2013-2014-…` and **wrong scores**
    (Chelsea 0-1 Hull for a match that finished 2-1). 45,657 corpus events agree; 380 disagree and are
    now rejected rather than imported as fabricated history.
  - **Fails closed.** Missing, non-integer, boolean or contradictory season metadata yields
    `UNVERIFIABLE`. Nothing is inferred from kickoff, team membership, calendar year or the requested
    season, and `0` is never substituted.
  - **Competition identity is a separate invariant**, checked independently from `event.uid`
    (scoreboard) or `event.league.slug` (schedule).
  - **Verified live:** eng.1 2019 → 380 records, 66 July fixtures preserved; eng.1 2020 → 380 records,
    relegated clubs absent, zero leakage. Current-season retrieval still issues one request.
  - **Regression-protected:** `tests/regression/test_season_integrity.py` (73 tests) keeps the old rule
    stated as an explicit function and asserts it gives the wrong answer on real data. Mutation-tested:
    7 weakenings of the guard, **7 killed**. Detail: `docs/EPIC_2B1_SEASON_INTEGRITY.md`.

### R3-001 — Run-3 is mathematically incapable of producing a selection

- **Component:** Run-3 decision logic
- **Problem:** Both decision branches are unreachable.
  - **R3-NO:** requires `P_R3_NO ≥ 0.78`. Since `P_R3_NO = (1-p³)(1-(1-p)³)`, which is maximised at
    `p = 0.5` giving `0.875 × 0.875 = 0.765625`, the threshold exceeds the function's global maximum.
  - **R3-YES:** requires dominance (`p ≥ 0.65 AND λ ≥ 2.2`) — precisely the states `run3_filters.py`
    rejects (`p ≥ 0.65` or `λ ≥ 2.2`). The two rules are mutually exclusive.
- **Evidence:** `run3_decision.py:26` (`R3_NO_MIN_PROB = 0.78`) vs `run3_probability.py:56-60`.
  Exhaustive simulation over 1,000,000 λ pairs (0.01–10.00): **0 states satisfy R3-NO**.
  Over 640,000 λ pairs: **0 states satisfy R3-YES**. Max achievable `P_R3_NO` = 0.765625.
- **Impact:** `main_run3.py` returns `SKIP` for every fixture, always, for any input. It runs, prints
  and writes JSON — so the output looks like a legitimately quiet day rather than a dead model. The
  whole Run-3 subsystem (757 lines) has never produced a single selection and cannot.
- **Root cause:** spec/code divergence. `run-3.md` specifies **0.75**; the code uses **0.78**. At the
  documented 0.75, the same exhaustive search finds **4,367 reachable states** (e.g. λ_h 0.9, λ_a 1.1 →
  `P_R3_NO` 0.7577). `run3_decision.py` also adds `R3_NO_MIN_TOTAL_GOALS`, `R3_NO_MAX_TOTAL_GOALS` and
  `R3_NO_MIN_EDGE`, none of which appear in the spec.
- **Future action:** Decide which side is authoritative (a product decision, not a code fix), then
  reconcile filters and decision so the reachable region is non-empty. Add a startup assertion that
  every threshold is satisfiable.

### LEAK-001 — Structural look-ahead bias makes backtesting invalid
- **Component:** Provider layer / any future evaluation
- **Problem:** `get_team_stats()` takes no date or matchweek parameter. `get_fixtures()` does. Running
  a past date therefore pairs a historical fixture list with **today's** cumulative season statistics.
- **Evidence:** `espn.py:83` signature `get_team_stats(league_id, team_id)` — no temporal argument.
  ESPN's `/teams/{id}` endpoint only serves current-season cumulative totals (verified live). The CLI
  accepts any date (`main.py:186`).
- **Impact:** Predicting Matchweek 10 today would use statistics containing every match played *after*
  Matchweek 10. Any accuracy figure produced this way is inflated and meaningless. Dangerous
  specifically because the date argument makes it look like a legitimate backtest. A
  Dixon-Coles-vs-Poisson comparison on leaking data would be worse than no comparison — it would
  produce confident numbers justifying a wrong choice.
- **Secondary leak:** within a single matchday, a team playing earlier has that result folded into its
  totals before a later fixture is predicted.
- **Future action:** Store point-in-time team-stat snapshots keyed by `(team, as_of_date)`. All model
  input must flow through an as-of cutoff. This is the prerequisite for Epic "historical evaluation" and
  must land before any model comparison.
- **Update (Epic 1B.4) — STILL OPEN. Deliberately NOT closed.** Epic 1B.4 introduced a strict
  `record.kickoff < target_kickoff` cutoff, so the *match-history* input is now point-in-time correct
  and the fixture cannot appear in its own history. **This does not resolve LEAK-001, and backtesting
  remains invalid.** One input is now clean; the others are not:

  | Input | Point-in-time correct? |
  |---|---|
  | Match history (clean-sheet / BTTS) | ✅ Yes — cutoff enforced |
  | Team aggregate statistics (λ inputs) | ❌ No — current-season-only (GG-024) |
  | League average goals | ❌ No — present-day standings |
  | Odds | ❌ No — current market |

  Evaluating a past fixture would still compute λ from results that had not been played at kickoff.
  The partial fix arguably makes the danger **worse**, not better: the filters would be honest while
  the probability they gate was computed with hindsight, so the output looks trustworthy and is not.
  LEAK-001 closes only when **every** model and filter input flows through one as-of cutoff.

- **Update (Epic 1B.5) — NARROWED TO ODDS. Still OPEN. Backtesting is still NOT safe.** All five
  POISSON_V1 inputs are now derived from completed matches with `kickoff < target`, so the λ inputs
  and the league baseline no longer come from present-day aggregates. `get_team_stats()` is not
  consulted on the model path at all; the table above becomes:

  | Input | Point-in-time correct? |
  |---|---|
  | Match history (clean-sheet / BTTS) | ✅ Yes — cutoff enforced (1B.4) |
  | Team λ inputs (goals scored/conceded by venue) | ✅ Yes — derived from pre-kickoff matches (1B.5) |
  | League average goals | ✅ Yes — derived from pre-kickoff matches (1B.5) |
  | Odds | ❌ **No — current market only** |

  **Why this is still not closed.** Three reasons, each sufficient on its own:

  1. **Odds are still today's.** `decision.py` compares the model probability against the price
     available *now*. Every recommendation, edge and value classification for a past fixture would be
     computed against a market that did not exist at kickoff. Since the edge decides whether a bet is
     placed, a backtest of *recommendations* remains invalid even with a perfectly clean probability.
  2. **The statistic endpoint has not changed** (GG-024). The point-in-time λ inputs come from the
     *schedule* endpoint; `get_team_stats()` is still current-season-only and is still used for
     display and diagnostics. It must never be reintroduced as a model fallback — a regression test
     now makes calling it from the model path an outright error.
  3. **Correct mechanics are not a validated backtest.** No historical run has been executed,
     scored, or compared against a holdout. "The inputs respect a cutoff" and "the measured accuracy
     is trustworthy" are different claims, and only the first is supported.

  **What is genuinely established** is narrower and worth stating precisely: given a target kickoff,
  the five model inputs are invariant to everything that happens at or after it. That is verified
  behaviourally rather than by inspection — `tests/regression/test_point_in_time_inputs.py` adds 30
  future matches and a whole future league programme and requires the inputs to be **byte-identical**,
  and the suite was mutation-tested (weakening `<` to `<=` and removing the cutoff each produce
  failures) to confirm the guard actually bites rather than passing vacuously.

  **Live confirmation (2026-08-09, read-only).** For a 2026-02-08 target: Arsenal HOME
  n=13, 2.385 GF / 0.615 GA; Chelsea AWAY n=13, 1.923 GF / 1.154 GA — 13 matches, not the
  19 a full-season aggregate would supply, which is the cutoff visibly doing its job on real data.

- **Update (Epic 2B.3) — STILL OPEN, narrowed no further. Probability quality is now measured;
  betting value is still not.** Epic 2B.3 built a point-in-time evaluation harness and ran it over
  7,234 real historical fixtures. Two things changed, and neither of them is the odds row:

  1. Point 3 above ("no historical run has been executed, scored, or compared") **is now
     satisfied for the probability**. POISSON_V1 has been replayed under a strict
     `kickoff < target` cutoff and scored: Brier 0.2657, coverage 0.9614, against a naive
     point-in-time base rate at 0.2479. The mechanics are no longer merely believed correct;
     they have produced numbers.
  2. The odds row of the table above is **unchanged and remains the blocker**. The harness is
     forbidden by an import-level regression test from reaching odds, prices, edges, thresholds
     or `decision.py` (`tests/regression/test_evaluation_leakage.py`). That firewall exists
     precisely because a recommendation backtest would be the most attractive-looking and least
     valid output the project could produce.

  **The distinction to hold onto:** "how good is the probability" is answerable today and has been
  answered. "Would these recommendations have made money" is not, because every historical edge
  would be computed against a market that did not exist at kickoff. LEAK-001 closes when odds are
  stored point-in-time, not before.



---

## HIGH

### GG-004 — Home/away match counts fabricated by halving — ✅ RESOLVED (Epic 1B.2)
- **Component:** ESPN provider · **Evidence:** `espn.py:117-118`, `main_run3.py:166-167`,
  `sofascore.py:134-145` (which halves goals and clean sheets too).
- **Impact:** Home/away schedules are genuinely uneven. Live-verified: Aalesund 9 home vs 6 away;
  AIK 7 vs 8; Athletico-PR 11 vs 10. Halving distorts every per-match rate fed to λ.
- **Future action:** Use the real split counts (they are present in the API response); fail explicitly
  when absent.
- **RESOLVED — Epic 1B.2.** ESPN **does** supply `homeGamesPlayed`/`awayGamesPlayed` (confirmed live
  on the team endpoint), so the real counts are now used and the `matches_played / 2` fabrication is
  removed from `espn.py`.
  - A missing count yields `None` — absence is absence, not a guess.
  - **A split with 0 games played yields `None`, not `0.0`.** Zero matches means the rate is
    UNDEFINED. Reporting `0.0` would assert "this team scores zero per home match", which is a
    different and much stronger claim than "this team has not played at home yet".
  - `home_matches`/`away_matches` are returned alongside the rates, so a caller can see the split it
    actually received rather than inferring one.
  - **Scope:** `espn.py` only. `run3/main_run3.py:166-167` and `sofascore.py:134-145` still halve
    (the duplication tracked as GG-010); both remain dead or out-of-path for the ESPN pipeline.

### GG-005 — Only season-long cumulative stats; no form or recency
- **Component:** ESPN provider · **Evidence:** `espn.py:83-133` reads only `record.items[0]` totals.
- **Impact:** A team's August form is weighted identically to its May form. No recency weighting is
  possible, and no matchweek-scoped query exists — this is also the mechanism behind LEAK-001.
- **Future action:** Fixture-level history in storage; derive rolling windows from it.

### GG-006 — Two entry points apply different filter semantics — ✅ RESOLVED (Epic 1B.3)

- **Component:** Entry points · **Evidence:** `main.py:100` passes `total_goals_avg` = `(GF+GA)/matches`;
  `analyze_all.py:97` passes `home_goals_scored` (home scoring rate). Both land in the parameter
  `home_avg_goals`, compared against `MIN_AVG_GOALS = 1.0`.
- **Impact:** The same fixture can pass one script and fail the other. Neither matches `GG.md`'s
  "one team averages < 1.0 goal", which reads as goals *scored*. Results are not comparable between
  scripts, and neither is reproducible against the spec.
- **Future action:** One service layer, one filter contract, named unambiguously
  (`goals_scored_per_match` vs `total_goals_per_match`).
- **RESOLVED — Epic 1B.3.** The intended meaning was recoverable, so no new interpretation was invented.
  Three independent sources agree the statistic is goals **scored**: `GG.md` §9 ("one team averages
  < 1.0 goal"), the `filters.py` parameter docstring ("average goals per match" *for that team*), and
  `analyze_all.py`, which was already passing `home_goals_scored`. **`main.py` was the sole outlier.**
  - `total_goals_avg` is `(GF + GA) / matches` — a different statistic entirely. It measures **how
    eventful a team's matches are**, not how reliably it scores.
  - The consequence was not academic, and is now a regression test: a side with **5 scored / 30 conceded
    in 20 matches** has a home scoring rate of **0.30** (fails) but a `total_goals_avg` of **1.75**
    (passes). A team that cannot score but leaks goals was being **approved** by the very filter meant
    to exclude that profile.
  - **One mapping point:** both entry points now call `domain.build_filter_stats`, the only place
    either script decides what a filter input means. Fields are named for their semantics
    (`home_avg_goals_scored`), because `home_avg_goals` was vague enough that passing a combined figure
    did not look wrong at the call site — a self-describing name would have made the bug visible.
  - Neither entry point imports `filters` any more, and a **structural test asserts they never do
    again**, so the two cannot drift apart a second time.
  - Spec disagreement **D4 is resolved** — its skipped test is now an active regression test.
  - **Verification:** `tests/integration/test_entry_point_consistency.py` (11 tests) drives both entry
    points from one mocked ESPN response and requires identical verdicts and identical reasons.


### GG-007 — Falsy-edge bug turns a zero edge into "no odds"
- **Component:** `shared/odds.py`, `analyze_all.py` · **Evidence:** `shared/odds.py:319`
  `"edge": round(edge, 4) if edge else None`; same at `:318` for `implied_probability` and
  `analyze_all.py:242`. Verified: `round(0.0, 4) if 0.0 else None` → `None`.
- **Impact:** An exactly-zero edge is serialised as `null`, indistinguishable from "odds unavailable".
  Corrupts any downstream analysis that counts missing odds.
- **Future action:** Use `is not None` checks rather than truthiness.

### GG-008 — Substring team-name matching for odds
- **Component:** Both odds clients · **Evidence:** `odds_api.py:90-92`, `shared/odds.py:209-210`
  (`if home in api_home or api_home in home`).
- **Impact:** Real collision risks: `"Athletic Club"`/`"Athletic"`, `"Milan"`/`"AC Milan"`/`"Inter Milan"`,
  `"Boca"`/`"Boca Juniors"`. A false positive attaches **another match's odds** to a fixture, producing a
  fabricated edge on real money. No canonical team-ID mapping exists anywhere in the repo.
- **Future action:** Canonical team registry with provider-specific alias tables; exact-match only.

### GG-009 — Dead providers would silently return zero fixtures
- **Component:** SportMonks (and SofaScore by the same pattern) · **Evidence:** `sportmonks.py:96`
  `if league_id not in ALLOWED_LEAGUES` where `league_id` is an **int** and the dict keys are **strings**.
- **Impact:** Never matches → zero fixtures, no error, no warning. A trap for whoever re-enables it,
  and it will look like "no matches today".
- **Future action:** Normalise league identifiers behind the provider interface; add a smoke test that
  asserts a non-empty result for a known-populated date.

### GG-024 — ESPN team endpoint ignores the `season` parameter
- **Component:** ESPN provider · **Discovered:** Epic 1B.2
- **Problem:** `/{league}/teams/{id}` serves **current-season cumulative totals only**. The `season`
  query parameter is accepted without error and has no effect on the response.
- **Evidence:** Live read-only verification during the 2026-27 preseason: the same team endpoint
  requested **with** `season=2025` and **without** any season parameter returned the same record,
  both reporting `gamesPlayed = 0.0`. Had the parameter been honoured, `season=2025` would have
  returned the completed 2025-26 totals. Reproducible via `scripts/espn_diagnostic.py`.
  Note the contrast: the **standings** endpoint (`ESPN_STANDINGS_BASE_URL`) *does* honour `season`,
  so the two ESPN endpoints disagree on whether the parameter means anything.
- **Impact:** Team statistics are **current-season-only**, so **historical backtesting cannot use this
  endpoint** — there is no way to ask it what a team's record was at a past point. This is the
  mechanism underlying LEAK-001: `get_team_stats()` has no temporal argument because the API has no
  temporal dimension to pass it to. Adding an `as_of` parameter here would produce a signature that
  *looks* point-in-time while silently returning today's figures — worse than the current honest gap.
  Separately, during preseason **every team returns zeros**; since Epic 1B.2 that correctly yields
  **no prediction** (`matches_played == 0` → `None`) rather than a fabricated one, so the failure is
  now loud instead of silent.
- **Future action:** Do not attempt historical work through this endpoint. Point-in-time team stats
  must come from stored fixture-level history (see LEAK-001 and GG-005), which is the prerequisite for
  any model comparison or backtest.
- **Update (Epic 1B.4) — new finding, still OPEN.** The **schedule** endpoint
  (`/{league}/teams/{id}/schedule`) *does* honour `season=`. Verified by comparing returned **event
  IDs**, not by parameter acceptance: `season=2026` returned 0 events and `season=2025` returned 38,
  with **zero shared IDs** — genuinely different data, not a re-labelled current season. That is a
  third ESPN behaviour, alongside standings (honours it) and team statistics (ignores it).
  **GG-024 stays open regardless.** It concerns the **team-statistics** endpoint, which still serves
  current-season totals only. That endpoint supplies the goals scored/conceded aggregates POISSON_V1
  needs, so historical λ inputs remain unavailable and no backtest is unblocked. A schedule endpoint
  that serves history does not fix a statistics endpoint that does not.


### GG-026 — Playoff/postseason inclusion is an undecided modelling policy — ✅ RESOLVED (Epic 2B.2)
- **Component:** Historical dataset / modelling policy · **Opened:** Epic 2B.1 · **Closed:** Epic 2B.2

- **Problem:** ESPN places promotion/relegation playoff fixtures in the **same competition** and the
  **same `season.year`** as the league programme, distinguished only by `season.slug`. Whether they
  belong in a dataset used for **regular-season** team-strength modelling is a statistical question,
  and no answer is currently recorded.
- **Evidence:** `promotionrelegation-playoffs` / `promotion-playoff-quarterfinals` in fra.1 (6 events
  across 20 seasons), `relegation-playoff` in ita.1 2022/23 (1 event: Spezia 1-3 Hellas Verona,
  2023-06-11 — a genuine third meeting, not a duplicate), and ~100 events in eng.2. **Material impact
  on production leagues today: ≈7 matches.**
- **Why it was not decided in the provider:** the obvious rule ("keep only `regular-season`") would
  **delete an entire legitimate Bundesliga season** — ESPN labels 303 ordinary ger.1 2010/11 fixtures
  `group-stage`. A policy that destroys real data to exclude 7 matches is not a parsing default.
- **Current behaviour — unchanged from before Epic 2B.1:** the phase is captured as
  `MatchRecord.season_phase` (provenance) and **never filters**. Nothing is excluded and nothing is
  silently included that was not included before.
- **Decision required (product/statistical, not provider):** should promotion/relegation playoff
  fixtures be included in regular-season team-strength datasets? **Recommendation:** exclude, as a
  different competitive context — but implemented at dataset-construction level in Epic 2B.2 using
  `season_phase`, with per-league validation, **not** inside provider parsing.
- **RESOLVED — Epic 2B.2.** The recommendation was implemented exactly as scoped: at
  dataset-construction level, from `season_phase`, outside provider parsing.
  - **Decision recorded: excluded from model training, retained in the dataset.**
    `domain/historical.classify_model_eligibility()` labels each record `ELIGIBLE` / `INELIGIBLE` /
    `UNCERTAIN`; `model_dataset()` is a **view** that narrows at the point of use. Nothing is deleted,
    so the decision is reversible by a later modelling Epic without re-fetching a single byte.
  - **The rule matches on postseason markers, never on `phase != "regular-season"`** — the trap this
    item warned about. Verified: the 303 `group-stage`-labelled ger.1 2010/11 fixtures remain
    `ELIGIBLE`, and a regression test asserts that whole legitimate season is not deleted.
  - **Measured on real data (cache replay, 5 leagues × 4 seasons, zero network):** fra.1 2018/19
    returns **384** accepted records = 380 league + **4 promotion-playoff ties** (Paris FC–Lens
    `STATUS_FINAL_PEN`, Troyes–Lens `STATUS_FINAL_AET`, Lens–Dijon ×2), all correctly `INELIGIBLE`
    and all still on disk. Every other audited league-season matched its expected count exactly.
  - **STATUS_FINAL_PEN is answered by this too:** the penalty-decided fixtures found in Epic 2A are
    postseason ties, not league matches. "Has a final score" is therefore **not** treated as "valid
    regular-season match" — completion semantics were left unchanged, and eligibility is a separate
    axis, as this item required.
  - **`UNCERTAIN` is not silently trained on.** An unrecognised phase is excluded from the model view
    and reported in the build manifest, so "I do not know what this is" never becomes evidence.


### GG-027 — ESPN's eng.1 2009/10 season metadata is self-contradictory
- **Component:** ESPN provider (historical data quality) · **Opened:** Epic 2B.1 · **OPEN**
- **Problem:** 380 events in eng.1's 2009 window carry `season.year = 2009` alongside
  `season.slug = "2013-2014-barclays-premier-league"`. Spot-checked against real results, the block is
  corrupt at source: it repeats the 2009-10 fixture list with **wrong scores** (Chelsea 0-1 Hull, for
  a match that really finished 2-1).
- **Impact:** The season is **refused** by the Epic 2B.1 fail-closed rule (`UNVERIFIABLE`), so it is
  unusable rather than wrong — which is the correct outcome. Trusting `season.year` alone would have
  imported 380 fabricated results into the historical dataset.
- **Scope:** Bounded. 380 of 45,657 corpus events with a season-encoding slug disagree; the other
  45,657 agree. Only matters if history before 2010 is wanted.
- **Action:** None required for Epic 2B.2 (which targets 2010 onwards). If pre-2010 history is ever
  needed, source it from a second provider and cross-check — do not relax the veto.

### GG-028 — POISSON_V1 returns exactly 0% BTTS on thin venue evidence
- **Component:** Model (`poisson.py`) · **Opened:** Epic 2B.3 (measurement) ·
  **ADDRESSED in Epic 2C via input estimation; STILL OPEN for the raw model**

- **Problem:** A venue average of `0.0` propagates to `lambda = 0.0`, and `P(both teams score)` is then
  **exactly 0.0**. A team whose only prior away match was a 0-goal loss is assigned a **0% chance** of
  scoring — an absolute claim derived from a single observation.
- **Evidence:** Direct call, no harness involved:
  `calculate_gg_probability(league_avg_goals=1.35, home_goals_scored_home=1.0,
  home_goals_conceded_home=1.0, away_goals_scored_away=0.0, away_goals_conceded_away=1.0)`
  → `lambda_away = 0.0`, `gg_probability = 0.0`. In eng.1 2019 this produced **17** predictions of
  exactly 0.0, and **BTTS actually occurred in 11 of them**.
- **Impact:** Measured over 7,234 fixtures (Epic 2B.3), POISSON_V1 scores **Brier 0.2657** against a
  naive point-in-time base rate at **0.2479** — worse than the trivial reference. The deficit is
  concentrated in thin evidence, not in the formula generally:

  | Prior venue matches | n | Brier |
  |---|---|---|
  | 1–2 | 40 | **0.4241** |
  | 3–5 | 60 | 0.2687 |
  | 6–9 | 80 | 0.2611 |
  | 10+ | 180 | **0.2555** |

  With 10+ matches the model is competitive; with 1–2 it is catastrophic. Log loss (1.09 vs 0.69) is
  dominated by the exact-zero predictions, which are punished at the epsilon clamp.
- **Not a data bug and not a harness bug.** This was checked before being recorded: the inputs are
  correct point-in-time derivations, and `poisson.py` reproduces the result when called directly.
  It is the model's genuine behaviour on small samples.
- **Deliberately NOT fixed in Epic 2B.3.** Changing `poisson.py` while measuring it would destroy the
  baseline. **This is the primary input to Epic 2C**, which must decide — as an explicit product
  choice, not a parsing default — whether to floor λ, shrink toward a prior, or leave thin-evidence
  fixtures unevaluable. Note that the third option **lowers coverage in exchange for calibration**,
  and on this evidence buying coverage cheaply would make the aggregate Brier worse.
- **ADDRESSED — Epic 2C, by fixing the INPUTS rather than the model.** Of the three options above, the
  second was chosen and implemented: shrink toward a prior. `poisson.py` is **byte-identical**.
  - **Mechanism, not merely frequency.** `domain/team_strength.py` replaces the raw venue ratio with a
    Gamma-Poisson posterior mean `λ̂ = (k·μ + Y)/(k + n)`. For `k > 0, μ > 0` the numerator contains
    `k·μ > 0`, so `λ̂ > 0` for **any** `Y` including zero. The exact-zero probability is now
    **arithmetically unreachable**, not just rare.
  - **No clipping was added.** There is no `max(0.05, …)` anywhere in the Epic. The extremes disappear
    because the estimate improved; had a clamp been introduced, those two explanations would be
    indistinguishable.
  - **A genuine zero remains evidence.** `Y = 0` still pulls the estimate down — it is not discarded —
    it simply can no longer certify impossibility from one observation.
  - **Measured on identical fixture intersections** (coverage differs between arms, so raw comparison
    would be invalid): exact-`0.0` predictions **19 → 0** on validation 2020 (1802 fixtures) and
    **17 → 0** on holdout 2023 (1726). The 1–2 bucket improves Brier 0.2884 → 0.2550 and log loss
    **2.0186 → 0.7039**; the 10+ bucket is unharmed (0.2468 → 0.2448).
  - **Still OPEN for the raw model.** `POISSON_V1` itself is unchanged and still returns exactly 0.0
    when handed a `0.0` venue rate — deliberately, so the baseline stays reproducible.
    `tests/regression/test_gg028_sparse_sample.py` asserts that original behaviour permanently and will
    fail if anyone "fixes" `poisson.py`. The defect is avoided by never constructing that input, which
    holds only for `POISSON_V1_SHRUNK_V1`; any caller using raw venue ratios reintroduces it.
  - Detail: `docs/EPIC_2C_COLD_START_MODEL.md`.

### GG-029 — POISSON_V1 has almost no discriminatory power, and loses to a constant
- **Component:** Model (`poisson.py`) / input signal · **Opened:** Epic 2C · **OPEN**
- **Problem:** The model barely ranks fixtures better than chance. Measured on development seasons
  (2018–2019), **ROC AUC ≈ 0.535** — for the raw baseline **and for every shrinkage configuration
  tested**, from `k=2` through `k=1000`. AUC depends only on the ordering of predictions, so this is
  not a calibration artefact: the ordering itself carries almost no information.
- **Evidence:** `research/epic2c_collapse_diagnostic.py` (reproducible, cache-backed, zero network):

  | arm | mean | sd | min | max | AUC |
  |---|---|---|---|---|---|
  | baseline raw | 0.4700 | 0.1243 | 0.000 | 0.933 | 0.5354 |
  | k=8 | 0.4955 | 0.0984 | 0.207 | 0.814 | 0.5383 |
  | k=40 | 0.5264 | 0.0599 | 0.353 | 0.715 | 0.5405 |
  | k=1000 | 0.5369 | 0.0470 | 0.430 | 0.650 | 0.5343 |

  Observed BTTS base rate **0.5202**, so a **constant predictor scores Brier 0.2496** — better than
  raw POISSON_V1's **0.2615** on the same fixtures.
- **Impact, and why it matters more than GG-028:** two consequences follow directly.
  1. **Brier is not a safe objective for tuning this model.** Because increasing prior strength
     flattens predictions toward the base rate, minimising Brier drives `k → ∞`, i.e. toward "always
     predict the base rate". Epic 2C's parameter search had **no interior optimum** for exactly this
     reason, and `k` was therefore selected by method of moments rather than by score. Any future Epic
     that optimises Brier alone will silently select a degenerate model **and it will look like a win**.
  2. **Improved calibration is not improved forecasting.** Epic 2C's gains are real but are
     error-removal, not skill. Presenting them as predictive improvement would be misleading.
- **Not caused by shrinkage, and not fixable by it.** The baseline's AUC is equally poor, so the
  deficit predates Epic 2C. Shrinkage adjusts magnitudes; it cannot create signal that the five inputs
  never contained.
- **Why this was not visible earlier:** Epic 2B.3 compared aggregate Brier across arms with **differing
  coverage** and reported no discrimination metric. Only the identical-intersection comparison plus AUC
  exposed it.
- **Action:** (a) report AUC and the constant-predictor score alongside Brier in the harness
  permanently, so a model that loses to a constant cannot look acceptable; (b) treat **discrimination**
  as Epic 2D's objective — bivariate/Dixon-Coles dependence and team-level attack/defence parameters
  attack this, whereas further prior tuning cannot. Do **not** respond by fitting a recalibration
  layer: it would improve the score while leaving the ranking, and therefore the real problem, intact.
- **Update (Epic 2D) — CONFIRMED on an untouched holdout, and action (b) is now ANSWERED: no.**
  Both recommendations were carried out. (a) AUC, prediction spread and the constant-predictor
  benchmark are now computed by `domain/discrimination.py` and printed beside every Brier in the 2D
  reports. (b) Discrimination *was* made the objective, and the structural candidates named above —
  Maher attack/defence, Dixon-Coles dependence, bivariate Poisson — were built and evaluated. **They
  do not help.** All ΔAUC 95% confidence intervals include zero on development, validation and the
  2024 holdout. On the holdout the constant still wins on Brier (0.2469 vs raw 0.2601), so this item's
  central warning survives contact with a clean partition. The "do not fit a recalibration layer"
  instruction is reinforced, not weakened, by GG-031 below: with AUC ≈ 0.54 a monotone recalibration
  provably cannot add skill. **Stays open** — it is a property of the feature set, and no work in 2D
  reduced it.

### GG-031 — Goal counts impose a hard discrimination ceiling of ≈0.568 AUC
- **Component:** Model class / feature set · **Opened:** Epic 2D · **OPEN (may be unfixable in kind)**
- **Problem:** The information needed to rank BTTS fixtures is largely **absent from goal counts**, so
  no estimator or model structure built solely on them can discriminate well. This is a stronger claim
  than GG-029 (which observed poor AUC) because it bounds what any future model of this class can
  achieve.
- **Evidence:** A deliberately **leaky** diagnostic (`research/epic2d_experiment.OracleCeilingProbe`)
  was fitted on the full dataset **including the target season and each target fixture itself**, giving
  it perfect hindsight knowledge of every team's strength. It reaches **AUC 0.5679**. Honest
  point-in-time candidates already reach **0.537–0.546**.

  | arm | AUC |
  |---|---|
  | POISSON_V1_RAW (honest) | 0.5229 |
  | C1_MAHER (honest) | 0.537–0.546 |
  | ORACLE_LEAKY_CEILING (**cheats**) | **0.5679** |

  The probe is quarantined by construction: its `model_id` is prefixed `ORACLE_LEAKY`, it is never
  registered in the harness model registry, and `tests/regression/test_epic2d_protocol.py` asserts all
  of that plus the fact that it really does see the target.
- **Impact:** Better *estimation* of the same quantities can recover **at most ~0.02–0.03 AUC**, and
  nothing can exceed the ceiling because the ceiling already cheats. Two concrete consequences:
  1. **Further structure on goal counts is not worth building.** Time decay (ξ̂ = 0 at the boundary)
     and the bivariate shared component (λ₃ not identifiable) were both rejected *by the data* in 2D,
     which is what a ceiling looks like from the inside.
  2. **Recalibration is disqualified as the next step.** A monotone recalibration cannot change
     ranking, so it would improve Brier while adding no skill — the GG-029 trap restated.
- **Caveat, stated deliberately:** the probe is in-sample, so 0.5679 is an *estimate* of the ceiling and
  most likely an **optimistic** one. That only strengthens the conclusion.
- **Action:** Do not attack this with another goal-count model. Either introduce genuinely new
  information (shots, xG, lineups, in-play state) or first measure whether the odds-derived market
  probability discriminates better than 0.568 — which would establish whether the ceiling is a property
  of football or of *this feature set*. Note the market measurement is currently blocked by LEAK-001
  (odds are not stored point-in-time).
- **UPDATE, Epic 2E:** the shot/xG half of that action is now **answered and closed** — see GG-032.
  The ceiling is a property of **football**, not of goal counts. The market half remains open and is
  still blocked by LEAK-001.

### GG-032 — The ≈0.568 ceiling is CONVERSION variance, not a goal-count limitation
- **Component:** Model class / feature set · **Opened:** Epic 2E · **OPEN (unfixable in kind — this is football)**
- **Problem:** GG-031 left open whether the ceiling was a property of *goal counts* or of *football*.
  Epic 2E answers it: **shot information does not beat it.** The limiting factor is the randomness of
  **finishing**, not noise in the rate estimate, so no better pre-match estimator of chance creation
  can help.
- **Evidence:** `research/epic2e_experiment.py`, artifacts in `research/epic2e_results/`. Deliberately
  leaky ceiling probes on 2018–19 (holdout 2025 never loaded), 5 leagues, n=3,530:

  | arm (all **cheat**) | AUC | 95% CI |
  |---|---|---|
  | goal-count ceiling (2D's probe, re-measured) | 0.5838 | — |
  | shot-strength, in-sample | 0.5737 | ΔAUC vs goals −0.0101, CI straddles 0 |
  | actual shots on target, **raw** | **0.7244** | [0.7084, 0.7416] |
  | actual shots on target, **confound-controlled** | **0.5121** | [0.4940, 0.5319] |

  **The raw 0.7244 is an artefact and must not be quoted as headroom.** Every goal *is* a shot on
  target, so `SOT == 0` proves that team did not score: the raw arm reads part of the label (182
  exact-0.0 predictions). Removing scoring shots — leaving chances *created but not converted*, the
  only part a pre-match model could forecast — collapses it to **0.5121**, a significant
  **degradation** against goal counts (ΔAUC −0.0718, CI [−0.0963, −0.0475]).
- **Impact:** Three directions are retired without further spend:
  1. **Shots/shots-on-target models** — the ceiling is below the goal-count ceiling.
  2. **xG** — xG is a weighted function of shots, so it is bounded by the same probe. No provider
     purchase is justified on discrimination grounds.
  3. **Better estimation generally** — in-sample perfect knowledge of shot profiles is
     indistinguishable from goal counts, so the residual is not estimation error.
- **What is NOT claimed:** only the shot channel is closed. A market price aggregates lineups,
  injuries and motivation — information of a *different kind*, not a better estimate of the same
  kind. It is untested and still blocked by LEAK-001.
- **Action:** Stop trying to raise BTTS discrimination with match-statistics features. The remaining
  candidate is the market probability, which requires point-in-time odds capture (LEAK-001) first.
  Consider whether ranking is the right objective at all, given a constant predictor still wins on
  Brier (GG-029).


---

## MEDIUM


### GG-010 — Run-3 duplicates the entire ESPN client, league map and λ formula

`run3/main_run3.py:37-230` re-implements `espn.py` plus `poisson.py`'s λ formula and a 36-league map.
Bug fixes must be applied twice; the two copies can drift silently.
**Action:** shared provider + shared λ once the regression tests exist.

### GG-011 — Run-3 only runs from inside `run3/`
Sibling imports (`from run3_probability import …`) with no package. `cd run3` is mandatory and
undocumented outside `run3/README.md`. **Action:** proper package + console entry point.

### GG-012 — No rate limiting, retry or backoff anywhere — ✅ RESOLVED (Epic 1B.2)
`espn.py`, `odds_api.py`, `shared/odds.py`, `main_run3.py` all issue single attempts. Run-3 makes
hundreds of sequential requests across 36 leagues. A throttled response is indistinguishable from
"no data" because of GG-001. **Action:** shared HTTP client with retry, backoff and rate limiting.

**RESOLVED — Epic 1B.2** (ESPN provider). `espn.py` now routes every request through one `_fetch()`
with an explicit `ESPN_TIMEOUT_SECONDS = 15` (previously an implicit 30s, and no timeout at all is a
hang) and a **bounded** retry: `ESPN_MAX_RETRIES = 2` (3 attempts total) with exponential backoff from
`ESPN_BACKOFF_SECONDS = 0.5`. Bounded deliberately — unbounded retry converts a permanent outage into
a hang, and hammering a free endpoint is its own failure mode.

Retry is applied **only to transient failures** — timeout, connection error, and 5xx. **4xx and
malformed JSON are permanent and are not retried**: a 404 never becomes a 200, so repeating it only
wastes time and quota. Failure modes are now named rather than collapsed (`ESPNError`: `TIMEOUT`,
`CONNECTION`, `SERVER_ERROR`, `HTTP_ERROR`, `MALFORMED_JSON`, `EMPTY_RESPONSE`), which closes the
GG-001 ambiguity this item flagged: a throttled or empty response is no longer indistinguishable from
"no data". `EMPTY_RESPONSE` is the named GG-003 signature — HTTP 200 with `{}`.

**Scope — still open:** `odds_api.py`, `shared/odds.py` and `run3/main_run3.py` remain single-attempt
`timeout=30` calls, and **no rate limiting is implemented anywhere**. The shared HTTP client in the
original action is not built; only the ESPN path is hardened. Covered by
`tests/unit/test_espn_transport.py` (45 tests).

### GG-013 — Fixture status is never checked — ✅ RESOLVED (Epic 1B.2)
`espn.py:69` captures `status`; nothing reads it. Finished, postponed, abandoned and in-play matches
are all predicted as if upcoming. **Action:** filter on status; treat non-scheduled as excluded.

**RESOLVED — Epic 1B.2** (provider side). Each fixture now exposes `state` (`pre`/`in`/`post`, from
ESPN's `status.type.state`), `is_completed`, and `is_postponed`. `espn.is_predictable(fixture)` returns
True **only** for a match that has not started and is still expected to happen. `state` alone is
insufficient — a postponed match still reports state `pre` — so cancelled/abandoned/postponed status
names are matched explicitly. This matters because a finished match's statistics already contain that
result, so "predicting" it is predicting a known outcome. Existing fixture keys are unchanged, so
current consumers keep working.

**Honest status — remaining work:** this is now **available to callers but not yet used by them.**
`main.py` and `analyze_all.py` do **not** filter on `is_predictable()` yet, so in practice finished and
postponed matches still reach the model. The capability exists and is tested; wiring it into the entry
points is outstanding.

### GG-014 — Timezone and date-parsing weaknesses — ✅ RESOLVED (Epic 1B.2)
ESPN returns UTC (`2026-01-17T12:30Z`); `date.today()` is local (machine UTC+1). Fixtures near midnight
land on the wrong day. Datetimes are stored as raw strings and never parsed. SofaScore uses Unix
timestamps — a third representation. **Action:** timezone-aware datetimes end to end; explicit
local-vs-UTC decision for "matchday".

**RESOLVED — Epic 1B.2** (provider side). `espn.parse_kickoff()` parses ESPN's trailing-`Z` timestamps
into **timezone-aware UTC** datetimes, exposed on each fixture as `kickoff_utc`; an unparseable value
returns `None` rather than a wrong instant. Aware rather than naive on purpose: a naive datetime
compares silently against local time, and on a UTC+1 machine a 23:30Z kickoff lands on the wrong
matchday. The raw `datetime` string is retained unchanged for existing consumers.

**Honest status — remaining work:** as with GG-013, `kickoff_utc` is **available but not yet used.**
Neither entry point buckets matchdays by it — `date.today()` is still local — so the near-midnight
boundary error is not yet fixed end to end. The explicit local-vs-UTC "matchday" decision is still
owed. SofaScore's Unix timestamps are untouched (dead code, GG-018).

### GG-015 — No duplicate-fixture protection
Nothing deduplicates by fixture ID. A team appearing in two whitelisted competitions on one date is
processed twice. **Action:** deduplicate on `(fixture_id, provider)`.

### GG-016 — Three divergent decision implementations
`decision.py`, `shared/odds.py::analyze_market`, `run3_decision.py` each compute edge independently,
with `MIN_ODDS`/`MIN_EDGE` duplicated in `config.py` and `shared/odds.py`. **Action:** single decision
service, thresholds from one config source.

### GG-017 — R3 model probability compared against BTTS odds
`shared/odds.py:295` maps `R3_YES` to BTTS odds as a "proxy". Different events, so any edge computed
this way is meaningless. Currently unreachable but present. **Action:** remove the proxy, or gate it
behind an explicit "no market available" state.

---

## LOW

### GG-018 — 654 lines of dead provider code
`sofascore.py`, `sportmonks.py`, `api_football.py` — never imported (17% of the codebase). Also
`config.PHASE_2_LEAGUES` and `odds_api.get_upcoming_odds()`. Worth noting: `api_football.py` is the
source `GG.md` names as primary. **Action:** keep as reference for the provider interface, then delete
or promote deliberately.

### GG-019 — `print()` instead of structured logging
No levels, no structure, no destination control, no run ID. Makes production monitoring impossible.
Minor security note: The Odds API takes the key as a **query parameter**, so a printed `requests`
exception can embed `apiKey=…`. **Action:** `logging` with redaction.

### GG-020 — Insecure HTTP for ESPN — ✅ RESOLVED (Epic 1B.2)
`config.py:14` and `run3/main_run3.py:37` use `http://`. Traffic is unauthenticated but plaintext and
MITM-modifiable, and a tampered response feeds the model directly. **Action:** switch to `https://`.

**RESOLVED — Epic 1B.2.** `config.ESPN_BASE_URL` and the new `config.ESPN_STANDINGS_BASE_URL` are both
`https://`, verified working over TLS for every endpoint the pipeline uses. A regression test asserts
both start with `https://` **and** that neither contains the substring `http://` — `startswith` alone
would miss an embedded downgrade (`tests/unit/test_espn_provider.py`).

**Scope — still open:** `run3/main_run3.py:37` still hardcodes `http://`. Run-3 keeps its own copy of
the ESPN client (GG-010) and cannot currently produce a selection at all (R3-001), so it was left
alone; the plaintext URL there is unfixed.

### GG-021 — Unresolved authored comments shipped to `main`
`main.py:114` and `:118` contain uncertainty notes
(`"might break int expectation in odds_api? check."`). **Action:** resolve and remove.

### GG-022 — Unreproducible dependency management
`requirements.txt` has two unpinned `>=` entries; no lockfile, no `pyproject.toml`, no Python version
constraint. Neither package is installed for the default interpreter, so **no entry point currently
runs**. **Action:** pin exact versions, add `pyproject.toml` with `requires-python`.

### GG-023 — Output artefacts written to CWD and committed to Git
Five `output_2026-01-*` files are tracked despite `.gitignore` (committed before the rule was added).
`output.py:142` uses `extrasaction="ignore"`, silently dropping schema drift. **Action:** dedicated
output directory; `git rm --cached` the artefacts.

---

## Summary by component

✅ = resolved.

| Component | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| ESPN provider | ✅ GG-001, ✅ GG-003, ✅ GG-025 | ✅ GG-004, GG-005, GG-024 | ✅ GG-012, ✅ GG-013, ✅ GG-014, GG-027 | ✅ GG-020 |
| Filters / entry points | ✅ GG-002 | ✅ GG-006 · GG-002-B (open) | GG-015 | GG-021 |
| Run-3 | R3-001 | — | GG-010, GG-011 | — |
| Odds | — | GG-007, GG-008 | GG-016, GG-017 | GG-019 |
| Dead providers | — | GG-009 | — | GG-018 |
| Storage / evaluation | LEAK-001 | ✅ GG-026 | — | — |
| Model (`poisson.py`) | — | GG-028 (addressed via inputs), GG-029 | — | — |
| Model class / feature set | — | GG-031, GG-032 | — | — |
| Tooling | — | — | — | GG-022, GG-023 |

**Open by severity (23 total):** 1 CRITICAL (R3-001) + LEAK-001 · 10 HIGH (GG-002-B, GG-005,
GG-007, GG-008, GG-009, GG-024, GG-028, GG-029, GG-031, GG-032) · 6 MEDIUM (GG-010, GG-011, GG-015,
GG-016, GG-017, GG-027) · 5 LOW (GG-018, GG-019, GG-021, GG-022, GG-023).

**New in Epic 2E.** Nothing was promoted and **nothing was even built** — Stage 0 was designed as a
hard stop-gate and it stopped. **GG-032 is opened and it closes GG-031's open question:** the ≈0.568
ceiling is a property of *football*, not of goal counts. A probe granted perfect knowledge of each
fixture's actual shots on target appears to reach AUC 0.7244, but that number is a **definitional
artefact** — every goal is a shot on target, so `SOT == 0` proves a team did not score and the probe was
reading part of its own label (182 exact-`0.0` predictions). The confound-controlled arm, restricted to
chances *created but not converted*, collapses to **0.5121** `[0.4940, 0.5319]` — a significant
**degradation** versus goal counts (ΔAUC −0.0718, CI `[−0.0963, −0.0475]`). Knowing every team's shot
profile perfectly in-sample is likewise indistinguishable from goal counts (ΔAUC −0.0101, CI straddling
zero). The residual is **conversion variance**, which no pre-match feature can forecast. This retires
shots, shots-on-target and — because xG is a weighted function of shots — **xG**, without buying a
provider. The 2025 holdout was **never loaded**; `build_dataset` raises if a caller would pull it in.
`competitor.form` was found to be **end-of-season contaminated** (a matchday-1 fixture carrying five
prior results) and is banned by an allowlist plus 36 regression tests. Production, odds gating and
Epic 2D's files are byte-identical.


**New in Epic 2D.** Nothing was promoted; the Epic bought an *answer*, not a model. **GG-029 is
confirmed and sharpened on a genuinely untouched holdout (2024):** the constant base-rate predictor
scores Brier 0.2469 against raw POISSON_V1's 0.2601 and the shrunk estimator's 0.2528 — the constant
still wins, so Brier remains disqualified as a selection objective. Every 2D parameter was therefore
chosen on out-of-sample **goal-count likelihood** instead. **GG-031 is opened: a discrimination
ceiling.** A deliberately leaky probe fitted on the full dataset *including each target fixture* reaches
only AUC 0.5679, while honest candidates already reach 0.537-0.546 — so perfect knowledge of team
strength is worth at most ~0.02-0.03 AUC and the limit is the information content of goal counts, not
estimation error. Consequences: **no further structure should be added to goal-count models** (Maher,
exponential time decay, Dixon-Coles and bivariate Poisson all produced ΔAUC confidence intervals
containing zero on all three partitions), and **recalibration must not be attempted next** — a monotone
recalibration cannot change ranking, so it would improve Brier while adding no skill, which is exactly
the GG-029 trap. Two candidates were dropped on evidence rather than constrained into behaving: the
decay rate maximised at the boundary ξ̂ = 0 (recency-weighting *reduces* predictive likelihood here) and
the bivariate shared component was not identifiable (λ₃ maximised at 0). GG-028's severity is further
quantified on unseen data: raw POISSON_V1 put 13 holdout fixtures in the `[0, 0.10)` bin with a mean
prediction of 0.005 against an observed BTTS rate of 0.538 — a **0.533 calibration gap** — and 12
exact-`0.0` predictions, all of which the estimator eliminates. Promotion identification is unchanged
from Epic 2C and still has no debt ID because ESPN exposes no promotion field: the 2D candidates refuse
teams absent from the fitting window rather than inferring promotion or substituting league average.

LEAK-001's odds row is untouched; the evaluation never reached odds or `decision.py`, and `poisson.py`,
`config.py`, `filters.py`, `decision.py`, `shared/odds.py` and `run3/` are unmodified.

**New in Epic 2C.** GG-028 is **addressed for the production path and left open for the raw model**:

the Gamma-Poisson input estimator makes an exact-zero probability arithmetically unreachable
(exact-`0.0` predictions 19→0 on validation, 17→0 on holdout, on identical fixture intersections), while
`poisson.py` stays byte-identical so the baseline remains reproducible. **GG-029 is opened, and it is
the more important finding:** AUC ≈0.535 for every arm including the baseline, and a constant predictor
(0.2496) beats raw POISSON_V1 (0.2615) — so the Epic's Brier/log-loss gains are calibration, not skill,
and Brier alone must not be used to select parameters. LEAK-001's odds row is untouched; the evaluation
never reached odds or `decision.py`.


**New in Epic 2B.3.** Nothing was closed — the Epic built the measurement, not a fix. **GG-028** is
opened: POISSON_V1 measurably scores worse than a naive base rate (Brier 0.2657 vs 0.2479 over 7,234
fixtures), concentrated in thin-evidence fixtures where a single goalless away match drives λ to 0 and
the predicted probability to exactly 0%. `poisson.py` was deliberately left untouched — changing the
model while measuring it would destroy the baseline Epic 2C needs. LEAK-001's odds row is unchanged and
still blocks any recommendation backtest.

**New in Epic 2B.1.** GG-025 is **closed and regression-protected** — event-level season identity is
authoritative, date-only membership is impossible, and 7 of 7 mutations were killed. Two items were
**opened rather than silently decided**: GG-026 (playoff inclusion is a modelling policy, ≈7 matches
in production leagues) and GG-027 (eng.1 2009/10 is corrupt at source and is refused). Nothing
unrelated was closed.



**Carried forward from Epic 1B.2** (capability landed, not yet consumed): GG-013 and GG-014 are
resolved in the provider but `main.py`/`analyze_all.py` do not yet filter on `is_predictable()` or
bucket matchdays by `kickoff_utc`. GG-012 hardened the ESPN path only — the odds clients and Run-3 are
still single-attempt and nothing implements rate limiting. GG-020 left `run3/main_run3.py` on `http://`.

**Also outstanding, tracked in `REPO_AUDIT.md` §7 rather than here** (they are decisions, not defects):
the five documented-vs-implemented disagreements D1–D5 between `GG.md` and the code, plus the Run-3
threshold divergence. Each needs a ruling from you before the corresponding fix can be written.

**Absent infrastructure** (not itemised above because it is absence rather than debt): tests, database,
backtesting, model versioning, structured logging, CI, validation layer, API, UI.
