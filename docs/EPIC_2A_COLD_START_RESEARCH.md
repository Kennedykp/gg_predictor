# Epic 2A — Cold-Start & Team-Strength Research

**Status:** Research and design only. No production code changed. No model implemented.
**Branch:** `epic/2a-cold-start-research`
**Date of live ESPN measurements:** 2026-08-09

Throughout this document three kinds of statement are labelled explicitly:

- **[PROJECT FACT]** — verified from this repository's source, or measured from ESPN by the research scripts in `research/`.
- **[EXTERNAL RESEARCH]** — established football-modelling literature and standard statistical method.
- **[RECOMMENDATION]** — my judgement, derived from the two above.

Anything not labelled is connective prose.

---

# Objective

Determine, with evidence, how the GG Predictor should estimate team attacking and defensive
strength when the current season contains little or no data — and to do so without inventing
a single production constant.

The deliverable is a decision-ready design, not an implementation. The success criterion is
that Epic 2D can be started by someone who was not present for this research, and that every
number they eventually ship can be traced to a validation run rather than to a guess.

---

# Current Problem

**[PROJECT FACT]** Epic 1B.5 made all five POISSON_V1 inputs point-in-time derived from
current-season match records, under a strict cutoff. The correctness win came with a direct
consequence: at the start of a season there is nothing to derive from.

`domain/match_records.py:243` is the rule that creates the problem:

```python
if record.kickoff is None or not record.kickoff < target_kickoff:
```

A record only counts if its kickoff strictly precedes the target kickoff. Combined with
current-season-only sourcing and venue purity, an opening fixture has an empty history on
both sides. There is no previous-season fallback anywhere in the codebase — a property Epic
1B.5 established deliberately, and which this Epic is the response to.

**[PROJECT FACT]** Measured, not assumed. `research/measure_cold_start.py` replays the
production cutoff semantics over ten clean Premier League seasons:

| Season | Fixtures | n=0 on ≥1 side | n=0 on BOTH sides | n<3 on either side |
|---|---|---|---|---|
| 2021 | 380 | 20 (5.3%) | 20 (5.3%) | 60 (15.8%) |
| 2022 | 380 | 20 (5.3%) | 20 (5.3%) | 60 (15.8%) |
| 2023 | 380 | 21 (5.5%) | 19 (5.0%) | 66 (17.4%) |
| 2024 | 380 | 20 (5.3%) | 20 (5.3%) | 60 (15.8%) |
| 2025 | 380 | 20 (5.3%) | 20 (5.3%) | 60 (15.8%) |

The 20 fully-cold fixtures per season are exactly the first two matchweeks. Because the model
is venue-specific, matchweek 1 leaves every home team with zero home matches and every away
team with zero away matches; matchweek 2 repeats this for the complementary set of teams. So
**the venue-specific cold start is two matchweeks deep, not one.**

The more important figure is the last column: **roughly one fixture in six is estimated from
fewer than three venue-matched observations.** Cold start is not a matchweek-1 edge case to be
special-cased. It is a continuous small-sample problem covering the opening ~15% of a season,
and — as the reliability measurements below show — the estimates never become fully trustworthy
even at n=19.

---

# Current POISSON_V1 Inputs

**[PROJECT FACT]** Confirmed by reading `poisson.py` and `domain/poisson_inputs.py`. Any future
cold-start work must produce exactly these five quantities, in these units, or it is changing
the model rather than feeding it.

| # | Input | Semantics | Venue-specific | Units |
|---|---|---|---|---|
| 1 | `league_avg_goals` | League average goals **per team per match** (divisor is `2 × fixtures`, not `fixtures`) | No | goals/team/match |
| 2 | `home_goals_scored_home` | Home team's mean goals scored, **home matches only** | Yes (HOME) | goals/match |
| 3 | `home_goals_conceded_home` | Home team's mean goals conceded, **home matches only** | Yes (HOME) | goals/match |
| 4 | `away_goals_scored_away` | Away team's mean goals scored, **away matches only** | Yes (AWAY) | goals/match |
| 5 | `away_goals_conceded_away` | Away team's mean goals conceded, **away matches only** | Yes (AWAY) | goals/match |

All five are **rates (per-match means), never totals.**

The core formula, `poisson.py`, marked `# Core formula - DO NOT MODIFY`:

```python
lambda_home = (home_goals_scored_home * away_goals_conceded_away) / league_avg_goals
lambda_away = (away_goals_scored_away * home_goals_conceded_home) / league_avg_goals

p_home_scores = 1 - math.exp(-lambda_home)
p_away_scores = 1 - math.exp(-lambda_away)
gg_probability = p_home_scores * p_away_scores
```

Guards: any input `None` or `< 0` returns `None`; `league_avg_goals == 0` returns `None`. A
`None` return means NO BET, not a default probability.

Two structural observations that constrain everything downstream:

1. **This is a ratio-form model, not a fitted Poisson regression.** `league_avg_goals` acts as a
   normaliser so that `scored × conceded / league_avg` has the units of goals. No parameters are
   estimated by likelihood. This is why a cold-start prior can be introduced *without touching
   `poisson.py`* — the prior's job is to produce better values for the same five slots.

2. **Independence is assumed twice.** `P(GG) = P(home scores) × P(away scores)` assumes the two
   scorelines are independent. They are not, in reality. This is precisely the deficiency
   Dixon-Coles addresses, and it is why the cold-start layer must be kept separate from the
   probability layer (see *Poisson vs Future Dixon-Coles*).

---

# Why Cold Start Exists

Four distinct causes, which are often conflated but need different treatment:

1. **Season boundary.** The current-season sample is empty at matchweek 1 by construction.
2. **Venue splitting.** A 38-match season yields ~19 home and ~19 away matches per team. Venue
   splitting halves an already-small sample.
3. **League composition change.** Promotion and relegation replace ~15% of a top-flight league
   every year. Promoted clubs have no top-flight history at all.
4. **Staleness.** Even a full previous season describes a squad that has since changed players
   and possibly managers.

Causes 1 and 2 are sample-size problems and are solved by shrinkage. Cause 3 is an
identification problem (the previous data is from a different competition). Cause 4 is a
*non-stationarity* problem and is the reason the prior must decay rather than persist.

---

# ESPN Historical Coverage Audit

## Method

**[PROJECT FACT]** `research/audit_espn_history.py`. Read-only, GET-only, one request per
(league, season), 1-second inter-request delay, hard cap of 400 requests, disk-cached so
re-analysis costs zero requests. It deliberately does **not** call
`espn.get_league_match_records()`, because that function answers the production question
("give me usable records") and discards exactly the defects an audit must surface. Instead it
counts the raw payload *and* runs the real adapter `espn.parse_scoreboard_events()` over the
same payload, so "raw events" and "what production would accept" are directly comparable.

Verification was by returned content — event IDs, per-event `season.year` and `season.slug`,
kickoff dates, status names, per-team match counts, distinct team IDs — never by HTTP status.

## Supported leagues discovered

**[PROJECT FACT]** From `config.py`:

```python
ALLOWED_LEAGUES = {"eng.1": "English Premier League", "ger.1": "Bundesliga",
                   "ita.1": "Serie A", "esp.1": "La Liga", "fra.1": "Ligue 1"}
PHASE_2_LEAGUES = {"eng.2": "EFL Championship", "ger.2": "Bundesliga 2"}
```

Seven league codes are configured. `PHASE_2_LEAGUES` is **not wired into production** (tracked
as GG-018). All seven were audited, because the second tiers are needed for promoted-team
research regardless of whether they are ever predicted on.

## A note on User-Agent, discovered during this audit

**[PROJECT FACT]** ESPN edge-filters on User-Agent. Measured against
`eng.1/scoreboard?dates=20250816`:

| User-Agent | Result |
|---|---|
| `requests` library default | HTTP 200, 53,764 bytes |
| `gg-predictor-research/1.0 (…)` | **HTTP 403**, 445 bytes |
| `curl/8.7.1` | HTTP 200, 53,764 bytes |
| Chrome-like browser string | **HTTP 403**, 445 bytes |

The research script therefore sends the same default UA production sends, rather than
identifying itself or impersonating a browser — impersonation was measurably *worse*.

**[RECOMMENDATION]** Record this as an operational risk: production `espn._fetch` relies on the
`requests` default User-Agent being accepted. A dependency bump that changes that default
string could turn every production ESPN call into a 403. Worth a monitoring note in
TECHNICAL_DEBT; it is not an Epic 2A change.

---

# Historical Coverage Table

**[PROJECT FACT]** Measured 2026-08-09. `Exp` = expected double-round-robin fixtures for the
league format in force that season. `Raw` = events returned. `Compl` = events ESPN marks
completed. `Valid` = MatchRecords the production adapter accepts. `Cov` = Valid / Exp.
`G/T/M` = goals per team per match among valid records.

| League | Season | Exp | Raw | Compl | Valid | Cov | Teams | G/T/M | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| eng.1 | 2014 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.283 | COMPLETE |
| eng.1 | 2015 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.350 | COMPLETE |
| eng.1 | 2016 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.400 | COMPLETE |
| eng.1 | 2017 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.339 | COMPLETE |
| eng.1 | 2018 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.411 | COMPLETE |
| eng.1 | 2019 | 380 | 314 | 314 | 314 | 82.6% | 20 | 1.338 | **SLICING DEFECT** |
| eng.1 | 2020 | 380 | 446 | 446 | 446 | 117.4% | 23 | 1.365 | **CONTAMINATED** |
| eng.1 | 2021 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.409 | COMPLETE |
| eng.1 | 2022 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.426 | COMPLETE |
| eng.1 | 2023 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.639 | COMPLETE |
| eng.1 | 2024 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.467 | COMPLETE |
| eng.1 | 2025 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.375 | COMPLETE |
| ger.1 | 2014–2025 | 306 | 306 | 306 | 306 | 100.0% | 18 | 1.377–1.618 | COMPLETE (all 12) |
| ita.1 | 2014 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.347 | COMPLETE |
| ita.1 | 2015 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.288 | COMPLETE |
| ita.1 | 2016 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.478 | COMPLETE |
| ita.1 | 2017 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.338 | COMPLETE |
| ita.1 | 2018 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.341 | COMPLETE |
| ita.1 | 2019 | 380 | 282 | 282 | 282 | 74.2% | 20 | 1.486 | **SLICING DEFECT** |
| ita.1 | 2020 | 380 | 478 | 478 | 478 | 125.8% | 23 | 1.544 | **CONTAMINATED** |
| ita.1 | 2021 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.433 | COMPLETE |
| ita.1 | 2022 | 380 | 381 | 381 | 381 | 100.3% | 20 | 1.283 | COMPLETE (+1 extra event) |
| ita.1 | 2023–2025 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.213–1.305 | COMPLETE |
| esp.1 | 2014–2018 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.293–1.471 | COMPLETE |
| esp.1 | 2019 | 380 | 323 | 323 | 323 | 85.0% | 20 | 1.259 | **SLICING DEFECT** |
| esp.1 | 2020 | 380 | 437 | 437 | 437 | 115.0% | 23 | 1.238 | **CONTAMINATED** |
| esp.1 | 2021 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.251 | COMPLETE |
| esp.1 | 2022 | 380 | 380 | 375 | 375 | 98.7% | 20 | 1.248 | NEAR-COMPLETE (5 unplayed) |
| esp.1 | 2023–2025 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.309–1.347 | COMPLETE |
| fra.1 | 2014–2015 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.246–1.263 | COMPLETE |
| fra.1 | 2016 | 380 | 382 | 381 | 381 | 100.3% | 21 | 1.304 | COMPLETE (+playoffs) |
| fra.1 | 2017 | 380 | 384 | 384 | 384 | 101.1% | 23 | 1.358 | COMPLETE (+playoffs) |
| fra.1 | 2018 | 380 | 384 | 384 | 384 | 101.1% | 23 | 1.280 | COMPLETE (+playoffs) |
| fra.1 | 2019 | 380 | 380 | 279 | 279 | 73.4% | 20 | 1.262 | **SEASON ABANDONED** |
| fra.1 | 2020–2022 | 380 | 380 | 380 | 380 | 100.0% | 20 | 1.380–1.404 | COMPLETE |
| fra.1 | 2023–2024 | 306 | 306 | 306 | 306 | 100.0% | 18 | 1.350–1.489 | COMPLETE (18-team era) |
| fra.1 | 2025 | 306 | 306 | 305 | 305 | 99.7% | 18 | 1.415 | NEAR-COMPLETE |
| eng.2 | 2014–2018 | 552 | 557–558 | 555–557 | 555–557 | ~100.8% | 24 | 1.211–1.335 | COMPLETE (+playoffs) |
| eng.2 | 2019 | 552 | 475 | 475 | 475 | 86.1% | 24 | 1.309 | **SLICING DEFECT** |
| eng.2 | 2020 | 552 | 639 | 639 | 639 | 115.8% | 30 | 1.182 | **CONTAMINATED** |
| eng.2 | 2021 | 552 | 557 | 557 | 557 | 100.9% | 24 | 1.252 | COMPLETE (+playoffs) |
| eng.2 | 2022 | 552 | 557 | 545 | 545 | 98.7% | 24 | 1.207 | NEAR-COMPLETE |
| eng.2 | 2023–2025 | 552 | 557 | 557 | 557 | 100.9% | 24 | 1.227–1.337 | COMPLETE (+playoffs) |
| ger.2 | 2014–2018 | 306 | 307–308 | 307–308 | 307–308 | ~100.5% | 19 | 1.242–1.481 | COMPLETE (+playoffs) |
| ger.2 | 2019–2022 | 306 | 306 | 306 | 306 | 100.0% | 18 | 1.440–1.484 | COMPLETE |
| ger.2 | 2023 | 306 | 126 | 126 | 126 | 41.2% | 18 | 1.599 | **UNUSABLE — GENUINE GAP** |
| ger.2 | 2024–2025 | 306 | 306 | 306 | 306 | 100.0% | 18 | 1.466–1.511 | COMPLETE |

## Depth probe: how far back does this go?

**[PROJECT FACT]** `research/investigate_anomalies.py --earliest`, seasons 2006–2013, all seven
leagues. Every league returned full seasons with complete scores and correct team counts:

| League | 2006 | 2007 | 2008 | 2009 | 2010 | 2011 | 2012 | 2013 |
|---|---|---|---|---|---|---|---|---|
| eng.1 | 390 | 389 | 380 | 737* | 380 | 380 | 380 | 380 |
| ger.1 | 311 | 309 | 308 | 308 | 308 | 308 | 306 | 306 |
| ita.1 | 406 | 381 | 380 | 380 | 381 | 380 | 380 | 380 |
| esp.1 | 380 | 381 | 380 | 380 | 380 | 380 | 380 | 380 |
| fra.1 | 384 | 382 | 380 | 380 | 380 | 380 | 380 | 380 |
| eng.2 | 567 | 569 | 567 | 577 | 570 | 564 | 559 | 561 |
| ger.2 | 319 | 323 | 306 | 306 | 306 | 306 | 306 | 306 |

In every case `scored == raw`, i.e. **every returned event carried a score**. Kickoff months
spanned August–May as expected.

\* eng.1 2009 returned 737 events — nearly double. This is a competition-contamination
signature and that single season must be excluded or re-derived before use. It is flagged, not
explained away.

**Earliest reliable season per league: 2006 for all seven leagues** (with eng.1 2009 excluded).
That is **twenty seasons** of match-level history, which is far more than needed to estimate a
small number of shrinkage parameters.

---

# Data Quality Risks

Every anomaly in the table above was root-caused rather than assumed. Five distinct classes:

### RISK-1 — Season-boundary slicing defect (most serious; ours, not ESPN's)

**[PROJECT FACT]** `espn._season_date_range(season)` returns `f"{season}0701-{season + 1}0630"` —
a fixed 1 July – 30 June window. When COVID pushed the 2019-20 season into July 2020, that
window cut the season in half.

Evidence: `eng.1` season=2019 returns 314 events, with kickoff months running
`2020-03: 12` then jumping to `2020-06: 26` — and stopping. The missing 66 fixtures appear in
the **2020** window, where per-event metadata is unambiguous:

```
per-event season.year: {2019: 66, 2020: 380}
per-event season.slug: {'2019-20-english-premier-league': 66,
                        '2020-21-english-premier-league': 380}
teams=23   matches-per-team: {6: 1, 7: 2, 38: 3, 44: 7, 45: 10}
teams NOT playing a full round robin: Watford 6, Bournemouth 7, Norwich 7, …
```

Watford, Bournemouth and Norwich were relegated *in* 2019-20. Their presence in the 2020-21
window, with 6–7 matches each, is conclusive: **the 2020-21 dataset is contaminated with 66
fixtures from the previous season, and the 2019-20 dataset is truncated by the same 66.**

Same signature in `ita.1`, `esp.1`, `eng.2` for the same two seasons.

**Impact today:** latent, not active. Production queries only the current season, and no recent
season has overrun into July. **Impact on this Epic's roadmap:** fatal if unaddressed — any
historical backtest that groups by date window will train on a contaminated 2020-21 and a
truncated 2019-20.

**[RECOMMENDATION]** Epic 2B must slice seasons on the event's own declared
`season.year` / `season.slug`, not on the date window. ESPN supplies this per event and it is
authoritative. **This is a production-code change to `espn.py` and is therefore explicitly
out of scope for Epic 2A** — flagged here, not made.

### RISK-2 — Playoff / non-league-fixture contamination

**[PROJECT FACT]** `eng.2` season=2021 returns 557 events:

```
per-event season.slug: {'regular-season': 552, 'promotion-semifinals': 4, 'promotion-final': 1}
status names: {'STATUS_FULL_TIME': 556, 'STATUS_FINAL_PEN': 1}
```

The production adapter accepts all 557. Event-level `league.slug` is `None` on the scoreboard
endpoint (`league slugs: {None: 557}`), so `parse_scoreboard_events` falls back to the
payload-level slug and cannot distinguish a playoff from a league match.

Same signature: `fra.1` 2016–2018 (382–384 events; relegation playoffs), `ger.2` 2014–2018
(307–308). Note `STATUS_FINAL_PEN` — a penalty shootout, whose "score" is not a normal-time
goal count and would corrupt a goals-per-match rate.

**Impact today:** none for the five production leagues in their current formats. Material for
`eng.2`/`ger.2`, which are exactly the leagues needed for promoted-team research.

**[RECOMMENDATION]** Filter on `season.slug == 'regular-season'` (or the league-specific
equivalent) when building the historical dataset, and exclude `STATUS_FINAL_PEN` outright.

### RISK-3 — Abandoned seasons

**[PROJECT FACT]** `fra.1` 2019: 380 events, `{'STATUS_FULL_TIME': 279, 'STATUS_CANCELED': 101}`.
Ligue 1 2019-20 was terminated in March 2020; 101 fixtures were never played. Production
correctly keeps only the 279 completed. The season is *genuinely* 73.4% complete — this is real
history, not a retrieval fault, and must not be "repaired".

### RISK-4 — Unplayed fixtures inside a finished season

**[PROJECT FACT]** `esp.1` 2022: 5 events still `STATUS_SCHEDULED` in a season long finished.
`fra.1` 2025 and `eng.2` 2022 show the same in smaller numbers. Production drops them. Harmless,
but it means **`Valid` must never be assumed equal to `Exp`**, and a coverage check should
tolerate small deficits without silently accepting large ones.

### RISK-5 — A genuine ESPN gap

**[PROJECT FACT]** `ger.2` 2023: only 126 of 306 fixtures, thinly and unevenly spread
(`2023-12: 1`, `2024-01: 1`, `2024-02: 1`), teams on 13–16 matches each. Not a slicing artifact —
the data simply is not there. This is the one league-season class in the audit that is
unusable, and it is why `ger.2` gets a weaker sufficiency verdict below.

### Clean bill of health on the rest

**[PROJECT FACT]** Across all 84 league-seasons audited for 2014–2025: **zero duplicate event
IDs, zero events missing a kickoff timestamp, zero completed events missing a score, zero
truncation at the 1000-event limit, and zero foreign-competition slugs.** The failure modes that
would have been hardest to work around simply are not present.

---

# ESPN Sufficiency Verdict

**Question:** can ESPN alone support cold-start research and validation?

| League | Usable seasons (2006–2025) | Verdict |
|---|---|---|
| eng.1 | 18 of 20 (excl. 2009 contamination; 2019/2020 recoverable via RISK-1 fix) | **SUFFICIENT** |
| ger.1 | 20 of 20 — cleanest league in the audit | **SUFFICIENT** |
| ita.1 | 19 of 20 (2019/2020 recoverable) | **SUFFICIENT** |
| esp.1 | 19 of 20 (2019/2020 recoverable) | **SUFFICIENT** |
| fra.1 | 19 of 20 (2019 genuinely partial; format change 2023) | **SUFFICIENT** |
| eng.2 | 18 of 20 (playoffs must be filtered) | **SUFFICIENT for promoted-team research** |
| ger.2 | 17 of 20 (2023 unusable) | **SUFFICIENT BUT DEGRADED** |

**[RECOMMENDATION] Verdict: ESPN is sufficient. Do not add a provider for this work.**

Justification against the Phase 4 criteria:

- **Number of usable seasons** — 17–20 per league. Estimating 2–4 shrinkage parameters from
  ~18 seasons × 380 fixtures is not sample-limited.
- **Completeness** — ~100% for the overwhelming majority of league-seasons.
- **Stable IDs** — verified; see next section.
- **Historical scores** — present on every completed event audited, back to 2006.
- **Kickoff accuracy** — ISO-8601 UTC timestamps present on every event.
- **Home/away identity** — preserved; the adapter already derives venue.
- **Competition identity** — usable via per-event `season.slug`, though **not** via
  `league.slug`, which is `None` on this endpoint. This is a real caveat and is why RISK-2
  needs an explicit filter.
- **Promoted teams** — second-tier coverage exists and IDs join across divisions.
- **Cross-season continuity** — verified.

The two things ESPN does **not** provide (historical odds, xG/lineups) are not needed for
Epic 2A, which is a model-estimation Epic. See *External Data Gaps*.

---

# Team-ID Continuity

**[PROJECT FACT]** `research/audit_espn_history.py --continuity`, 2014–2025:

| League | Distinct team IDs | IDs whose display name changed | Names mapping to >1 ID | IDs present in all 12 seasons |
|---|---|---|---|---|
| eng.1 | 35 | **0** | **0** | 9 |
| ger.1 | 30 | **0** | **0** | 9 |

**[PROJECT FACT]** Promotion continuity, `eng.2 → eng.1`, every transition 2014→2025:

| Transition | New clubs in top flight | Also found in 2nd tier by ID |
|---|---|---|
| 2014→2015 | 3 | 3 — ALL MATCHED |
| 2015→2016 | 3 | 3 — ALL MATCHED |
| 2016→2017 | 3 | 3 — ALL MATCHED |
| 2017→2018 | 3 | 3 — ALL MATCHED |
| 2018→2021 | 4 | 4 — ALL MATCHED |
| 2021→2022 | 3 | 3 — ALL MATCHED |
| 2022→2023 | 3 | 3 — ALL MATCHED |
| 2023→2024 | 3 | 3 — ALL MATCHED |
| 2024→2025 | 3 | 3 — ALL MATCHED |

**Zero unmatched newcomers across nine promotion cohorts.**

**Conclusion.** ESPN team IDs are stable across seasons *and* across the divisional boundary.
A cross-season, cross-division prior can be joined reliably on `team_id`.

**[RECOMMENDATION]** Join on `team_id` only. Never on team name. The audit found zero name
instability in this window, but a name-keyed prior would silently reset a club's entire history
the first year ESPN adjusted a display string, and that failure would be invisible — the model
would simply treat an established club as a newcomer.

---

# Cold-Start Methods Researched

Nine families, assessed against this project's actual constraints.

---

## A. Previous-season carryover

### Mathematical Idea
Use the team's previous-season venue rate directly until current-season data exists:
$\hat{\lambda} = \bar{x}_{\text{prev}}$, switching to $\bar{x}_{\text{cur}}$ at some threshold.

### n=0 Behavior
Returns the previous-season rate. Defined and stable.

### Small-Sample Behavior
Poor. The switch is a discontinuity: the estimate lurches when the threshold is crossed, and
a team's prediction can change materially because of one match.

### Large-Sample Behavior
Correct — converges to the current-season rate.

### Parameters
Switch threshold (matches). Optionally a staleness cutoff.

### Advantages
Trivial to implement and explain. Uses real data about the actual team.

### Risks
Ignores that the previous season is itself a noisy estimate. Undefined for promoted clubs.
Discontinuity is hard to defend and hard to calibrate.

### POISSON_V1 Compatibility
Perfect — same units, drop-in.

### Dixon-Coles Compatibility
Poor. DC estimates parameters by likelihood over a weighted match window; a hard switch has no
natural expression there.

---

## B. Fixed weighted previous/current blend

### Mathematical Idea
$\hat{\lambda} = w\,\bar{x}_{\text{prev}} + (1-w)\,\bar{x}_{\text{cur}}$ for fixed $w$.

### n=0 Behavior
Requires $w=1$ as a special case, or the current term is undefined. Usually special-cased.

### Small-Sample Behavior
Better than A — continuous if $w$ varies with $n$; but with fixed $w$ it under-weights current
data late in the season and over-weights it early. Fixed $w$ is exactly the wrong shape: the
appropriate weight on the prior *must* fall as $n$ grows.

### Large-Sample Behavior
**Wrong.** With fixed $w$ the previous season never stops contributing, so the estimate is
permanently biased toward last year even at $n=38$.

### Parameters
$w$.

### Advantages
Simple; continuous.

### Risks
This is the "70/30" family the Epic brief explicitly warns against. Its central flaw is not that
70/30 is the wrong number — it is that **a constant is the wrong functional form.**

### POISSON_V1 Compatibility
Perfect.

### Dixon-Coles Compatibility
Poor, same reason as A.

---

## C. Regression / shrinkage toward the league mean

### Mathematical Idea
**[EXTERNAL RESEARCH]** Stein-type shrinkage / empirical Bayes (James & Stein 1961; Efron &
Morris 1975; Morris 1983). Shrink a noisy individual estimate toward the group mean by an
amount determined by that estimate's *reliability*:

$$\hat{\lambda}_i = \bar{x}_{\text{league}} + r\,(\bar{x}_i - \bar{x}_{\text{league}}), \qquad r = \frac{n}{n+k}$$

$k$ is the shrinkage constant, expressible in units of matches. Crucially, $k$ is not a taste
parameter — it is estimable from the ratio of between-team variance to within-team variance:
$k = \sigma^2_{\text{within}} / \sigma^2_{\text{between}}$.

### n=0 Behavior
$r=0$ → returns the league mean exactly. Defined, no special case needed.

### Small-Sample Behavior
Exactly right in shape. At $n=1$, $r = 1/(1+k)$, so one match moves the estimate slightly.
The transition is smooth and monotone.

### Large-Sample Behavior
$r \to 1$ → converges to the observed rate. No permanent bias.

### Parameters
$k$ (one per quantity, or shared).

### Advantages
Minimises expected squared error under the hierarchical model. Single interpretable parameter
with a natural reading: "the prior is worth $k$ matches". Estimable from data.

### Risks
Shrinking to the *league* mean ignores that we often know something team-specific (last season).
That is what D and E fix. Also, a league mean is itself estimated and is noisy early.

### POISSON_V1 Compatibility
Perfect — outputs a rate in goals/match.

### Dixon-Coles Compatibility
Good. Equivalent to a ridge penalty on attack/defence parameters.

---

## D. Pseudo-observation / pseudo-match priors

### Mathematical Idea
**[EXTERNAL RESEARCH]** Conjugate Bayesian updating. For Poisson counts with a Gamma prior
$\text{Gamma}(\alpha,\beta)$, the posterior mean after observing $g$ goals in $n$ matches is

$$\hat{\lambda} = \frac{\alpha + g}{\beta + n}$$

Setting $\alpha = k\mu_{\text{prior}}$ and $\beta = k$ makes this exactly *"start the team with
$k$ matches' worth of prior evidence at rate $\mu_{\text{prior}}$"*:

$$\hat{\lambda} = \frac{k\,\mu_{\text{prior}} + g}{k + n}$$

Algebraically identical to C when $\mu_{\text{prior}}$ is the league mean, but the framing
generalises: $\mu_{\text{prior}}$ can be *any* pre-kickoff belief.

### n=0 Behavior
Returns $\mu_{\text{prior}}$ exactly.

### Small-Sample Behavior
Smooth, monotone, principled. Goals and matches accumulate additively.

### Large-Sample Behavior
Converges to the observed rate.

### Parameters
$k$ (pseudo-match count) and the construction of $\mu_{\text{prior}}$.

### Advantages
The Gamma-Poisson conjugacy is the *correct* model for goal counts, not an analogy. Extremely
interpretable. Composes cleanly: $\mu_{\text{prior}}$ can itself be a shrunk previous-season
estimate, which is precisely what the promoted-team and staleness problems need.

### Risks
Requires care to keep the *prior mean* on a comparable scale (a second-tier rate is not a
top-flight rate). Assumes the prior is exchangeable across teams unless deliberately made
team-specific.

### POISSON_V1 Compatibility
Perfect — a rate in goals/match, all five inputs computable this way.

### Dixon-Coles Compatibility
Excellent. A Gamma prior on attack/defence maps directly to a penalised likelihood.

---

## E. Bayesian / hierarchical team-strength models

### Mathematical Idea
**[EXTERNAL RESEARCH]** Baio & Blangiardo (2010), *Bayesian hierarchical model for the
prediction of football results*. Team attack and defence parameters are drawn from
league-level hyper-distributions; strength is estimated jointly with full posterior
uncertainty, typically by MCMC.

### n=0 Behavior
Excellent — falls back to the hyper-prior automatically, with honest uncertainty.

### Small-Sample Behavior
The best of any method here. Partial pooling is exactly the right answer, and uncertainty is
quantified rather than assumed.

### Large-Sample Behavior
Converges correctly, with credible intervals.

### Parameters
Hyper-priors; sampler configuration.

### Advantages
Statistically the most defensible. Naturally handles opponent adjustment, which raw venue rates
do not do at all.

### Risks
Heavy dependency (PyMC/Stan). Slow. Non-deterministic without seed control — a serious problem
for the golden-regression discipline this repo has built. **And it does not fit the current
architecture**: it estimates opponent-adjusted attack/defence parameters, which are *not* the
five raw venue rates POISSON_V1 consumes. Adopting it means replacing the model, not priming it.

### POISSON_V1 Compatibility
**Poor** — would require changing the meaning of the five inputs. That is a STOP condition for
this Epic.

### Dixon-Coles Compatibility
Excellent — it is a natural generalisation.

---

## F. Time-decayed historical observations

### Mathematical Idea
**[EXTERNAL RESEARCH]** Dixon & Coles (1997) weight each historical match by
$\phi(t) = \exp(-\xi\,\Delta t)$, where $\Delta t$ is time before the target kickoff. Estimates
become weighted means. Half-life $= \ln 2 / \xi$.

### n=0 Behavior
**Undefined.** With no observations, all weights are irrelevant — there is nothing to weight.
Decay alone does not solve cold start.

### Small-Sample Behavior
Helps with staleness, not with scarcity.

### Large-Sample Behavior
Converges to a recency-weighted rate — arguably better than an unweighted one, since team
strength is non-stationary.

### Parameters
$\xi$ (or half-life).

### Advantages
Directly addresses cause 4 (staleness). Crosses the season boundary continuously — no
discontinuity at all. Dixon-Coles' own mechanism, so it is the natural bridge to a future DC
implementation.

### Risks
Solves the wrong half of the problem on its own. Interacts awkwardly with venue splitting
(halving an already-decayed sample). Optimal $\xi$ is known to be sensitive to the fitting
window.

### POISSON_V1 Compatibility
Good — weighted means are still rates in goals/match.

### Dixon-Coles Compatibility
**Native.**

---

## G. Rolling-window approaches

### Mathematical Idea
Use the last $N$ matches only.

### n=0 / Small-Sample Behavior
Same failure as F — an empty window is still empty.

### Large-Sample Behavior
Never converges to a season rate; deliberately tracks recent form instead.

### Parameters
$N$.

### Advantages
Trivially simple; responsive.

### Risks
A hard window is a rectangular kernel — strictly worse than exponential decay for the same
effective sample size, and it discards a match entirely the moment it falls out of the window.

### POISSON_V1 / Dixon-Coles Compatibility
Compatible in units; conceptually inferior to F.

---

## H. Multi-season weighted history

### Mathematical Idea
Weighted mean across several previous seasons, weights decreasing with age. Effectively F
applied at season granularity.

### n=0 Behavior
Defined and considerably more stable than a single previous season — averaging two or three
seasons reduces prior variance.

### Small/Large-Sample Behavior
As F.

### Parameters
Number of seasons; per-season weights.

### Advantages
A better-estimated prior mean than one season alone. **[PROJECT FACT]** We have 17–20 seasons,
so this is affordable.

### Risks
Older seasons describe different squads. More parameters. Diminishing returns.

### POISSON_V1 / Dixon-Coles Compatibility
Good / Good.

---

## I. Combination approaches

### Mathematical Idea
Compose the above: a time-decayed, multi-season, league-adjusted prior mean, injected as
pseudo-matches, with the pseudo-match count set by measured reliability.

### n=0 Behavior
Returns the composed prior.

### Small-Sample Behavior
Smooth and principled throughout.

### Large-Sample Behavior
Converges to the observed (optionally decayed) rate.

### Parameters
$k$, decay/half-life, season weights, promotion adjustment — the union of its parts.

### Advantages
Addresses all four causes of cold start with one coherent mechanism.

### Risks
**Parameter proliferation is the main danger.** Every added parameter is another
overfitting opportunity, and they interact.

### POISSON_V1 / Dixon-Coles Compatibility
Perfect / Excellent.

---

# Venue-Specific Strength

**[PROJECT FACT]** POISSON_V1 is venue-split by construction: inputs 2–3 are the home team's
HOME rates, inputs 4–5 the away team's AWAY rates. A 38-match season gives ~19 observations per
venue-rate.

The critical question is not whether 19 is "enough" in the abstract, but how much *signal* those
19 matches carry. `research/measure_cold_start.py` measures this directly by split-half
reliability — correlating a team's odd-numbered venue matches against its even-numbered ones
within the same season:

**[PROJECT FACT]** eng.1, ten clean seasons:

| Season | Teams | r (goals for) | r (goals against) | Spearman-Brown corrected r(GF) |
|---|---|---|---|---|
| 2014 | 40 | 0.498 | 0.434 | 0.665 |
| 2015 | 40 | 0.373 | 0.399 | 0.543 |
| 2016 | 40 | 0.692 | 0.489 | 0.818 |
| 2017 | 40 | 0.676 | 0.213 | 0.807 |
| 2018 | 40 | 0.581 | 0.477 | 0.735 |
| 2021 | 40 | 0.550 | 0.465 | 0.709 |
| 2022 | 40 | 0.636 | 0.314 | 0.778 |
| 2023 | 40 | 0.569 | 0.495 | 0.725 |
| 2024 | 40 | 0.489 | 0.360 | 0.657 |
| 2025 | 40 | 0.471 | 0.236 | 0.641 |

Two findings with direct design consequences:

1. **Even a full-season venue rate is substantially noise.** Corrected reliability of ~0.64–0.82
   for goals-for means a "complete" 19-match venue sample still carries meaningful estimation
   error. Shrinkage is therefore justified *at every $n$*, not only at small $n$. This is a much
   stronger argument than the usual "small samples are noisy" hand-wave.

2. **Defence is markedly less reliable than attack** (r(GA) 0.21–0.50 vs r(GF) 0.37–0.69). Goals
   conceded is the noisier quantity — unsurprising, since it depends heavily on opponent quality,
   which raw venue rates do not adjust for.

**[RECOMMENDATION]** Because reliability differs systematically between goals-for and
goals-against, **the shrinkage constant should be allowed to differ between the attacking and
defensive inputs** — plausibly $k_{\text{GA}} > k_{\text{GF}}$, since lower reliability warrants
stronger shrinkage. Whether the extra parameter earns its keep is a validation question, and
the candidate design should be tested both ways (shared $k$ vs separate $k$).

**[RECOMMENDATION]** Shrink **raw venue rates**, not league-relative strengths or
attack/defence coefficients. Reasons, in order of importance:

1. Raw venue rates are exactly what POISSON_V1 consumes. Shrinking them changes no input
   semantics and requires no change to `poisson.py`. Any other representation would.
2. The Gamma-Poisson conjugacy applies directly to goal counts.
3. It stays interpretable: "this team's home scoring estimate is worth $k$ matches of prior".

The main cost is honest and should be recorded: **raw venue rates are not opponent-adjusted.** A
team that has played the three strongest defences will look weak. This is a pre-existing property
of POISSON_V1, not something cold-start work introduces — but it is the single strongest
argument for eventually moving to Dixon-Coles, and it caps how much a better prior can achieve.

**[RECOMMENDATION]** A secondary question worth testing in 2D: should venue-specific priors pool
across venues? A team with 0 home matches may have 2 away matches. A partially-pooled prior
(team overall rate → team venue rate) would use them. This is a *hierarchical* refinement and
should be evaluated only after the simpler version is working.

---

# League Baseline Across Seasons

**[PROJECT FACT]** `league_avg_goals` has the same cold-start problem, and it is arguably more
dangerous because it is a *divisor* in both lambda calculations.

Measured on eng.1, ten clean seasons:

| Season | Final | After 10 fixtures | After 30 | After 60 | \|prev season − final\| |
|---|---|---|---|---|---|
| 2014 | 1.283 | 1.300 | 1.350 | 1.408 | — |
| 2015 | 1.350 | 1.500 | 1.283 | 1.217 | 0.067 |
| 2016 | 1.400 | 1.400 | 1.200 | 1.467 | 0.050 |
| 2017 | 1.339 | 1.550 | 1.217 | 1.258 | 0.061 |
| 2018 | 1.411 | 1.250 | 1.483 | 1.450 | 0.071 |
| 2021 | 1.409 | 1.700 | 1.417 | 1.317 | 0.001 |
| 2022 | 1.426 | 1.400 | 1.467 | 1.458 | 0.017 |
| 2023 | 1.639 | 1.400 | 1.483 | 1.567 | 0.213 |
| 2024 | 1.467 | 1.050 | 1.383 | 1.417 | 0.172 |
| 2025 | 1.375 | 1.200 | 1.300 | 1.308 | 0.092 |

Across seasons: mean 1.410, sd 0.091, range 1.283–1.639.

Deriving mean absolute error of each estimator against the season's final value:

| Estimator of the current season's baseline | Mean absolute error |
|---|---|
| **Previous season's final baseline** | **≈ 0.083** |
| Current season after 10 fixtures | ≈ 0.169 |
| Current season after 30 fixtures | ≈ 0.089 |
| Current season after 60 fixtures | ≈ 0.076 |

**This is the clearest quantitative result in the audit.** The previous season's final league
baseline is about **twice as accurate** as the current season's first 10 fixtures, and remains
competitive until roughly 60 fixtures have been played. The early current-season baseline is
badly unstable — 2024 opened at 1.050 against a final of 1.467, a 30% error, and 2021 opened at
1.700 against a final of 1.409.

**[RECOMMENDATION]** Matchweek 1 should use the **previous season's final league baseline**, and
the current season should be blended in *slowly* — not switched to. On this evidence a
shrinkage/pseudo-match treatment of the baseline is justified on its own merits, independent of
the team-level prior. A multi-season baseline (family H) is worth testing too, since sd across
seasons is only 0.091 and the 2023 outlier suggests a single previous season can mislead.

**[RECOMMENDATION]** Team priors and league priors are related but genuinely different
statistical problems, and should get **separate parameters**:

- The league baseline aggregates ~380 fixtures, so it is well-estimated *in aggregate* but drifts
  between seasons (a real change in the competition).
- A team venue rate aggregates ~19 matches, so it is poorly estimated but the underlying quantity
  is comparatively stable.

Sharing one shrinkage constant between them would be a category error.

---

# Promoted Teams

**[PROJECT FACT]** Promoted clubs are ~15% of a top-flight league every season and have *zero*
top-flight history. Their previous season happened in a different competition, where the
prevailing scoring level is different.

Team-ID continuity across the divisional boundary is confirmed (see above): **9/9 cohorts, 100%
matched**. So the join is available. The question is what to do with the joined data.

**[PROJECT FACT]** Measured, `eng.2 → eng.1`:

| Transition | Clubs | 2nd-tier GF/match | Top-flight GF/match | Top-flight league mean | Ratio to own past |
|---|---|---|---|---|---|
| 2014→2015 | 3 | 2.002 | 1.088 | 1.350 | 0.543 |
| 2015→2016 | 3 | 1.475 | 0.904 | 1.400 | 0.613 |
| 2016→2017 | 3 | 1.540 | 0.886 | 1.339 | 0.575 |
| 2017→2018 | 3 | 1.652 | 1.009 | 1.411 | 0.611 |
| 2018→2021 | 4 | 1.731 | 1.086 | 1.409 | 0.627 |
| 2021→2022 | 3 | 1.828 | 1.140 | 1.426 | 0.624 |
| 2022→2023 | 3 | 1.560 | 1.123 | 1.639 | 0.720 |
| 2023→2024 | 3 | 1.931 | 0.833 | 1.467 | 0.432 |
| 2024→2025 | 3 | 1.617 | 1.132 | 1.375 | 0.700 |

Two effects, both large and both consistent in sign across all nine cohorts:

1. **A second-tier scoring rate overstates the subsequent top-flight rate by roughly 40%.** The
   ratio averages ≈ 0.605 and never exceeds 0.720. Using a Championship rate as if it were a
   Premier League rate would be a serious, systematic error.
2. **Promoted clubs also score below their new league's average**, at ≈ 0.72 of the
   destination-league mean on average. So regressing them to the destination-league mean is
   *also* biased — optimistically, in this case.

**[RECOMMENDATION]** Neither naive option is acceptable: carrying the second-tier rate over is
badly wrong, and shrinking to the destination-league mean is wrong in the opposite direction.
A promoted club needs its own prior mean, sitting below the destination-league mean.

**[RECOMMENDATION] Do not hardcode 0.605 or 0.72.** These come from nine cohorts of three clubs
in one country — roughly 28 clubs. They are strong enough to establish *that* the effect exists
and its direction, and far too thin to fix a production constant. Specifically:

- The per-cohort ratio ranges 0.432–0.720, so between-cohort variance is substantial.
- The measurement pools all venues; the venue-split version will be noisier still.
- It is measured only for `eng.2 → eng.1`. `ger.2 → ger.1` may differ, and Serie A / La Liga /
  Ligue 1 second tiers are not currently configured at all.

**[RECOMMENDATION]** What data would be required to estimate a promotion adjustment properly:

1. Second-tier coverage for every league where promoted clubs must be predicted. **[PROJECT FACT]**
   We have `eng.2` and `ger.2` only. `esp.2`, `ita.2`, `fra.2` are **not configured** — so for
   La Liga, Serie A and Ligue 1 a promoted club currently has *no* prior source whatsoever.
   Whether those ESPN league codes exist and are complete is an open question this Epic did not
   resolve.
2. ~20 seasons × 3 clubs per league — enough for a pooled, hierarchical estimate but not for
   per-league estimates without partial pooling.
3. Venue-split versions of the same measurement.

**[RECOMMENDATION]** Until that exists, the defensible interim treatment is: **treat promoted
clubs as maximally uncertain** — give them a prior mean at or below the destination-league mean
with a *large* pseudo-match count, so current-season evidence dominates quickly. And note the
option that costs nothing statistically: because `poisson.py` already returns `None` (NO BET)
when inputs are unavailable, **declining to predict on promoted clubs early in a season is a
legitimate, honest choice** and should be one of the arms evaluated in Epic 2D.

---

# Transfers and Manager Changes

**[PROJECT FACT]** No player, squad, transfer or managerial data exists anywhere in this
repository or in the ESPN endpoints currently used.

A previous-season prior cannot know about: major incoming/outgoing transfers, a new manager,
tactical change, injuries, squad turnover, or European qualification altering squad rotation.

The measured carryover correlations are the *net* evidence on this question:

**[PROJECT FACT]** eng.1, previous-season → current-season venue rate, teams surviving in the
league, pooled over nine transitions (n = 152 team-venue-seasons):

| Quantity | Pooled r |
|---|---|
| Goals for, home | **0.637** |
| Goals against, home | **0.440** |
| Goals for, away | **0.521** |
| Goals against, away | **0.421** |

Per-transition values range widely — r(GF home) from 0.366 to 0.881, and r(GA home) hit −0.020
in 2022→2023.

Interpretation, carefully:

- **A previous season does carry real signal.** r ≈ 0.42–0.64 is far from zero, so a
  previous-season prior is worth having. This validates the entire premise.
- **It is not strong signal.** r ≈ 0.5 means roughly 25% of variance explained. The previous
  season should inform the prior, not *be* the estimate.
- **Attack carries over better than defence** — consistent with the within-season reliability
  finding, and pointing the same way: defensive estimates deserve more shrinkage.
- **[PROJECT FACT] Survivorship caveat.** These correlations are computed only over teams present
  in both seasons. Relegated clubs drop out of the pair by construction, so this is a
  *conditional* correlation — an upper bound on what a naive carryover would achieve across the
  whole league, since the excluded clubs are exactly the ones whose strength changed most.

**[RECOMMENDATION]** These limitations justify **stronger shrinkage and a decaying prior**, not
player-level features. The unmodelled factors (transfers, managers) are precisely what pushes
r down from ~0.9 to ~0.5; the correct response to unmodelled variance is to weight the prior
less and let current-season evidence take over faster. No player data is needed to do that
properly.

**[RECOMMENDATION]** Explicit uncertainty is also a legitimate output. A Bayesian prior yields a
posterior *variance*, not just a mean. Since the decision layer already declines bets on missing
data, "estimate is too uncertain to bet" is expressible in the existing architecture without new
concepts.

---

# Current Form vs Underlying Strength

**[RECOMMENDATION]** "Last N matches" should eventually exist as a *separate concept* from
season-long strength, and must not be conflated with it.

The statistical tradeoff is the classic bias-variance one:

- **Short window** — responsive to genuine change (new manager, key injury), but high variance.
  At N=5 a venue-split rate rests on ~2–3 observations; single-match noise dominates.
- **Long window** — stable but slow. A team that changed materially in January is still described
  by its August self.

**[EXTERNAL RESEARCH]** The literature's answer is exponential time decay (Dixon & Coles 1997)
rather than a hard window: every match contributes, weighted by recency, which achieves
responsiveness without the discontinuity of a match dropping out of a window.

**[RECOMMENDATION]** Do not model "form" as a separate additive feature initially. Model it as
the *decay rate* on the same observations. This is one parameter instead of two concepts, it
composes with the recommended approach, and it is the native Dixon-Coles mechanism. If a
genuinely separate form signal is wanted later, it should be justified by showing it beats a
tuned decay rate — not assumed.

An important caution: goals scored in recent matches are *both* form and noise, and the two are
not separable from results alone. Reliability measurements above suggest that at small $n$ the
noise component dominates. Treating a hot streak as a strength change is a well-known way to
degrade a probabilistic model.

---

# Candidate Approaches

Reduced to three serious candidates. Each is expressed as the same pipeline shape, so they are
directly comparable and can share one evaluation harness:

```
Historical MatchRecords (point-in-time filtered)
        ↓
Prior estimate  (differs per candidate)
        ↓
Current-season observations (kickoff < target_kickoff)
        ↓
Updated team strength  (venue-specific rate, goals/match)
        ↓
the five existing POISSON_V1 inputs — unchanged in meaning
```

---

## Candidate 1 — League-mean shrinkage (family C/D)

Prior mean = current league venue mean (itself shrunk toward the previous season's baseline).

$$\hat{\lambda}_{\text{team,venue}} = \frac{k\,\mu_{\text{league,venue}} + g_{\text{observed}}}{k + n_{\text{observed}}}$$

- **Parameters:** $k$ (possibly separate for GF and GA); league-baseline blend.
- **Promoted teams:** handled automatically — they simply get the league mean. Biased optimistic
  by the measured ~0.72 factor.
- **Why it is a candidate:** the simplest thing that is statistically correct, and the natural
  baseline against which anything more complex must justify itself.

---

## Candidate 2 — Two-level prior: previous season → league mean, injected as pseudo-matches (family I, restrained)

The team's own previous-season venue rate is first shrunk toward the league mean, and the result
becomes the prior mean:

$$\mu_{\text{prior}} = \frac{k_{\text{prev}}\,\mu_{\text{league,venue}} + g_{\text{prev}}}{k_{\text{prev}} + n_{\text{prev}}}$$
$$\hat{\lambda} = \frac{k_{\text{cur}}\,\mu_{\text{prior}} + g_{\text{cur}}}{k_{\text{cur}} + n_{\text{cur}}}$$

- **Parameters:** $k_{\text{prev}}$, $k_{\text{cur}}$, plus a promoted-club rule.
- **Promoted teams:** need an explicit rule, since $g_{\text{prev}}$ is from another competition.
- **Why it is a candidate:** it uses the carryover signal we *measured* (r ≈ 0.42–0.64) instead of
  discarding it, while the two-stage shrinkage prevents the previous season from being trusted
  more than its reliability warrants. Both stages have the same conjugate form, so there is one
  mechanism to implement, test and explain.

---

## Candidate 3 — Time-decayed multi-season history with league-level offset (families F/H/I)

All historical matches, across seasons, weighted by $\exp(-\xi\,\Delta t)$, with an additive or
multiplicative offset per source competition so second-tier matches are on a comparable scale.

- **Parameters:** $\xi$ (half-life), competition offsets, plus a floor for teams with no history
  at all.
- **Promoted teams:** handled by the competition offset — the most principled treatment on offer.
- **Why it is a candidate:** no season-boundary discontinuity, handles staleness and scarcity in
  one mechanism, and is the native Dixon-Coles formulation, so 2F would inherit it directly.

---

**Candidates deliberately excluded:**

- **Fixed weighted blend (B)** — wrong functional form; the weight must depend on $n$.
- **Hard switch (A)** — discontinuous and undefined for promoted clubs.
- **Rolling window (G)** — strictly dominated by exponential decay.
- **Full Bayesian hierarchical (E)** — statistically the best, but it estimates opponent-adjusted
  parameters rather than the five raw venue rates POISSON_V1 consumes. Adopting it now would
  change input semantics — a STOP condition for this Epic. It belongs in the Dixon-Coles Epic.

---

# Recommended First Experimental Approach

**[RECOMMENDATION] Candidate 2 — a two-level conjugate (Gamma-Poisson) pseudo-match prior:
previous-season venue rate shrunk toward the league venue mean, then used as the prior mean for
current-season shrinkage — with the league baseline given its own separate shrinkage, and
promoted clubs given an explicit, separately-evaluated rule.**

Why this one, against the criteria in Phase 15:

**Available data.** It needs exactly what we verified we have: 17–20 seasons of complete
match-level history with stable team IDs. It needs nothing we lack. The two quantities it
depends on were *measured* here, not assumed: carryover signal exists (r ≈ 0.42–0.64) and venue
rates are unreliable enough to warrant shrinkage (corrected r ≈ 0.64–0.82 even at n≈19).

**Interpretability.** Every quantity has a plain-language reading: "this team starts the season
with $k$ matches' worth of prior evidence, and that prior is last season's rate pulled part of
the way to the league average". That is auditable by a human, which matters for a repo that has
invested heavily in explicit semantics and golden tests.

**Point-in-time requirements.** Both stages consume only completed matches with
`kickoff < target_kickoff`. The existing cutoff at `domain/match_records.py:243` is sufficient
unchanged. No new leakage surface is created.

**Sample sizes.** The conjugate form is *designed* for this regime. It degrades gracefully from
n=0 through n=19 with no thresholds, no special cases, and no discontinuities.

**Current POISSON_V1.** Decisive. It emits the same five quantities in the same units. `poisson.py`
does not change; `config.py` does not change; no threshold changes. The work lands entirely in
the derivation layer, exactly where Epic 1B.5 put the point-in-time logic.

**Future Dixon-Coles.** A Gamma prior on a Poisson rate maps directly onto a penalised likelihood
term in a DC fit. The prior is reusable rather than throwaway.

**Backtesting needs.** Deterministic and cheap — a closed-form arithmetic update, no sampler, no
convergence diagnostics, and reproducible bit-for-bit. Given the golden-regression culture here,
determinism is not a nice-to-have.

**Complexity and overfitting risk.** Two or three parameters, each with a natural interpretation
and a data-driven starting estimator. Compare Candidate 3, which needs a half-life *plus* a
per-competition offset *plus* a no-history floor.

**Strongest alternative: Candidate 3 (time-decayed multi-season history with competition
offsets).** It is more elegant — one mechanism for staleness, scarcity and the season boundary,
with no discontinuity anywhere — and it is what Dixon-Coles actually does, so it would transfer
directly to Epic 2F. It is not the recommendation *first* because it has more parameters,
because the competition offset it needs for promoted clubs is precisely the quantity we showed we
cannot yet estimate reliably, and because it still requires a fallback for teams with no history
at all. **[RECOMMENDATION]** It should be implemented as the second arm in Epic 2D and compared
head-to-head — not deferred indefinitely.

---

# Why No Production Parameter Is Chosen Yet

This document contains several numbers that would make tempting constants. None of them is one:

| Measured quantity | Value | Why it is NOT a production parameter |
|---|---|---|
| Split-half reliability, venue GF | r ≈ 0.37–0.69 | Descriptive. Implies a *starting estimator* for $k$, not a value. |
| Carryover, GF home | r ≈ 0.637 pooled | Survivorship-conditioned; varies 0.366–0.881 by season. |
| Promoted-club ratio to own past | ≈ 0.605 | ~28 clubs, one country, range 0.432–0.720. |
| Promoted-club ratio to league mean | ≈ 0.72 | Same limitation. |
| League baseline, eng.1 | mean 1.410, sd 0.091 | A measurement of a league, not a model constant. |

The reliability numbers *do* provide a principled starting point. Classical measurement theory
gives $k = n(1-r)/r$: at $n \approx 19$ with corrected $r \approx 0.7$, $k \approx 8$. That is a
**starting estimator to bracket a search around**, not a decision. Per Phase 7, candidate values
such as $k \in \{2, 4, 6, 8, 10, 15, 20\}$ should be evaluated, and the shipped value must come
from the validation protocol below.

---

# Parameter Estimation Plan

Parameters requiring future estimation:

| Parameter | Meaning | Candidate range to search | Initial estimator |
|---|---|---|---|
| $k_{\text{cur,GF}}$ | Pseudo-matches, current-season attack | 2–20 | $n(1-r)/r$ from split-half reliability |
| $k_{\text{cur,GA}}$ | Pseudo-matches, current-season defence | 2–30 | as above; expected larger (lower r) |
| $k_{\text{prev}}$ | Shrinkage of previous season toward league mean | 2–20 | from carryover r |
| $k_{\text{league}}$ | Shrinkage of current league baseline toward previous season | 10–200 fixtures | from the baseline-error table |
| Promotion rule | Prior mean for promoted clubs | discrete arms | measured ratios as a prior belief only |
| $\xi$ (Candidate 3) | Decay half-life | 6 months – 3 years | Dixon-Coles literature values |

**[RECOMMENDATION]** Estimation procedure:

1. **Derive starting values analytically** from variance decomposition, as above. This is not
   fitting; it is a principled bracket that prevents an unnecessarily wide search.
2. **Grid-search on development seasons only**, optimising a proper scoring rule (log loss or
   Brier), never accuracy.
3. **Confirm the optimum is a plateau, not a spike.** A sharp optimum in one parameter is
   evidence of overfitting; a broad plateau is evidence of a real effect. Report the curve, not
   just the argmin.
4. **Prefer the simpler model on ties.** If separate $k_{\text{GF}}$/$k_{\text{GA}}$ does not beat
   a shared $k$ by a margin exceeding cross-season variability, ship the shared $k$.
5. **Freeze, then test once.**

---

# Chronological Validation Strategy

**[PROJECT FACT]** We have ~18 usable seasons per league across 5–7 leagues. That is enough to
afford a genuinely held-out test set.

**[RECOMMENDATION] Rolling-origin (walk-forward) validation on development seasons, plus a single
untouched final test season.**

```
2006 ────────────────────────────────────── 2022 │ 2023  2024 │ 2025
        DEVELOPMENT (rolling origin)              │ VALIDATION  │ TEST
                                                  │             │ (touch once)
```

- **Development (2006–2022).** Rolling origin: fit the prior using only data before season S,
  evaluate on season S, advance. This mirrors production exactly — at every point the model sees
  only the past.
- **Validation (2023–2024).** Model *selection* between candidates and parameter settings.
- **Test (2025).** Touched **once**, after everything is frozen. If the result disappoints, the
  honest action is to report it, not to return to the test season.

**[RECOMMENDATION]** Why rolling-origin rather than a simple three-way split: it produces many
more evaluation points from the same data, and it directly measures *stability across seasons*,
which is the thing we actually care about. A parameter that wins in 2015 and loses in 2019 is not
a parameter we should ship, and a single split cannot reveal that.

**[RECOMMENDATION]** Additional discipline specific to this project:

- **Evaluate by matchweek bucket, not just in aggregate.** Cold start affects ~5% of fixtures at
  n=0 and ~16% at n<3. A change that fixes matchweek 1 but slightly harms matchweek 30 could look
  like a wash in aggregate while being exactly the wrong trade. Report MW1–2, MW3–6, MW7+
  separately.
- **Report per-league, not just pooled.** Five leagues are five semi-independent replications;
  agreement between them is real evidence, and disagreement is a warning.
- **Exclude the defective seasons** (2019/2020 pre-fix, eng.1 2009, ger.2 2023) explicitly and
  visibly, rather than letting them silently distort results.

---

# Leakage Controls

**[PROJECT FACT]** The invariant that must hold for every historical prediction:

```
information_timestamp < target_kickoff
```

Previous-season data satisfies this trivially — it existed before kickoff. The risks are subtler.
Leakage paths identified in cold-start research specifically:

**LEAK-2A-1 — Season-label leakage (the serious one).**
**[PROJECT FACT]** RISK-1 showed a "2019-20" fixture can have a July 2020 kickoff. If a prior is
built from "all of season S−1" *by season label*, it may include matches played later than some
matches in season S. **Control:** the cutoff must always be enforced on `kickoff`, never on the
season label. The season label is a grouping convenience only. The existing rule at
`domain/match_records.py:243` is correct and must remain the sole gate.

**LEAK-2A-2 — Full-season aggregates as priors.** Computing "last season's rate" from a final
end-of-season table is safe for a *new* season, but the same code path used mid-season would
leak. **Control:** one derivation function, always parameterised by `target_kickoff`; never a
separate "season summary" path.

**LEAK-2A-3 — Parameter leakage.** Fitting $k$ on all seasons and evaluating on the same seasons
is training on the test set. This is the most likely way this project would fool itself.
**Control:** the chronological protocol above; test season touched once.

**LEAK-2A-4 — Promotion-status leakage.** Whether a club was promoted is known before the season
starts — safe. But *final league position*, *relegation outcome*, or "did this promoted club
survive" are not. **Control:** permit only facts established before the first kickoff of the
season.

**LEAK-2A-5 — League-composition leakage.** Deriving "which teams are in this league" from a
complete season's fixture list is safe (fixture lists are published pre-season), but deriving it
from *completed* fixtures is not. **Control:** derive from schedule, not from results.

**LEAK-2A-6 — Revised historical data.** ESPN serves *today's* view of a 2015 match. Corrections
and ID reassignments since then are invisible to us. **Control:** snapshot with a retrieval
timestamp (see below). This is a known and accepted limitation, not a solvable one.

**LEAK-2A-7 — Odds leakage.** **[PROJECT FACT]** LEAK-001 remains open: point-in-time historical
odds are unresolved. **Control:** Epic 2A and its successors evaluate *model* performance only.
No odds in historical evaluation. See the next section.

---

# Evaluation Metrics

**[RECOMMENDATION]** We are predicting P(BTTS), a probability — so evaluation must use proper
scoring rules. A proper scoring rule is one that is optimised only by reporting one's true
belief; accuracy is not one.

**Primary:**

- **Brier score** — mean squared error of the probability, $\frac{1}{N}\sum (p_i - y_i)^2$.
  Decomposable into reliability, resolution and uncertainty, which is exactly the diagnostic
  breakdown this project needs.
- **Log loss** — $-\frac{1}{N}\sum [y\log p + (1-y)\log(1-p)]$. Punishes confident errors harshly.
  Report alongside Brier; they disagree in informative ways.
- **Brier Skill Score vs climatology** — improvement over always predicting the base rate. This
  is the honest headline number: it answers "does the model know anything beyond the base rate?"

**Calibration:**

- **Reliability diagram** with 10 bins, plus counts per bin.
- **Expected Calibration Error.**
- Calibration matters more than usual here: the eventual decision layer compares probability to
  odds, so a miscalibrated 0.70 is directly and financially wrong in a way that a
  correctly-ranked-but-miscalibrated probability is not.

**Discrimination:**

- **ROC-AUC** — measures ranking only, invariant to monotone recalibration. Useful precisely
  *because* it separates "does the model rank fixtures correctly" from "are the numbers right".
  A model can have good AUC and terrible calibration; the fix for that is recalibration, not a
  new model, and only these two metrics together reveal it.

**Operational:**

- **Coverage / selection rate** — what fraction of fixtures yield a prediction at all. Critical
  here, because `poisson.py` returns `None` on missing inputs. A cold-start prior will *increase*
  coverage, and a fair comparison must account for that: a method that predicts more fixtures with
  slightly worse average score may still be the better one, or may not.
- **Accuracy / hit rate** — reported for continuity with existing project reporting, and
  always alongside the base rate.

### Why "80% accuracy" alone is misleading

**[PROJECT FACT]** eng.1 averages ~1.41 goals per team per match. Under the model's own Poisson
assumption that implies P(a given team scores) ≈ $1 - e^{-1.41}$ ≈ 0.76, and hence a BTTS base
rate in the neighbourhood of 0.55–0.60. (Stated as an implication of the measured scoring rate,
not as a directly measured BTTS frequency — that measurement belongs in Epic 2C.)

Consequences:

1. **A model that always predicts YES scores ~57% accuracy while containing zero information.**
   Accuracy is measured against a baseline that is already high.
2. **Accuracy discards the probability.** A fixture predicted at 0.51 and one at 0.95 are treated
   identically once thresholded — yet the difference between them is the entire product.
3. **Accuracy is not a proper scoring rule.** It can be improved by *deliberately* misreporting
   one's belief, pushing probabilities toward 0 or 1. Optimising it trains the model to lie.
4. **Selection interacts with it.** Filters change the evaluated subset, so accuracy can rise
   purely because easier fixtures were selected. Coverage must always be reported with it.

**[RECOMMENDATION]** Headline the Brier Skill Score against the base rate, with a calibration
plot beside it. Accuracy may appear, never alone.

---

# Model Performance vs Betting Performance

**[RECOMMENDATION]** These must remain strictly separated, and Epic 2A concerns only the first.

```
MODEL PERFORMANCE                 BETTING PERFORMANCE
P(BTTS)                           probability + odds + edge + selection rules
Brier, log loss, calibration      ROI, yield, drawdown, Kelly growth
answerable with ESPN data         requires point-in-time historical odds
```

**[PROJECT FACT]** LEAK-001 is open: point-in-time historical odds are unresolved. Today's odds
for a 2015 fixture are (a) unavailable and (b) would be catastrophically leaky if approximated
— closing odds encode the result-adjacent information the market accumulated.

Therefore, for Epic 2A and its immediate successors:

- **Do not** use odds in historical evaluation.
- **Do not** compute or claim ROI, yield or profitability.
- **Do not** tune the model against betting outcomes.
- `EDGE_THRESHOLD = 0.05` and `MIN_ODDS = 1.60` are untouched and out of scope.

A better-calibrated probability is a prerequisite for a profitable strategy, not evidence of one.
Conflating them is how backtests come to promise returns they cannot deliver.

---

# Historical Dataset Requirements

**[RECOMMENDATION]** Raw layer — one row per fixture, provider-neutral, mirroring the existing
`MatchRecord` contract so the provider adapter remains the only ESPN-aware component:

| Field | Type | Notes |
|---|---|---|
| `event_id` | str | ESPN event id; primary key. Zero duplicates found in the audit. |
| `league` | str | e.g. `eng.1` |
| `season` | int | **From the event's own `season.year`**, not the request window (RISK-1) |
| `season_slug` | str | e.g. `regular-season`; needed to exclude playoffs (RISK-2) |
| `kickoff` | datetime (tz-aware, UTC) | The point-in-time gate. Non-null required. |
| `home_team_id` | str | Stable across seasons and divisions (verified) |
| `away_team_id` | str | |
| `home_team_name` | str | Diagnostics only — never a join key |
| `away_team_name` | str | |
| `home_goals` | int \| null | Null iff not completed |
| `away_goals` | int \| null | |
| `status` | str | `STATUS_FULL_TIME`, `STATUS_CANCELED`, `STATUS_SCHEDULED`, `STATUS_FINAL_PEN`… |
| `completed` | bool | ESPN's own flag |
| `retrieved_at` | datetime | Provenance (RISK / LEAK-2A-6) |
| `source_snapshot_id` | str | Links the row to the snapshot it came from |

**[RECOMMENDATION] Derived point-in-time features should be reproducibly generated, never
stored.** Reasons, in priority order:

1. **Leakage safety.** A stored feature is a feature whose cutoff can no longer be audited. A
   generated one carries its `target_kickoff` in the call. Epic 1B.5's guarantee lives in the
   derivation function; persisting its outputs would move the guarantee somewhere it cannot be
   checked.
2. **Parameter sweeps.** Epic 2D will evaluate many values of $k$. Storing derived features would
   require regenerating the entire store per candidate.
3. **Cost.** ~56,000 fixtures total. Derivation is arithmetic over a filtered list; there is no
   performance argument for persisting it.

Caching within a single experiment run is fine. Persisting derived features to disk as a
dataset is not.

---

# Storage Recommendation

**[PROJECT FACT]** Scale: 7 leagues × ~20 seasons × ~400 fixtures ≈ **56,000 rows**. This is
small. So the decision is *not* about performance.

| Option | Assessment |
|---|---|
| JSON | Best for **raw snapshots** — it is what the API returns, so it preserves everything including fields we have not yet decided we need. Poor for analysis. |
| CSV | Rejected for the normalised layer. **CSV cannot distinguish `None` from `0` or `""`** — which would silently destroy the missing-data semantics Epic 1B.1 was built to establish. That is a project-specific disqualifier, not a general one. |
| SQLite | Good; single file, ubiquitous, transactional. Weaker typing (no native tz-aware datetime), more ceremony for column-oriented analysis. |
| **Parquet** | **Recommended for the normalised layer.** Preserves nullable integers and timezone-aware timestamps natively — exactly the two things at risk. Columnar, compressed, immutable, and read directly by pandas/polars/DuckDB with no import step. |
| DuckDB | **Recommended as the query engine, not the storage format.** Reads Parquet in place with zero ETL, gives full SQL for exploratory work. |

**[RECOMMENDATION]** Two layers:

```
research/data/raw/{league}/{season}.json.gz     ← verbatim ESPN payload + provenance
research/data/matches/{league}/{season}.parquet ← normalised MatchRecord rows
```

Query with DuckDB over the Parquet layer. Keep raw payloads so the normalisation can be
re-derived if the adapter is fixed (as RISK-1 and RISK-2 will require) **without re-fetching from
ESPN** — this is what makes the RISK-1/RISK-2 corrections cheap and non-destructive.

**Versioning:** Parquet and gzipped JSON are binary and do not belong in git. Commit the
*manifest* (hashes, counts, timestamps); store the payloads outside version control, or in
Git LFS if they must be tracked. `research/.cache/` is already gitignored.

---

# Dataset Snapshot / Reproducibility

**Goal:** be able to state *"model experiment X used dataset snapshot Y"* and have that mean
something a year from now.

**[RECOMMENDATION]**

1. **Raw snapshots.** Persist verbatim ESPN payloads. **[PROJECT FACT]** The audit fetcher already
   records `retrieved_at`, `url` and `params` alongside each cached payload — the mechanism exists
   and only needs formalising.
2. **Normalised snapshots.** Parquet, regenerable from raw by a versioned adapter. Record the
   adapter version, since RISK-1/RISK-2 fixes will change normalisation output from identical raw
   input — and that difference must be attributable.
3. **Dataset hash.** SHA-256 per raw payload plus a manifest hash over the sorted set. One
   fingerprint identifies the whole dataset.
4. **Manifest.** `snapshot_id`, creation timestamp, leagues, seasons, per-league-season row
   counts, per-file hashes, adapter version, and the known-exclusions list (eng.1 2009, ger.2
   2023, pre-fix 2019/2020).
5. **Experiment records reference `snapshot_id`.** Every result carries the fingerprint of the
   data it came from.

**[RECOMMENDATION]** Deliberately *not* doing: no snapshot diffing UI, no automated drift
detection, no dataset registry service. A manifest file with hashes achieves the stated goal.
Per Phase 23, this should not be overengineered.

**Honest limitation.** A snapshot fixes what *we* saw, when we saw it. It cannot recover what
ESPN served in 2015. LEAK-2A-6 is mitigated, not eliminated.

---

# ESPN Request-Cost Estimate

**[PROJECT FACT]** Measured during this audit. The league scoreboard endpoint with a season-wide
date window and `limit=1000` returns an entire season in **one request**:

| Scope | Requests | Evidence |
|---|---|---|
| One league-season | **1** | e.g. eng.1 2025 → 380 events in one call |
| One league, 20 seasons | **20** | |
| 5 production leagues, 20 seasons | **100** | |
| All 7 configured leagues, 20 seasons | **140** | |
| *Actually issued by this Epic* | **140** | 84 (2014–2025 × 7) + 56 (2006–2013 × 7) |

**[RECOMMENDATION]** League scoreboards are decisively more efficient than team schedules. A
team-schedule approach would need ~20 requests per league-season (one per team) — **20× more
traffic for the same data**, with the added burden of deduplicating each fixture seen from both
sides. This confirms and extends the Epic 1B.5 finding.

At 1 request/second with caching, the entire historical dataset is ~140 requests and about two
and a half minutes, **once**. Re-analysis costs zero. This is a negligible load on ESPN and no
rate limiting was encountered.

**[RECOMMENDATION]** Retain in Epic 2B: bounded request caps, a fixed inter-request delay,
mandatory on-disk caching, and read-only GET. Fetch once into a snapshot, then never hit the
network during experimentation.

---

# Future xG / Player Data

**[RECOMMENDATION]** What match results cannot capture: finishing luck vs chance creation (a team
that created 2.5 xG and scored 0 is not a bad attacking team, but goals-based rates say it is),
personnel availability, fixture congestion, and in-match state effects.

Ranked by likely research value for BTTS specifically, with the reconstruction requirement
applied to each:

| Rank | Feature | Value for BTTS | Historical reconstructability |
|---|---|---|---|
| 1 | **xG / xGA** | Highest. Better predictor of future goals than past goals; directly attacks the reliability ceiling measured above (r(GA) ≈ 0.21–0.50). | Post-match value is a match *outcome*, so it is safe to use as history. **Not available from ESPN's current endpoints.** |
| 2 | **Shots / shots on target** | High. A cruder proxy for the same signal, but far more widely available. | Same status as xG. |
| 3 | **Starting XI** | Medium-high. A missing first-choice striker materially changes P(score). | **DANGEROUS.** Lineups are published ~1h before kickoff. Reconstructing *what was known* at an arbitrary earlier decision time is very difficult. |
| 4 | **Injuries / suspensions / availability** | Medium. | **DANGEROUS.** Availability is a continuously revised state; providers expose current state, not historical state. |
| 5 | **Rest days** | Low-medium. Computable from the fixture list we already hold. | **Fully reconstructable — cheapest to add.** |
| 6 | **Managerial changes** | Low-medium. Explains part of the carryover decay measured above. | Appointment dates are historically documented; reconstructable with effort. |

**[RECOMMENDATION]** The stated danger criterion deserves emphasis, because it is the one most
likely to be violated in good faith:

> A feature is dangerous if we can obtain it today but cannot reconstruct what was known before
> historical kickoff.

xG passes: it is computed from what happened in the match, so using *past matches'* xG as history
is exactly as safe as using past goals. Lineups and injuries fail: today's API returns the final
confirmed state, and a backtest using it would silently know things the model could not have
known — producing an excellent, unachievable backtest.

**Nothing here is to be implemented.** Rest days are the only item obtainable from data already in
hand, and even that belongs to a later Epic.

---

# External Data Gaps

Gaps only. **No provider is integrated, evaluated or revived in this Epic**, and the dead provider
modules (`api_football.py`, `sofascore.py`, `sportmonks.py`) were deliberately left untouched.

| Gap | Needed for | Status |
|---|---|---|
| **Historical point-in-time odds** | Betting evaluation, edge, ROI | **[PROJECT FACT]** LEAK-001, open. Blocks all profitability claims. Not needed for 2A–2E. |
| **Historical xG / shots** | Highest-value future model feature | Not available from ESPN endpoints in use. |
| **Historical lineups / availability** | Team-strength adjustment | Not available; and reconstructability is doubtful even elsewhere. |
| **Second-tier coverage for esp/ita/fra** | Promoted-club priors outside England and Germany | **Not configured.** Whether `esp.2` / `ita.2` / `fra.2` exist and are complete on ESPN is an **open question this Epic did not resolve.** |
| **Competition-format metadata** | Two GG.md filters that cannot fire | **[PROJECT FACT]** GG-002-B, open. |
| **ESPN current-season aggregate endpoint** | — | **[PROJECT FACT]** GG-024, open. Unaffected by this Epic; the match-level path is used throughout. |

**[RECOMMENDATION]** Provider selection belongs in a dedicated Epic *after* requirements are
known. The gap most likely to justify one is historical xG — but only after Epic 2C establishes
what the goals-only model actually achieves. Adding a data source before knowing the baseline
would make it impossible to attribute any improvement.

---

# Proposed Experimental Roadmap

**[RECOMMENDATION]** The audit changed the natural ordering: two data-correctness defects
(RISK-1, RISK-2) must be fixed before any historical evaluation, or every downstream result is
computed on contaminated data.

### Epic 2B — Historical dataset, snapshot infrastructure, and season-slicing correction
- **Fix RISK-1**: slice seasons on per-event `season.year`/`season.slug`, not the date window.
  *This is a production change to `espn.py` and needs its own regression tests.*
- **Fix RISK-2**: exclude playoff fixtures and `STATUS_FINAL_PEN`.
- Build the raw + Parquet snapshot layers, manifest and hashing.
- Re-run the coverage audit against the corrected pipeline; confirm 2019/2020 resolve to 380.
- **Exit criteria:** every league-season either passes its coverage check or is explicitly listed
  as a known exclusion.

### Epic 2C — Baseline POISSON_V1 historical evaluation
- Evaluate the model *as it exists today*, point-in-time, over the development seasons.
- Establish Brier, log loss, calibration, AUC, coverage — overall, per league, and per matchweek
  bucket.
- Measure the actual BTTS base rate (this document only implies it).
- **Exit criteria:** a reproducible baseline number. Without this, no later improvement can be
  claimed. **No model changes in this Epic.**

### Epic 2D — Cold-start candidate implementation and evaluation
- Implement Candidates 1, 2 and 3 behind the derivation layer.
- Rolling-origin parameter search on development seasons; select on validation seasons.
- Include a "decline to predict" arm for promoted clubs; report the coverage trade explicitly.
- **Exit criteria:** one approach with validated parameters, or a documented finding that none
  beats the 2C baseline — which is a legitimate outcome and must be reportable as such.

### Epic 2E — Calibration
- Reliability diagrams; assess whether a recalibration layer (Platt / isotonic) is warranted.
- Only meaningful once 2D fixes the input estimates.

### Epic 2F — Dixon-Coles
- Implement DC on the *same* snapshot, the *same* point-in-time rules and the *same* priors where
  conceptually appropriate, so the comparison against POISSON_V1 is fair.
- Addresses the low-scoring-scoreline dependence that POISSON_V1's independence assumption
  ignores.

### Later, and only if 2C/2D justify it
- Historical odds resolution (LEAK-001) → betting evaluation.
- xG provider gap analysis.

---

# Poisson vs Future Dixon-Coles

**[RECOMMENDATION]** The architecture that keeps the eventual comparison fair:

```
                    ONE snapshot (Epic 2B)
                            ↓
                    MatchRecord (provider-neutral)
                            ↓
                    Point-in-time filter (kickoff < target_kickoff)
                            ↓
        ┌───────────────────┴───────────────────┐
        ↓                                       ↓
  Cold-start / strength layer            Cold-start / strength layer
  (venue rates + priors)                 (attack/defence + same priors)
        ↓                                       ↓
     POISSON_V1                            DIXON-COLES
        └───────────────────┬───────────────────┘
                            ↓
                 Same metrics, same seasons
```

The recommended approach supports both because a Gamma prior on a Poisson rate is exactly a
penalised-likelihood term in a DC fit — the same prior belief, expressed in the form each model
needs. Concretely:

- **Shared:** dataset, snapshot, point-in-time rules, prior *concept*, evaluation metrics,
  train/validation/test season split.
- **Differs:** POISSON_V1 consumes raw venue rates and assumes independence; DC estimates
  opponent-adjusted attack/defence parameters with a low-score dependence correction $\tau$ and
  native time decay.

**One caveat recorded honestly.** POISSON_V1's inputs are *not* opponent-adjusted, while DC's
are. A prior built for raw venue rates is not literally the same object as a prior on DC attack
parameters. The *belief* transfers; the parameterisation does not. Epic 2F must re-estimate its
prior strength in DC's parameterisation rather than copying $k$ across — copying the number would
be a category error of exactly the kind this document is trying to prevent.

---

# Open Questions

1. **RISK-1 fix requires a production change.** Correct season slicing means changing
   `espn._season_date_range` usage in `espn.py`. Out of scope here by the Epic's own rules.
   **Needs approval before Epic 2B.**
2. **Are `esp.2`, `ita.2`, `fra.2` available on ESPN?** Without them, promoted clubs in Spain,
   Italy and France have no prior source at all. **Not resolved by this Epic.**
3. **eng.1 2009 returned 737 events.** Flagged, not root-caused. Excluded for now; Epic 2B should
   diagnose or permanently exclude it.
4. **Should promoted clubs be predicted at all early in a season?** "Decline to predict" is
   already expressible via the existing `None` → NO BET path and may outperform any prior.
   An empirical question for 2D.
5. **Should the prior pool across venues?** A team with 0 home matches may have 2 away matches.
   Hierarchical partial pooling could use them. Test after the simpler version works.
6. **What is the actual BTTS base rate per league?** This document *implies* ~0.55–0.60 from the
   measured scoring rate but does not measure it. Epic 2C must.
7. **Does `ger.2` 2023's gap extend to other unaudited leagues/seasons?** Only 2006–2025 for seven
   leagues was audited.
8. **Is the ESPN User-Agent dependency a production risk worth tracking?** The 403 finding
   suggests yes.

---

# Recommended Next Epic

**[RECOMMENDATION] Epic 2B — Historical dataset, snapshot infrastructure, and season-slicing
correction.**

It is first because everything else depends on it and because it is the only Epic that fixes a
*correctness* defect rather than adding capability. Running Epic 2C or 2D on the current
pipeline would compute a baseline against a contaminated 2020-21 and a truncated 2019-20 — and
those results would be wrong in a way that is invisible from the output.

Epic 2B should begin with an explicit decision on **Open Question 1**, since it requires the
first production change since this research began.

---

# Summary of Definition-of-Done

| # | Requirement | Status |
|---|---|---|
| 1 | Historical ESPN coverage empirically investigated | ✅ 140 league-seasons, 2006–2025 |
| 2 | Coverage depth documented per league | ✅ |
| 3 | ESPN sufficiency assessed | ✅ Sufficient; no new provider |
| 4 | Team-ID continuity investigated | ✅ 0 renames, 0 collisions, 9/9 promotion cohorts matched |
| 5 | Cold-start approaches researched | ✅ 9 families (A–I) |
| 6 | No arbitrary production weight selected | ✅ None chosen |
| 7 | Promoted-team handling researched | ✅ Measured, not assumed |
| 8 | Venue-specific implications documented | ✅ With split-half reliability |
| 9 | League-baseline transition researched | ✅ With error quantification |
| 10 | Candidates reduced to a shortlist | ✅ 3 |
| 11 | One leading approach recommended | ✅ Candidate 2 |
| 12 | Strongest alternative identified | ✅ Candidate 3 |
| 13 | Parameter estimation methodology designed | ✅ |
| 14 | Chronological validation designed | ✅ Rolling origin + held-out test |
| 15 | Leakage risks documented | ✅ 7 paths |
| 16 | Probability metrics defined | ✅ |
| 17 | Model vs betting evaluation separated | ✅ |
| 18 | Dataset requirements defined | ✅ |
| 19 | Storage/snapshot strategy recommended | ✅ Parquet + raw JSON + manifest |
| 20 | xG/player gaps documented without implementation | ✅ |
| 21 | Experimental roadmap proposed | ✅ 2B–2F |
| 22 | Production prediction behaviour unchanged | ✅ Verified by git diff |
| 23 | POISSON_V1 unchanged | ✅ |
| 24 | No thresholds changed | ✅ |
| 25 | Run-3 untouched | ✅ |
| 26–28 | pytest / ruff / mypy pass | ✅ See Epic report |

---

## Research Artefacts

All read-only, GET-only, bounded, disk-cached, and **not imported by production code**:

| File | Purpose |
|---|---|
| `research/audit_espn_history.py` | Per-league-season coverage audit; team-ID continuity |
| `research/investigate_anomalies.py` | Root-causes each anomaly class; earliest-season depth probe |
| `research/measure_cold_start.py` | Prevalence, carryover, reliability, baseline, promotion measurements |
