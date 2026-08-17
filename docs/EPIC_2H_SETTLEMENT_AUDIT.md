# Epic 2H — Prediction Settlement & Evaluation Audit

**Status:** AUDIT ONLY — nothing implemented, nothing approved
**Baseline commit:** `c88169d` (Epic 2G merged, `main`)
**Audit date:** 2026-08-17
**Scope:** answer one question — *what is the safest architecture for settling a stored prediction against the completed fixture result?*

**Files changed by this epic: 1**
- `docs/EPIC_2H_SETTLEMENT_AUDIT.md` — this document (new)

**No production code was modified.** `poisson.py`, `filters.py`, `decision.py`,
`config.py`, `main.py`, `espn.py`, `output.py`, `prediction_ledger.py`,
`domain/prediction_log.py`, `domain/evaluation.py` and all of `domain/` are
byte-identical to `c88169d`. `git status --short` was clean at the start and the
only addition is this document. No probability formula, threshold, filter rule or
recommendation rule was touched, and none needs to be touched to resolve anything
below.

**The headline answer is: YES — deterministically, for one identity space, with
six named unresolved states.**


Settlement is a dictionary lookup on the ESPN event id, not a join. The identity
is already shared: `prediction_ledger` writes `fixture_id` from
`espn.get_fixtures` (`espn.py:253`, `"fixture_id": event.get("id")`) and
`espn.get_league_history` writes `HistoricalMatch.event_id` from the same field
(`espn.py:1206,1238`). `domain/prediction_log.py:194-196` states this as a
contract:

> Identity. `fixture_id` is the ESPN event id, the same identity space as
> `domain.historical.HistoricalMatch.event_id`, so settlement is a lookup and
> never a team-name match (GG-008).

That claim is verified below and holds — with one qualification that matters
enough to be a finding: **the event id alone is the join key, but it is not the
uniqueness key.** See 2H-F2.

The genuinely hard part of Epic 2H is not the join. It is that a settlement
record must be able to say **"no result, and here is precisely why"** in six
distinguishable ways, and that the existing evaluation firewall makes the
obvious home for settlement — `domain/evaluation.py` — the one place it must not
live. See §2.

---

## Executive summary

| Question | Answer | Where |
|---|---|---|
| Can every ledger prediction be matched deterministically to a result? | ✅ **Yes**, on `fixture_id` == `event_id` | §1 |
| Is the identifier authoritative? | ✅ ESPN event id, single provider, no aliasing | 2H-F1 |
| Is it globally unique? | ⚠️ **Not asserted** — unique per `(competition, season, event_id)` | 2H-F2 |
| Should settlement live in `domain/evaluation.py`? | ❌ **No** — firewall violation | §2 |
| Should settlement be its own module? | ✅ **Yes** — contract + job, two files | §2 |
| Do metrics already exist? | ✅ **All of them.** Zero new metric code needed | §3 |
| Can settlement reuse the harness's replay path? | ❌ **No** — it would re-run the model | 2H-F6 |
| Can historical predictions be backfilled? | ❌ **No predictions exist to settle** | §6 |
| Can *settlement* be backfilled? | ✅ Yes — results are durable at ESPN | §6 |
| Is the ledger currently non-empty? | ❌ **`data/` does not exist** | 2H-R1 |

---

## 1. Can every ledger prediction be settled deterministically?

### 1.1 The authoritative identifier

**The ESPN event id, carried as `fixture_id` on the ledger record and as
`event_id` on the historical record. One provider, one field, no translation
layer.**

Traced end to end at `c88169d`:

```
  ESPN scoreboard payload
    event["id"]                          (a string, e.g. "740123")
        │
        ├── espn.py:253   get_fixtures()          → fixture["fixture_id"]
        │       │
        │       └── main.py:62  process_fixture() → result["fixture_id"]
        │               │
        │               └── domain/prediction_log.py:411
        │                     fixture_id=str(result["fixture_id"])
        │                       → data/predictions/YYYY-MM.jsonl  "fixture_id"
        │
        └── espn.py:1206  parse_scoreboard_history() → event_id
                │             event.get("id") or competition.get("id")
                └── espn.py:1238  event_id=str(event_id)
                      → HistoricalMatch.event_id
```

Both sides are coerced with `str()` at the contract boundary
(`domain/prediction_log.py:411`, `espn.py:1238`), so there is no int/str
mismatch of the GG-009 kind (`sportmonks.py:96`, where an int `league_id` is
tested against string dict keys and never matches).

**Why this is authoritative and not merely convenient:** it is the provider's own
primary key, and both readers take it from the same JSON field of the same
endpoint (`{ESPN_BASE_URL}/{league}/scoreboard`). There is no name normalisation,
no alias table, no fuzzy comparison anywhere on the path. This is the direct
opposite of GG-008, still open in the odds clients
(`docs/TECHNICAL_DEBT.md:445-451`), where `if home in api_home or api_home in
home` can attach `"Athletic"`'s odds to `"Athletic Club"`. **Settlement must not
acquire a team-name comparison at any point**, and it does not need one.

### 1.2 Are live fixture ids and historical event ids compatible?

**Yes. Same endpoint, same field, same identity space — verified, not assumed.**

| Property | Live (`get_fixtures`) | Historical (`get_league_history`) | Compatible? |
|---|---|---|---|
| Endpoint | `/{league}/scoreboard` (`espn.py:229`) | `/{league}/scoreboard` (`espn.py:1282`) | ✅ identical |
| Id field | `event.get("id")` (`:253`) | `event.get("id") or competition.get("id")` (`:1206`) | ✅ same primary field |
| Type at rest | `str` (`prediction_log.py:411`) | `str` (`espn.py:1238`) | ✅ |
| Query param | `dates=YYYYMMDD` (`:226`) | `dates=YYYYMMDD-YYYYMMDD` (`:1288`) | ✅ same param, different width |
| Competition | `league_code` from `ALLOWED_LEAGUES` | `league_code` argument | ✅ same `eng.1` slug space |
| Season | `espn.resolve_season` (`:154`) | event's own `season.year` (`:1240`) | ⚠️ see 2H-F3 |

The one asymmetry worth stating plainly: the historical path applies
`classify_event_season` (`espn.py:1165`) and a `uid` league cross-check
(`espn.py:1174-1177`) before accepting an event, and **the live path applies
neither**. A live fixture is accepted purely because it appeared in the response
for a requested league and date. This is not a defect in either path — they
answer different questions — but it means the *set* of fixtures the ledger can
contain is slightly wider than the set the historical reader will return. That is
the origin of 2H-F3 and of the `FIXTURE_NOT_FOUND` unresolved state.

### 1.3 Collision risks

**No collision risk on the join key itself. Three real risks nearby.**

| Risk | Real? | Evidence | Consequence |
|---|---|---|---|
| Two different fixtures sharing an event id | **No** | ESPN's own primary key; `espn.py:1306-1309` de-duplicates on it across overlapping discovery windows | — |
| Same fixture, two ledger records | **Yes, by design** | `prediction_ledger.py:92-102` — `new_prediction_id()` is random, *not* a content hash, so a re-run of the same date writes a second record | Settlement must be **per `prediction_id`**, never per `fixture_id`, or n predictions collapse to one settlement |
| Event id not unique across competitions | **Unproven** | `domain/historical.py:478` keys duplicates as `f"{competition}:{season}:{event_id}"` — the codebase never asserts bare-id uniqueness | Lookup must be keyed on the composite, not the bare id — see 2H-F2 |
| Same pairing twice in a season (replay of a postponed fixture) | **Yes, real** | `domain/historical.py:483-499`: *"eng.2 keeps the original row of a postponed fixture alongside its replay. Both are real history with distinct event ids"* | The original settles UNKNOWN forever; the replay is a **different fixture** the ledger may never have predicted |

The last row is the subtle one. A postponed fixture that is later replayed does
**not** transfer its result to the original event id. The original event id stays
`completed=False` with both scores `None` at ESPN indefinitely. A settlement job
that retries unresolved records forever will retry that one forever. This is why
the contract needs a terminal unresolved state and a retry bound (§4.4).

---

## 2. Where should settlement live?

**Recommendation: (b) a separate settlement module — split into a pure contract
and an IO job, mirroring the 2G split exactly.**

```
domain/settlement.py     PURE CONTRACT — no IO, no network, no clock
settle_predictions.py    THE JOB — reads ledger, fetches results, appends
```

### 2.1 Why not `domain/evaluation.py` (option a)

Three independent reasons, any one of which is sufficient.

**Reason 1 — it would breach an enforced firewall, in the direction the firewall
was built to stop.**

`tests/regression/test_evaluation_leakage.py:26-30` hardcodes
`EVALUATION_MODULES` as exactly `domain/evaluation.py`, `evaluation_harness.py`,
`run_evaluation.py`, and `test_no_odds_or_decision_imports` (`:67`) AST-parses
each for forbidden imports. `tests/regression/test_ledger_isolation.py:131`
closes the other direction:

> `test_the_evaluation_layer_cannot_import_the_ledger` — *"The ledger
> legitimately records prices and the recommendation, so importing the ledger
> would hand the evaluator a price-bearing object and defeat the firewall without
> ever naming a forbidden module."*

Settlement must read the ledger. If settlement lived in `domain/evaluation.py`,
that module would import `prediction_ledger`, and
`test_the_evaluation_layer_cannot_import_the_ledger` fails — correctly. The test
is not an obstacle to work around; it is the design telling us where the module
goes.

**Reason 2 — `domain/evaluation.py` is documented as never touching a provider.**

Its docstring (`:31-32`): *"This module performs NO model mathematics. It never
calls a model; it receives probabilities and grades them."* It receives an
already-known `BttsOutcome`. Settlement's entire job is the step *before* that:
going to a provider and finding out what the outcome was. Putting a network call
into the referee makes the referee's own metrics depend on ESPN's availability.

**Reason 3 — the roles are genuinely different.**

`domain/evaluation.py` answers *"how good was this probability?"* Settlement
answers *"what happened?"* The 2G audit already separated these deliberately
(`docs/EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md:751-755`):

> **`outcome` is deliberately NOT on the prediction record.** A prediction is
> immutable; the result is a separate later fact written to a separate file.

### 2.2 Why not "another layer" (option c)

Considered and rejected:

| Alternative | Why not |
|---|---|
| Inside `prediction_ledger.py` | The ledger is the *writer of predictions*. `tests/unit/test_prediction_ledger.py:127` (`test_every_open_call_appends_or_reads`) AST-checks that **every** `open()` mode in that module starts with `a` or `r`. Adding a second artifact type to a module whose entire tested identity is "one append-only prediction log" muddies the one guarantee it exists to make. |
| Inside `espn.py` | `espn.py` is a provider adapter. It must not know that a ledger exists. Settlement would invert the dependency and put `data/` IO into the provider. |
| Extend `evaluation_harness.py` | It is inside `EVALUATION_MODULES` (same firewall as above) and its `replay()` **re-runs the model** (`:545-620`). See 2H-F6. |
| A `settlement/` package | Five leagues, ~10 fixtures/day. Two modules is the honest size; a package is premature structure. |

### 2.3 The recommended shape, and why it mirrors 2G

```
                    ┌──────────────────────────────────────┐
                    │ UNCHANGED                            │
                    │ poisson · filters · decision · config│
                    │ main · output · prediction_ledger    │
                    └──────────────────────────────────────┘
                                     │ (read-only)
   ┌─────────────────────────────────┼──────────────────────────┐
   │                                 ▼                          │
   │  data/predictions/YYYY-MM.jsonl        espn.get_league_history
   │         │  prediction_ledger.load_records()      │  (existing, unchanged)
   │         │                                        │
   │         └──────────────┬─────────────────────────┘
   │                        ▼
   │   ①  domain/settlement.py        PURE CONTRACT
   │       SettlementRecord · SettlementStatus · UnresolvedReason
   │       settle_one(prediction, match, *, settled_at) → SettlementRecord
   │       to_json_dict() with fixed key order
   │       imports: domain.evaluation.btts_outcome ONLY
   │                        │
   │                        ▼
   │   ②  settle_predictions.py       THE JOB (CLI, offline, not imported by main)
   │       group unsettled by (competition, season) → fetch → lookup → append
   │       → data/settlements/YYYY-MM.jsonl        APPEND-ONLY
   └───────────────────────────────────────────────────────────────┘
```

**The critical import direction:** `domain/settlement.py` imports
`domain.evaluation.btts_outcome`. The reverse never happens.
`domain/evaluation.py` stays unaware that settlement exists, so the firewall
tests keep passing unmodified, and `btts_outcome`'s hard-won semantics — *"A
missing score, or a fixture that never completed, yields UNKNOWN. It does NOT
yield NO"* (`domain/evaluation.py:120-122`) — are reused rather than
reimplemented.

**Why the contract/job split rather than one file:** it is exactly the 2G split
(`domain/prediction_log.py` pure, `prediction_ledger.py` does IO), and it earns
the same property: `settle_one` becomes a pure function of
`(prediction, match, settled_at)`, so every settlement outcome — including all
six unresolved states — is unit-testable with no network and a pinned clock.
`tests/regression/test_ledger_isolation.py:84`
(`test_the_contract_is_pure_data`) is the template for enforcing it.

---

## 3. Existing evaluation capabilities

### 3.1 What already exists — the answer is "everything"

**Epic 2H needs to write zero metric code.** Every number the definition of done
asks for is implemented, tested, and consumes only `PredictionRecord`.

| Capability | Symbol | Location | Consumes |
|---|---|---|---|
| BTTS grading | `btts_outcome` | `domain/evaluation.py:107` | `(home_goals, away_goals, completed=)` |
| Outcome enum incl. UNKNOWN | `BttsOutcome` | `domain/evaluation.py:77` | — |
| Unevaluable taxonomy | `UnevaluableReason` | `domain/evaluation.py:91` | — |
| Brier score | `brier_score` | `domain/evaluation.py:302` | `PredictionRecord` |
| Log loss | `log_loss` | `domain/evaluation.py:322` | `PredictionRecord` |
| Calibration bins | `calibration_table` | `domain/evaluation.py:355` | `PredictionRecord` |
| Quality + coverage together | `summarise` / `MetricSummary` | `domain/evaluation.py:403` / `:268` | `PredictionRecord` |
| ROC AUC | `roc_auc` | `domain/discrimination.py:147` | `PredictionRecord` |
| Constant-predictor benchmark | `constant_predictor_brier` | `domain/discrimination.py:210` | `PredictionRecord` |
| Prediction spread | `prediction_spread` | `domain/discrimination.py:183` | `PredictionRecord` |
| Discrimination summary | `summarise_discrimination` | `domain/discrimination.py:256` | `PredictionRecord` |
| Bootstrap CIs | `BootstrapInterval`, `paired_auc_delta`, `paired_brier_delta` | `domain/discrimination.py:287,472,494` | `PredictionRecord` |
| Group breakdowns | `auc_by_group` | `domain/discrimination.py:523` | `PredictionRecord` |
| Breakdown by key | `EvaluationRun.breakdown` | `evaluation_harness.py:711` | `PredictionRecord` |
| Deterministic artifacts | `write_artifacts` | `evaluation_harness.py:806` | `EvaluationRun` |

### 3.2 What can be reused, exactly

**Reusable without modification — the whole metric layer.** Everything in the
table above depends on `PredictionRecord` and nothing else. It does not care
whether the probability came from a replay or from a stored ledger line. So the
reporting phase's only real work is an **adapter**:

```
(ledger record + settlement record)  →  domain.evaluation.PredictionRecord
```

Field-by-field, that adapter is total — every required field is present:

| `PredictionRecord` field | Source | Available? |
|---|---|---|
| `model_id`, `model_version` | ledger `provenance.model_id/model_version` | ✅ `prediction_log.py:52-53` |
| `competition` | ledger `competition` | ✅ |
| `season` (`int`) | ledger `season` | ⚠️ nullable — see 2H-F3 |
| `event_id` | ledger `fixture_id` | ✅ |
| `kickoff` (tz-aware, required) | ledger `kickoff` | ⚠️ nullable — see 2H-F4 |
| `home_team_id`, `away_team_id` | ledger | ✅ |
| `outcome` | **settlement** `gg_outcome` | ✅ this Epic |
| `probability` | ledger `probability` | ✅ |
| `unevaluable_reason` | derived from ledger `status` + settlement status | ✅ mapping in §4.3 |
| `home_sample`, `away_sample`, `league_sample` | ledger | ✅ |
| `history_matches` | — | ❌ not captured by 2G; `0` is the honest default |

**Reusable with care:** `btts_outcome` — reuse it, but note it takes
`(home_goals, away_goals, completed=)` and *not* a `HistoricalMatch`. Settlement
must pass `completed=match.completed` explicitly. Passing the default
`completed=True` with a `None` score still yields UNKNOWN (`:128-129`), so the
failure mode is safe — but stating it explicitly is what makes the intent
reviewable.

### 3.3 What would create leakage

Five specific reuses that look attractive and are wrong:

| Do not reuse | Why it leaks or breaks |
|---|---|
| **`evaluation_harness.replay()`** (`:545`) | It **re-runs the model**: it builds `history = matches_before(pool, target.kickoff)` and calls `model.predict()`. A settlement job that called it would produce a *fresh* probability from *today's* data and could silently substitute it for the stored one. That is the 2G-R1 fabrication failure in a new place — a number with a plausible schema and no provenance. Settlement must **only read** `probability` from the ledger line. |
| **`PoissonV1Adapter`** (`:262`) | Same reason, one level down. Settlement must never import `poisson`. |
| **`domain/historical.matches_before`** (`:418`) | A point-in-time *feature* cutoff. Settlement is deliberately **post-kickoff** — it is the only component in the repo allowed to look after kickoff. Importing a leakage guard into the one place leakage is legitimate would confuse both. |
| **`model_dataset()`** (`:397`) | Filters to `ELIGIBLE and has_result`. Settlement needs the **unfiltered** set, because a fixture with no result is exactly the case it must record as UNRESOLVED rather than skip. |
| **Deriving the outcome from anything but the score** | `btts_outcome` is the single chokepoint. Any second derivation reintroduces the `None`→`NO` collapse that `:120-122` exists to prevent. |

**One more, in the other direction:** the reporting module must not emit Brier
without AUC and the constant-predictor benchmark. That is 2G-R5
(`docs/EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md:809`), and with POISSON_V1's
measured AUC ≈ 0.54 it is not hypothetical: Brier alone would look respectable
while the ranking carries almost no skill.

### 3.4 Does anything already read the ledger?

**No. Definitively.** `domain/evaluation.py`, `evaluation_harness.py` and
`run_evaluation.py` are forbidden from importing it
(`test_ledger_isolation.py:131`) and do not. `prediction_ledger.load_records`
(`:285`) currently has **no non-test caller**. It was written in 2G specifically
so that settlement would have a reader, and returns raw dicts on purpose:

> Dicts rather than `LedgerRecord`s on purpose: a reader must be able to load
> records written under an OLDER `schema_version` without this module refusing
> them. — `prediction_ledger.py:290-297`

That is a deliberate gift to this Epic and should be used as-is.

---

## 4. The settlement contract

### 4.1 Required fields

Every field the task names, plus the four that make the record auditable.
`settlement_id` and `ledger_schema_version` are additions I am proposing, each
justified below.

| Field | Type | Required | Why |
|---|---|---|---|
| `settlement_id` | `str` (uuid hex) | ✅ | One id per settlement attempt. Mirrors `prediction_id`; makes a re-settlement a new observation rather than an ambiguous duplicate |
| `prediction_id` | `str` | ✅ | **The join key.** Settlement is per *prediction*, not per fixture — two re-run predictions of one fixture are two predictions and must be two settlements |
| `fixture_id` | `str` | ✅ | The ESPN event id. Denormalised deliberately so a settlement line is readable alone |
| `competition` | `str` | ✅ | Half of the composite lookup key (2H-F2) |
| `season` | `Optional[int]` | ✅ (nullable) | Other half. Nullable because the ledger's is (2H-F3) |
| `home_goals` | `Optional[int]` | ✅ | **Final score.** `None` unless `SETTLED` |
| `away_goals` | `Optional[int]` | ✅ | as above |
| `gg_outcome` | `str` | ✅ | `YES` / `NO` / `UNKNOWN`, from `btts_outcome` only |
| `status` | `str` | ✅ | `SETTLED` / `UNRESOLVED` — see 4.2 |
| `unresolved_reason` | `Optional[str]` | ✅ | One of six named reasons; `None` iff `SETTLED` — see 4.4 |
| `settled_at` | `datetime` (tz-aware UTC) | ✅ | **Settlement timestamp.** Injectable, never `now()` inside the contract |
| `source` | `str` | ✅ | **Settlement source.** `"espn/scoreboard"` today; a second provider must be distinguishable, not inferred |
| `provider_status` | `Optional[str]` | ✅ | ESPN's raw status name (`STATUS_FULL_TIME`, `STATUS_POSTPONED`, …), verbatim. The evidence behind the verdict |
| `attempt` | `int` | ✅ | Which settlement pass this was. Makes the retry history legible without a second file |
| `schema_version` | `str` | ✅ | `"2h.1"`. Never reused from `LEDGER_SCHEMA_VERSION` or `EVALUATION_SCHEMA_VERSION` |
| `ledger_schema_version` | `str` | ✅ | The version of the record being settled. A settlement of a `2g.1` prediction must stay identifiable after the ledger bumps |
| `code_revision` | `Optional[str]` | ✅ | Reuse `prediction_ledger.code_revision()`. `None`, never a guess |

**Deliberately absent:** `probability`, `odds`, `edge`, `recommendation`, `brier`,
`roi`, `stake`, `won`, `payout`. A settlement record states **what happened**.
Copying the probability in would create a second copy of a ledger field that
could drift from the first; computing a per-prediction score in would put metric
logic outside `domain/evaluation.py`; and any of the money words would breach
LEAK-001 and 2G-R4.

### 4.2 Two statuses, six reasons — and why not one enum

```
SETTLED      a trustworthy final score exists; gg_outcome is YES or NO
UNRESOLVED   no trustworthy final score; gg_outcome is UNKNOWN
```

`status` and `gg_outcome` are separate fields even though today they are
perfectly correlated. Collapsing them would mean a reader could not distinguish
*"we have a result and it was 0-0"* (`SETTLED` / `NO`) from *"we have no
result"* (`UNRESOLVED` / `UNKNOWN`) without knowing the encoding — which is
precisely the GG-002 boolean-collapse defect that
`domain/prediction_log.py:64-68` cites as the reason `FILTER_OUTCOMES` has three
values instead of two.

### 4.3 Invariants the contract must enforce in `__post_init__`

Following the house pattern of enforcing rather than trusting
(`LedgerRecord.__post_init__`, `HistoricalMatch.__post_init__`):

| Invariant | Rationale |
|---|---|
| `status == SETTLED` ⟺ `unresolved_reason is None` | A record is settled or it says why not. Never both, never neither |
| `status == SETTLED` ⟹ both goals `is not None` | The `HistoricalMatch:246-250` rule: *"Zero is never substituted for an unknown result"* |
| `status == UNRESOLVED` ⟹ both goals `is None` **and** `gg_outcome == UNKNOWN` | A partial score from an abandoned match is not a result |
| `gg_outcome` recomputed from the goals must equal the stored value | Makes the stored outcome a checkable derivation, not a claim |
| `settled_at.tzinfo is not None` | GG-014. Naive datetimes compare against local time |
| `prediction_id`, `fixture_id`, `source` non-empty | Same as `LedgerRecord:257-262` |
| goals are non-negative non-bool `int` | `True > 0` is True in Python (`evaluation.py:130-132`) |

### 4.4 Unresolved states — six, each terminal or retryable

This is the load-bearing part of the contract. Six reasons, because collapsing
any two of them destroys information the operator needs.

| `unresolved_reason` | Meaning | ESPN evidence | Retry? |
|---|---|---|---|
| `NOT_YET_PLAYED` | Kickoff is in the future, or the match is in progress | `state` ∈ {`pre`,`in`}, not in `_NOT_PLAYABLE` | ✅ retry |
| `POSTPONED` | Will not be played as scheduled | `STATUS_POSTPONED` (`espn.py:188`) | ✅ bounded |
| `CANCELLED` | Will not be played at all | `STATUS_CANCELED` / `STATUS_CANCELLED` | ⛔ **terminal** |
| `ABANDONED` | Started, not completed; any partial score is not a result | `STATUS_ABANDONED` | ⛔ **terminal** |
| `FIXTURE_NOT_FOUND` | Provider returned the league-season but not this event | id absent from the readout | ✅ bounded |
| `PROVIDER_UNAVAILABLE` | We could not ask | `get_league_history` returned `None` | ✅ retry |

**Why `FIXTURE_NOT_FOUND` and `PROVIDER_UNAVAILABLE` must not merge:** the first
means ESPN answered and does not have the fixture (a data question — possibly
2H-F3, a season mismatch). The second means ESPN did not answer (an operations
question). `espn.py:1279-1280` already makes exactly this distinction load-bearing:

> None means "we do not know". An empty match list means "ESPN has nothing here",
> which is a real and different answer.

Merging them would make an outage indistinguishable from a systematic join
failure — and the systematic join failure is the one that silently shrinks
coverage.

**Why `POSTPONED` is retryable but bounded:** a postponed fixture's event id is
*not* updated with the replay's result. `domain/historical.py:488-492` records
that eng.2 keeps both rows with distinct event ids. So the original settles
`UNRESOLVED/POSTPONED` permanently, and an unbounded retry loop revisits it every
run forever. The `attempt` field plus an explicit bound is what stops the job's
cost growing monotonically with season length.

### 4.5 Storage

`data/settlements/YYYY-MM.jsonl`, append-only, keyed by the month of
**settlement**, matching `prediction_ledger.ledger_filename` semantics
(`:142-144`) and the layout the 2G audit already named
(`EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md:954`).

**A settlement is never written into a prediction file, and a settlement line is
never rewritten.** Two guarantees, one reason: `LedgerRecord` is documented as
having no `outcome` field precisely so that grading cannot destroy the audit
trail (`prediction_log.py:180-184`). A settlement file that mutated in place
would reintroduce the same defect one directory over. A corrected settlement is
a **new line** with a new `settlement_id` and a higher `attempt`; the reader
resolves by taking the latest per `prediction_id`.

---

## 5. Edge cases

All twelve, each with its evidence and its required handling. The seven the task
names are marked ★.

| # | Edge case | Provider reality | Required handling |
|---|---|---|---|
| E1 ★ | **Postponed fixture** | `STATUS_POSTPONED` still reports `state == "pre"` (`espn.py:186-188`). `parse_scoreboard_history` **keeps** it with `completed=False`, both goals `None` (`:1231-1233`) | `UNRESOLVED / POSTPONED`. Retry bounded. Never `NO` |
| E2 ★ | **Abandoned match** | Reports `state == "post"` and may carry a partial score. Excluded by `_is_completed_event` because `STATUS_ABANDONED` ∉ `_COMPLETED_STATUS_NAMES` (`:505-525`); goals then forced to `None` (`:1231-1233`) | `UNRESOLVED / ABANDONED`, **terminal**. The partial score must never be graded |
| E3 ★ | **Missing ESPN result** | Two distinct causes: `get_league_history` returns `None` (provider failed, `:1289-1290`) **or** returns a readout without the id | `PROVIDER_UNAVAILABLE` vs `FIXTURE_NOT_FOUND` — never merged (§4.4) |
| E4 ★ | **Cancelled fixture** | `STATUS_CANCELED` **and** `STATUS_CANCELLED` — both spellings are in `_NOT_PLAYABLE` (`:188`) | `UNRESOLVED / CANCELLED`, **terminal**. Match on both spellings |
| E5 ★ | **Duplicate fixture ids** | Not possible from one league-season: `espn.py:1306-1309` de-dupes on `event_id`. **But** GG-015 (`TECHNICAL_DEBT.md:862+`) is open: nothing de-dupes fixtures at the entry point, so a fixture in two whitelisted competitions is processed twice | Lookup keyed `(competition, season, event_id)`. If two ledger records share a `fixture_id`, settle **each `prediction_id` separately** |
| E6 ★ | **Prediction made after kickoff** | Real today. `main.py:245` iterates every fixture; `espn.is_predictable` (`:191`) exists but **has no production caller** — GG-013 remains "available but not yet used" (`TECHNICAL_DEBT.md:776-779`) | Settle it normally, then **flag it**. Compare ledger `kickoff` to `created_at`. Excluding it silently would hide the defect's frequency; grading it silently would inflate every metric |
| E7 ★ | **Re-runs of the same day** | Two ledger records, two `prediction_id`s, one shared `run_id` each (`prediction_ledger.py:20-24`) | Two settlement records. Reporting must **de-duplicate by policy** (e.g. first per fixture) and state which policy it used |
| E8 | **Re-running settlement itself** | The job is idempotent only if it checks what is already settled | Read existing settlements, skip terminal ones. Appending a duplicate `SETTLED` line is not corruption (append-only, latest wins) but it wastes requests and muddies `attempt` |
| E9 | **Scoreboard 100-event truncation** | Verified live: ESPN silently truncates at 100 events. `get_league_history` refuses a possibly-truncated season and returns `None` (`espn.py:1291-1297`) | Surfaces as `PROVIDER_UNAVAILABLE`, which is correct: a truncated season is *unknown*, not *empty* |
| E10 | **Season boundary / rollover** | `resolve_season` rolls over in July (`espn.py:169-171`, `EUROPEAN_SEASON_ROLLOVER_MONTH = 7`). A prediction stores the *resolved* season; history stores the *event's own* `season.year` (`:1240`) | If they disagree the lookup misses → `FIXTURE_NOT_FOUND`. Mitigation: fall back to fetching the adjacent season before concluding not-found (2H-F3) |
| E11 | **Score is `0-0`** | A real goalless draw | `SETTLED` / `gg_outcome = NO`. This is the single most important case not to conflate with E1–E4 (`evaluation.py:120-122`) |
| E12 | **Refused prediction (`status != SCORED`)** | The ledger records refusals deliberately (`prediction_ledger.py:262-265`) | **Still settle it.** The result is a fact about the fixture regardless of whether the model spoke. This is what makes *coverage* measurable: `NO_TEAM_STATS` on a match that finished 2-1 is a missed opportunity, and only settlement can show that |

### 5.1 The three that would silently corrupt metrics

E2, E6 and E11 share a property: each has a plausible-looking wrong handling that
no test would catch unless written for it.

- **E2 (abandoned):** a 1-0 abandonment graded as `NO` is a fabricated
  observation. Protected today only because `_is_completed_event` requires all
  three signals to agree (`espn.py:521-525`). Settlement must not weaken that by
  reading goals directly from a payload.
- **E6 (post-kickoff prediction):** these are not forecasts. The model's inputs
  already contain the result (GG-013's original wording: *"predicting a known
  outcome"*). If they are graded indistinguishably from genuine forecasts, every
  aggregate is optimistically biased by an unknown amount. **The ledger already
  has the fields to detect this** — `kickoff` and `created_at` — so this is a
  reporting flag, not new capture.
- **E11 (`0-0`):** the one case where `NO` is correct and `UNKNOWN` is wrong.
  Getting E1–E4 right by defaulting everything to `UNKNOWN` while accidentally
  catching `0-0` in the same net would destroy roughly a tenth of all
  observations.

---

## 6. Historical backfill feasibility

### 6.1 Can existing historical datasets settle old predictions?

**Technically yes; practically there is nothing to settle.** The two halves of
that answer must not be merged.

The **mechanism** works: `historical_dataset.load_dataset(out_dir)`
(`historical_dataset.py:235`) reads `data/historical/{league}_{season}.jsonl`
into `HistoricalMatch` objects carrying `event_id`, `completed`, `home_goals`,
`away_goals` — everything settlement needs, offline, checksummed
(`file_checksum`, `:201`), with a `manifest.json` (`:321`). A settlement job
could read from disk instead of the network and produce identical results.

The **data** does not exist:

```
$ ls -la data/
ls: data/: No such file or directory
```

`data/` is gitignored in full (`.gitignore`, Epic 2G block: *"Ignored
deliberately. This is OPERATIONAL evidence … not source"*). So on this checkout
there is **no historical dataset and no ledger**. Both are buildable — the
historical one by `python historical_dataset.py --seasons ...` — but neither is
present.

**Recommendation:** support both sources, prefer the local dataset when present.

| Source | Pros | Cons |
|---|---|---|
| `espn.get_league_history` (live) | Always current; no build step; already used by 2G's design | Network-dependent; subject to E9 truncation; slower |
| `historical_dataset.load_dataset` (local) | Offline, checksummed, reproducible, byte-stable | Needs a build; goes stale for the current season |

Making the result source an injected argument keeps `settle_one` pure and testable
either way — and the `source` field in the contract (§4.1) is what records which
one actually answered.

### 6.2 Are historical predictions available?

**No. None. This is the hard limit on Epic 2H, and it is not fixable.**

The 2G lifecycle audit's headline was that *"every prediction the system has ever
produced is unrecoverable … a prediction is never stored as a prediction"*
(`EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md:19-24`). Epic 2G fixed that going
forward. It did not — and could not — recover the past.

What exists instead:

| Artifact | Contains | Usable to settle? |
|---|---|---|
| `output_*.json` (5 committed) | `fixture_id`, `gg_probability`, `decision` | ❌ **No** — see below |
| `output_*.csv` | 13 columns, **no `fixture_id`** (`:981`) | ❌ Unjoinable by construction (2G-R2) |
| `data/predictions/*.jsonl` | Real ledger records | ✅ — but the directory does not exist yet |

**Why the committed `output_*.json` files must not be settled**, even though they
carry `fixture_id` and `gg_probability`:

1. **No `created_at`.** There is no way to know whether the prediction preceded
   kickoff (E6). Settling them assumes the thing most in doubt.
2. **No provenance.** No `model_version`, no `config_fingerprint`, no
   `code_revision`. `EPIC_2G_PREDICTION_LIFECYCLE_AUDIT.md:982` verifies these
   artifacts predate the newer fields entirely.
3. **No `prediction_id`.** The settlement contract's join key does not exist, so
   a synthetic one would have to be invented — indistinguishable afterwards from
   a genuine one.
4. **They are all `NO BET` with `odds: null`** (`:983`). Even settled, they
   carry no recommendation signal.

Settling them would produce records that **look** like the real thing and carry
none of the provenance that makes the real thing trustworthy. That is 2G-R1's
fabrication failure — *"data fabrication with a plausible schema"*
(`:925`) — arriving through the back door.

### 6.3 Are we limited to future ledger records only?

**Yes, for predictions. No, for settlement. The asymmetry is the whole point.**

```
  PREDICTION   forward-only, permanently.  A prediction not recorded when it was
               made cannot be reconstructed: it depended on provider state that
               no longer exists.

  SETTLEMENT   backfillable, indefinitely. The result is a durable fact ESPN will
               still report years later, and it does not depend on when we ask.
```

Stated in 2G as a rule (`:925`): *"Settlement may be backfilled; prediction may
not."* And (`:864-866`): *"it can lag it safely. Results remain available from
ESPN indefinitely, so settlement is the one part of the lifecycle that can be
backfilled."*

**Two practical consequences for Epic 2H:**

1. **Settlement has no deadline.** Unlike 2G-2, delay costs nothing. There is no
   pressure to ship it half-designed.
2. **Settlement is bounded by the ledger's start date, and the ledger is empty.**
   Which means the first useful settlement run cannot happen until the ledger has
   accumulated fixtures — and any metric over the first few days would be noise
   (GG-028 measured Brier 0.4241 at n=1–2). See 2H-R1.

---

## 7. Findings

Eight findings. Each is a verified statement about `c88169d`, not a proposal.

### 2H-F1 — The identity space is already shared, and settlement is a lookup
**Severity:** ✅ enabling finding
`fixture_id` (ledger) and `event_id` (historical) both originate at
`event["id"]` from the same ESPN scoreboard endpoint, both coerced to `str`.
Settlement therefore needs **no** team-name matching, no alias table and no fuzzy
comparison — the GG-008 class of defect cannot enter through this door. This is
the single most important precondition for Epic 2H and it is already satisfied.

### 2H-F2 — The event id is the join key but not a proven uniqueness key
**Severity:** 🟡 MEDIUM — design constraint
Nothing in the repo asserts that an ESPN event id is unique across competitions.
The evidence points the other way: `domain/historical.py:478` deliberately keys
duplicate detection as `f"{competition}:{season}:{event_id}"`, i.e. the codebase's
own uniqueness unit is the **composite**. Settlement lookups must be keyed
`(competition, season, event_id)`. A bare-id dictionary would work in almost all
cases and fail silently in the one that matters.

### 2H-F3 — Season resolution differs between the write and read paths
**Severity:** 🟡 MEDIUM — the most likely cause of systematic settlement misses
The ledger stores the season from `espn.resolve_season` (a July-rollover
calculation over the fixture's kickoff, `espn.py:169-171`). The historical reader
stores the season from the **event's own** `season.year` field (`espn.py:1240`).
These normally agree. Around the rollover, and for competitions whose season
labelling differs from the European convention, they can differ by one. Because
`get_league_history` is fetched **per league-season**, a one-year disagreement
means the fixture is fetched from the wrong season and reported as
`FIXTURE_NOT_FOUND` — a data-shaped failure that looks like a provider gap.
Mitigation: on a miss, retry the adjacent season before concluding not-found, and
record which season actually answered.

### 2H-F4 — `kickoff` and `season` are nullable on the ledger, required by `PredictionRecord`
**Severity:** 🟡 MEDIUM — affects the reporting adapter, not settlement itself
`LedgerRecord.kickoff` is `Optional[datetime]` (it is `None` when
`espn.parse_kickoff` cannot parse, which is correct — GG-014's *"an unparseable
value returns `None` rather than a wrong instant"*). But
`domain/evaluation.PredictionRecord` requires a tz-aware `kickoff` and an `int`
season. So the adapter in the reporting phase must decide what to do with a
`None`. The honest answer is to exclude it with a named `unevaluable_reason`
rather than substitute a value — but it must be **decided**, not discovered at
runtime.

### 2H-F5 — Settlement per `prediction_id`, not per `fixture_id`
**Severity:** 🟡 MEDIUM — a design error that would be invisible once made
`new_prediction_id()` (`prediction_ledger.py:92-102`) is random rather than a
content hash, *specifically* so that a re-run is "distinguishable, not
duplicated" (`tests/unit/test_prediction_ledger.py:113`). A settlement keyed on
`fixture_id` would therefore collapse n predictions of one fixture into one
settlement and silently discard the re-run evidence 2G went out of its way to
preserve.

### 2H-F6 — The evaluation harness's replay path must not be reused
**Severity:** 🔴 HIGH if violated
`evaluation_harness.replay()` (`:545`) computes `history = matches_before(pool,
target.kickoff)` and calls `model.predict()`. It **generates** probabilities. A
settlement job that reached for it — a natural instinct, since it already pairs
fixtures with outcomes — would produce a fresh probability from today's data
alongside the stored one, with nothing in the file to distinguish them.
Settlement must read `probability` from the ledger line and never compute one.

### 2H-F7 — Post-kickoff predictions are currently possible and already detectable
**Severity:** 🟡 MEDIUM — pre-existing (GG-013), newly *measurable*
`espn.is_predictable()` exists (`espn.py:191`), is tested, and has **no
production caller** — `main.py:245` iterates every fixture returned. So finished
and postponed matches can reach the model today. Epic 2H does not fix this (it
would change which fixtures are processed — explicitly out of scope), but the
ledger records both `kickoff` and `created_at`, so settlement/reporting can
**count** it for the first time. That count is the evidence needed to justify
fixing GG-013 later.

### 2H-F8 — The entire metric layer is reusable unchanged
**Severity:** ✅ enabling finding
Brier, log loss, calibration, coverage, AUC, prediction spread, the
constant-predictor benchmark, bootstrap CIs and group breakdowns are all
implemented and consume only `PredictionRecord`. Epic 2H's reporting phase needs
an adapter, not arithmetic. Any new metric function written during this Epic
should be treated as a review failure.

---

## 8. Risks

| ID | Risk | Sev | Why it is real here | Mitigation |
|---|---|---|---|---|
| 2H-R1 | **Nothing to settle yet** — `data/` does not exist | 🔴 HIGH | Verified: `ls data/` → no such file. The ledger has never run. A settlement job shipped now would be exercised only by its own tests, and its first real-world contact would be its first debugging session | Build the ledger first; require a minimum-n gate before any aggregate is printed |
| 2H-R2 | **A fabricated result** — inventing `0-0` for a missing score | 🔴 HIGH | The exact defect `HistoricalMatch:246-250` and `btts_outcome:120-122` were written to prevent. In settlement it would be *worse*: a fabricated observation contaminates the metric permanently | `status == SETTLED ⟹ goals not None`, enforced in `__post_init__`; `UNKNOWN` is the only outcome an unresolved record may carry |
| 2H-R3 | **Re-running the model at settlement time** | 🔴 HIGH | 2H-F6. Would silently replace the published probability with a hindsight one | Add `settlement` to the AST-forbidden-import set alongside the ledger: no `poisson`, `filters`, `decision`, `evaluation_harness` |
| 2H-R4 | **Metrics before settlement is trustworthy** | 🔴 HIGH | 2G's own instruction: *"Do not build until settlement exists: anything that reports accuracy, Brier or ROI"* (`EPIC_2G_PREDICTION_LEDGER.md:218-222`) | Keep settlement and reporting in **separate epics**. 2H settles; a later epic reports |
| 2H-R5 | **Silent coverage collapse via 2H-F3** | 🟡 MED | If the season disagrees, *every* fixture in that league-season reports `FIXTURE_NOT_FOUND`. Looks like a provider gap; is actually a join bug | The job must print unresolved counts **by reason and by league-season**. A 100% not-found league-season is a bug signature, not a data gap |
| 2H-R6 | **Collapsing the six unresolved reasons** | 🟡 MED | The pressure to simplify to a single `UNRESOLVED` is real, and it destroys the ability to tell an outage from a join failure from a cancelled match | Enum in the pure contract; one unit test per reason |
| 2H-R7 | **Unbounded retries on terminal states** | 🟢 LOW | A cancelled fixture never resolves. Without a terminal classification the job's cost grows with season length | `CANCELLED` / `ABANDONED` terminal; `attempt` bound on the rest |
| 2H-R8 | **The odds firewall re-opening through settlement** | 🟡 MED | `test_ledger_isolation.py:131` stops the evaluator importing the ledger. A settlement module that carried `odds` forward into an evaluation-visible artifact would route around that test without naming a forbidden module | Settlement record carries **no** price, edge, recommendation or stake field (§4.1) |
| 2H-R9 | **Mutating a settlement in place** | 🟡 MED | The instinct on a corrected result is to overwrite. That destroys the record that we once believed otherwise | Append-only; new `settlement_id`, higher `attempt`; reader takes latest per `prediction_id` |
| 2H-R10 | **Scope creep into GG-013 / GG-015** | 🟡 MED | Both are genuinely adjacent and both change **which fixtures are processed** — i.e. production output | Out of scope by the same reasoning 2G used to exclude them |

---

## 9. Recommended architecture

### 9.1 Two new modules, both additive

```
domain/settlement.py                  NEW — pure contract, no IO
    SettlementStatus                  SETTLED | UNRESOLVED
    UnresolvedReason                  the six of §4.4
    SettlementRecord                  the fields of §4.1, invariants in __post_init__
    settle_one(prediction, match, *, settled_at, source, attempt) -> SettlementRecord
    to_json_dict(record)              fixed key order, UTC timestamps
    imports: domain.evaluation.btts_outcome  (and stdlib)

settle_predictions.py                 NEW — the job, CLI, offline-capable
    unsettled(predictions, settlements)          set difference, terminal-aware
    settle(predictions, *, result_source, now)   group → fetch → lookup → build
    write_settlements(records, dir)              append-only JSONL
    main(argv)                                   --month, --dataset, --dry-run
    imports: prediction_ledger (read), espn OR historical_dataset, domain.settlement
    NOT imported by main.py, analyze_all.py or run3/
```

### 9.2 The settlement algorithm, stated once

```
1. records   = prediction_ledger.load_records(month)         # raw dicts
2. done      = load_settlements(month)                       # latest per prediction_id
3. todo      = [r for r in records if not terminal(done.get(r["prediction_id"]))]
4. groups    = group todo by (competition, season)
5. for each group:
       readout = result_source(competition, season)          # ESPN or local dataset
       if readout is None:
           emit UNRESOLVED / PROVIDER_UNAVAILABLE for the whole group
           continue
       index = {(m.competition, m.season, m.event_id): m for m in readout}
6. for each prediction:
       match = index.get((competition, season, fixture_id))
       if match is None:  → try adjacent season (2H-F3) → else FIXTURE_NOT_FOUND
       else:              → settle_one(prediction, match, settled_at=now)
7. append every record produced, settled or not
8. print counts by status, by unresolved reason, and by league-season
```

**Step 7 is the one that is easy to get wrong.** Unresolved records must be
*written*, not skipped. A skipped record is indistinguishable from a record that
was never attempted, and the difference is exactly what tells an operator whether
the job is working.

### 9.3 Why `settle_one` takes a `HistoricalMatch` and not a payload

`HistoricalMatch.__post_init__` already refuses a completed match with a missing
score, refuses a naive kickoff, and refuses negative or boolean goals
(`domain/historical.py:241-258`). Accepting the validated object rather than raw
JSON means settlement inherits all of that for free and cannot be handed a
malformed result. Accepting a payload would require re-implementing those checks
— a second derivation, which is the thing to avoid.

### 9.4 Tests the architecture implies

| Test | What it pins |
|---|---|
| `tests/unit/test_settlement.py` | One case per unresolved reason; `0-0 → NO`; abandoned-with-partial-score → `UNKNOWN`; every `__post_init__` invariant |
| `tests/unit/test_settle_predictions.py` | Append-only; idempotent re-run; terminal states not retried; unresolved records **written** not skipped |
| `tests/regression/test_settlement_isolation.py` | AST: `domain/settlement.py` imports no `poisson`/`filters`/`decision`/`evaluation_harness`; performs no IO and reads no clock; every `open()` in the job appends or reads |
| Extend `test_ledger_isolation.py` | `EVALUATION_MODULES` still cannot import `prediction_ledger` **or** the settlement job |

### 9.5 Sequencing

```
2H (this)   audit only                                        ← you are here
2H-2        domain/settlement.py + settle_predictions.py + tests
2H-3        run the ledger, accumulate real predictions, settle them
2I          reporting: the PredictionRecord adapter + existing metrics
            gated on minimum-n, and never Brier without AUC (2G-R5)
```

**2I must not be merged into 2H-2.** Shipping settlement and reporting together
means the first metric is printed by code whose settlement has never been checked
against a real fixture — and a wrong number with a plausible schema is harder to
detect than no number.

---

## 10. Proposed file changes

**Nothing in this section has been implemented.** This is the proposal for 2H-2.

| File | Change | Risk to production |
|---|---|---|
| `domain/settlement.py` | **NEW** — pure contract | None. Imported by nothing existing |
| `settle_predictions.py` | **NEW** — the job | None. Not imported by `main.py`, `analyze_all.py` or `run3/` |
| `tests/unit/test_settlement.py` | **NEW** | None |
| `tests/unit/test_settle_predictions.py` | **NEW** | None |
| `tests/regression/test_settlement_isolation.py` | **NEW** | None |
| `tests/regression/test_ledger_isolation.py` | **MODIFY** — add the settlement job to the set the evaluation layer may not import | None. Test-only, tightening |
| `.gitignore` | **NO CHANGE NEEDED** — `data/` already covers `data/settlements/` | None |
| `docs/EPIC_2H_SETTLEMENT.md` | **NEW** — implementation doc for 2H-2 | None |
| `docs/TECHNICAL_DEBT.md` | **MODIFY** — record 2H-F3 (season disagreement) and note that 2H-F7 makes GG-013's frequency measurable | None |

**Explicitly unchanged, and verified unchanged by this audit:**
`poisson.py` · `filters.py` · `decision.py` · `config.py` · `main.py` ·
`espn.py` · `output.py` · `odds_api.py` · `prediction_ledger.py` ·
`domain/prediction_log.py` · `domain/evaluation.py` · `evaluation_harness.py` ·
`run_evaluation.py` · `domain/historical.py` · `historical_dataset.py` ·
everything under `run3/`.

---

## 11. Non-goals

Explicit. Each of these is a thing a reviewer might reasonably expect and each is
**deliberately excluded**.

| Non-goal | Why excluded |
|---|---|
| **Any metric, anywhere in 2H** | 2G's standing instruction: nothing that reports accuracy, Brier or ROI until settlement is trustworthy. Settlement produces *outcomes*; a later epic produces *numbers* |
| **ROI, P&L, staking, bankroll** | LEAK-001 and 2G-R4. A settlement record carries no money field. Settling the result and pricing the bet are different questions and must stay in different files |
| **Modifying prediction records** | The ledger is append-only and has no `outcome` field on purpose (`prediction_log.py:180-184`). Settlement writes a **new file**, never back into a prediction |
| **Changing which fixtures are predicted** | Wiring `is_predictable()` (GG-013) or de-duplicating fixtures (GG-015) changes production output. 2H measures the consequence; it does not change the behaviour |
| **Recomputing any probability** | 2H-F6, 2H-R3. Settlement reads the stored probability and never generates one |
| **Team-name matching of any kind** | GG-008. The event id is sufficient; introducing a name comparison would import a known-defective pattern into the one place it is not needed |
| **Settling the committed `output_*.json` files** | §6.2. No `created_at`, no provenance, no `prediction_id`. Would manufacture plausible-looking records with no evidence behind them |
| **A database** | ~10 fixtures/day across 5 leagues. JSONL until volume justifies a schema migration — same reasoning 2G used |
| **A second result provider** | `sofascore.py` / `sportmonks.py` are dead code (GG-018, GG-009). The `source` field exists so a second provider *could* be added and remain distinguishable; adding one is not this epic |
| **Backfilling predictions** | Impossible, not merely out of scope. A prediction not recorded when it was made cannot be reconstructed |
| **Automatic scheduling / cron** | An operational concern. The job is a CLI; when it runs is a deployment decision |
| **Changing `domain/evaluation.py`** | It stays unaware settlement exists. That is what keeps both firewall tests passing unmodified |

---

## 12. Conclusion

**Settlement is architecturally ready and operationally blocked.**

Ready, because the hard precondition is already met: `fixture_id` and `event_id`
are the same identifier from the same endpoint, so settlement is a composite-key
lookup with no name matching anywhere (2H-F1). `btts_outcome` already refuses to
collapse a missing score into `NO`. `load_records` already exists, unused, with a
docstring explaining it was written for this. Every metric that will eventually
consume the result is already implemented and tested (2H-F8).

Blocked, because `data/` does not exist (2H-R1). The ledger has never run, so
there is not one prediction to settle. That is not an argument for delaying the
design — settlement can be backfilled indefinitely (§6.3), so there is no
deadline — but it is a decisive argument against shipping settlement and
reporting in the same change.

The three decisions that matter most, restated:

1. **A separate module, not `domain/evaluation.py`.** Not a style preference: the
   evaluation layer is forbidden by an enforced test from importing the ledger,
   and settlement must read the ledger.
2. **Per `prediction_id`, not per `fixture_id`.** 2G made re-runs deliberately
   distinguishable; a fixture-keyed settlement would throw that away invisibly.
3. **Six unresolved reasons, two of them terminal.** "No result" is not one fact.
   An outage, a cancelled match and a season-key mismatch are three different
   problems with three different responses, and a single `UNRESOLVED` value makes
   all three look like bad luck.

The most dangerous available shortcut is reusing `evaluation_harness.replay()`,
because it pairs fixtures with outcomes and looks like exactly the right tool —
while quietly re-running the model against today's data (2H-F6). Settlement reads
the probability that was published. It never computes one.

**Recommended next step:** 2H-2 — implement `domain/settlement.py` and
`settle_predictions.py` with the contract in §4, the algorithm in §9.2 and the
tests in §9.4. Reporting waits for 2I, and 2I waits for real settled records.


