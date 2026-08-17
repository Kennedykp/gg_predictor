# Epic 2G — Prediction Lifecycle & Observability Audit

**Status:** AUDIT + DESIGN ONLY — nothing implemented, nothing approved
**Baseline commit:** `e43b86e`
**Audit date:** 2026-08-17
**Scope:** answer one question — *can every prediction be tracked from creation → recommendation → match result → evaluation?*

**Files changed by this epic: 1**
- `docs/EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md` — this document (new)

**No production code was modified.** `poisson.py`, `filters.py`, `decision.py`,
`config.py`, `main.py`, `analyze_all.py`, `output.py`, `espn.py`, `odds_api.py`,
`shared/` and all of `domain/` are byte-identical to `e43b86e`. No probability
formula, threshold, or recommendation rule was touched, and none needs to be
touched to resolve anything below.

**The headline answer is: NO.**

Not partially, not with effort — **no**. Every prediction the system has ever
produced is unrecoverable. Not because the data was stored badly, but because
**a prediction is never stored as a prediction**. It is printed, flattened into
a file named after the fixture date, and overwritten by the next run. There is
no ledger, no outcome field, no run identity, and no version stamp anywhere on
the production path.

This is stated as an **absence, not a defect**. Every prior Epic was explicitly
scoped to the model's *correctness*; none was scoped to its *memory*.
`docs/TECHNICAL_DEBT.md:952-953` says so in as many words, listing "database,
backtesting, model versioning, structured logging" under **"Absent
infrastructure (not itemised above because it is absence rather than debt)"**.
Epic 2G is the Epic that itemises it.

---

## Executive summary

| Lifecycle stage | Exists? | Where |
|---|---|---|
| Prediction **created** | ✅ Yes | `main.py:140` / `analyze_all.py:182` → `poisson.calculate_gg_probability` |
| Prediction **transformed** | ✅ Yes | `main.py:107` `build_fixture_poisson_inputs`; `decision.py:31` `calculate_edge` |
| Prediction **scored** (gated) | ✅ Yes | `domain/filter_evaluation.py:79` `evaluate_filters` |
| Prediction **recommended** | ✅ Yes | `decision.py:41` `make_decision`; `analyze_all.py:44` `_gate_recommendation_on_filters` |
| Prediction **output** | ✅ Yes | `output.py:114/155`; `analyze_all.py:392` |
| Prediction **persisted as a record** | ❌ **No** | — |
| Match **result collected** | ❌ **No** (production) | capability exists, unwired: `espn.py:1267` |
| Prediction **graded** | ❌ **No** | — |
| Prediction **versioned** | ❌ **No** (production) | versions exist only offline |

The pipeline is a **pure function with a print statement**. It computes
correctly and then forgets. Six of nine stages work; the three that constitute
*observability* do not exist.

One asymmetry dominates this entire audit and is worth stating before the
detail:

> The offline research layer has **prediction records, schema versions, model
> versions, dataset checksums, Brier, log loss, calibration bins and per-league
> breakdowns**. The live production layer has **none of them**.
> `domain/evaluation.py` and `evaluation_harness.py` already solved this
> problem — for a corpus that is replayed from disk, not for the predictions the
> system actually publishes.

Epic 2G is therefore mostly **wiring an existing, tested contract to the live
path**, not inventing a measurement framework. That is the single most important
finding here, because it changes the cost estimate from "build observability"
to "connect observability".

---

## 1. Current architecture map

### 1.1 Entry points

Three exist. Two are live; one is dead.

| Entry point | Status | Persists to |
|---|---|---|
| `main.py` | **Live** — canonical GG pipeline | `output_{date}.csv` + `output_{date}.json` |
| `analyze_all.py` | **Live** — dual-market (GG_YES/GG_NO) classification | `analysis_output_{date}.json` |
| `run3/main_run3.py` | **Dead** — R3-001: mathematically cannot emit a selection | `run3/run3_output_*.json` |

`run3/` is excluded from the rest of this audit. Instrumenting a subsystem that
provably returns `SKIP` for every input, forever (`docs/TECHNICAL_DEBT.md:262-282`),
would produce a ledger of nothing at a cost. See §7.

### 1.2 The `main.py` lifecycle, traced to exact call sites

```
  date argument (main.py:266-274)  ──  defaults to date.today()  [LOCAL, not UTC — GG-014]
        │
        ▼
  espn.get_fixtures(target_date)                              main.py:231
        │   returns dicts carrying: fixture_id, datetime, league_id,
        │   home/away_team_id+name, status, state, is_completed, is_postponed,
        │   kickoff_utc                                        espn.py:~60-90
        │
        │   ⚠ is_completed / is_postponed / kickoff_utc are NEVER READ here
        │     (GG-013, GG-014 — capability landed 1B.2, still unwired)
        ▼
  espn.get_league_avg_goals(league_id)                        main.py:252-253
        │   memoised in the local dict `league_avg_cache`      main.py:243
        │   ⚠ passed to process_fixture and then NEVER USED    see 2G-F6
        ▼
  ┌─ per fixture ─ process_fixture(fixture, league_avg_goals)  main.py:57
  │
  │   espn.get_team_stats(home_id, league) / (away_id, league) main.py:82-83
  │       └─ None → "Missing or unreliable team stats", return main.py:86-88
  │
  │   shared.match_history.build_fixture_poisson_inputs(       main.py:107
  │       fixture, get_team_venue_averages, get_league_baseline)
  │       └─ POINT-IN-TIME: only matches with kickoff < target (Epic 1B.5)
  │       └─ not is_complete → refuse, record samples, return  main.py:113-127
  │       └─ carries: 5 model inputs + home/away/league sample counts
  │
  │   poisson.calculate_gg_probability(5 inputs)               main.py:140
  │       └─ → lambda_home, lambda_away, gg_probability        main.py:153-155
  │
  │   domain.evaluate_filters(build_fixture_filter_stats(...)) main.py:173
  │       └─ FilterStats → PASSED / FAILED / UNEVALUATED
  │       └─ → passes_filters, filter_outcome,                 main.py:178-183
  │            filter_data_unavailable, rejection_reasons
  │
  │   odds_api.get_btts_odds(home_name, away_name, league_id)  main.py:188
  │       └─ returns a BARE FLOAT or None                      odds_api.py:~55
  │       └─ ⚠ bookmaker, commence_time, price timestamp DISCARDED
  │
  │   decision.make_decision(gg_probability, odds,             main.py:203
  │       passes_filters=filter_result.allows_recommendation)
  │       └─ EDGE_THRESHOLD 0.05, MIN_ODDS 1.60 (config.py:77-78)
  │       └─ → implied_probability, edge, decision, reasons    main.py:210-217
  │
  └─ returns a plain `dict`                                    main.py:61-79
        │
        ▼
  output.print_results(results)                               main.py:277
  output.write_csv(results, f"output_{date_str}.csv")          main.py:280
  output.write_json(results, f"output_{date_str}.json")        main.py:281
        │
        ▼
  PROCESS EXITS ── every cache, every input, every provenance fact is gone
```

### 1.3 The result object

`process_fixture` returns an **untyped `dict`**, initialised at `main.py:61-79`
with 17 keys, plus up to 3 added conditionally:

| Field | Set at | Survives to CSV | Survives to JSON |
|---|---|---|---|
| `fixture_id` | main.py:62 | ❌ **dropped** | ✅ |
| `datetime` | main.py:63 | ✅ | ✅ |
| `league_id` | main.py:64 | ❌ dropped | ✅ |
| `league_name` | main.py:65 | ✅ | ✅ |
| `home_team` / `away_team` | main.py:66-67 | ✅ | ✅ |
| `home_team_id` / `away_team_id` | main.py:68-69 | ❌ dropped | ✅ |
| `lambda_home` / `lambda_away` | main.py:153-154 | ✅ | ✅ |
| `gg_probability` | main.py:155 | ✅ | ✅ |
| `odds` / `implied_probability` / `edge` | main.py:193, 210-211 | ✅ | ✅ |
| `passes_filters` | main.py:178 | ✅ | ✅ |
| `decision` | main.py:212 | ✅ | ✅ |
| `rejection_reasons` | main.py:78 | ✅ (`; `-joined) | ✅ |
| `filter_outcome` | main.py:179 | ❌ **dropped** | ✅ |
| `filter_data_unavailable` | main.py:181 | ❌ **dropped** | ✅ |
| `model_input_samples` | main.py:122, 133 | ❌ **dropped** | ✅ |

The CSV fieldname list is fixed at `output.py:125-139` and the writer is
constructed with `extrasaction="ignore"` (`output.py:142`), so the six dropped
fields vanish **silently**. This is already recorded as GG-023
(`docs/TECHNICAL_DEBT.md:849-852`) but its lifecycle consequence has not been:

> **`output_*.csv` contains no fixture identifier of any kind.** Not
> `fixture_id`, not `league_id`, not team ids. A CSV row can only be
> re-identified by the tuple `(datetime, home_team, away_team)` — free-text
> provider names, the exact quantity GG-008 documents as unreliable for
> matching. **The CSV is unjoinable to a result by construction.**

`output_*.json` **does** retain `fixture_id`, and that is the single most
valuable fact in this audit — see §5.1.

### 1.4 The `analyze_all.py` lifecycle

Same shape, different output contract, and it emits **two rows per fixture**
(`GG_YES` and `GG_NO`, `analyze_all.py:247-287`):

- `shared.odds.analyze_market` (`shared/odds.py:273`) returns `market`,
  `model_probability`, `odds`, `implied_probability`, `edge`, `classification`,
  `system_recommendation` (`shared/odds.py:314-322`).
- `classification` ∈ `STRONG_VALUE` / `VALUE` / `FAIR_NO_EDGE` / `OVERPRICED` /
  `NO_ODDS` (`shared/odds.py:234-254`), thresholds `STRONG_VALUE_EDGE = 0.10`,
  `VALUE_EDGE = 0.05`, `FAIR_EDGE_LOW = -0.05`, `MIN_ODDS_FOR_PLAY = 1.60`
  (`shared/odds.py:41-44`) — **duplicates** `config.EDGE_THRESHOLD` / `MIN_ODDS`
  (2F-P1-2, GG-016).
- `_gate_recommendation_on_filters` (`analyze_all.py:44`) forces
  `RECOMMEND_NO_PLAY` unless `filter_status == "PASSED"` (Epic 2F's fix).
- `write_output` (`analyze_all.py:392-400`) writes a **bare JSON list** — no
  envelope, no `generated_at`, no counts. `output.write_json` at least records
  `generated_at` (`output.py:164`).

The two entry points therefore persist **different schemas with different
identity fields and different recommendation vocabularies** for the same
fixtures. Any observability layer must reconcile them or explicitly cover one.

### 1.5 Everything that is memory-only

| Cache | Declared | Scope | Lost at exit |
|---|---|---|---|
| `espn._schedule_cache` | espn.py:787 | keyed `(league, team, season)` | ✅ |
| `espn._league_cache` | espn.py:1015 | keyed `(league, season)` | ✅ |
| `shared.odds._odds_cache` | shared/odds.py:79 | keyed by league code | ✅ |
| `main.league_avg_cache` | main.py:243 | function-local dict | ✅ |

No TTL, no eviction, no disk backing, no Redis. `espn.py` performs **zero disk
IO**. Every ESPN and odds payload the system has ever seen is gone. The
inputs to any past prediction are therefore **not reconstructible**, even
approximately, because the provider will not serve them again as they were.

---

## 2. Existing capabilities

Stated deliberately before the gaps: this repository is much better positioned
than a "no observability" verdict suggests, because the hard part is already
built and tested.

### 2G-C1 — A complete, frozen prediction-record contract exists

`domain/evaluation.py` (500 lines) already defines everything a prediction
ledger needs:

| Component | Location | What it gives 2G |
|---|---|---|
| `PredictionRecord` | evaluation.py:175-235 | 16-field frozen dataclass, `model_id`, `model_version`, `competition`, `season`, `event_id`, `kickoff`, team ids, `outcome`, `probability`, `unevaluable_reason`, `detail`, `history_matches`, `home_sample`, `away_sample`, `league_sample` |
| `BttsOutcome` | evaluation.py:77-88 | `YES` / `NO` / **`UNKNOWN`** as a first-class value |
| `UnevaluableReason` | evaluation.py:91-104 | `NO_RESULT`, `INSUFFICIENT_HISTORY`, `MODEL_RETURNED_NONE`, `MODEL_ERROR`, `NOT_MODEL_ELIGIBLE` |
| `btts_outcome()` | evaluation.py:107-133 | grading function; refuses booleans, refuses to turn a missing score into `NO` |
| `EVALUATION_SCHEMA_VERSION` | evaluation.py:64 | `"2b3.1"` — written into every artifact |
| `to_json_dict()` | evaluation.py:479-500 | fixed key order → byte-stable serialisation |
| `prediction_sort_key()` | evaluation.py:457-471 | total order; identical inputs → identical bytes |

Two invariants are **enforced in `__post_init__`** (evaluation.py:210-224) and
are exactly the invariants a live ledger needs:

1. `probability is None` **iff** `unevaluable_reason is not None` — a record can
   never be both scored and refused, and never neither.
2. `kickoff.tzinfo is not None` — no naive datetimes, which is the GG-014 trap
   closed at the type level.

**A live prediction is one field short of being a `PredictionRecord`.** It needs
a *creation timestamp* and a *provenance block*; the identity, probability,
outcome and refusal semantics are all already there.

### 2G-C2 — All the metrics exist and are already reused

| Metric | Location | Notes |
|---|---|---|
| Brier | evaluation.py:302-319 | returns `None` for an empty set, never `0.0` |
| Log loss | evaluation.py:322+ | `LOG_LOSS_EPSILON = 1e-15` clip applied **only inside the log**, never to the stored value |
| Calibration | `CalibrationBin` evaluation.py:238-265 + `calibration_table` | signed `gap = observed − predicted` |
| Coverage | `MetricSummary.coverage` evaluation.py:284-295 | `scored / targets`, reported *beside* quality, never instead of it |
| `accuracy_at_half` | evaluation.py:435-440 | explicitly labelled **diagnostic only** |
| Per-league / per-season / per-evidence splits | `EvaluationRun.breakdown` evaluation_harness.py:711-740 | keys `"competition"`, `"season"`, `"evidence"` |
| AUC, prediction spread, constant-predictor benchmark | `domain/discrimination.py` | built in Epic 2D per GG-029 action (a) |

### 2G-C3 — Deterministic, checksummed artifact writing exists

`evaluation_harness.write_artifacts` (evaluation_harness.py:806-872) writes
`evaluation_predictions.jsonl`, `evaluation_summary.json` and
`calibration.json`, each carrying `schema_version`, and accepts
`dataset_checksum` for exactly the reason 2G cares about
(evaluation_harness.py:818-821):

> *"`dataset_checksum` ties results to the exact data they came from. Without
> it, two summaries with different numbers are unattributable — a model change
> and a data change look identical."*

### 2G-C4 — A result-collection mechanism exists (offline)

| Function | Location | Capability |
|---|---|---|
| `espn.get_league_history` | espn.py:1267 | full league-season history with season identity enforced (Epic 2B.1) |
| `espn.HistoricalReadout` | espn.py:1112 | typed readout; provider failure ≠ empty season |
| `historical_dataset.build_dataset` | historical_dataset.py | JSONL corpus, one file per league-season |
| `write_dataset` / `load_dataset` | historical_dataset.py:206 / 235 | `{league}_{season}.jsonl`, `newline="\n"`, SHA-256 per file |
| `build_manifest` | historical_dataset.py:252-318 | `schema_version` `"2b2.1"`, `eligibility_rule_version`, `built_at`, `provider`, per-season counts, `duplicate_event_ids`, `repeated_pairings` |
| `file_checksum` | historical_dataset.py:201-203 | SHA-256 of file bytes |

`HistoricalMatch` (`domain/historical.py`) carries `event_id`, `competition`,
`season`, `kickoff`, team ids, `completed`, `home_goals`, `away_goals`,
`status`, `season_phase`, `provider`.

**`HistoricalMatch.event_id` and the live `fixture_id` are the same ESPN event
id.** That shared key is what makes settlement possible without a new provider,
a new endpoint, or a schema negotiation.

### 2G-C5 — Fixture status and UTC kickoff are already available

`espn.is_predictable(fixture)` exists at `espn.py:191`, and each fixture
carries `state`, `is_completed`, `is_postponed`, `kickoff_utc`. Verified: the
only non-test callers are `scripts/espn_diagnostic.py:338/388`. **Neither
`main.py` nor `analyze_all.py` calls it** (GG-013, GG-014 — "capability landed,
not yet consumed").

### 2G-C6 — Regression tests already enforce the boundaries 2G must respect

- `tests/regression/test_evaluation_leakage.py` — **import-level firewall**: the
  evaluation layer may not reach odds, prices, edges, thresholds or
  `decision.py`.
- `tests/regression/test_point_in_time_inputs.py` — 30 future matches + a whole
  future league programme must leave inputs **byte-identical**; mutation-tested.
- `tests/regression/test_gg028_sparse_sample.py` — pins POISSON_V1's raw
  exact-`0.0` behaviour so nobody "fixes" `poisson.py`.
- `tests/integration/test_entry_point_consistency.py` — both entry points, one
  mocked ESPN response, identical verdicts and reasons.

---

## 3. Missing capabilities

### 2G-F1 — No prediction is ever persisted as a prediction (**CRITICAL**)

There is no ledger, no table, no append-only log. The complete inventory of
production disk writes is:

| Writer | Location | Path | Semantics |
|---|---|---|---|
| `output.write_csv` | output.py:141-150 | `output_{target_date}.csv` | 13 of 20 fields, no identifier |
| `output.write_json` | output.py:171-172 | `output_{target_date}.json` | full dicts + `generated_at` |
| `analyze_all.write_output` | analyze_all.py:397-398 | `analysis_output_{target_date}.json` | bare list, no envelope |
| `run3/main_run3.py` | run3/main_run3.py:413-414 | `run3/run3_output_*.json` | dead (R3-001) |

Three structural problems follow:

1. **Filename keyed on target date, not run identity.** `main.py:279-281` uses
   `target_date`, so re-running the same date **overwrites in place**. A morning
   run and an evening run of the same fixtures are indistinguishable, and the
   earlier one is destroyed. There is no `run_id` anywhere in the repository.
2. **Written to the current working directory.** No output dir; git-ignored at
   `.gitignore:15-17`; five artifacts committed before that rule existed
   (GG-023). Confirmed tracked: `output_2026-01-16.json`,
   `output_2026-01-17.{csv,json}`, `output_2026-01-18.{csv,json}`.
3. **There is no field for what happened.** No `outcome`, no `home_goals`, no
   `settled_at`, no `graded` flag. The schema has no slot for the truth.

Empirically confirmed on the committed artifacts: 39 records across two dates,
**all** `"decision": "NO BET"`, **all** `"odds": null`, and **zero** occurrences
of `model_input_samples` or `filter_outcome` (those keys were added to
`process_fixture` after those files were written). The stored artifacts are
already a *different schema* from what the code emits today, with nothing
recording that fact — which is precisely what a schema version prevents.

### 2G-F2 — The model inputs are destroyed (**HIGH**)

`build_fixture_poisson_inputs` (`main.py:107`) produces five inputs plus three
sample counts. What survives:

| Quantity | Persisted? |
|---|---|
| `league_avg_goals` | ❌ |
| `home_goals_scored_home` | ❌ |
| `home_goals_conceded_home` | ❌ |
| `away_goals_scored_away` | ❌ |
| `away_goals_conceded_away` | ❌ |
| `lambda_home`, `lambda_away` | ✅ |
| `gg_probability` | ✅ |
| `home/away/league_sample` | ✅ JSON only (`model_input_samples`) — ❌ CSV |

λ is **not invertible** to the five inputs: `lambda_home` is a product of a home
attack ratio, an away defence ratio and the baseline, so infinitely many input
triples yield the same λ. Storing λ and discarding the inputs means *"why did
this prediction happen?"* is unanswerable even with the code in hand.

This matters more here than in a typical system, because Epic 2C's central
finding (GG-028) is that POISSON_V1's behaviour is **dominated by input
thinness**, and Epic 2B.3's evidence buckets (Brier 0.4241 at n=1–2 vs 0.2555
at n=10+) are exactly the stratification that becomes impossible without the
inputs. The samples *are* on the JSON — so the most important stratifier
survives in one of two live formats and neither is ever read back.

### 2G-F3 — Filter *values* are destroyed; only the verdict survives (**HIGH**)

`FilterResult` (`domain/filter_evaluation.py:45-57`) carries exactly three
fields: `outcome`, `reasons`, `unavailable_fields`. The `FilterStats` object it
consumed — `home_avg_goals_scored`, `away_avg_goals_scored`,
`home_clean_sheet_pct`, `away_clean_sheet_pct`, `home_history_sample`,
`away_history_sample`, `home_btts_pct`, `away_btts_pct`, `clean_sheet_source`,
`avg_goals_source` (`domain/filter_stats.py:89-129`) — is **discarded at the
call boundary**.

So the system can say *"FAILED: home avg goals 0.30 < 1.0"* in a free-text
reason string, but cannot later compute the distribution of rejection margins,
cannot tell a 0.99 near-miss from a 0.10 rout, and cannot count how often
`StatSource.UNAVAILABLE` fired per league. Rejections are **categorical, not
quantitative**.

Note this is not a design error in `FilterResult` — a filter verdict is what
`evaluate_filters` exists to return. The gap is that nothing else captures the
inputs on the way past.

### 2G-F4 — Odds provenance is destroyed at the type level (**HIGH**)

`odds_api.get_btts_odds` is typed `-> Optional[float]`. It iterates bookmakers
and markets and returns `outcome.get("price")` — a bare number. Discarded:

- **which bookmaker** quoted it (2F-P1-5: "first bookmaker wins", so the price
  depends on arbitrary provider ordering)
- **`commence_time`** (2F-P1-3: never checked against fixture kickoff, so a
  price from a *different match* can attach)
- **when the price was observed** (no snapshot time)
- **the other bookmakers' prices** (no spread, no consensus)
- **which fixture the API thought it was pricing** (matched by bidirectional
  substring fuzzing, GG-008 / 2F-P1-4)

`shared/odds.py` is marginally better — it caches a per-league map
(`_odds_cache`, line 79) — but `analyze_market` (line 314-322) still emits a
bare `odds` float, and `round(edge, 4) if edge else None` (line 319) converts a
genuine `0.0` edge to `null` (GG-007), making "no edge" and "no odds"
indistinguishable in the persisted artifact.

**This is the field with the highest option value in the whole design.** See
2G-F8 and §5.4.

### 2G-F5 — No result collection on the production path (**CRITICAL**)

The production path never asks what happened. Confirmed by import analysis:
`main.py`, `analyze_all.py`, `output.py`, `decision.py`, `filters.py`,
`poisson.py`, `shared/` and `run3/` contain **no reference** to
`evaluation_harness`, `historical_dataset`, `run_evaluation`,
`domain.evaluation` or `domain.historical`. The single crossing edge is
`espn.py:36` importing `HistoricalMatch` — provider → contract, not production →
evaluation.

`data/` **does not exist** on disk. `historical_dataset.py:345` defaults
`--out` to `data/historical`; nothing has ever been built in-tree. The corpus,
the manifest and the checksums are a *capability*, not an *artifact*.

Consequences:
- No fixture is ever revisited after kickoff.
- `espn.is_predictable` is not consulted, so **completed and postponed matches
  are still predicted** (GG-013), and their "predictions" are indistinguishable
  from genuine forecasts in the output file.
- `date.today()` is **local** (`main.py:274`); `kickoff_utc` exists and is
  ignored, so a 23:30Z fixture is filed under the wrong matchday on a UTC+1
  machine (GG-014). Any future daily aggregation inherits that error.

### 2G-F6 — The league-average diagnostic is fetched and thrown away (**LOW**)

`main.py:252-253` fetches `get_league_avg_goals(league_id)` per league and
passes it into `process_fixture(fixture, league_avg_goals)` (`main.py:57, 258`).
The comment at `main.py:103-105` states it is retained "as a diagnostic
comparison". **It is not compared to anything.** Reading the whole function
body: `league_avg_goals` is never referenced after the signature — the model
consumes `model_inputs.league_avg_goals` (`main.py:141`) instead.

So the pipeline spends a network request per league to compute a
current-season baseline, then discards it without recording it. As a *lifecycle*
observation this is instructive: the point-in-time baseline vs the
current-season baseline is exactly the comparison that would evidence Epic
1B.5's fix continuing to hold in production — and the value is already in hand.
It costs nothing to record and cannot be recovered later.

### 2G-F7 — No versioning anywhere on the production path (**CRITICAL**)

Complete repository inventory of version identifiers (excluding `tests/`,
`research/`, `.venv/`):

| Constant | Location | Value | Layer |
|---|---|---|---|
| `EVALUATION_SCHEMA_VERSION` | domain/evaluation.py:64 | `"2b3.1"` | offline |
| `SCHEMA_VERSION` | domain/historical.py:57 | `"2b2.1"` | offline |
| `ELIGIBILITY_RULE_VERSION` | domain/historical.py:62 | `"2b2.1"` | offline |
| `ESTIMATOR_VERSION` | domain/team_strength.py:145 | `"2c.1"` | offline |
| `PoissonV1Adapter.model_version` | evaluation_harness.py:283 | `"1.0.0"` | offline |
| `ReferenceBaseRateAdapter.model_version` | evaluation_harness.py:463 | `"1.0.0"` | offline |
| `PROVIDER_NAME` | espn.py:50 | `"espn"` | provider (a *name*, not a version) |

**Production count: zero.** `config.py` (82 lines, every constant read) contains
no version identifier of any kind. `main.py`, `analyze_all.py`, `output.py`,
`poisson.py`, `filters.py`, `decision.py` contain none.

The consequence is sharp. The harness can state:

> POISSON_V1 `1.0.0`, schema `2b3.1`, dataset checksum `a1b2…`

The live path can state:

> *(nothing)*

`output_2026-01-18.json` records `generated_at` and 16 fields of football. It
does not record which model, which thresholds, which filter semantics, which
provider, or which commit produced it. Months later, *"why did this prediction
happen?"* is answerable only by `git log` guesswork against a file whose own
schema has already drifted (2G-F1).

This matters concretely, not hypothetically. The repository has already shipped
**semantic changes that alter recommendations without altering the output
schema**:

| Change | Epic | Observable in an old artifact? |
|---|---|---|
| GG-003: league average `1.35` → real standings | 1B.2 | ❌ |
| GG-006: filter input `total_goals_avg` → `goals_scored` | 1B.3 | ❌ |
| LEAK-001 partial: current-season → point-in-time inputs | 1B.5 | ❌ |
| 2F-P0-1: `analyze_all.py` filter gate on recommendation | 2F | ❌ |

Four changes, each of which alters what the system publishes for the same
fixture, and **not one of them is detectable from a stored output file**.

### 2G-F8 — ROI is not merely unmeasured; the concept does not exist (**HIGH**)

Repository-wide search for `roi`, `bankroll`, `stake`, `settle`, `payout`,
`profit`: the only hits outside `docs/` are prose — `GG.md:182` and
`filters.py:11` ("They protect the bankroll"). There is **no stake, no
settlement, no P&L, no unit, no currency** anywhere in the codebase.

`domain/evaluation.py:26-29` forbids it deliberately:

> *"NO ODDS. Nothing here imports odds, prices, edges or thresholds, and nothing
> may. Probability quality is a football question; betting value is blocked by
> LEAK-001 and is not measured in this Epic."*

— enforced by `tests/regression/test_evaluation_leakage.py` as an import-level
guard. **This is a correct constraint and Epic 2G must not weaken it.** See
2G-R4.

### 2G-F9 — No structured logging, no run identity (**MEDIUM**)

Every diagnostic is `print()` (GG-019). No levels, no timestamps, no
destination, no correlation id. `output.py:82-111` prints a human summary that
is never machine-readable. There is no way to associate a failed ESPN fetch with
the fixture it starved, or two fixtures with the same run.

Security note carried from GG-019 and still true: The Odds API takes its key as
a **query parameter** (`shared/odds.py:91`, `params["apiKey"] = ODDS_API_KEY`),
so a printed `requests` exception can embed the secret. Any new logging must
redact before it emits.

---

## 4. Risks

Ordered by cost of delay, not by likelihood.

### 2G-R1 — Every day without capture is permanently unrecoverable (**CRITICAL**)

`docs/EPIC_2F_SEASON_READINESS.md:6` dates the season-readiness audit at
"approx. one week before Matchday 1". **The season is starting now.**

Predictions cannot be reconstructed retrospectively. Goals can — ESPN serves
settled results indefinitely. But the *prediction* cannot, because it depends
on:

- ESPN's schedule/standings responses **as they were at that moment** (nothing
  is cached to disk; the provider will not reserve them)
- odds **as they were at that moment** (the market has moved; LEAK-001)
- the code **as it was at that moment** (no version stamp, so even the commit is
  a guess)

Running `main.py 2026-08-24` in November does not reproduce the 24 August
prediction. It produces a *new* prediction with November's inputs. It will look
identical in every field and be a different number.

**This is the only finding in this document with a deadline.** Every other gap
can be closed later at the same cost. This one gets more expensive daily, and
the evidence lost is precisely the first-season evidence that is most valuable
for calibration.

### 2G-R2 — The CSV is already unjoinable (**HIGH**)

Per 2G-F1, `output_*.csv` has no identifier. If any downstream consumer or
manual review depends on the CSV, those rows cannot be graded even after a
ledger exists. The JSON is joinable. **Anyone treating the two as equivalent
records is mistaken**, and nothing in the code says so.

### 2G-R3 — Silent overwrite destroys same-date history (**HIGH**)

`output_{target_date}.json` (`main.py:281`) means the *second* run of a date
erases the first. Two runs may legitimately differ — odds move, ESPN backfills a
result, a team's history grows — and the difference between them is exactly the
observability signal worth having. It is currently destroyed by `open(…, "w")`.

### 2G-R4 — ROI is a trap, and it is the feature most likely to be requested (**CRITICAL**)

The audit brief asks "What was ROI?". The honest answer must be a **refusal for
historical data** and a **conditional yes for future data**, and conflating them
would undo LEAK-001's most important guard.

| Direction | Valid? | Why |
|---|---|---|
| ROI computed over **past** fixtures using **today's** odds | ❌ **Invalid** | The price did not exist at kickoff. `docs/TECHNICAL_DEBT.md:330-335`: "*a backtest of recommendations remains invalid even with a perfectly clean probability*" |
| ROI computed over **future** fixtures using odds **recorded at prediction time** | ✅ **Valid** | The recorded price *is* point-in-time by construction — it was observed before kickoff |

The trap is that both produce a plausible percentage, and only one means
anything. A ledger makes the second possible for the first time in the
project's history — and simultaneously makes the first *easy*, which is the
danger. The design in §5 addresses this structurally, not by convention.

### 2G-R5 — A performance report will re-run the Brier trap (**HIGH**)

GG-029 and GG-031 are unambiguous and were confirmed on an untouched holdout:

- Constant base-rate predictor: **Brier 0.2469**
- Raw POISSON_V1: **Brier 0.2601**
- Shrunk estimator: **Brier 0.2528**
- AUC ≈ **0.535**; leaky oracle ceiling ≈ **0.568**

A naive report showing "Brier 0.25, looks fine" would be **worse than no
report**, because minimising Brier drives the model toward the base rate and
that degenerate outcome *looks like a win* (`docs/TECHNICAL_DEBT.md:621-627`).
GG-029 action (a) is explicit: *"report AUC and the constant-predictor score
alongside Brier in the harness permanently, so a model that loses to a constant
cannot look acceptable."*

Any 2G reporting surface must therefore print **Brier, AUC, coverage and the
constant-predictor benchmark together, or print nothing.** `domain/discrimination.py`
already computes all of it.

### 2G-R6 — Unevaluated vs failed must not be re-conflated (**HIGH**)

Epics 1B.1/1B.3 spent their entire budget establishing that *"unknown is not
zero, unknown is not neutral, unknown is not pass"*
(`domain/filter_stats.py:17`). GG-002 survived for as long as it did precisely
because *"passed filters" was indistinguishable from "filters never ran"*.

A ledger schema that stores `passes_filters: bool` — as `main.py:178` and both
output writers currently do — **re-flattens three states into two**. The record
must persist `FilterOutcome` (`PASSED` / `FAILED` / `UNEVALUATED`), and
`unavailable_fields`, not a boolean. This is a schema-design risk, and schemas
are the hardest thing to change after data exists.

### 2G-R7 — Instrumenting a dead subsystem (**MEDIUM**)

`run3/` is 757 lines that provably cannot emit a selection (R3-001) and
duplicates the ESPN client, the league map and the λ formula (GG-010). Adding
capture there would produce a ledger of `SKIP`s and double the surface that must
be kept consistent. Excluded by design; stated so the exclusion is deliberate
rather than forgotten.

### 2G-R8 — Capture must not be able to change a prediction (**CRITICAL**)

The single largest engineering risk in implementing 2G is that observability
code sits *inside* `process_fixture` and `analyze_gg_match`. A ledger write that
raises, blocks, or mutates the result dict would change what the system
recommends — turning an observability layer into a model change, which the
absolute rules forbid.

Mitigation must be structural, not careful: capture is a **pure read** of a
completed result, invoked *after* the decision is final, and a capture failure
must be reported without altering the prediction or aborting the run. This is
testable and must be tested (§5.5).

---

## 5. Proposed architecture

Design principles, in priority order:

1. **Additive only.** No existing function's behaviour, signature or return
   value changes. No threshold, formula or recommendation rule is touched.
2. **Reuse, do not rebuild.** `domain/evaluation.py` already defines the
   record, the outcome semantics and every metric. 2G extends it; it does not
   parallel it.
3. **Capture cannot alter a prediction.** Observation is a read of a finished
   result.
4. **Absence stays absent.** No defaulting, no `0.0` for unknown, no boolean
   collapse of a three-state verdict.
5. **Forward-only.** Nothing is backfilled. A record is written when the
   prediction is made, or never.

### 5.1 The join key already exists

```
  live prediction            settled result
  ─────────────────          ──────────────────────────
  result["fixture_id"]  ══   HistoricalMatch.event_id
  (main.py:62,            (domain/historical.py,
   from espn.py            from espn.get_league_history)
   event.get("id"))
```

Both are the ESPN event id, from the same provider, in the same identity space.
No new provider, no name matching, no fuzzy join, no new endpoint. Settlement is
a dictionary lookup.

`output_*.json` already persists `fixture_id`. **`output_*.csv` does not** —
which is why the ledger must be its own artifact rather than an enrichment of
the CSV.

### 5.2 Component design (5 new files, 0 rewrites)

```
                        ┌─────────────────────────────────────┐
                        │ UNCHANGED PRODUCTION                │
                        │ poisson · filters · decision        │
                        │ config · espn · odds_api · output   │
                        └──────────────┬──────────────────────┘
                                       │ result dict (read-only)
                                       ▼
  ①  domain/prediction_log.py   ── PURE CONTRACT, no IO, no network
     LivePredictionRecord  (extends the PredictionRecord discipline)
     PredictionProvenance  (the 4 version axes + run identity)
     from_result_dict()    (main.py dict  → record)
     from_market_dict()    (analyze_all.py dict → record)
                                       │
                                       ▼
  ②  prediction_ledger.py       ── APPEND-ONLY WRITER / READER
     append(records, run)  → data/predictions/YYYY-MM.jsonl
     load(...)             → List[LivePredictionRecord]
     never rewrites a line · never overwrites a file
                                       │
                    ┌──────────────────┴───────────────────┐
                    ▼                                      ▼
  ③  settle_predictions.py                    ④  report_performance.py
     espn.get_league_history                     ledger + settlements
       → domain.evaluation.btts_outcome            → domain.evaluation.summarise
       → data/settlements/YYYY-MM.jsonl            → domain.discrimination (AUC,
     SEPARATE FILE — a prediction record             constant benchmark)
     is immutable once written                    → breakdown by league /
                                                    confidence / evidence / version
  ⑤  ONE call site per live entry point (the only production edit)
     main.py            after run_daily_workflow, before/beside output.*
     analyze_all.py     after run_unified_analysis
     wrapped so a ledger failure cannot change or abort the prediction
```

### 5.3 The record: what must be captured that is not captured today

Extends `PredictionRecord`'s existing 16 fields. **New** fields, each justified
by a finding above:

| Field | Closes | Why it cannot be derived later |
|---|---|---|
| `created_at` (tz-aware UTC) | 2G-F1 | When the prediction was *made*, distinct from kickoff. The prediction/kickoff gap is itself a variable. |
| `run_id` (uuid) | 2G-F1, 2G-R3 | Groups one execution; makes same-date re-runs distinguishable instead of destructive |
| `provenance` (block below) | 2G-F7 | Version state is not recoverable from a stored file |
| `model_inputs` (the 5 values) | 2G-F2 | λ is not invertible |
| `filter_outcome` (`FilterOutcome`) | 2G-R6 | Three states, never a bool |
| `filter_unavailable_fields` | 2G-F3, 2G-R6 | Distinguishes FAILED from UNEVALUATED at the field level |
| `filter_inputs` (the 4 stats + samples + sources) | 2G-F3 | Rejection margins, not rejection categories |
| `odds_snapshot` (price, bookmaker, observed_at, commence_time) | 2G-F4 | Market state is gone within hours |
| `decision` / `system_recommendation` / `classification` | 2G-F1 | The published verdict is the thing being audited |
| `fixture_state` (`state`, `is_completed`, `is_postponed`, `kickoff_utc`) | 2G-F5 | Separates genuine forecasts from GG-013 pseudo-predictions |
| `league_avg_goals_current_season` | 2G-F6 | Already fetched and discarded; free to record |
| `schema_version` | 2G-F1, 2G-F7 | The stored artifacts have *already* drifted once, silently |

Retained unchanged from `PredictionRecord`: `event_id`, `competition`, `season`,
`kickoff`, `home_team_id`, `away_team_id`, `probability`,
`unevaluable_reason`, `detail`, `history_matches`, `home_sample`,
`away_sample`, `league_sample`, and the `probability` XOR `unevaluable_reason`
invariant.

**`outcome` is deliberately NOT on the prediction record.** A prediction is
immutable; the result is a separate later fact written to a separate file. Two
reasons: mutating a record in place destroys the audit trail it exists to
provide, and an in-place `outcome` field invites the `None`-means-`NO` collapse
that `btts_outcome` (evaluation.py:107-133) was written to prevent.

### 5.4 Versioning strategy — four axes, recorded not declared

The audit asks how we will know, months later, *why did this prediction
happen?* Four independent things can change the answer, so four independent
identifiers are required:

| Axis | Constant | Initial value | Bumped when |
|---|---|---|---|
| **Model** | `MODEL_VERSION` | `"POISSON_V1/1.0.0"` | `poisson.py` mathematics change (currently frozen; `test_poisson_v1_regression.py` guards it) |
| **Filter** | `FILTER_VERSION` | `"1b3.1"` | filter thresholds, semantics or the `FilterStats` mapping change |
| **Decision** | `DECISION_VERSION` | `"1.0.0"` | `make_decision` rules, `EDGE_THRESHOLD`, `MIN_ODDS`, or the recommendation gate change |
| **Data source** | `DATA_SOURCE_VERSION` | `"espn/1b5.1"` | provider, endpoint, or derivation provenance changes (pairs with `espn.PROVIDER_NAME`) |

Aligning `MODEL_VERSION` with the existing `PoissonV1Adapter.model_version`
`"1.0.0"` (evaluation_harness.py:283) is deliberate: the offline harness and the
live path must be able to state that they ran *the same model*, which is
currently unprovable.

Three supporting identifiers, because a declared version can be forgotten:

- **`config_fingerprint`** — a short hash over the ordered threshold tuple
  `(EDGE_THRESHOLD, MIN_ODDS, MIN_AVG_GOALS, MAX_CLEAN_SHEET_PCT,
  sorted(ALLOWED_LEAGUES))`, computed at capture time by **reading `config.py`**.
  This is the load-bearing part of the design. A hand-maintained version string
  is a promise; a fingerprint is a measurement. If someone edits a threshold and
  forgets to bump `DECISION_VERSION`, the fingerprint changes anyway and the
  ledger shows a discontinuity. This is the same reasoning that made
  `dataset_checksum` necessary in `write_artifacts` (evaluation_harness.py:818-821).
- **`code_revision`** — `git rev-parse HEAD` plus a dirty flag, captured
  best-effort. Answers "which commit" without inference.
- **`schema_version`** — a new constant for the ledger's own shape, following
  the established `"2b2.1"` / `"2b3.1"` convention. `EVALUATION_SCHEMA_VERSION`
  must **not** be reused: the harness record and the live record are different
  shapes and merging them silently is exactly what a schema version prevents.

Note that `config_fingerprint` reads `config.py` — it does not modify it. No new
constant is added to `config.py` by this design; the version constants live in
the new `domain/prediction_log.py`, keeping the frozen-config guarantee intact.

### 5.5 Enforcement — the guards this design needs

Following house practice, the invariants must be enforced by tests, not by
documentation:

| Guard | Enforces |
|---|---|
| Production output byte-identical with capture enabled and disabled | 2G-R8 — capture cannot change a prediction |
| A ledger writer that raises still yields a complete, correct prediction run | 2G-R8 — failure isolation |
| The ledger module may not import `poisson`, `filters` or `decision` | Additive-only; no back-channel into the model |
| No `bool` field may represent a filter verdict | 2G-R6 — three states stay three |
| A record with `probability=None` and `unevaluable_reason=None` raises | Inherited `PredictionRecord` invariant |
| `created_at` and `kickoff` must be tz-aware | GG-014 — no naive datetimes |
| The reporting surface cannot emit Brier without AUC + constant benchmark | 2G-R5 — the Brier trap |
| Settlement may not write into a prediction file | Immutability of the audit trail |
| ROI/stake symbols absent from ledger, settlement and report modules | 2G-R4 / LEAK-001 firewall, mirroring `test_evaluation_leakage.py` |

The mutation-testing practice used in 2B.1 (7 weakenings, 7 killed) and 1B.5
should be applied to the capture-cannot-alter guard specifically, since a
vacuous test there is exactly the 2F-P1-1 failure mode: green CI over a live
defect.

---

## 6. Recommended implementation order

Sequenced by **dependency and irreversibility**, not by effort. Each phase is
independently shippable and independently valuable.

### Phase 2G-1 — Record contract + version constants (**FIRST, no production edit**)

Create `domain/prediction_log.py`: `LivePredictionRecord`,
`PredictionProvenance`, the four version constants, `config_fingerprint()`,
`to_json_dict()` with fixed key order, and `from_result_dict()` /
`from_market_dict()` adapters for the two entry-point shapes.

Pure, offline, no IO, no network, no production import. Unit-testable against
the committed `output_*.json` artifacts.

**Why first:** every later phase depends on the schema, and schemas are the one
thing that cannot be changed cheaply after data exists. Doing this before
capture costs one phase; doing it after costs a migration.

### Phase 2G-2 — Ledger writer + capture wiring (**the only production edit**)

Create `prediction_ledger.py` (append-only JSONL,
`data/predictions/YYYY-MM.jsonl`, `newline="\n"`, one record per line, never
rewrites, never overwrites). Then add **one guarded call site** per live entry
point, after the decision is final.

Must be additive, `try`-isolated, and covered by the byte-identical-output guard
from §5.5. This is the phase that requires explicit approval, because it is the
only one that touches `main.py` / `analyze_all.py` at all.

**Why second:** 2G-R1. This is the only phase with a deadline. Until it ships,
the season's evidence is being discarded daily and cannot be recovered.

### Phase 2G-3 — Settlement job

Create `settle_predictions.py`: read unsettled ledger records, group by
`(competition, season)`, call the **existing** `espn.get_league_history`, join on
`event_id`, derive the outcome with the **existing**
`domain.evaluation.btts_outcome`, and append to
`data/settlements/YYYY-MM.jsonl`. Never mutates a prediction record. An absent
or postponed fixture settles as `BttsOutcome.UNKNOWN`, never as `NO`.

Runs as a separate offline job. Not imported by production.

**Why third:** it consumes the ledger, so it cannot precede it — but it can lag
it safely. Results remain available from ESPN indefinitely, so settlement is the
one part of the lifecycle that *can* be backfilled.

### Phase 2G-4 — Reporting

Create `report_performance.py`: join ledger + settlements, feed
`domain.evaluation.summarise` and `domain.discrimination`, and emit **Brier, log
loss, AUC, coverage, the constant-predictor benchmark, and calibration bins
together**, with breakdowns by league, confidence band, evidence bucket and
`model_version` / `config_fingerprint`.

Reuses `EvaluationRun.breakdown` (evaluation_harness.py:711-740) semantics; adds
`model_version` and `config_fingerprint` as new breakdown keys so a threshold
change shows up as a visible discontinuity rather than a blurred average.

**Why fourth:** metrics over a thin ledger are noise. The GG-028 evidence
buckets showed Brier 0.4241 at n=1–2; a report over two matchdays would be
worse than silence. Ship the plumbing, accumulate data, then report.

### Phase 2G-5 — Odds snapshot enrichment (**deferred, high option value**)

Extend the ledger's `odds_snapshot` to record bookmaker identity, `commence_time`
and observation time, without changing `odds_api.get_btts_odds`'s signature or
`decision.py`'s inputs.

**Why last, and why it matters most in the long run:** this is the only path
that eventually closes LEAK-001's odds row. Recorded-at-prediction-time prices
are point-in-time **by construction**, so after a season of capture the project
would — for the first time — hold a genuinely valid dataset for the one open
question GG-031 and GG-032 both terminate on: *does the market probability
discriminate better than 0.568 AUC?*

It is deferred rather than dropped because it must be **forward-only**. Any
attempt to accelerate it by backfilling historical odds reintroduces LEAK-001 in
its original form.

---

## 7. Explicit scope boundaries

### 7.1 What must be built FIRST

| Order | Item | Reason |
|---|---|---|
| 1 | **`LivePredictionRecord` + `PredictionProvenance` contract** (2G-1) | Everything depends on the schema. It is the only artifact that is expensive to change after data exists. |
| 2 | **Append-only ledger + guarded capture** (2G-2) | 2G-R1: the only finding with a deadline. Unrecorded predictions are permanently unrecoverable — goals can be backfilled, predictions cannot. |
| 3 | **The four version axes + `config_fingerprint`** (inside 2G-1) | Data captured without provenance is data that must be recaptured. Four semantic changes have already shipped invisibly (2G-F7). |

Everything else can wait without loss. These three cannot.

### 7.2 What must NOT be built yet

| Not yet | Why |
|---|---|
| **ROI / P&L / stake / bankroll accounting** | 2G-R4 + LEAK-001. Historical ROI is invalid because the price did not exist at kickoff. Forward ROI needs 2G-5 plus a settled season. Building the arithmetic before the data exists guarantees it will be run on invalid inputs — the most attractive-looking and least valid output this project could produce. |
| **A dashboard or UI** | Explicitly out of scope. A visualisation over an empty ledger is a visualisation of nothing, and it would create pressure to fill it with the metrics 2G-R5 forbids. |
| **A database (SQLite / Postgres)** | JSONL is append-only, greppable, diffable, byte-stable and checksummable, and matches the established `historical_dataset.py` / `write_artifacts` convention. A schema migration is a cost to pay when volume demands it, not before. Five leagues × ~10 fixtures/day is a rounding error. |
| **Any change to `poisson.py`, `filters.py`, `decision.py`, `config.py`** | Absolute rule. Also: `test_poisson_v1_regression.py` and `test_gg028_sparse_sample.py` exist to make POISSON_V1's behaviour permanent, including its flaws, so the baseline stays reproducible. |
| **A new model, new features, new providers** | Absolute rule — and GG-031/GG-032 already establish that goal counts, shots and xG are exhausted. The open question is the *market*, which needs 2G-5 first. |
| **Recalibration of stored probabilities** | GG-029 and GG-031 both forbid it: with AUC ≈ 0.54 a monotone recalibration provably cannot add skill, so it would improve Brier while leaving the ranking — and the real problem — untouched. Store raw probabilities; recalibrate never, and certainly not before measuring. |
| **Retrospective backfill of predictions** | 2G-R1. Re-running a past date produces a *new* prediction from *today's* inputs, indistinguishable in the schema from a genuine one. That is data fabrication with a plausible schema. Settlement may be backfilled; prediction may not. |
| **Repairing GG-013 / GG-014 in the entry points** | Real bugs (completed matches predicted; local-vs-UTC matchday), but fixing them **changes which fixtures are processed**, i.e. changes production output. 2G must *record* `state`, `is_completed` and `kickoff_utc` so the frequency becomes measurable; the wiring fix is its own approved change. |
| **`run3/` instrumentation** | 2G-R7. R3-001: it cannot emit a selection. A ledger of `SKIP`s at double the maintenance surface. |
| **A structured-logging refactor** | GG-019 is real but orthogonal. `run_id` in the ledger delivers most of the traceability value; replacing every `print()` is a separate, larger, unrelated change. |
| **Unifying the two entry points' output schemas** | GG-016 / 2F-P1-2 are real debt, but merging them is a refactor with recommendation-changing risk. The ledger should *normalise* both into one record shape; the entry points keep their existing outputs untouched. |
| **Alerting / monitoring / SLOs** | Requires a baseline of normal behaviour. There is no data yet. Premature thresholds would fire on nothing or on everything. |

### 7.3 Debt this Epic would touch

| Item | Effect if 2G-1…2G-4 ship |
|---|---|
| **LEAK-001** (odds row) | **Unblocked, not closed.** 2G-5 begins accumulating genuine point-in-time prices. Closure needs a settled season of forward capture. |
| **GG-005** (no form/recency) | Unaffected as a model gap, but the ledger records `home_sample`/`away_sample` per prediction, making evidence thinness measurable in production for the first time. |
| **GG-007** (falsy-edge `0.0` → `null`) | **Avoided in the ledger** by using `is not None` in the new record; `shared/odds.py:319` itself is untouched and stays open. |
| **GG-013 / GG-014** | **Made measurable, not fixed.** Recording `is_completed`, `is_postponed` and `kickoff_utc` quantifies how often a non-predictable fixture is predicted. |
| **GG-019** (no run id) | **Partially addressed** — `run_id` exists in the ledger; `print()` remains. |
| **GG-023** (artifacts in CWD) | **Not fixed.** The ledger writes to `data/predictions/`; `output_*.csv/json` stay where they are. |
| **GG-028 / GG-029 / GG-031 / GG-032** | Unaffected — all four are properties of the model and the feature set, and 2G changes neither. 2G makes them **observable in production** instead of only in replay. |
| **"Absent infrastructure"** (`TECHNICAL_DEBT.md:952-953`) | **Model versioning** and **backtesting-of-record** become present rather than absent. Database, UI, CI and API remain absent by choice. |

### 7.4 Definition of done for Epic 2G

Epic 2G is complete when this question is answerable from stored artifacts
alone, with no code archaeology:

> *"Prediction `X` for fixture `Y` on date `Z` said 0.58. Why did it say that,
> what did we recommend, what happened, and were we right?"*

Concretely — every one of these resolvable from `data/predictions/` and
`data/settlements/`:

- [ ] the five model inputs and their sample sizes
- [ ] the filter verdict as one of three states, with the input values and the unavailable fields
- [ ] the odds, the bookmaker, and when the price was observed
- [ ] the published decision / recommendation / classification
- [ ] the model, filter, decision and data-source versions, plus the config fingerprint and the commit
- [ ] the actual BTTS outcome, or an explicit `UNKNOWN` with a reason
- [ ] aggregate Brier, log loss, AUC, coverage and the constant-predictor benchmark, split by league, confidence band, evidence bucket and version

Not in scope for "done": ROI, a dashboard, a database, or any change to what
the system predicts.

---

## 8. Verification of this audit

Every claim above was checked against the repository at `e43b86e`. Method:

| Claim | How verified |
|---|---|
| Lifecycle call sites | Full reads of `main.py` (285 lines), `analyze_all.py` (421), `output.py` (174), `decision.py` (109), `shared/odds.py` (328), `config.py` (82), `domain/filter_evaluation.py` (118), `domain/filter_stats.py` (254), `domain/evaluation.py` (500), `evaluation_harness.py` (872), `run_evaluation.py` (143), `historical_dataset.py` (381) |
| Production never imports the evaluation layer | `git grep` for `evaluation_harness\|historical_dataset\|run_evaluation\|domain\.evaluation\|domain\.historical` across the production file set — single hit, `espn.py:36` (provider → contract) |
| Complete disk-write inventory | `git grep -E "open\(.*'w'\|json\.dump\(\|write_text\(\|writerow" -- '*.py'` excluding `tests/`, `research/` — 12 hits, all tabulated in 2G-F1 |
| Version constants | `git grep -E "^[A-Z_]*VERSION[A-Z_]* *=\|^ *model_version *= *\""` — 6 hits, all offline; zero in production |
| No ROI concept | `git grep -i -E "\broi\b\|bankroll\|stake\|settle\|payout\|profit"` — prose only (`GG.md:182`, `filters.py:11`) |
| `data/` absent | `ls -la data` → *No such file or directory* |
| Output artifact shape | `python3 -c "json.load(open('output_2026-01-18.json'))"` → 5 envelope keys, 17 per-record keys; `head -2 output_2026-01-18.csv` → 13 columns, **no `fixture_id`** |
| Committed artifacts lack newer fields | `grep -l "model_input_samples\|filter_outcome" output_*.json` → no matches |
| 39 records, all `NO BET`, all `odds: null` | `grep -o '"decision": "[^"]*"' output_*.json \| sort \| uniq -c` and the same for `"odds"` |
| `is_predictable` unwired | `git grep -n "is_predictable"` — defined `espn.py:191`; non-test callers only in `scripts/espn_diagnostic.py` |
| `league_avg_goals` unused in `process_fixture` | Full read of `main.py:57-219`; parameter never referenced in the body — model uses `model_inputs.league_avg_goals` (`main.py:141`) |
| `fixture_id` == `event_id` | `espn.py` fixture construction (`"fixture_id": event.get("id")`) vs `HistoricalMatch.event_id` from the same provider parsing |
| Tracked output artifacts | `git ls-files \| grep output_` → 5 files |

**No files were modified. No commits were made. No production code was
touched.** `git status --short` was clean at the start of this audit and the only
addition is this document.
