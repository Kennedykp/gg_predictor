# GG Predictor — Data Flow (as of `be67223`)

Every flow below is derived from the code. Nothing is inferred or invented.
`⚠` marks a point where fabricated or fallback data enters the pipeline.

---

## 1. GG primary flow — `python main.py [YYYY-MM-DD]`

```
CLI arg (or date.today() ── ⚠ LOCAL timezone, machine is UTC+1)
   │
   ▼
config.ALLOWED_LEAGUES  (5 ESPN league codes)
   │
   ▼
espn.get_fixtures(league_id, date)
   GET http://site.api.espn.com/.../{league}/scoreboard?dates=YYYYMMDD
   │      ⚠ plain HTTP
   │      ⚠ 'status' is captured but NEVER checked (finished/postponed treated as upcoming)
   ▼
[fixture dicts: fixture_id, league, home/away id+name, datetime(str), status]
   │
   ▼
espn.get_league_avg_goals(league_id)
   GET .../{league}/standings
   │      ⚠⚠ LIVE-VERIFIED: returns HTTP 200 with body '{}' for every league
   │      ⚠⚠ therefore ALWAYS falls through to the hardcoded 1.35
   ▼
league_avg_goals = 1.35   (in practice, always)
   │
   ▼
espn.get_team_stats(league_id, team_id)   ×2 per fixture
   GET .../{league}/teams/{id}
   │      ⚠ get_stat() returns 0 for ANY missing statistic
   │      ⚠ if homeGamesPlayed == 0 → substitute matches_played / 2
   │      ⚠ home_clean_sheet_pct / away_clean_sheet_pct HARDCODED to 0
   │      ✔ returns None if matches_played == 0  (correct guard)
   ▼
[home_stats, away_stats]
   │
   ▼
main.py None-checks (lines 82-95)
   ✔ skips fixture if either stats dict is None
   ✘ does NOT detect fabricated zeros — provider returns 0, not None
   │
   ▼
poisson.calculate_gg_probability(
     league_avg_goals,                  ← always 1.35
     home_stats.home_goals_scored,
     home_stats.home_goals_conceded,
     away_stats.away_goals_scored,
     away_stats.away_goals_conceded)
   │
   │   λ_home = (home_scored_home × away_conceded_away) / league_avg
   │   λ_away = (away_scored_away × home_conceded_home) / league_avg
   │   P(GG)  = (1 - e^-λ_home) × (1 - e^-λ_away)
   │      ⚠ rejects None and negatives; ACCEPTS 0.0 as valid data
   │      ⚠ no upper bound on λ (committed output contains λ = 4.36)
   ▼
{lambda_home, lambda_away, gg_probability}
   │
   ▼
filters.apply_filters(
     home_avg_goals   = home_stats.total_goals_avg   ⚠ (GF+GA)/matches, not goals scored
     away_avg_goals   = away_stats.total_goals_avg
     home_clean_sheet_pct = 0                        ⚠ hardcoded by provider → filter can never fire
     away_clean_sheet_pct = 0                        ⚠
     is_knockout_first_leg      = False              ⚠ hardcoded in main.py:104
     is_heavy_favorite_mismatch = False              ⚠ hardcoded in main.py:105
     has_reliable_data          = True               ⚠ hardcoded in main.py:106
   )
   ▼
passes_filters   ── EMPIRICALLY True for 39/39 committed fixtures
   │
   ▼
odds_api.get_btts_odds(league_id, home_name, away_name)
   GET https://api.the-odds-api.com/v4/.../odds?markets=btts
   │      ⚠ returns None SILENTLY if ODDS_API_KEY unset
   │      ⚠ substring team-name matching
   │      ⚠ first bookmaker wins — not best price
   │      ⚠ no cache: refetches the whole league per fixture
   │      ⚠ odds timestamp never read → staleness invisible
   ▼
odds (null in 39/39 committed runs)
   │
   ▼
decision.make_decision(gg_probability, odds, passes_filters)
   implied = 1/odds ;  edge = P(GG) - implied
   FLAG GG  iff  passes_filters AND odds ≥ 1.60 AND edge ≥ 0.05
   ▼
NO BET ("No odds available") ── 39/39 committed fixtures
   │
   ▼
output.print_results()  →  terminal
output.write_csv()      →  ./output_YYYY-MM-DD.csv   ⚠ writes to CWD
output.write_json()     →  ./output_YYYY-MM-DD.json
```

**Nine fallback injection points before the model, and none of them raises or logs an error.**

---

## 2. GG analysis flow — `python analyze_all.py [YYYY-MM-DD]`

Identical up to the model, then diverges:

```
… same fixture + stats + poisson path …
   │
   ▼
filters.apply_filters(
     home_avg_goals = home_stats.home_goals_scored   ⚠ DIFFERENT QUANTITY from main.py
     …)
   ▼
shared/odds.get_btts_odds()      (cached per league per run ✔)
   │
   ▼
shared/odds.analyze_market(market_type, model_prob, odds)
   │      ⚠ edge serialised as: round(edge,4) if edge else None
   │        → an exact 0.0 edge becomes null, indistinguishable from "no odds"
   │      ⚠ R3_YES market maps to BTTS odds as a "proxy" (different event; dead path today)
   ▼
classification ∈ {VALUE_BET, FAIR_PRICE, NO_VALUE, LOW_ODDS, NO_ODDS}
recommendation ∈ {RECOMMEND_PLAY, RECOMMEND_NO_PLAY}
   ▼
terminal + ./analysis_YYYY-MM-DD.json   (own writer, not output.py)
```

**The two GG entry points feed different quantities into the same filter parameter.**
The same fixture can therefore be filtered differently depending on which script you run.

---

## 3. Run-3 flow — `cd run3 && python main_run3.py [YYYY-MM-DD]`

```
CLI arg (default date.today())    ⚠ must be run from inside run3/ (sibling imports)
   │
   ▼
main_run3.ALL_LEAGUES  (36 hardcoded codes — its own map, not config.ALLOWED_LEAGUES)
   │
   ▼
main_run3.get_all_fixtures()      ⚠ duplicate ESPN client
   │      ⚠ bare except swallows the exception WITHOUT printing (line 91)
   ▼
main_run3.get_team_stats()  ×2 per fixture   ⚠ no caching across 36 leagues
   │      ⚠ same 0-for-missing, same matches_played/2 halving
   ▼
main_run3.get_league_avg_goals()
   │      ⚠⚠ same /standings endpoint → always the hardcoded 1.35
   ▼
main_run3.calculate_lambdas()     ⚠ re-implements the GG λ formula instead of importing poisson.py
   ▼
run3_probability.calculate_run3_probabilities(λ_home, λ_away)
   p_home = λ_h/(λ_h+λ_a)        P_home_run3 = p_home³
   p_away = λ_a/(λ_h+λ_a)        P_away_run3 = p_away³
   P_R3_YES = 1 - (1-P_home_run3)(1-P_away_run3)
   P_R3_NO  = 1 - P_R3_YES
   ▼
run3_filters.apply_run3_filters()
   reject if  λ_h+λ_a ≥ 3.5  |  p ≥ 0.65 either side  |  λ ≥ 2.2 either side
   ▼
run3_decision.make_run3_decision()
   R3-NO  requires P_R3_NO ≥ 0.78
          ⚠⚠ MATHEMATICALLY IMPOSSIBLE — max(P_R3_NO) = 0.765625 at p = 0.5
   R3-YES requires p ≥ 0.65 AND λ ≥ 2.2
          ⚠⚠ exactly the states the filters above reject — mutually exclusive
   ▼
SKIP ── for 100% of fixtures, always, for any input
   ▼
terminal + ./run3_output_YYYY-MM-DD.json  ("no selections", indistinguishable from a quiet day)
```

---

## 4. Where fabricated data enters (consolidated)

| # | Location | Fabrication | Reaches the model? |
|---|---|---|---|
| 1 | `espn.py:101` | any missing stat → `0` | **Yes, directly as a λ term** |
| 2 | `espn.py:117-118` | `homeGamesPlayed == 0` → `matches_played / 2` | **Yes, as a rate denominator** |
| 3 | `espn.py:127-128` | clean-sheet rates → `0` | No — but disables a hard filter |
| 4 | `espn.py:142,157,162` | league average → `1.35` | **Yes, as the λ denominator (always)** |
| 5 | `main.py:171` | league average → `1.35` again | Yes |
| 6 | `analyze_all.py:182` | league average → `1.35` again | Yes |
| 7 | `main.py:104-106` | three filter flags → constants | No — disables three hard filters |
| 8 | `main_run3.py:166-167` | same halving as #2 | Yes |
| 9 | `main_run3.py:~200` | league average → `1.35` | Yes |
| 10 | `sofascore.py:134-145` | halves goals *and* clean sheets | Dead code |

Nothing downstream can distinguish any of these from real observations, because the pipeline carries
plain floats with no provenance, no confidence and no "unavailable" sentinel.

---

## 5. Historical / point-in-time data flow

**There is none.**

```
espn.get_team_stats(league_id, team_id)
        └── no date parameter
        └── no matchweek parameter
        └── no "as of" concept anywhere in the signature or the endpoint
```

`get_fixtures()` accepts a date; `get_team_stats()` does not. So a run for a past date pairs a
**historical fixture list** with **today's cumulative season statistics**.

```
   main.py 2025-09-15   ← historical fixtures (Sept 2025)
          +
   espn team stats      ← current totals (Aug 2026, includes 11 months of later matches)
          ↓
   λ computed from information that did not exist at kickoff
```

**LEAK-001.** Any backtest built on this flow is contaminated by construction. See
`REPO_AUDIT.md` §11.

**No persistence exists at any point:** no database, no snapshot table, no cached response on disk, no
results ingestion, no prediction history. The only artefacts are the per-date output files, which are
overwritten on re-run and `.gitignore`d.

---

## 6. Output schemas

| Writer | File | Schema |
|---|---|---|
| `output.write_csv` | `output_<date>.csv` | 13 columns, `extrasaction="ignore"` ⚠ silently drops drift |
| `output.write_json` | `output_<date>.json` | `{date, generated_at, total_fixtures, results[]}` |
| `analyze_all.py` inline | `analysis_<date>.json` | adds `classification`, `recommendation`, `market_type` |
| `main_run3.py` inline | `run3_output_<date>.json` | `{date, total, selections[], skipped[]}` |

Three different JSON shapes for the same conceptual object. No shared serialisation layer, no schema
version field, no model version field — so a stored prediction cannot be attributed to the code that
produced it. That is a prerequisite for the prediction/model version tracking the future architecture
calls for.
