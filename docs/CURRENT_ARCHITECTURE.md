# GG Predictor — Current Architecture (as of `be67223`)

Descriptive, not aspirational. This is what exists today.

---

## 1. Shape of the system

Three loosely-coupled subsystems sharing a flat root package:

| Subsystem | Entry point | Model | Odds client | Output writer |
|---|---|---|---|---|
| **GG (primary)** | `main.py` | `poisson.py` | `odds_api.py` | `output.py` |
| **GG (analysis)** | `analyze_all.py` | `poisson.py` | `shared/odds.py` | inline in `analyze_all.py` |
| **Run-3** | `run3/main_run3.py` | `run3_probability.py` | none (hardcoded `None`) | inline in `main_run3.py` |

There is **no package structure** — no `__init__.py` at root, no `pyproject.toml`, no `src/` layout.
Modules import each other as top-level siblings, so every entry point depends on its CWD.

Note the pattern: each of the three subsystems has its own odds client and its own output writer.
The only genuinely shared component is `poisson.py` (shared between the two GG entry points), and even
that is duplicated by Run-3, which reimplements the λ formula inline rather than importing it.

---

## 2. Dependency graph (actual imports)

```
                        ┌──────────┐
                        │ config.py│◄──────── .env  (load_dotenv at import)
                        └────┬─────┘
        ┌────────────────────┼─────────────────────┬──────────────┐
        │                    │                     │              │
   ┌────▼────┐         ┌─────▼─────┐         ┌─────▼─────┐  ┌─────▼──────┐
   │ espn.py │         │ filters.py│         │decision.py│  │odds_api.py │
   └────┬────┘         └─────┬─────┘         └─────┬─────┘  └─────┬──────┘
        │                    │                     │              │
        │   ┌────────────────┴─────────┐           │              │
        │   │                          │           │              │
   ┌────▼───▼──┐              ┌────────▼──────┐    │              │
   │  main.py  │──────────────► poisson.py    │◄───┼──────────────┘
   └─────┬─────┘              └───────▲───────┘    │
         │                            │            │
         └────────► output.py         │            │
                                      │            │
   ┌──────────────┐                   │            │
   │analyze_all.py│───────────────────┘            │
   └──────┬───────┘                                │
          └────────► shared/odds.py ───────────────┘ (config only)

   ── DEAD (never imported by anything) ──
   sofascore.py     sportmonks.py     api_football.py
   config.PHASE_2_LEAGUES             odds_api.get_upcoming_odds()

   ── ISOLATED SUBSYSTEM (imports nothing from root) ──
   run3/main_run3.py ──► run3_probability.py
                    ├──► run3_filters.py
                    └──► run3_decision.py
        (own ESPN client, own league map, own λ formula, own JSON writer)
```

**No circular dependencies.** `config.py` is the only shared root.
`poisson.py` is a true leaf — it imports nothing but `math` and `typing`.

---

## 3. Layer analysis

| Conceptual layer | Where it actually lives | State |
|---|---|---|
| Configuration | `config.py` + constants scattered across 6 other modules | Fragmented |
| Provider / ingestion | `espn.py`, plus a second copy inside `run3/main_run3.py` | Duplicated, 1 live of 4 |
| Domain model | **nowhere** — untyped `Dict[str, Any]` throughout | Missing |
| Validation | **nowhere** — no schema checks; silent fallbacks instead | Missing |
| Model | `poisson.py`, `run3_probability.py` | Clean, the strongest part |
| Filtering | `filters.py`, `run3_filters.py` | Clean code, broken wiring |
| Decision | `decision.py`, `run3_decision.py`, `shared/odds.py` | Three divergent implementations |
| Output | `output.py` + two inline writers | Triplicated |
| Storage | **nowhere** | Missing |
| API | **nowhere** | Missing |
| Tests | **nowhere** | Missing |

Four of eleven layers do not exist. The three that do exist cleanly are the mathematical ones.

---

## 4. Module inventory

### Live modules
| Module | Lines | Role | Purity |
|---|---|---|---|
| `config.py` | 45 | Constants, env loading | Side effect at import |
| `espn.py` | 162 | Only active provider | I/O |
| `poisson.py` | 68 | **POISSON_V1** | **Pure** |
| `filters.py` | 80 | 5 hard filters | **Pure** |
| `decision.py` | 109 | Edge + bet gating | **Pure** |
| `odds_api.py` | 143 | Odds client (v1) | I/O |
| `output.py` | 174 | Writers | I/O |
| `main.py` | 204 | GG entry point | Orchestration |
| `analyze_all.py` | 276 | GG analysis entry point | Orchestration |
| `shared/odds.py` | 328 | Odds client (v2) + classification | I/O + cached state |

### Run-3 modules
| Module | Lines | Role | Purity |
|---|---|---|---|
| `run3_probability.py` | 71 | Cubed-share model | **Pure** |
| `run3_filters.py` | 77 | 3 rejection rules | **Pure** |
| `run3_decision.py` | 169 | R3-NO / R3-YES / SKIP | **Pure** |
| `main_run3.py` | 440 | Entry + embedded provider | Orchestration + I/O |

### Dead modules
| Module | Lines | Why dead |
|---|---|---|
| `sofascore.py` | 205 | Never imported |
| `sportmonks.py` | 249 | Never imported; would return 0 fixtures (GG-009) |
| `api_football.py` | 200 | Never imported — although `GG.md` names it the primary source |

**654 lines — 17% of the codebase — is unreachable.**

---

## 5. External interfaces

| Service | Transport | Auth | Used by | Status |
|---|---|---|---|---|
| ESPN site API | **HTTP** (not HTTPS) | none | `espn.py`, `main_run3.py` | Live; `/standings` returns `{}` |
| The Odds API v4 | HTTPS | `apiKey` query param | `odds_api.py`, `shared/odds.py` | Never succeeded in any committed run |
| SportMonks v3 | HTTPS | `api_token` | `sportmonks.py` | Dead |
| SofaScore | HTTPS | none (browser UA spoof) | `sofascore.py` | Dead |
| API-Football | HTTPS | RapidAPI headers | `api_football.py` | Dead |

No database. No message queue. No cache server. No outbound webhooks.

---

## 6. Execution model

Single-threaded, synchronous, sequential. One `requests` call at a time, 10–30 s timeouts,
no retry, no backoff, no connection pooling beyond `requests` defaults, no rate limiting.

For GG (5 leagues): roughly `5 + 2N + N` requests for N fixtures (`shared/odds.py` caches odds per
league; `odds_api.py` does not).
For Run-3 (36 leagues): roughly `36 + 2N`, uncached — hundreds of sequential requests on a busy day.

State is entirely in-process. Nothing persists between runs except the output files.

---

## 7. Configuration surface

`config.py` holds: `ESPN_BASE_URL`, three legacy API-key pairs, `ALLOWED_LEAGUES` (5 ESPN codes),
`PHASE_2_LEAGUES` (dead), and four thresholds (`MIN_ODDS 1.60`, `MIN_EDGE 0.05`, `MIN_AVG_GOALS 1.0`,
`MAX_CLEAN_SHEET_PCT 0.40`).

Constants also live in `run3_filters.py` (3), `run3_decision.py` (14), `shared/odds.py` (4 + a
28-league map), `odds_api.py` (5-league map), `sofascore.py` (7-league map) and
`run3/main_run3.py` (36-league map + a duplicate `ESPN_BASE_URL`).

**Five league maps exist in five different files with different contents.**

---

## 8. Structural observations

1. **The mathematical core is well isolated.** `poisson.py`, `filters.py`, `decision.py`,
   `run3_probability.py`, `run3_filters.py`, `run3_decision.py` are all pure functions with no I/O.
   That is 574 lines of directly testable logic, and it is why an incremental migration is viable.
2. **No abstraction boundary at the provider layer.** Entry points import `espn` concretely. Swapping
   providers means editing every entry point.
3. **Untyped dictionaries as the universal interface.** Every function passes `Dict[str, Any]`.
   The `api_football` / `main.py` schema mismatch (missing `total_goals_avg`) is invisible until runtime.
4. **Run-3 is a fork, not a module.** It shares zero code with GG despite computing identical λ values,
   because `run-3.md` asked for isolation. The result is duplication rather than separation.
5. **Two GG entry points have diverged.** Different odds clients, different decision logic, and
   different filter inputs for the same fixture (GG-006).
6. **Silent-fallback architecture.** Every failure path returns a plausible number rather than an
   error. The system is structurally incapable of reporting "I don't know", which is the root cause of
   the CRITICAL data-quality findings in `REPO_AUDIT.md` §10.
