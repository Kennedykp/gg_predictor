# Epic 1B.2 — ESPN Provider Reliability: the fallback that always won

Closes **GG-003** (league average) and **GG-004** (home/away counts), and fixes the transport defects
underneath them: **GG-012** (no timeout/retry), **GG-013** (fixture status unchecked), **GG-014**
(naive datetimes), **GG-020** (plaintext HTTP).

Scope is the ESPN provider's transport and its two fabricated inputs. **POISSON_V1 mathematics in
`poisson.py` is UNCHANGED** and remains the frozen baseline — the 38 golden input/output pairs and the
wider regression suite pass unchanged (see [Verification](#verification)).

---

## The root cause

`get_league_avg_goals` requested:

```
/apis/site/v2/sports/soccer/{league}/standings
```

That path returns **HTTP 200 with a 2-byte body: `{}`**.

Because the status code is 200, `raise_for_status()` never fired. `data` was falsy, the `if not data`
guard caught it, and the function returned the hardcoded `1.35` — **on every single call, in all
conditions**. The system never once computed a league average.

The working path is `/apis/v2/sports/soccer/{league}/standings` — the same host, **without `/site`**.

### Live evidence

Same league, same season, same query string. The only variable is the path:

| Path | Status | Body | Content |
|---|---|---|---|
| `/apis/site/v2/sports/soccer/eng.1/standings` | **200** | **2 bytes** | `{}` |
| `/apis/v2/sports/soccer/eng.1/standings` | **200** | **~68 KB** | full standings table |

Both answer HTTP 200. Only the second carries data.

### The lesson

**A 200 status is not evidence of data.** A status-code-only check cannot distinguish these two
responses, which is precisely how the defect survived: success by status, nothing by content.

**A fallback that looks plausible hides its own cause.** `1.35` is a credible league average. Nothing
in the output looked wrong, so nothing prompted anyone to check the URL. Had the fallback been absurd —
or absent — the broken path would have been found immediately.

`_fetch` now names this case explicitly as `ESPNError.EMPTY_RESPONSE`, so a 200-with-`{}` can never
again pass as success.

---

## Units: goals per team per match

This matters because it is **the denominator of both lambdas** in POISSON_V1:

```
λ_home = (Home_GF_home × Away_GA_away) / League_Avg_Goals
λ_away = (Away_GF_away × Home_GA_home) / League_Avg_Goals
```

The units must match the quantities divided by it:

```
total goals scored by all teams / total team-games
```

A standings table counts **each fixture twice**, once per team. So summing `gamesPlayed` across the
table gives **team-games, not fixtures**.

| Quantity | EPL 2025-26 (live) |
|---|---|
| Total goals | 1045 |
| Total team-games (Σ `gamesPlayed`) | 760 |
| **Goals per team per match** | **1.3750** ← the correct denominator |
| Goals per fixture | 2.7500 |

Using the per-fixture figure would **halve every lambda**. `poisson.py:26` ("goals per team per match")
and `GG.md:110` ("per team") both agree with the value now computed.

The old constant `1.35` was in the right ballpark — about 1.8% below the real figure — which is exactly
why the defect survived. It was not merely unsourced; it was wrong, and close enough to look right.

---

## What changed

| # | Change | Rationale |
|---|---|---|
| 1 | **HTTPS** (`ESPN_BASE_URL`, `ESPN_STANDINGS_BASE_URL`) | Traffic is unauthenticated but was plaintext and therefore MITM-modifiable. A tampered response feeds the model directly. |
| 2 | **Correct standings path** (`ESPN_STANDINGS_BASE_URL`, no `/site`) | The root cause above. Kept as a separate constant because standings genuinely live on a different path from every other endpoint. |
| 3 | **Season resolution** (`resolve_season`) | ESPN identifies a season by its **starting year** — verified: the 2025-26 EPL season is `2025`. Two conventions: European (Aug–May) rolls over in July; calendar-year leagues (Brazil, MLS, Nordics) use the current year. Listed explicitly in `CALENDAR_YEAR_LEAGUES` rather than inferred — guessing calendar conventions is the same class of silent wrongness this epic removes. |
| 4 | **Real home/away counts** replace halving | `if not home_matches: home_matches = matches_played / 2` invented an even split. Schedules are genuinely uneven, so halving distorted every rate feeding lambda — invisibly. ESPN supplies the real counts. |
| 5 | **Explicit timeout** (`ESPN_TIMEOUT_SECONDS = 15`) | There was none. A hung socket hung the run. |
| 6 | **Bounded retry on transient failures only** (`ESPN_MAX_RETRIES = 2`, exponential backoff) | Timeouts, connection errors and 5xx are worth another attempt; a 404 never becomes a 200. Bounded because a retry storm against a free endpoint is its own failure mode, and an unbounded one turns an outage into a hang. |
| 7 | **Typed error semantics** (`ESPNError`, `FetchResult`) | "ESPN is down" and "no matches today" both used to arrive as `None`/`[]` — the same observation for two entirely different facts. They are now distinguishable without a large exception hierarchy. |
| 8 | **Fixture status and timezone-aware kickoffs** (`FixtureState`, `is_predictable`, `parse_kickoff`) | A pre-match model must not be handed a finished match: its statistics already contain that result. `state` alone is insufficient — a postponed match still reports `pre` — so status *names* are checked too. Kickoffs are returned tz-aware; a naive datetime would compare against local time and land on the wrong matchday. |
| 9 | **Provenance is `CALCULATED` / `UNAVAILABLE`, never `UNATTRIBUTED`** | The provider now reports its own source. A value reaching `main.py` or `analyze_all.py` is genuinely measured; failure arrives as `None` and stops the prediction. The `UNATTRIBUTED` state Epic 1B.1 had to invent is no longer produced. |

On failure, `get_league_avg_goals` now returns **`None`**, not `1.35`. Absence is reported as absence.

---

## Verified live findings

Stated as observed facts, from `scripts/espn_diagnostic.py` against live endpoints:

1. **The standings path difference is real and reproducible** — `/apis/site/v2/…` → 200 / 2 bytes /
   `{}`; `/apis/v2/…` → 200 / ~68 KB / full table.
2. **`homeGamesPlayed` and `awayGamesPlayed` exist on the team endpoint.** Halving `matches_played`
   was never necessary.
3. **The team endpoint ignores `?season=`.** Probed with and without the parameter during the 2026-27
   preseason: both returned `gamesPlayed=0.0`. Team statistics are therefore **current-season-only**.
   `get_team_stats` consequently sends no season parameter — there is no point.
4. **Fixture status exposes `state` (`pre` / `in` / `post`) plus a `completed` boolean.**
5. **ESPN timestamps are UTC with a trailing `Z`** (e.g. `2025-08-16T11:30Z`).

> ⚠️ **Finding 3 is a hard blocker for historical backtesting** and should be flagged for a future
> epic. No season-scoped team query exists on this endpoint, so past-season or as-of-date team stats
> cannot be retrieved from ESPN at all. This is the mechanism behind **LEAK-001**, and no amount of
> data-quality work in the provider removes it.

---

## Explicitly out of scope

Honest accounting of what was **not** done:

| Item | Status | Why |
|---|---|---|
| **GG-002** — `home/away_clean_sheet_pct` | Still hardcoded `0` in the provider | ESPN supplies **no clean-sheet data at all**. The domain contract can represent them as unavailable, but switching them here would make every fixture fail the filter and change production output. That is GG-002's job. |
| **GG-006** — two entry points, two filter semantics | Untouched | `main.py` passes combined goals-per-match and `analyze_all.py` passes the home-only scoring rate into the *same* filter parameter. Resolving it requires a product decision on the intended quantity, not a provider fix. |
| `is_predictable` / `kickoff_utc` | Available, **not yet wired** | The fields are populated on every fixture, but the pipeline's fixture selection does not consume them yet. Wiring them changes which fixtures are predicted, i.e. production output. |
| `sofascore.py`, `sportmonks.py`, `api_football.py` | **Not touched** | Confirmed by empty `git diff`. They still contain their own fallbacks — `stats.get("matches", 0)`, `matches_played // 2`, `goals_scored // 2`, `or 0` — i.e. the same class of defect this epic removed from `espn.py`. They are not on the live path, so fixing them here would have been unverifiable churn. |

---

## Verification

| Check | Result |
|---|---|
| Full suite | **1129 passed, 3 skipped** |
| Golden POISSON_V1 pairs (`-m golden`) | **38 passed, unchanged** |
| `tests/regression/` | **52 passed, 3 skipped** (was 51 passed / 4 skipped — D5 moved from skipped to passing) |
| `git diff` on `poisson.py`, `filters.py`, `decision.py`, `shared/`, `run3/` | **empty** |

The golden suite passing unchanged is the load-bearing check: the model did not move. Only the inputs
it is fed, and the honesty of the failure path, changed.

Note the regression directory went **51 → 52 passing** not because a test was added, but because a
previously-skipped spec placeholder now asserts real behaviour (see below). No test was deleted.

### New coverage

All offline and deterministic — the transport is stubbed, so nothing in CI touches the network:

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_espn_provider.py` | 85 | `resolve_season` (both conventions, rollover boundaries), `parse_kickoff`, `is_predictable`, the enums, and URL regressions pinning the correct standings path |
| `tests/unit/test_espn_league_average.py` | 29 | Hand-calculated averages, **units are per-team-per-match**, no hardcoded fallback, preseason, scored/conceded integrity, partial tables, season forwarding |
| `tests/unit/test_espn_home_away_splits.py` | 23 | Real split counts as divisors, odd totals, uneven splits changing the answer, missing counts as unavailable rather than halved, zero-match splits as undefined |
| `tests/unit/test_espn_transport.py` | 45 | Success, `{}`-is-not-success, malformed JSON, 4xx permanent, 5xx retried-but-bounded, timeout/connection handling |

**`scripts/espn_diagnostic.py`** — the live diagnostic, **manually run and excluded from CI**. It lives
outside `testpaths`, has no `__init__.py`, and does all work inside `main()` behind an
`if __name__ == "__main__"` guard, so importing it performs no I/O. It reimplements nothing: it imports
`espn.py` and `config.py` and calls the real functions, so what it prints is what production sees. A
test that depends on a third-party endpoint being up is not a test.

### Transitioned, not deleted

The two GG-004 characterization tests in `tests/unit/test_espn_missing_data.py` were **transitioned**:
their assertions are **inverted, not removed**. `test_absent_home_games_played_is_still_halved` became
`test_absent_home_games_played_is_no_longer_halved`, and the uneven-split test now asserts `None`
instead of the fabricated `1.8`. The file still documents the defect, and now proves it cannot return.

### Spec agreement

**D5 is resolved.** `test_d5_league_average_is_no_longer_always_the_fallback` was a skipped placeholder;
it is now a **real passing test** asserting both halves of the contract — a real table is computed
(deliberately yielding `1.0`, not `1.35`, so a lingering fallback cannot pass), and an unavailable table
yields `None`. D1, D3 and D4 remain open product decisions.

---

## What this unblocks

The league average is now measured rather than assumed, so every published probability is derived from
real data end to end for the first time. `LeagueAverageSource.CALCULATED` makes that auditable.

Recommended next: **GG-002** (clean-sheet data needs a provider that supplies it), then **GG-006** (a
product decision on the filter quantity), then wiring `is_predictable` into fixture selection.

**LEAK-001 is not addressed and is now better understood:** finding 3 above shows ESPN's team endpoint
cannot serve historical statistics at all. Backtesting needs a different data source, not a better
ESPN client.
