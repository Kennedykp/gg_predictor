# GG Predictor — Technical Debt Register (as of `be67223`)

Prioritised. Items marked ✅ RESOLVED were fixed in the Epic named on the heading; everything else is
still open and the recommendation stands. Original problem text is kept verbatim under each resolved
item — resolutions are **appended, not substituted**, so the history stays readable.

**Counts:** 4 CRITICAL (3 RESOLVED) · 7 HIGH (2 RESOLVED) · 8 MEDIUM (3 RESOLVED) · 6 LOW (1 RESOLVED)
· 1 leakage risk (counted within CRITICAL, open)

**Status: 27 items tracked · 9 RESOLVED · 18 open.**

| Epic | Resolved | Items |
|---|---|---|
| 1B.1 — data contracts | 1 | GG-001 |
| 1B.2 — ESPN provider | 6 | GG-003, GG-004, GG-012, GG-013, GG-014, GG-020 |
| 1B.3 — filter wiring | 2 | GG-002, GG-006 |
| **Total** | **9** | 18 remain open, incl. LEAK-001 and R3-001 |

New in Epic 1B.2: **GG-024** (HIGH) — the ESPN team endpoint ignores `?season=`.

New in Epic 1B.3: **GG-002-B** (HIGH) — two of the five GG.md hard filters
(knockout-first-leg, heavy-favourite mismatch) have **no data source at all**. Split out of GG-002 so
the resolved part is not held open by a distinct problem: GG-002 was a *wiring* defect (fabricated
values reaching real filters), whereas GG-002-B is a *missing feed*.


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
| ESPN provider | ✅ GG-001, ✅ GG-003 | ✅ GG-004, GG-005, GG-024 | ✅ GG-012, ✅ GG-013, ✅ GG-014 | ✅ GG-020 |
| Filters / entry points | ✅ GG-002 | ✅ GG-006 · GG-002-B (open) | GG-015 | GG-021 |
| Run-3 | R3-001 | — | GG-010, GG-011 | — |
| Odds | — | GG-007, GG-008 | GG-016, GG-017 | GG-019 |
| Dead providers | — | GG-009 | — | GG-018 |
| Storage / evaluation | LEAK-001 | — | — | — |
| Tooling | — | — | — | GG-022, GG-023 |

**Open by severity (18 total):** 1 CRITICAL (R3-001) + LEAK-001 · 6 HIGH (GG-002-B, GG-005,
GG-007, GG-008, GG-009, GG-024) · 5 MEDIUM (GG-010, GG-011, GG-015, GG-016, GG-017) · 5 LOW (GG-018,
GG-019, GG-021, GG-022, GG-023).


**Carried forward from Epic 1B.2** (capability landed, not yet consumed): GG-013 and GG-014 are
resolved in the provider but `main.py`/`analyze_all.py` do not yet filter on `is_predictable()` or
bucket matchdays by `kickoff_utc`. GG-012 hardened the ESPN path only — the odds clients and Run-3 are
still single-attempt and nothing implements rate limiting. GG-020 left `run3/main_run3.py` on `http://`.

**Also outstanding, tracked in `REPO_AUDIT.md` §7 rather than here** (they are decisions, not defects):
the five documented-vs-implemented disagreements D1–D5 between `GG.md` and the code, plus the Run-3
threshold divergence. Each needs a ruling from you before the corresponding fix can be written.

**Absent infrastructure** (not itemised above because it is absence rather than debt): tests, database,
backtesting, model versioning, structured logging, CI, validation layer, API, UI.
