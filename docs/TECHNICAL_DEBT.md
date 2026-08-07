# GG Predictor — Technical Debt Register (as of `be67223`)

Prioritised. Nothing here has been implemented — recommendations are for Epic 1 onward.

**Counts:** 4 CRITICAL · 6 HIGH · 8 MEDIUM · 6 LOW · 1 leakage risk (counted within CRITICAL)

---

## CRITICAL

### GG-001 — Missing statistics are silently converted to `0`
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

### GG-002 — Four of five documented hard filters cannot fire
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

### GG-003 — League average goals is always the hardcoded `1.35`
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

---

## HIGH

### GG-004 — Home/away match counts fabricated by halving
- **Component:** ESPN provider · **Evidence:** `espn.py:117-118`, `main_run3.py:166-167`,
  `sofascore.py:134-145` (which halves goals and clean sheets too).
- **Impact:** Home/away schedules are genuinely uneven. Live-verified: Aalesund 9 home vs 6 away;
  AIK 7 vs 8; Athletico-PR 11 vs 10. Halving distorts every per-match rate fed to λ.
- **Future action:** Use the real split counts (they are present in the API response); fail explicitly
  when absent.

### GG-005 — Only season-long cumulative stats; no form or recency
- **Component:** ESPN provider · **Evidence:** `espn.py:83-133` reads only `record.items[0]` totals.
- **Impact:** A team's August form is weighted identically to its May form. No recency weighting is
  possible, and no matchweek-scoped query exists — this is also the mechanism behind LEAK-001.
- **Future action:** Fixture-level history in storage; derive rolling windows from it.

### GG-006 — Two entry points apply different filter semantics
- **Component:** Entry points · **Evidence:** `main.py:100` passes `total_goals_avg` = `(GF+GA)/matches`;
  `analyze_all.py:97` passes `home_goals_scored` (home scoring rate). Both land in the parameter
  `home_avg_goals`, compared against `MIN_AVG_GOALS = 1.0`.
- **Impact:** The same fixture can pass one script and fail the other. Neither matches `GG.md`'s
  "one team averages < 1.0 goal", which reads as goals *scored*. Results are not comparable between
  scripts, and neither is reproducible against the spec.
- **Future action:** One service layer, one filter contract, named unambiguously
  (`goals_scored_per_match` vs `total_goals_per_match`).

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

---

## MEDIUM

### GG-010 — Run-3 duplicates the entire ESPN client, league map and λ formula
`run3/main_run3.py:37-230` re-implements `espn.py` plus `poisson.py`'s λ formula and a 36-league map.
Bug fixes must be applied twice; the two copies can drift silently.
**Action:** shared provider + shared λ once the regression tests exist.

### GG-011 — Run-3 only runs from inside `run3/`
Sibling imports (`from run3_probability import …`) with no package. `cd run3` is mandatory and
undocumented outside `run3/README.md`. **Action:** proper package + console entry point.

### GG-012 — No rate limiting, retry or backoff anywhere
`espn.py`, `odds_api.py`, `shared/odds.py`, `main_run3.py` all issue single attempts. Run-3 makes
hundreds of sequential requests across 36 leagues. A throttled response is indistinguishable from
"no data" because of GG-001. **Action:** shared HTTP client with retry, backoff and rate limiting.

### GG-013 — Fixture status is never checked
`espn.py:69` captures `status`; nothing reads it. Finished, postponed, abandoned and in-play matches
are all predicted as if upcoming. **Action:** filter on status; treat non-scheduled as excluded.

### GG-014 — Timezone and date-parsing weaknesses
ESPN returns UTC (`2026-01-17T12:30Z`); `date.today()` is local (machine UTC+1). Fixtures near midnight
land on the wrong day. Datetimes are stored as raw strings and never parsed. SofaScore uses Unix
timestamps — a third representation. **Action:** timezone-aware datetimes end to end; explicit
local-vs-UTC decision for "matchday".

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

### GG-020 — Insecure HTTP for ESPN
`config.py:14` and `run3/main_run3.py:37` use `http://`. Traffic is unauthenticated but plaintext and
MITM-modifiable, and a tampered response feeds the model directly. **Action:** switch to `https://`.

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

| Component | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| ESPN provider | GG-001, GG-003 | GG-004, GG-005 | GG-012, GG-013, GG-014 | GG-020 |
| Filters / entry points | GG-002 | GG-006 | GG-015 | GG-021 |
| Run-3 | R3-001 | — | GG-010, GG-011 | — |
| Odds | — | GG-007, GG-008 | GG-016, GG-017 | GG-019 |
| Dead providers | — | GG-009 | — | GG-018 |
| Storage / evaluation | LEAK-001 | — | — | — |
| Tooling | — | — | — | GG-022, GG-023 |

**Also outstanding, tracked in `REPO_AUDIT.md` §7 rather than here** (they are decisions, not defects):
the five documented-vs-implemented disagreements D1–D5 between `GG.md` and the code, plus the Run-3
threshold divergence. Each needs a ruling from you before the corresponding fix can be written.

**Absent infrastructure** (not itemised above because it is absence rather than debt): tests, database,
backtesting, model versioning, structured logging, CI, validation layer, API, UI.
