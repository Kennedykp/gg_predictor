# Epic 1B.5 — Point-in-Time POISSON_V1 Inputs

## Objective

Epic 1B.4 made the *filter* statistics point-in-time correct. The *model* inputs were not: all five
POISSON_V1 inputs still came from ESPN's current-season aggregates, which describe the season **as it
stands today**. Scoring a fixture that has already been played therefore used that fixture's own result
as evidence for itself.

This Epic moves all five inputs onto the same match-level, cutoff-enforced foundation Epic 1B.4 built
for filters, so that for a target kickoff `T` every model input is derived only from matches that
kicked off strictly before `T`.

**POISSON_V1's mathematics are unchanged.** Only the provenance of the numbers fed into it changed.

## The Five Inputs

Read from `poisson.py` rather than from documentation:

| Input | Source before 1B.5 | Source after 1B.5 |
|---|---|---|
| `league_avg_goals` | standings table (today) | completed league matches before `T` |
| `home_goals_scored_home` | team aggregate (today) | home team's HOME matches before `T` |
| `home_goals_conceded_home` | team aggregate (today) | home team's HOME matches before `T` |
| `away_goals_scored_away` | team aggregate (today) | away team's AWAY matches before `T` |
| `away_goals_conceded_away` | team aggregate (today) | away team's AWAY matches before `T` |

`league_avg_goals` is **goals per TEAM per match**, not per fixture. `GG.md` and `poisson.py` agree on
this, and it matters: using the per-fixture figure would halve every λ. A league programme counts each
match twice (once per team), which is what makes the team-level figure the correct denominator.

## Live Endpoint Findings (2026-08-09, read-only)

**Team schedule — `/{league}/teams/{team_id}/schedule`**

| Question | Finding |
|---|---|
| Honours `season=`? | **Yes** — `season=2026` → 0 events; `season=2025` → 38 events |
| Competition-pure? | **Yes** — 38/38 events carry competition id `700` (Premier League) |
| Completion status | `status.type.state = "post"`, `name = "STATUS_FULL_TIME"` |
| Scores | strings (`"3"`, `"0"`), per competitor |
| Home/away identity | `competitors[].homeAway` |
| Event IDs | present and stable |
| Kickoff | ISO-8601 with trailing `Z` |

Two traps worth recording:

1. **The echoed season year lies.** A `season=2025` request comes back with `season.year = 2026` in the
   payload. Verification therefore compares **returned event IDs**, never the echo — a 200 response and
   a plausible-looking field prove only that the parameter was accepted.
2. **ESPN soccer season year = the year the season starts.** `season=2025` is the 2025/26 season, whose
   matches are dated into May 2026. Off-by-one here silently yields the wrong season's data.

**League programme.** The league-wide baseline uses the scoreboard endpoint with an explicit date range
and `limit=1000`. The default limit is **100**, which a full matchweek across a date range can exceed;
a truncated response would produce a baseline that looks fine and is computed from a subset. The
provider treats a suspected-truncated response as a failure rather than averaging it.

## Cutoff Enforcement

The rule is `record.kickoff < T`, strict. A match kicking off at exactly `T` is excluded — same instant
means no information was available.

The cutoff is a **required keyword argument** at the derivation boundary, not an optional filter applied
by callers. There is no code path that returns a derived input without one, which is the structural
reason a caller cannot forget it.

All comparisons are timezone-aware UTC. Naive datetimes are rejected rather than coerced, since a naive
value compares silently against local time and on a UTC+1 machine a 23:30Z kickoff lands on the wrong
matchday.

## Live Cutoff Confirmation

For a target of 2026-02-08 15:00 UTC:

| Team | Venue | n | GF/match | GA/match |
|---|---|---|---|---|
| Arsenal (359) | HOME | 13 | 2.385 | 0.615 |
| Chelsea (363) | AWAY | 13 | 1.923 | 1.154 |

`n = 13`, not the 19 a full-season aggregate would supply. The cutoff is visibly doing its job on real
data rather than only in fixtures.

## Failure Behaviour

No fallback to current-season aggregates. That fallback would fire exactly when history is thin — early
season, promoted sides — which is where the leak was most valuable and least visible.

Incomplete inputs mean **no prediction**, not a substituted value. This matters most in
`analyze_all.py`, which publishes `gg_no_prob = 1 - gg_yes_prob`: a fabricated `0.0` there becomes a
**100%-confident GG_NO** that can classify as STRONG_VALUE against real odds. The distinction Epic 1B.1
established is preserved — absence stays absent.

Sample sizes for each input are reported alongside the refusal, so "no prediction" is diagnosable rather
than opaque.

## Entry-Point Consistency

`main.py` and `analyze_all.py` both call `build_fixture_poisson_inputs` with identical arguments. This
is the same structural fix GG-006 applied to filters, now applied to the model: the two entry points
cannot derive different inputs for the same fixture because there is only one place that decides what an
input means.

## Caching

Per-run, in-memory only. The team-schedule fetch is shared with Epic 1B.4's history derivation, so
turning on point-in-time model inputs costs **no additional requests** — the filter statistics and the
model inputs are two derivations over one fetch. The league programme is cached per league.

Cache keys include every parameter that changes the response (league, team, season). Caching stores
**raw records**, never derived figures, so a cache hit cannot bypass the per-fixture cutoff.

## Verification

Behavioural, not by inspection. `tests/regression/test_point_in_time_inputs.py` adds 30 future matches
and an entire future league programme, then requires the derived inputs to be **byte-identical** to the
run without them.

The suite was **mutation-tested**: weakening `<` to `<=`, and removing the cutoff entirely, each produce
failures. A regression test that passes whether or not the guard exists proves nothing, so this was
checked rather than assumed.

`get_team_stats()` is not reachable from the model path, and a test makes calling it from there an
error, so the leaking source cannot quietly return as a fallback.

## Historical Safety Boundary

**LEAK-001 remains OPEN. Historical backtesting is still NOT safe.**

What improved: the λ inputs and the league baseline are now point-in-time correct. What has not:

1. **Odds are still today's.** `decision.py` compares the model probability against the price available
   *now*. Since the edge decides whether a bet is placed, a backtest of *recommendations* stays invalid
   even with a perfectly clean probability.
2. **The statistics endpoint is unchanged** (GG-024). It remains current-season-only and is still used
   for display and diagnostics.
3. **Correct mechanics are not a validated backtest.** No historical run has been executed, scored, or
   compared against a holdout. "The inputs respect a cutoff" and "the measured accuracy is trustworthy"
   are different claims; only the first is supported.

The precise, defensible claim is narrower than "the leak is fixed": *given a target kickoff, the five
model inputs are invariant to everything that happens at or after it.*

## Issue Status

| Issue | Status |
|---|---|
| LEAK-001 | **OPEN** — narrowed to odds; explicitly not closed |
| GG-024 | **OPEN** — schedule honours `season=`, statistics endpoint still does not |
| GG-002-B | **OPEN, unchanged** — knockout-first-leg and heavy-favourite mismatch still have no feed |

Nothing was closed in this Epic. Partial improvement is not resolution.

## Model Safety Verification

`git diff` against `origin/main` for `poisson.py`, `config.py`, `filters.py`, `decision.py` and `run3/`
is **empty**. No threshold was touched: `MAX_CLEAN_SHEET_PCT`, `MIN_AVG_GOALS`, `EDGE_THRESHOLD`,
`MIN_ODDS` and the decision thresholds are unchanged. No new filter was added.

Golden regression: **38 passed**, outputs identical for identical pure inputs.

## Validation

| Check | Result |
|---|---|
| pytest | **1332 passed, 2 skipped** (skips are the pre-existing D1/D3 spec decisions) |
| golden regression | **38 passed**, unchanged |
| ruff | **All checks passed** |
| mypy | **Success: no issues found in 46 source files** |

Tests remain offline and deterministic. The live diagnostic in `scripts/espn_diagnostic.py` is manual
and outside `testpaths`.

## Files Changed

**Production:** `espn.py`, `main.py`, `analyze_all.py`, `domain/poisson_inputs.py` (new),
`domain/__init__.py`, `shared/match_history.py`

**Tests:** `tests/regression/test_point_in_time_inputs.py` (new), `tests/conftest.py` (new),
`tests/integration/test_entry_point_consistency.py`, `tests/integration/test_pipeline_missing_data.py`

**Docs:** `docs/TECHNICAL_DEBT.md`, this file

An annotation correction was also made in `analyze_all.py`: `home_stats`/`away_stats` are now
`Optional`, matching what the provider actually returns and what the function body already checked for,
and the now-vestigial `league_avg` parameter is documented as no longer used for modelling.

## Recommended Next Step

Point-in-time **odds** are the remaining blocker for LEAK-001. Until historical prices are available,
any backtest measures the model against a market that did not exist. That is a data-acquisition
question, not a code change, and it should be settled before any accuracy figure is produced or any
model comparison is attempted.
