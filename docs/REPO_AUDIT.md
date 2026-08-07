# GG Predictor — Full Repository Audit (Epic 0)

**Audit date:** 2026-08-07
**Commit audited:** `be67223` (branch `epic/0-repository-audit`, identical to `origin/main`)
**Mode:** Read-only. No source file was modified, renamed or deleted.
**Size:** 29 tracked files, ~4,000 lines. All 18 Python files read in full.

> **Verification method.** Static reading, plus (a) read-only unauthenticated GET requests to the
> *public* ESPN endpoints to confirm real provider behaviour, and (b) pure-arithmetic simulations run
> in `/tmp` that re-implement the repo's constants to test threshold reachability. No repository code
> was executed, no dependency installed, nothing written outside `docs/`.

---

## 1. Repository Map

```
gg_predictor/
├── .env.example                 env var template
├── .gitignore                   ignores .env, __pycache__, output_*
├── requirements.txt             requests, python-dotenv (2 lines)
├── GG.md                        GG/BTTS spec (325 lines)
├── run-3.md                     Run-3 spec (253 lines)
│
├── config.py                    constants + env loading            [USED]
├── main.py                      GG entry point #1                  [USED]
├── analyze_all.py               GG entry point #2                  [USED]
├── poisson.py                   POISSON_V1 core model              [USED]
├── filters.py                   GG hard filters                    [USED]
├── decision.py                  GG edge/bet decision               [USED by main.py only]
├── output.py                    terminal/CSV/JSON writers          [USED by main.py only]
├── espn.py                      ESPN provider (the only live one)  [USED]
├── odds_api.py                  The Odds API client v1             [USED by main.py only]
│
├── sofascore.py                 SofaScore provider                 [DEAD — never imported]
├── sportmonks.py                SportMonks provider                [DEAD — never imported]
├── api_football.py              API-Football provider              [DEAD — never imported]
│
├── shared/
│   ├── __init__.py              1-line comment
│   └── odds.py                  Odds API client v2 + classification [USED by analyze_all.py]
│
├── run3/
│   ├── README.md                usage notes
│   ├── main_run3.py             Run-3 entry point + own embedded ESPN client
│   ├── run3_probability.py      Run-3 model
│   ├── run3_filters.py          Run-3 filters
│   └── run3_decision.py         Run-3 decision logic
│
└── output_2026-01-1{6,7,8}.{json,csv}   5 committed sample outputs
```

**Absent entirely:** any test, `tests/`, `pytest.ini`, `pyproject.toml`, `setup.py`, `Dockerfile`,
`.github/` (no CI), any database, migration, `src/` layout, package metadata, logging module,
persistent cache, or historical data store.

---

## 2. Per-File Analysis

Legend for "Survives?": whether the file should be carried into the future restructure.

### `config.py` (45 lines)
- **Does:** central constants — ESPN base URL, legacy API keys from env, league whitelist, 4 thresholds.
- **Used:** yes, imported by 10 modules. **Depends on:** `os`, `dotenv`. **In:** `.env`. **Out:** constants.
- **Complete:** yes for the ESPN path.
- **Duplication:** `ALLOWED_LEAGUES` and `ESPN_BASE_URL` are both re-declared inside `run3/main_run3.py`.
- **Bugs:** `PHASE_2_LEAGUES` is defined and **never referenced anywhere** (dead). `load_dotenv()` runs as
  an import-time side effect. `ESPN_BASE_URL` is **`http://`, not `https://`**.
- **Survives?** Yes — but should become a typed settings object under `config/`.

### `poisson.py` (68 lines) — **POISSON_V1 BASELINE**
- **Does:** the entire GG model. One pure function `calculate_gg_probability()`.
- **Used:** by `main.py` and `analyze_all.py`. **Depends on:** `math`, `typing` only — no I/O, no config.
- **In:** 5 floats. **Out:** `{lambda_home, lambda_away, gg_probability}` or `None`.
- **Complete:** yes. **Deterministic:** yes — trivially unit-testable today.
- **Duplication:** the λ formula is re-implemented in `run3/main_run3.py::calculate_lambdas` (L234–235).
- **Bugs:** arithmetic is correct. It accepts `0.0` as a *valid* input (see GG-001) — the guard is
  `val < 0`, so a fabricated zero passes straight through.
- **Survives?** **Yes — preserve verbatim as `POISSON_V1`.** Cleanest file in the repository.

### `filters.py` (80 lines)
- **Does:** the 5 documented hard safety filters. Faithful to `GG.md` §9.
- **Used:** by both GG entry points. **Depends on:** two config thresholds.
- **Complete:** the function itself is correct.
- **Bugs:** the function is fine; **its callers neuter it** (GG-002). Callers hardcode 3 of 7 arguments
  and feed it a provider-fabricated `0` clean-sheet rate.
- **Survives?** Yes — the defect is at the call sites, not here.

### `decision.py` (109 lines)
- **Does:** implied probability, edge, FLAG GG / NO BET gating.
- **Used:** by `main.py` **only**. `analyze_all.py` uses `shared/odds.py` instead → two divergent
  decision paths (GG-006).
- **Bugs:** `calculate_implied_probability()` returns `0.0` when `odds <= 0`, which would present a
  nonsense edge of `P(GG) - 0` as if real; unreachable today only because odds never arrive.
- **Survives?** Yes, as `services/decision`, after the duplicate path is reconciled.

### `output.py` (174 lines)
- **Does:** terminal formatting, CSV writer, JSON writer.
- **Used:** by `main.py` only. `analyze_all.py` and `run3/main_run3.py` each have their own writers.
- **Bugs:** none functional. Writes into the repo root (CWD), not an output directory.
- **Survives?** Partially — replace with a single serialisation layer.

### `espn.py` (162 lines) — the only live provider
- **Does:** `get_fixtures()`, `get_team_stats()`, `get_league_avg_goals()`.
- **Used:** by `main.py` and `analyze_all.py`.
- **Complete:** **No.** See §4 — it fabricates clean-sheet data and its league-average function is
  currently non-functional against the live API.
- **Bugs:** GG-001, GG-003, GG-004, GG-005 (below). Also lines 117–118 use a bare one-line `if`
  style inconsistent with the rest of the file.
- **Survives?** Yes as a *provider adapter*, but it must be rewritten to return explicit
  "data unavailable" states rather than fabricated numbers.

### `odds_api.py` (143 lines)
- **Does:** BTTS Yes odds from The Odds API. `get_upcoming_odds()` is **defined but never called**.
- **Used:** by `main.py` only. **Bugs:** substring team-name matching (GG-008); refetches the whole
  league per fixture — no caching, unlike `shared/odds.py`.
- **Survives?** No — superseded by `shared/odds.py`; consolidate.

### `sofascore.py` (205), `sportmonks.py` (249), `api_football.py` (200)
- **Does:** three complete alternative providers. **Used: never.** Verified — no module imports them.
- **Schema clash:** `sportmonks`/`sofascore` key `ALLOWED_LEAGUES` by **integer** league IDs, but
  `config.ALLOWED_LEAGUES` contains **string** ESPN codes (`"eng.1"`). `sportmonks.get_fixtures()`
  filters with `if league_id not in ALLOWED_LEAGUES` — an int is never in a dict of strings, so
  **it would silently return zero fixtures if ever re-enabled** (GG-009).
- **`sofascore.py` additionally fabricates** home/away splits by halving season totals (L134–145).
- **Survives?** Keep `api_football.py` and `sportmonks.py` as references for a future provider
  interface; both are more complete than `espn.py` on clean sheets. But **all three are dead today**.

### `shared/odds.py` (328 lines)
- **Does:** Odds API client with per-run cache, edge calculation, 5-way classification, recommendation.
- **Used:** by `analyze_all.py`. **Bugs:** falsy-edge bug (GG-007) — verified; substring matching
  (GG-008); `sys.path` mutation at import; `date`/`List` imported unused.
- **Survives?** Yes — the classification concept is good; the client needs hardening.

### `analyze_all.py` (276 lines)
- **Does:** second GG entry point. Emits every match with a classification instead of hiding NO BETs.
- **Bugs:** passes `home_goals_scored` (a *home-only scoring* rate) into the filter parameter named
  `home_avg_goals`, while `main.py` passes `total_goals_avg` (a *combined goals-per-match* figure) into
  the same parameter. **The two entry points therefore filter differently on the same fixture** (GG-006).
- **Survives?** Concept yes, implementation to be folded into one service.

### `main.py` (204 lines)
- **Does:** primary GG entry point; orchestrates fetch → stats → model → filters → odds → decision → output.
- **Bugs:** hardcodes `is_knockout_first_leg=False`, `is_heavy_favorite_mismatch=False`,
  `has_reliable_data=True` (L104–106) — three documented safety filters permanently disabled.
  Contains two authored uncertainty comments (L114, L118) left unresolved.
- **Survives?** Rewrite as a thin CLI over a service layer.

### `run3/*`
- `run3_probability.py` (71) — pure, deterministic, correct per spec. **Survives.**
- `run3_filters.py` (77) — pure, correct per spec. **Survives.**
- `run3_decision.py` (169) — correct code, but **thresholds make it unreachable** (R3-001). Survives
  only after the threshold contradiction is resolved.
- `main_run3.py` (440) — duplicates the entire ESPN client, the league map and the λ formula.
  Uses **implicit sibling imports** (`from run3_probability import …`), so it only runs when CWD is
  `run3/`. **Survives?** No — replace with shared infrastructure.

### Committed outputs (`output_2026-01-*`)
Five generated artefacts committed to Git despite `.gitignore` listing `output_*.csv`/`output_*.json` —
they were committed **before** the ignore rule, so Git keeps tracking them. They are useful *evidence*
(see §3) but are build artefacts in source control.

---

## 3. What the Committed Outputs Prove

These are the only record of a real run. Across **2026-01-17 (23 fixtures)** and **2026-01-18 (16 fixtures)**:

| Observation | Count | Implication |
|---|---|---|
| `"decision": "NO BET"` | 39 / 39 | The system has **never produced a bet signal** in any committed run |
| `"odds": null` | 39 / 39 | Odds never resolved — no `ODDS_API_KEY`, or matching failed |
| `"passes_filters": true` | 39 / 39 | **Not one fixture was ever rejected by a filter** |
| Rejection reason | "No odds available" ×39 | The only reason ever emitted |

The 100% filter pass rate is the empirical fingerprint of GG-002: with clean-sheet fixed at `0` and
three flags hardcoded, the only filter that *can* fire is the goals-average one — and it never did.

The λ values in those files also corroborate GG-003. `Liverpool vs Burnley` shows λ_home 3.12; the
Poisson λ is only that high because it was divided by the fabricated league average `1.35` rather than
a real one. `Strasbourg vs Metz` shows λ_home **4.36** — an implausible expected-goals figure that
should have been an obvious signal that the inputs were wrong.

---

## 4. Data Provider Audit

### 4.1 ESPN — `espn.py` — **ACTIVE, the only provider in use**

| Property | Finding |
|---|---|
| Base URL | `http://site.api.espn.com/apis/site/v2/sports/soccer` — **plain HTTP** |
| Auth | None required |
| Env vars | None |
| Endpoints | `/{league}/scoreboard?dates=YYYYMMDD`, `/{league}/teams/{id}`, `/{league}/standings` |
| Competitions | 5 (GG) / 36 (Run-3, its own hardcoded list) |
| Home/away splits | Yes — `home/awayPointsFor`, `home/awayPointsAgainst`, `home/awayGamesPlayed` |
| Historical data | **None.** Returns only current-season cumulative totals |
| Rate limiting | **None** |
| Timeout | 30 s, hardcoded |
| Retries | **None** |
| Error handling | `except requests.RequestException` → prints, returns `None` |
| Caching | In-memory dict for league averages, per run only |
| Normalisation | Partial — ESPN's `pointsFor`/`pointsAgainst` are mapped to goals |
| Fallbacks | **Multiple silent ones — see below** |

**Live verification performed 2026-08-07 (read-only GETs):**

1. **`/standings` returns `{}` — HTTP 200 with an empty body.**
   ```
   eng.1 -> HTTP 200, bytes=2, body: {}
   ger.1 -> HTTP 200, bytes=2, body: {}
   esp.1 -> HTTP 200, bytes=2, body: {}
   bra.1 -> HTTP 200, bytes=2, body: {}   ← in-season league, still empty
   ```
   `get_league_avg_goals()` checks `if not data or "children" not in data` and returns **`1.35`**.
   This is not a rare edge case: **every league average is currently the hardcoded constant.**
   Because HTTP 200 is returned, no error is raised or printed — it is completely silent.

2. **`/teams/{id}` does return real home/away splits for in-season leagues.**
   ```
   nor.1 Aalesund : gamesPlayed 15, homeGamesPlayed 9,  awayGamesPlayed 6,
                    homePointsFor 15, awayPointsFor 6, homePointsAgainst 21, awayPointsAgainst 14
   bra.1 Athletico: gamesPlayed 21, homeGamesPlayed 11, awayGamesPlayed 10, homePointsFor 19 …
   ```
   So the splits are genuine **when the season is under way**.

3. **Out-of-season leagues return all-zero stats, not an error.**
   ```
   eng.1 Manchester United (Aug 2026, pre-season): gamesPlayed 0.0, pointsFor 0.0, homePointsFor 0.0 …
   ```
   `get_team_stats()` does guard this (`if matches_played == 0: return None`), which is correct.
   But `run3/main_run3.py` has the same guard while its `/standings` fallback still yields `1.35`.

**Silent fallback inventory in `espn.py`:**

| Line | Fallback | Severity |
|---|---|---|
| 101 | `get_stat()` returns **`0`** for any missing statistic | CRITICAL |
| 117–118 | If `homeGamesPlayed == 0`, substitute `matches_played / 2` | HIGH |
| 127–128 | `home_clean_sheet_pct` / `away_clean_sheet_pct` hardcoded to **`0`** | CRITICAL |
| 142, 157, 162 | League average → **`1.35`** on any failure | CRITICAL |

`main.py:171` and `analyze_all.py:182` add a *fourth* `1.35` fallback on top.

### 4.2 The Odds API — `odds_api.py` + `shared/odds.py` — configured, never successfully used
| Property | Finding |
|---|---|
| Base URL | `https://api.the-odds-api.com/v4` |
| Auth | `apiKey` query parameter, from `ODDS_API_KEY` |
| Market | `btts` only, `regions=eu`, `oddsFormat=decimal` |
| Missing key | Returns `None` **silently** (`odds_api.py:22`, no warning) |
| Bookmaker selection | **First bookmaker encountered** — not best price, not consensus |
| Stale odds | **No timestamp checked anywhere.** Odds age is never considered |
| Caching | `shared/odds.py` caches per league per run; `odds_api.py` does not cache at all |
| Effect on model | **None** — odds are used only for edge/decision, correctly per `GG.md` §5 |

### 4.3 SofaScore / SportMonks / API-Football — dead code
All three are complete but **never imported**. Schemas are **mutually incompatible**:
`espn` returns `league_id` as a string code and has no `season_id`; `sportmonks`/`sofascore` return
integer IDs plus `season_id`; `api_football` requires an explicit `season` argument. Any future
multi-provider work needs a normalisation layer — there is none today.

---

## 5. Configuration Audit

| Variable | Referenced in | Defined? | Required? | Problem |
|---|---|---|---|---|
| `ODDS_API_KEY` | `config.py:21`, `odds_api.py:10`, `shared/odds.py:29` | `.env.example` ✔ | Optional | Absent ⇒ **silent** `None`; every committed run shows `odds: null` |
| `SPORTMONKS_API_KEY` | `config.py:17`, `sportmonks.py:17` | `.env.example` ✔ | No | Consumer module is dead code |
| `SPORTMONKS_BASE_URL` | `config.py:18` | **Not in `.env.example`** | No | Undocumented; has a default |
| `API_FOOTBALL_KEY` | `config.py:19`, `api_football.py:15` | `.env.example` ✔ | No | Consumer module is dead code |
| `API_FOOTBALL_HOST` | `config.py:20`, `api_football.py:17` | **Not in `.env.example`** | No | Undocumented; has a default |
| `ESPN_BASE_URL` | `config.py:14` | Hardcoded constant | Yes | Not overridable; uses `http://` |

**No import references a config variable that does not exist** — all imports resolve.
`PHASE_2_LEAGUES` exists but has no consumer.

**Constants defined outside `config.py`** (fragmenting configuration):
`run3/run3_filters.py` (3 thresholds), `run3/run3_decision.py` (14 thresholds),
`shared/odds.py` (4 thresholds + 28-league map), `odds_api.py` (5-league map),
`sofascore.py` (7-league map), `run3/main_run3.py` (36-league map).

---

## 6. Dependency Audit

`requirements.txt` declares exactly two packages:
```
requests>=2.28.0
python-dotenv>=1.0.0
```

- **Imported and declared:** `requests`, `dotenv`. ✔
- **Imported but not declared:** none — everything else is stdlib (`math`, `csv`, `json`, `os`, `sys`,
  `datetime`, `typing`).
- **Declared but unused:** none.
- **Python version:** unspecified anywhere. No `python_requires`, no `pyproject.toml`, no `.python-version`.
  Code is compatible with 3.7+. The machine's interpreter is **Python 3.14.6**.
- **Reproducibility: poor.** Unpinned lower bounds only, no lockfile, no hashes, no virtualenv
  definition, no package metadata.
- **Current environment:** `requests` and `dotenv` are **not installed** for the default `python3`.
  Every entry point will fail at import with `ModuleNotFoundError` until a venv is provisioned.
  *(Not fixed — audit only.)*

---

## 7. POISSON_V1 Audit

**Location:** `poisson.py`, single function `calculate_gg_probability()`.

**Required inputs** (all 5, per `GG.md` §6): `league_avg_goals`, `home_goals_scored_home`,
`home_goals_conceded_home`, `away_goals_scored_away`, `away_goals_conceded_away`.

**Formulas as implemented (lines 54–62):**
```
lambda_home = (home_goals_scored_home * away_goals_conceded_away) / league_avg_goals
lambda_away = (away_goals_scored_away * home_goals_conceded_home) / league_avg_goals
p_home_scores = 1 - exp(-lambda_home)
p_away_scores = 1 - exp(-lambda_away)
gg_probability = p_home_scores * p_away_scores
```

**These match `GG.md` §7 exactly.** The core mathematics is faithful to the specification.

**Assumptions:** goals Poisson-distributed; home and away scoring **independent** (BTTS is the product
of two marginals — the known weakness Dixon-Coles later addresses); league average acts as the
normalising divisor.

**Rounding:** none inside the model — full float precision returned. Rounding happens only at output
(`shared/odds.py` rounds to 4 dp; `run3` rounds to 3 dp; `output.py` formats to 2 dp).

**Numerical safeguards:** rejects `None`; rejects any negative; rejects `league_avg_goals == 0`.
**No upper bound** — the committed output contains λ = 4.36, which is accepted silently.

**Missing-data behaviour — the critical gap.** The guard is `if val is None or val < 0`. A value of
**`0.0` is treated as valid data**. Since `espn.get_stat()` returns `0` for any absent statistic, a
missing statistic reaches the model as a legitimate zero. `main.py` does check for `None` before
calling, but the provider never returns `None` for individual stats — it returns `0`. **The
documented contract "if any of these are missing → NO BET" (`GG.md` §6) is therefore not enforced
in practice.**

**Filters before the model:** league whitelist; `matches_played == 0` → `None`; `main.py` `None` checks.
**Filters after the model:** `filters.apply_filters()`, then `decision.make_decision()`
(edge ≥ 0.05, odds ≥ 1.60, filters passed).

### Documentation vs code — disagreements found (not resolved, per instructions)

| # | `GG.md` says | Code does | Where |
|---|---|---|---|
| D1 | Primary data source is **API-Football** (§5) | Uses **ESPN**; API-Football is dead code | `main.py:26` |
| D2 | "If any of these are missing → **NO BET**" (§6) | Missing stats become `0` and flow into the model | `espn.py:101` |
| D3 | Five hard filters are "mandatory… they protect the bankroll" (§9) | 3 hardcoded off, 1 fed a constant `0`; empirically 39/39 passed | `main.py:102–106` |
| D4 | Filter is "one team averages < 1.0 **goal**" (§9) | `main.py` passes *combined* goals-per-match; `analyze_all.py` passes *home-only scoring rate* | `main.py:100`, `analyze_all.py:97` |
| D5 | League average goals is a required model input (§6) | Currently always the hardcoded `1.35` | `espn.py:142` |

I am **not** ruling on which side is correct — that is an Epic 1 decision.

---

## 8. Run-3 Audit

**Run-3 exists.** Files: `run3/main_run3.py`, `run3_probability.py`, `run3_filters.py`,
`run3_decision.py`, `run3/README.md`, plus spec `run-3.md`.

**Model (`run3_probability.py`, faithful to `run-3.md` §Core):**
```
p_home = λ_home / (λ_home + λ_away)      p_away = λ_away / (λ_home + λ_away)
P_home_run3 = p_home³                    P_away_run3 = p_away³
P_R3_YES = 1 - (1 - P_home_run3)(1 - P_away_run3)
P_R3_NO  = 1 - P_R3_YES
```
**Inputs:** λ_home, λ_away — computed by `main_run3.py::calculate_lambdas`, which **re-implements the
GG λ formula** rather than importing it, in direct tension with `run-3.md`'s "DO NOT reuse GG logic"
(the spec wanted isolation; the result is duplication of the identical formula).

**Filters (`run3_filters.py`):** reject if `λ_h + λ_a ≥ 3.5`, `p ≥ 0.65` either side, `λ ≥ 2.2` either side.
**Decision (`run3_decision.py`):** R3-NO needs `P_R3_NO ≥ 0.78`, both λ in `[0.9, 1.8]`,
`p_home ∈ [0.35, 0.65]`, total λ ∈ `[2.0, 3.2]`. R3-YES needs dominance `p ≥ 0.65 AND λ ≥ 2.2`.

### R3-001 — Run-3 can never emit a selection (CRITICAL, mathematically proven)

`P_R3_NO = (1 - p³)(1 - (1-p)³)` where `p = p_home`. This expression is **maximised at `p = 0.5`**,
giving:
```
(1 - 0.125)(1 - 0.125) = 0.875 × 0.875 = 0.765625
```
**The global maximum of `P_R3_NO` across all possible inputs is 0.7656.**
The code requires `P_R3_NO ≥ 0.78` (`run3_decision.py:26`).

> **0.7656 < 0.78 — the R3-NO threshold is unreachable for every conceivable fixture.**

Exhaustive check over 1,000,000 λ pairs (0.01–10.00 each) confirmed: **0 states satisfy R3-NO.**

**R3-YES is also unreachable, for a different reason:** the decision requires dominance
(`p ≥ 0.65 AND λ ≥ 2.2`) while the filters reject exactly those states (`p ≥ 0.65` or `λ ≥ 2.2`).
The two rules are mutually exclusive. Exhaustive check over 640,000 λ pairs: **0 states satisfy R3-YES.**

**Therefore `main_run3.py` returns `SKIP` for 100% of fixtures, always, regardless of input data.**

**Root cause of the R3-NO half — a spec/code divergence:** `run-3.md` specifies **`P_R3_NO ≥ 0.75`**;
the code uses **`0.78`**. Re-running the same exhaustive search at the *documented* 0.75 yields
**4,367 reachable states** (e.g. λ_h 0.9, λ_a 1.1 → `P_R3_NO` 0.7577). So the documented threshold is
satisfiable and the implemented one is not. Additionally, `run3_decision.py` introduces
`R3_NO_MIN_TOTAL_GOALS`, `R3_NO_MAX_TOTAL_GOALS` and `R3_NO_MIN_EDGE` which **appear nowhere in
`run-3.md`**. I am not deciding which is authoritative.

**Other Run-3 findings:**
- `main_run3.py` duplicates the ESPN client, the λ formula and a 36-league map (GG-010).
- Sibling imports (`from run3_probability import …`) mean it **only runs from inside `run3/`** (GG-011).
- `run3_decision.py:168` builds `result["reasons"]` from both branches, so a SKIP reports R3-YES
  rejection reasons that are irrelevant to the primary market — confusing but not incorrect.
- It fetches team stats **per fixture with no caching** across ~36 leagues: on a busy day this is
  hundreds of sequential un-rate-limited requests (GG-012).
- Odds are hardcoded `None` at the call site (`main_run3.py:313`), consistent with the spec's
  "do NOT fetch odds automatically".

---

## 9. Odds Audit

- **Provider:** The Odds API v4. Two independent clients: `odds_api.py` (used by `main.py`) and
  `shared/odds.py` (used by `analyze_all.py`).
- **Markets:** `btts` only. `shared/odds.py` reads both `btts_yes` and `btts_no`.
- **Do odds affect probability?** **No** — correctly. Odds enter only edge/decision, per `GG.md` §5.
- **Implied probability:** `1 / odds` in both clients.
- **Edge:** `model_probability − implied_probability` in both.
- **Missing odds:** `main.py` → `decision` returns NO BET with "No odds available".
  `analyze_all.py` → classification `NO_ODDS`, recommendation `RECOMMEND_NO_PLAY`. Both fail safe. ✔
- **Decision gating:** edge ≥ 0.05 **and** odds ≥ 1.60 in both paths (thresholds agree; the
  constants are duplicated in `config.py` and `shared/odds.py` rather than shared).
- **Stale odds:** **not handled at all.** No timestamp is read, stored or compared.
- **Bookmaker choice:** first one returned, silently. Not best-price, not consensus.
- **R3 proxy:** `shared/odds.py:295` maps `R3_YES` to **BTTS odds** as a "proxy" — comparing a Run-3
  model probability against a Both-Teams-To-Score price. These are different events; any edge computed
  this way is meaningless. Currently unreachable (nothing calls `analyze_market` with `R3_*`), but the
  code is there.

---

## 10. Data-Quality Audit

### CRITICAL

**GG-001 — Missing statistics silently become `0`.** `espn.py:101`:
`return next((s.get("value", 0) for s in stats_list if s.get("name") == name), 0)`.
Any statistic ESPN omits becomes a real-looking zero, flows into λ, and produces a confident-looking
probability. `poisson.py` rejects negatives and `None` but **accepts `0.0`**. Missing data and genuine
zero are indistinguishable throughout the entire pipeline.

**GG-002 — The hard filters are effectively inert.** Verified three ways:
- `espn.py:127–128` hardcodes both clean-sheet rates to `0`; the filter fires only when `> 0.40`, so
  `0 > 0.40` is **never true**.
- `main.py:104–106` hardcodes `is_knockout_first_leg=False`, `is_heavy_favorite_mismatch=False`,
  `has_reliable_data=True`, disabling three more filters.
- The only live filter receives the wrong quantity (see GG-006), so even a dire team clears it:
  a side with 5 GF / 30 GA in 20 games yields `total_goals_avg = 1.75 > 1.0` → **passes**.
- Empirical confirmation: **39 of 39 committed fixtures have `passes_filters: true`.**
`GG.md` calls these filters "mandatory… they protect the bankroll". Four of five cannot fire.

**GG-003 — League average is always the hardcoded `1.35`.** Live-verified: ESPN `/standings` returns
HTTP 200 with body `{}` for every league tested, including in-season ones. `get_league_avg_goals()`
therefore always falls through to `1.35`. This constant is the **denominator of both λ values**, so it
scales every prediction the system makes. Because HTTP 200 is returned, nothing logs a problem.

**R3-001 — Run-3 is mathematically incapable of producing a selection.** See §8. `P_R3_NO` maxes at
0.7656 against a required 0.78; R3-YES conditions are mutually exclusive with the filters. Proven
exhaustively. The module runs, prints and writes JSON — always "no selections", which reads as a
legitimate quiet day rather than a broken model.

### HIGH

**GG-004 — Home/away match counts fabricated by halving.** `espn.py:117–118` substitutes
`matches_played / 2` when `homeGamesPlayed` is 0. Real fixture lists are uneven — live-verified:
Aalesund 9 home vs 6 away, AIK 7 vs 8. Halving distorts per-match rates. Same pattern at
`run3/main_run3.py:166–167`; `sofascore.py:134–145` is worse, halving *goals* and *clean sheets* too.

**GG-005 — Season-long averages only; no form, no recency, no date filtering.** ESPN returns
cumulative current-season totals. There is no way to request "as of matchweek N".

**GG-006 — Two entry points, two different filter semantics.** `main.py:100` passes
`total_goals_avg` = `(GF + GA) / matches` — *both* teams' goals. `analyze_all.py:97` passes
`home_goals_scored` — home scoring rate only. Both land in the parameter `home_avg_goals` checked
against `MIN_AVG_GOALS = 1.0`. **The same fixture can pass one entry point and fail the other.**
Neither matches `GG.md`'s "one team averages < 1.0 goal", which reads as goals *scored*.

**GG-007 — Falsy-edge bug.** `shared/odds.py:319`: `"edge": round(edge, 4) if edge else None`.
When `edge == 0.0` exactly, `0.0` is falsy → serialised as `null`, i.e. reported as "no odds" rather
than "no edge". Same pattern at line 318 for `implied_probability` and at `analyze_all.py:242`.
Verified: `round(0.0, 4) if 0.0 else None` → `None`.

**GG-008 — Substring team-name matching.** `odds_api.py:90–92` and `shared/odds.py:209–210` accept a
match if either name is a substring of the other. `"Manchester United"` vs `"Manchester City"` do not
collide, but genuine risks exist: `"Athletic Club"` / `"Athletic"`, `"Milan"` / `"AC Milan"` /
`"Inter Milan"`, `"Boca"` / `"Boca Juniors"`. A false positive attaches **another match's odds** to a
fixture. No canonical team-ID mapping exists anywhere in the repository.

**GG-009 — Dead providers would silently return nothing.** `sportmonks.py:96` filters
`if league_id not in ALLOWED_LEAGUES` where `league_id` is an **int** and `ALLOWED_LEAGUES` keys are
**strings** — never matches, so zero fixtures, no error. A latent trap for whoever re-enables it.

### MEDIUM

- **GG-010** — `run3/main_run3.py` duplicates the ESPN client, λ formula and league map.
- **GG-011** — Run-3 uses sibling imports; runs only with CWD = `run3/`.
- **GG-012** — No rate limiting, no retry, no backoff anywhere. Run-3 issues hundreds of sequential
  requests across 36 leagues. ESPN may throttle; the code cannot tell throttling from "no data".
- **GG-013** — **Fixture status is never checked.** `espn.py:69` captures `status` and nothing ever
  reads it. Finished, postponed, abandoned and in-play matches are all predicted identically.
- **GG-014** — Timezone handling. ESPN returns UTC (`2026-01-17T12:30Z`); the CLI takes a bare date and
  `date.today()` is **local** (machine is UTC+1). Fixtures near midnight fall into the wrong day.
  Datetimes are stored as raw strings and never parsed. SofaScore returns Unix timestamps — a third
  incompatible representation.
- **GG-015** — No duplicate-fixture protection. Nothing deduplicates by fixture ID; a team appearing
  in two whitelisted competitions on one date would be processed twice.

### LOW

- Output files written to CWD, colliding with the repo root.
- `print()` used throughout instead of `logging`; no levels, no structure, no destination control.
- `output.py:142` uses `extrasaction="ignore"`, so schema drift silently drops columns.
- `analyze_all.py:210` initialises `recommendations` with mixed types (a list and an int).

---

## 11. Historical Data / Data-Leakage Audit

**Does the project store historical data? No.**
- No database of any kind — no SQLite, no PostgreSQL, no ORM, no migrations.
- No persistent cache. `shared/odds.py::_odds_cache` is an in-memory dict cleared each run.
- No prediction history: outputs are overwritten per date and are `.gitignore`d (the five committed
  ones predate the rule).
- No match results are ever fetched — **the system never learns whether a prediction was correct.**
- No team-stat snapshots.

**Is historical model evaluation currently possible? No.** Three independent blockers:
1. No stored predictions to evaluate.
2. No stored outcomes to evaluate against.
3. No point-in-time statistics — ESPN only serves current cumulative totals.

### LEAK-001 — Structural look-ahead bias (CRITICAL if backtesting is attempted)

**Answering the question posed directly: yes.** If you ran `python main.py 2025-09-15` today, the code
would fetch *today's* season-to-date statistics — including every match played **after** 2025-09-15 —
and predict a fixture from September using data from August 2026. `espn.get_team_stats()` takes no
date or matchweek parameter; there is nowhere to express "as of". `get_fixtures()` accepts a date, so
the *fixture list* is historical while the *statistics* are current. The date parameter creates a
convincing illusion of backtesting.

**Any backtest built on the current provider layer would produce inflated, meaningless accuracy.**
This is the single most important thing to fix before any model comparison work begins — a
Dixon-Coles-vs-Poisson comparison run on leaking data would be worse than no comparison, because it
would produce confident numbers justifying a wrong choice.

**Secondary leak:** even within a single day, a team playing earlier in the day has that result folded
into its cumulative totals before a later fixture is predicted — mild, but real.

---

## 12. Testing Audit

**There are zero tests in this repository.** No `tests/`, no `test_*.py`, no `conftest.py`, no
`pytest.ini`, no `pyproject.toml`, no CI workflow. `pytest` is not declared and not installed.

Coverage by component:

| Component | Coverage | Testability today |
|---|---|---|
| `poisson.py` (**POISSON_V1**) | **None** | **Excellent** — pure, deterministic, 5 floats in, dict out |
| `run3_probability.py` | None | Excellent — pure |
| `run3_filters.py` | None | Excellent — pure |
| `run3_decision.py` | None | Excellent — pure (would have caught R3-001 instantly) |
| `filters.py` | None | Excellent — pure |
| `decision.py` | None | Excellent — pure |
| `espn.py` | None | Needs HTTP mocking |
| `shared/odds.py` | None | Needs HTTP mocking |
| Entry points | None | Needs end-to-end fixtures |

**Does POISSON_V1 have deterministic regression tests? No.** This is the most consequential gap:
the baseline the whole restructure is meant to preserve has **nothing pinning its behaviour**. Locking
it with golden-value tests should be the first action of Epic 1 — it is a few hours of work and it is
what makes every later change safe.

Two of the four CRITICAL findings (R3-001, GG-002) are pure-logic defects that a handful of unit tests
would have caught immediately.

---

## 13. Code-Quality Audit

- **Duplicated functions:** ESPN client ×2 (`espn.py`, `run3/main_run3.py`); λ formula ×2
  (`poisson.py`, `main_run3.py:234`); Odds API client ×2 (`odds_api.py`, `shared/odds.py`);
  edge calculation ×3; `get_league_avg_goals` ×4 (one per provider); JSON writers ×3.
- **Circular dependencies:** none found.
- **Dead code:** `sofascore.py`, `sportmonks.py`, `api_football.py` (entire modules, 654 lines);
  `PHASE_2_LEAGUES`; `odds_api.get_upcoming_odds()`; `run3_probability`'s `P_home_run3`/`P_away_run3`
  outputs (returned, never read).
- **Unused imports:** `date` and `List` in `shared/odds.py`; `Optional` in `analyze_all.py`;
  `List` in `run3_decision.py`.
- **Inconsistent return schemas:** `espn.get_team_stats` omits `season_id` (others include it);
  `api_football.get_team_stats` omits `total_goals_avg` — which `main.py:100` depends on;
  `run3`'s stats dict omits clean-sheet fields entirely.
- **Inconsistent naming:** `league_id` holds a string code in `espn`, an int in `sportmonks`;
  `season` vs `season_id`; `P_R3_YES` (upper) beside `p_home` (lower) in one dict.
- **Giant functions:** `main_run3.py::get_all_fixtures` and `sofascore.get_team_stats` both do
  fetch + parse + normalise + fabricate in one body.
- **Hidden side effects:** `load_dotenv()` at import (`config.py:11`); `sys.path.insert` at import in
  three files; module-level mutable `_odds_cache`.
- **Module-level API calls:** none (good — no network at import).
- **Weak typing:** `Dict[str, Any]` everywhere; no dataclasses, no Pydantic, no validation.
- **Inconsistent exceptions:** every failure returns `None` or a fallback; no custom exception types.
  `run3/main_run3.py:91` swallows the exception **without even printing it**.
- **Debug/abandoned code:** `main.py:114` and `:118` contain unresolved authored comments
  (`"might break int expectation in odds_api? check."`) shipped to `main`.
- **File naming:** all clean — no spaces or unusual characters. `run-3.md` (hyphen) vs `run3/`
  (no hyphen) is a minor inconsistency.

---

## 14. Security Audit

| Check | Result |
|---|---|
| Committed API keys | **None found.** Scanned tree and full history |
| `.env` tracked by Git | **No.** Correctly ignored; absent from working tree and from all history |
| `.env.example` contents | Placeholders only (`your_odds_api_key_here`) ✔ |
| Secrets printed to terminal | **No.** Failures print exception text, not key values |
| Secrets in output files | No |
| Unsafe logging | `print()` of raw exceptions could echo a URL; The Odds API takes the key as a
  **query parameter**, so a `requests` exception string can embed `apiKey=…`. Low but real |
| **Insecure HTTP** | **Yes** — `config.py:14` and `run3/main_run3.py:37` use `http://` for ESPN.
  Traffic is unauthenticated but plaintext and MITM-modifiable; a tampered response feeds the model |
| Unvalidated external input | **Yes** — API JSON is consumed with `.get()` chains and no schema validation |
| Command injection / eval / pickle | None — no `eval`, `exec`, `pickle` or `subprocess` anywhere ✔ |
| Dependency risk | Only `requests` + `python-dotenv`, but unpinned (`>=`), so builds are not reproducible |

No secret values are reproduced in this document; none were found to redact.

---

## 15. What Actually Works

| Component | Status | Basis |
|---|---|---|
| `poisson.py` (POISSON_V1) | **WORKING** | Pure arithmetic, matches `GG.md` exactly, verified by hand |
| `run3_probability.py` | **WORKING** | Pure, matches spec |
| `run3_filters.py` | **WORKING** | Pure, matches spec |
| `filters.py` (function) | **WORKING** | Correct in isolation — but never given real inputs |
| `decision.py` | **LIKELY WORKING** | Sound logic; never exercised with real odds |
| `output.py` | **LIKELY WORKING** | Committed CSV/JSON are well-formed |
| ESPN fixtures | **LIKELY WORKING** | Endpoint responds correctly; 39 real fixtures in committed output |
| ESPN team stats | **PARTIALLY WORKING** | Real home/away splits in-season (verified), but fabricates
  clean sheets and halves match counts |
| ESPN league average | **BROKEN** | `/standings` returns `{}`; always falls back to `1.35` (verified) |
| GG hard filters (end-to-end) | **BROKEN** | 4 of 5 cannot fire; 39/39 passed |
| Run-3 decision | **BROKEN** | Mathematically unreachable; always SKIP (proven) |
| Odds integration | **UNKNOWN — REQUIRES RUNTIME TEST** | Never succeeded in any committed run;
  needs a valid `ODDS_API_KEY` to assess |
| `sofascore.py` | **DEAD/UNUSED** | Never imported |
| `sportmonks.py` | **DEAD/UNUSED** | Never imported; would return 0 fixtures (GG-009) |
| `api_football.py` | **DEAD/UNUSED** | Never imported |
| `odds_api.get_upcoming_odds` | **DEAD/UNUSED** | Never called |
| Historical evaluation | **DOES NOT EXIST** | No storage, no results, no point-in-time stats |
| Test suite | **DOES NOT EXIST** | Zero tests |

**Runtime caveat:** `requests` and `python-dotenv` are not installed for the default interpreter, so
**no entry point runs at all right now** until a virtualenv is provisioned. Every "working" verdict
above is about the logic, not about the current machine state.

---

## 16. Gap Analysis vs Target Architecture

Target:
```
src/gg_predictor/{config,domain,providers,models,services,validation,storage,api}/
tests/{unit,integration}/   scripts/   docs/
```

| Target | Current | Gap |
|---|---|---|
| `src/` layout | Flat root modules | No package structure, no `pyproject.toml` |
| `config/` | `config.py` + constants in 6 other files | Consolidate; add typed settings |
| `domain/` | **Nothing** | No entities. Dicts everywhere |
| `providers/` | 4 modules, incompatible schemas, 3 dead | No interface, no normalisation, no DI |
| `models/` | `poisson.py`, `run3_probability.py` | No versioning, no registry, no common interface |
| `services/` | Logic inlined in entry points | No service layer |
| `validation/` | **Nothing** | No schema validation; the root cause of most CRITICALs |
| `storage/` | **Nothing** | No DB, no repositories, no history |
| `api/` | **Nothing** | No FastAPI |
| `tests/` | **Nothing** | Zero tests |
| `scripts/` | Entry points at root | Move CLIs |
| `docs/` | Created by this audit | Specs exist (`GG.md`, `run-3.md`) but drift from code |

Technology gap: Python 3.12+ target unpinned; **NumPy, SciPy, pandas, scikit-learn, httpx, Pydantic,
FastAPI, PostgreSQL, SQLAlchemy, Alembic, pytest, Ruff, mypy** all absent. Frontend (Next.js,
TypeScript, Tailwind, shadcn/ui, Recharts) entirely absent — no `package.json`.

**Assessment: this is a working prototype, not a platform.** The mathematical core is sound and worth
keeping; everything around it — validation, storage, testing, provider abstraction — is missing.

---

## 17. Recommended Refactor Order

**Do not rewrite from scratch.** The models are correct and specification-faithful; the failures are
concentrated in the data layer and in two threshold constants. A rewrite would risk the one asset
worth preserving. Incremental migration, in this order:

**Phase 1 — Preserve working behaviour**
1. Provision a virtualenv + pinned dependencies so the code runs at all.
2. Write golden-value regression tests for `poisson.py` **before touching anything**. Freeze as `POISSON_V1`.
3. Same for `run3_probability.py`, `filters.py`, `decision.py`.
4. Tag the current commit as the historical baseline.

**Phase 2 — Fix reliability** (each change guarded by Phase-1 tests)
5. Introduce an explicit `DATA_UNAVAILABLE` state; stop returning `0` for missing stats (GG-001).
6. Fix league average — real source or explicit failure, never a silent `1.35` (GG-003).
7. Restore the hard filters: real clean-sheet data, remove hardcoded flags (GG-002).
8. Resolve the Run-3 threshold contradiction — a decision for you, not the code (R3-001).
9. Decide `GG.md` vs code on D1–D5 and align both.

**Phase 3 — Normalise data**
10. Provider interface + normalised schema; retire or fix the three dead providers.
11. Canonical team/league ID mapping to replace substring matching (GG-008).
12. Timezone-aware datetimes; check fixture status (GG-013, GG-014).

**Phase 4 — Storage & history**
13. PostgreSQL + SQLAlchemy; persist fixtures, **point-in-time** team-stat snapshots, predictions, results.
14. This is the prerequisite that makes LEAK-001 fixable.

**Phase 5 — Evaluation** → backtesting with strict as-of cutoffs; Brier/log-loss/calibration.
**Phase 6 — New models** → Dixon-Coles V2 as a challenger, compared only on leak-free backtests.
**Phase 7 — API** → FastAPI over the service layer.
**Phase 8 — UI** → Next.js dashboard last, per `GG.md` §16.

---

## 18. Companion Documents

- `docs/CURRENT_ARCHITECTURE.md` — module structure and dependency graph
- `docs/DATA_FLOW.md` — execution flows with fallback injection points
- `docs/TECHNICAL_DEBT.md` — the prioritised debt register (IDs referenced above)
- `docs/EPIC_0_SUMMARY.md` — the short read
