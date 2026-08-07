# Epic 0 Summary — GG Predictor Repository Audit

**Audited:** commit `be67223` · 2026-08-07 · 29 files, ~4,000 lines, all 18 Python files read.
**Changes made to source code:** none. Five documents in `docs/` were added; nothing else was touched.

---

## Project Status

A working prototype with a sound mathematical core and an unreliable data layer.

The Poisson model is correct and matches its specification exactly. Almost everything around it —
validation, storage, testing, provider abstraction — is either missing or silently substituting
fabricated values for real data. Two subsystems are provably non-functional end to end, and neither
fails loudly: both produce plausible-looking output.

Assessment: **worth reviving, not worth rewriting.** The 574 lines of pure model logic are the asset.
The data layer needs the work.

---

## What The Project Currently Does

Given a date, it pulls that day's fixtures from ESPN for a whitelist of leagues, retrieves each team's
season home/away goal statistics, computes Poisson expected goals (λ) for both sides, derives a
both-teams-to-score probability, applies safety filters, optionally compares against bookmaker BTTS
odds to compute an edge, and writes results to the terminal plus CSV/JSON.

A separate Run-3 subsystem predicts "three unanswered goals" using the same λ values.

---

## Current Models

| Model | File | Status |
|---|---|---|
| **POISSON_V1** (GG/BTTS) | `poisson.py` | Correct, matches `GG.md` exactly, deterministic |
| **Run-3** (three unanswered goals) | `run3/run3_probability.py` | Model correct; decision layer unreachable |

No Dixon-Coles. No machine learning. No model versioning or registry.

---

## Current Data Providers

| Provider | File | Status |
|---|---|---|
| **ESPN** | `espn.py` | **The only live provider.** Partially working |
| The Odds API | `odds_api.py`, `shared/odds.py` | Configured; never succeeded in any committed run |
| SofaScore | `sofascore.py` | Dead — never imported |
| SportMonks | `sportmonks.py` | Dead — never imported |
| API-Football | `api_football.py` | Dead — never imported, though `GG.md` names it primary |

Schemas are mutually incompatible (string vs integer league IDs, differing field sets). There is no
normalisation layer.

---

## What Works

- `poisson.py` — **WORKING.** Pure, deterministic, faithful to spec. The baseline worth preserving.
- `run3_probability.py`, `run3_filters.py` — **WORKING.** Pure, faithful to spec.
- `filters.py`, `decision.py` — **WORKING in isolation.** Correct logic, never given real inputs.
- ESPN fixture retrieval — **LIKELY WORKING.** 39 real fixtures appear in the committed output.
- ESPN home/away splits — **PARTIALLY WORKING.** Live-verified as genuine for in-season leagues.
- Output writers — **LIKELY WORKING.** Committed CSV/JSON are well-formed.

**Caveat:** `requests` and `python-dotenv` are not installed for the default interpreter, so no entry
point runs on this machine right now. The verdicts above are about logic, not the current environment.

---

## What Is Broken

1. **ESPN league-average lookup.** The `/standings` endpoint returns HTTP 200 with an empty body `{}`
   for every league tested. Every league average is therefore the hardcoded `1.35` — and it is the
   denominator of both λ values, so it scales every prediction. Silent, because the status code is 200.
2. **The GG hard filters.** Four of five cannot fire. Confirmed empirically: `passes_filters: true`
   for 39 of 39 committed fixtures.
3. **Run-3, entirely.** `P_R3_NO` has a global maximum of 0.7656; the code requires ≥ 0.78. R3-YES
   requires exactly the states its own filters reject. Proven by exhaustive search — 757 lines that
   return SKIP for every fixture, always.
4. **Odds integration.** `odds: null` in 39 of 39 committed fixtures. Requires a runtime test with a
   valid key to distinguish a missing key from a matching failure.

---

## Critical Risks

**Data integrity — fabricated values are indistinguishable from real ones.** `espn.get_stat()` returns
`0` for any missing statistic, and `poisson.py` accepts `0.0` as valid data (it only rejects `None` and
negatives). Combined with the `1.35` league-average fallback and the halved home/away match counts, the
model can produce a confident probability from data that was never received. `GG.md` §6 promises
"if any of these are missing → NO BET"; that contract is not enforced anywhere.

**Data leakage — backtesting is invalid by construction (LEAK-001).** `get_team_stats()` takes no date
parameter, while `get_fixtures()` does. Running `main.py 2025-09-15` today pairs September's fixture
list with today's cumulative season statistics — including 11 months of matches played afterwards.
The date argument makes this look like a legitimate backtest. Any accuracy figure from it would be
inflated and meaningless, and a Dixon-Coles-vs-Poisson comparison on leaking data would produce
confident numbers justifying a wrong choice.

**Silent failure as an architectural pattern.** Every failure path returns a plausible number instead of
an error. The system is structurally incapable of saying "I don't know", which is the root cause behind
most findings above.

---

## Missing Infrastructure

- **Tests:** zero. No `tests/`, no `pytest`, no CI. **POISSON_V1 has no regression test.**
- **Database:** none. No SQLite, no PostgreSQL, no ORM, no migrations.
- **Historical storage:** none. No stored predictions, no match results, no point-in-time snapshots.
- **Backtesting:** does not exist, and cannot exist until point-in-time data does.
- **Validation:** none. API JSON is consumed with `.get()` chains and no schema checks.
- **Model versioning:** none. Output carries no model or schema version.
- **Logging:** `print()` only. No levels, no structure, no run ID.
- **API / UI:** neither exists. No FastAPI, no `package.json`.

---

## Top 10 Problems

| # | ID | Severity | Problem |
|---|---|---|---|
| 1 | GG-001 | CRITICAL | Missing statistics silently become `0` and reach the model as real data |
| 2 | LEAK-001 | CRITICAL | No point-in-time data — any backtest is contaminated by look-ahead bias |
| 3 | GG-003 | CRITICAL | League average always `1.35`; `/standings` returns `{}` (live-verified) |
| 4 | GG-002 | CRITICAL | 4 of 5 hard filters cannot fire; 39/39 fixtures passed |
| 5 | R3-001 | CRITICAL | Run-3 threshold 0.78 exceeds the function's maximum 0.7656 — always SKIP |
| 6 | — | CRITICAL | Zero tests; POISSON_V1 baseline has nothing pinning its behaviour |
| 7 | GG-006 | HIGH | Two GG entry points feed different quantities into the same filter |
| 8 | GG-008 | HIGH | Substring team-name matching can attach another match's odds to a fixture |
| 9 | GG-004 | HIGH | Home/away match counts fabricated by halving when absent |
| 10 | GG-013/014 | MEDIUM | Fixture status never checked; naive local-vs-UTC date handling |

Full register with evidence: `docs/TECHNICAL_DEBT.md`.

---

## Recommended Next Step

**Epic 1 should be "Lock the baseline, then make failure visible" — in that order.**

1. **Provision a runnable environment.** Virtualenv with pinned dependencies. Nothing runs today.
2. **Write deterministic regression tests for `poisson.py` before changing any code.** Golden input/
   output pairs, frozen as `POISSON_V1`. This is a few hours of work and it is what makes every
   subsequent change safe. Do the same for `filters.py`, `decision.py` and the Run-3 pure modules.
3. **Replace silent fallbacks with an explicit `DATA_UNAVAILABLE` state** (GG-001, GG-003, GG-004).
   Have the model refuse to score rather than substitute. Expect the number of predictions to drop
   sharply — that is the correct outcome, and it is why step 2 comes first.
4. **Bring the decisions to you, not the code.** Five `GG.md`-vs-code disagreements (D1–D5 in
   `REPO_AUDIT.md` §7) and the Run-3 `0.75`-vs-`0.78` divergence need a ruling on which side is
   authoritative. The audit deliberately does not decide these.

Defer Dixon-Coles, the API and the UI. Point-in-time storage (LEAK-001) is the gate for any model
comparison work — building a challenger model before that lands would produce trustworthy-looking
results that cannot be trusted.

---

## Files Created During Audit

| File | Contents |
|---|---|
| `docs/REPO_AUDIT.md` | Full audit — repository map, per-file analysis, providers, config, dependencies, POISSON_V1, Run-3, odds, data quality, leakage, testing, code quality, security, what works, gap analysis, refactor order |
| `docs/CURRENT_ARCHITECTURE.md` | Module structure, dependency graph, layer analysis, execution model |
| `docs/DATA_FLOW.md` | Execution flows for all three entry points with fallback injection points marked |
| `docs/TECHNICAL_DEBT.md` | Prioritised register: 4 CRITICAL, 6 HIGH, 8 MEDIUM, 6 LOW, with evidence |
| `docs/EPIC_0_SUMMARY.md` | This document |

No source file was modified, renamed or deleted.
