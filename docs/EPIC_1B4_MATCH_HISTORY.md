# Epic 1B.4 — ESPN Match-Level History Feed & Derived Filter Statistics

Status: complete, pending review.
Scope: GG / BTTS only. Run-3 untouched.
Date: August 2026.

---

## Objective

Epic 1B.3 wired the GG hard filters correctly but left one of them with no data.
`clean_sheet_pct` could not be obtained from ESPN's aggregate team endpoint,
because aggregate goals-conceded destroys the match-by-match information the
statistic needs:

```
GA = 5 over 5 matches
  → 0,1,1,1,2  = 0 clean sheets
  → 0,0,0,0,5  = 4 clean sheets
```

Both are consistent with the same aggregate. No exact derivation exists.

This Epic supplies the missing evidence from ESPN's **team schedule** endpoint,
which returns individual completed matches with final scores:

```
ESPN completed events
  → validated MatchRecord objects
  → point-in-time-safe history (strictly before the target kickoff)
  → exact derived statistics
  → FilterStats
  → existing GG filter evaluation (unchanged)
```

Primary target: `clean_sheet_pct`. Secondary: BTTS percentage, exposed as data
only — **no new filter, no new threshold** (TASK 29).

---

## ESPN Schedule Endpoint Investigation

Verified against the live API on 2026-08-08 (see *Live Diagnostic Results*).

```
GET https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team_id}/schedule
    ?season=YYYY
```

| Property | Finding |
|---|---|
| Path | The suggested candidate path is correct — **verified, not assumed** |
| Host | Same `site.api.espn.com` as fixtures/teams. **Not** the `/apis/v2/` standings host |
| HTTPS | Yes. No redirect from the HTTPS form |
| Auth | None required, none sent |
| `season=` | **Honoured** (see below) — unlike the team-statistics endpoint |
| Pagination | None observed. A full 38-match league season returned in one response |
| Completed matches | Yes, with final scores |
| Future fixtures | Yes, mixed into the same `events` array — the reason the kickoff cutoff is mandatory |
| Event IDs | Stable, unique, string-typed (e.g. `"740972"`) |
| Opponent IDs | Present under `competitors[].team.id` |

---

## Response Semantics

```
events[]
  id                                    -> event ID (stable)
  date                                  -> ISO 8601 kickoff, "Z" suffix
  league.slug                           -> COMPETITION IDENTITY  (e.g. "eng.1")
  competitions[0]
    status.type.name                    -> canonical status ("STATUS_FULL_TIME")
    status.type.completed               -> boolean
    competitors[]
      homeAway                          -> "home" | "away"
      team.id                           -> team identity
      score                             -> see below
```

The score field is the one real trap. It is **not** a plain number:

```json
"score": { "$ref": "http://…/scores/38", "value": 1.0, "displayValue": "1" }
```

It is a dict with a `$ref` to an unfetched sub-resource. Reading it as a scalar
yields a dict, and `int(dict)` raises. The adapter reads `value` (falling back
to `displayValue`), and rejects the record if neither yields an integer —
**never substituting zero** (TASK 5). Some payload variants do return a bare
string, so both shapes are handled and both are covered by offline tests.

---

## Season Parameter Findings

Empirically compared **returned event IDs**, not parameter acceptance
(TASK 4). A 200 response proves nothing about whether the parameter did
anything — that was exactly how GG-024 hid in the team-statistics endpoint.

| Request | Events | Shared IDs with 2026 |
|---|---|---|
| `season=2026` (current) | 0 | — |
| `season=2025` (previous) | 38 | **0** |

Verdict: **HONOURED — disjoint event sets.** The schedule endpoint genuinely
serves historical seasons, unlike the team-statistics endpoint.

This does **not** close GG-024 and does **not** make backtesting safe. See
*Historical Safety Boundary*.

---

## Competition Contamination Findings

TASK 3 required this be established, not assumed.

Live result for `eng.1`, team 349, season 2025:

```
competitions present: 1
  eng.1 : 38          <- league only
```

For this team/league/season the feed was competition-pure. **The adapter does
not rely on that.** One observation of one team in one league is not a
guarantee, and ESPN's `league.slug` field exists precisely because the shape
allows mixing. Cup and European runs are plausible contaminants for other
clubs.

Decision: **default to same-competition records**, filtered on `league.slug`.

- GG.md specifies league-context filters and never asks for all competitions.
- Where the specification is ambiguous, the safer reading is the narrower one.
- A record whose competition is **unknown** is excluded, not assumed to be the
  league (`test_unknown_competition_is_excluded_not_assumed`).

---

## MatchRecord Mapping

TASK 1 audit. `MatchRecord` already existed from Epic 1B.3 with `venue`,
`goals_for`, `goals_against`, `completed`, `kickoff`. It had no identity fields,
so target-fixture exclusion and deduplication were impossible.

Minimal, provider-independent extension — two optional fields:

| Field | Added | Purpose |
|---|---|---|
| `event_id: Optional[str]` | Yes | Target-fixture exclusion (TASK 9), dedup (TASK 11) |
| `competition: Optional[str]` | Yes | Contamination filter (TASK 3) |

Both optional, so every Epic 1B.3 construction site still compiles and behaves
identically. Neither names ESPN. The provider maps `league.slug` into
`competition`; the domain only compares strings.

Mapping performed in `espn.py` (provider layer — no ESPN JSON reaches the
domain):

```
event.id                              -> event_id
event.date                            -> kickoff (parsed to aware UTC)
event.league.slug                     -> competition
competitors[homeAway == "home"]       -> venue / goals_for / goals_against
status.type.name / .completed         -> completed
```

---

## Completed-Match Policy

TASK 7. An **allowlist** of ESPN status names, not a blocklist:

```python
_COMPLETED_STATUS_NAMES = frozenset({
    "STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN",
})
```

A blocklist fails open — an unrecognised status would be treated as completed
and a fabricated result would enter the history. An allowlist fails closed:
anything unknown is excluded (TASK 7, "when uncertain: exclude").

| ESPN status | Included | Why |
|---|---|---|
| `STATUS_FULL_TIME` | ✅ | Final score |
| `STATUS_FINAL` / `STATUS_FINAL_AET` / `STATUS_FINAL_PEN` | ✅ | Final score |
| `STATUS_SCHEDULED` | ❌ | Not played |
| `STATUS_IN_PROGRESS` / `STATUS_FIRST_HALF` | ❌ | Score not final |
| `STATUS_HALFTIME` | ❌ | Score not final |
| `STATUS_POSTPONED` | ❌ | Not played |
| `STATUS_CANCELED` | ❌ | Never played |
| `STATUS_ABANDONED` / `STATUS_SUSPENDED` | ❌ | No reliable final score |
| Anything unrecognised | ❌ | Fails closed |

The name allowlist is not used alone. **All three signals must agree**:

```python
bool(status_type["completed"])
    and status_type["state"] == "post"
    and status_type["name"] in _COMPLETED_STATUS_NAMES
```

Neither flag is sufficient by itself: an ABANDONED match also reports
`state == "post"` and can carry `completed: true`, yet its partial score is not
a result. It fails the name check. Display text is never consulted.


---

## Target-Kickoff Cutoff

TASK 8. The core rule of this Epic:

```python
record.kickoff < target_kickoff     # strict, never <=
```

Enforced in `domain/match_records.eligible_history()`, which every derivation
path routes through. `target_kickoff` is a **required keyword argument** — a
caller cannot forget it, because omitting it is a `TypeError`, not a silently
unbounded history.

| Record kickoff | Included |
|---|---|
| T − 1 second | ✅ |
| T exactly | ❌ |
| T + 1 second | ❌ |
| No kickoff at all | ❌ — cannot prove it happened first |

A match kicking off at exactly T has not been played when the prediction is
made, so `<=` would leak a result into its own prediction.

---

## Venue Perspective

TASK 6, the reversal risk. Perspective is assigned from `homeAway`:

| Team was | goals_for | goals_against | venue |
|---|---|---|---|
| home | home score | away score | HOME |
| away | away score | home score | AWAY |

Tested with **asymmetric** scorelines throughout — 3–1, never 1–1 — so a
swapped mapping cannot pass. For `Home 3–1 Away`:

- Home team: GF=3, GA=1
- Away team: GF=1, GA=3

A symmetric scoreline would make a reversed implementation look correct, which
is why none of the perspective tests use one.

Venue-specific history (TASK 12): the home team's **HOME** matches and the away
team's **AWAY** matches, matching the existing venue-sensitive filters. Records
are never merged across venues to inflate the sample
(`test_venue_split_is_not_merged`).

---

## Deduplication

TASK 11. On **event ID only**:

- Two genuine 1–0 home wins are two matches. Deduplicating by scoreline would
  understate the sample and distort every percentage.
- Repeated event IDs collapse to one record.
- Records with no event ID are kept — the schedule feed always supplies one,
  and dropping them would silently discard evidence.

---

## Clean-Sheet Derivation

TASK 13. `clean sheet ⇔ goals_against == 0`.

```
clean_sheet_pct = clean_sheet_matches / eligible_matches
```

Not calculated when `eligible_matches == 0` — returns **unavailable** (`None`).

```
GA 0,1,0,2,0  → 3/5 = 0.60
GA 1,1,1      → 0/3 = 0.0     GENUINE ZERO, not unavailable
```

The `0.0` versus `None` distinction is the single most important property in
this Epic. `0.0` is a measurement — a team that never kept a clean sheet, which
*passes* `MAX_CLEAN_SHEET_PCT`. `None` is silence, which must **block**. Both
are asserted directly against the filter outcome, not just the value.

---

## BTTS Derivation

TASK 14. `BTTS ⇔ goals_for > 0 AND goals_against > 0`.

```
btts_pct = btts_yes_matches / eligible_matches
```

| Score | BTTS |
|---|---|
| 0–0 | NO |
| 1–0 | NO |
| 0–2 | NO |
| 1–1 | YES |
| 3–1 | YES |
| 2–4 | YES |

Exposed on `FilterStats` as `home_btts_pct` / `away_btts_pct`. **No filter
consumes it** (TASK 29). A regression test constructs a 0% BTTS history — the
worst possible reading for a GG bet — and asserts the fixture still `PASSED`.
If anyone adds a BTTS threshold, that test fails immediately.

---

## Sample Size

TASK 15. Both percentages derive from the **same** eligible record set, so one
`sample_size` describes both. Documented here and asserted in
`test_both_rates_share_one_sample_size`.

Surfaced on `FilterStats` as `home_history_sample` / `away_history_sample`.

**No minimum-sample threshold was introduced.** No `MIN_RECENT_GAMES`. n=1 is
calculated honestly and reported as n=1. Calibration of minimum history is a
later modelling decision, not a silent rejection rule smuggled in here.

---

## Data Provenance

TASK 16. `FilterStats.clean_sheet_source` carries `StatSource.DERIVED`,
distinguishing an exactly-calculated figure from a `DIRECT` provider reading.
It is set at construction from which branch actually produced the value, never
inferred from the number itself — a rate of `0.6` says nothing about where it
came from.

The originating feed is recorded structurally rather than as an enum value:
a derived clean-sheet percentage exists only when a `DerivedHistory` was built
from ESPN match records, and `home_history_sample` / `away_history_sample` are
non-`None` exactly in that case. A dedicated `HistorySource` enum was **not**
added — with one history feed it would encode a single constant, and the
`StatSource` distinction plus the sample-size fields already answer "derived,
from match records, over n matches".


---

## Pipeline Integration

TASK 17. One composition boundary, `shared/match_history.py`:

```python
build_fixture_filter_stats(fixture, home_stats, away_stats, history_provider=None)
```

It resolves the target kickoff, requests each team's venue-specific history, and
hands the result to the existing `domain.build_filter_stats`. **No derivation
logic lives in `main.py` or `analyze_all.py`** — both call this one function.

`history_provider=None` reproduces exact pre-1B.4 behaviour (aggregates only,
clean-sheet unavailable), which keeps every Epic 1B.3 test meaningful.

---

## Entry-Point Consistency

TASK 18. Enforced structurally, not by convention:

- `test_both_entry_points_call_the_same_composition_function` — identity check
  on the imported symbol.
- `test_neither_entry_point_calls_build_filter_stats_directly` — inspects the
  source of both modules and fails if either bypasses the shared boundary. This
  is the GG-006 regression guard extended to history.
- Identical inputs → identical `FilterStats` → identical filter outcome, in both
  the success and failure paths.

---

## Failure Behavior

TASK 19. A failed schedule fetch returns `None`, which becomes
`clean_sheet_pct = None`:

```
provider failure
  → clean_sheet_pct UNAVAILABLE   (never 0, never a neutral value)
  → mandatory clean-sheet filter cannot be trusted
  → FilterOutcome.UNEVALUATED
  → allows_recommendation = False
```

The **MODEL AVAILABLE / FILTER DATA UNAVAILABLE** distinction is preserved:
`result.was_evaluated` is `False` while POISSON_V1's own inputs remain complete,
so a raw probability can still be displayed while the recommendation is
withheld.

---

## HTTP / Caching Behavior

TASK 20 — schedule requests go through the **same** `espn._fetch` introduced in
Epic 1B.2. No second HTTP stack. They inherit HTTPS, timeout, bounded retry,
status validation, malformed-JSON handling and typed failure semantics, and
permanent errors are not retried indefinitely.

TASK 21 — a per-run in-memory cache. The pipeline analyses many fixtures per
league, and a team appears in multiple fixtures, so the same schedule would
otherwise be fetched repeatedly. Key:

```
(team_id, league_code, season)
```

Every parameter that materially changes the response is in the key. **Venue and
target kickoff are deliberately not** — they are applied *after* retrieval, so
caching can never bypass the cutoff. A cached payload is re-filtered per
fixture; `test_cache_cannot_bypass_the_kickoff_cutoff` proves two different
target kickoffs against one cached payload still produce different, correct
histories.

No Redis, no database, no persistence. `clear_schedule_cache()` is exposed for
run boundaries and test isolation.

---

## Offline Tests

All deterministic, all offline. The transport is stubbed; no socket is opened.

`tests/unit/test_espn_schedule_provider.py` (64 tests) — valid completed home
and away matches, asymmetric perspective, scheduled/in-progress/halftime/
postponed/cancelled/abandoned exclusion, unknown status fails closed, missing
score, malformed score, `$ref` dict score, missing competitor, missing team ID,
missing kickoff, duplicate events, wrong competition, target fixture itself,
before/at/after cutoff, timezone handling, empty schedule, provider failure,
caching behaviour.

`tests/unit/test_match_history_derivation.py` (33 tests) — hand-calculated
derivations (TASK 23), BTTS scoreline table, genuine-zero vs unavailable,
cutoff boundary at ±1 second, timezone equivalence, dedup, venue and
competition exclusion.

`tests/integration/test_match_history_pipeline.py` (18 tests) — end to end from
fixture dict to filter verdict: derived stats reaching the filter, venue split
preserved, future match unable to affect the verdict, target fixture excluded
from its own history, provider failure blocking without fabricating, August
2026 behaviour, entry-point consistency.

---

## Live Diagnostic Results

TASK 24. `scripts/espn_diagnostic.py` gained sections 6 and 7. Manual only —
`scripts/` is outside pytest's `testpaths`, and it is not in CI.

Run: 2026-08-08, `eng.1`, AFC Bournemouth (349).

```
Section 6 — TEAM SCHEDULE
  endpoint            : …/eng.1/teams/349/schedule
  season param        : 2026
  events returned     : 0
  competition purity  : PURE (eng.1)
  parsed MatchRecords : 0
  HOME clean-sheet %  : unavailable (n=0)
  AWAY clean-sheet %  : unavailable (n=0)

Section 7 — SCHEDULE season= BEHAVIOUR
  season=2026 : 0 events
  season=2025 : 38 events
  shared ids  : 0
  VERDICT     : HONOURED — disjoint event sets
```

Against the 2025 payload, with a cutoff of 2026-07-01 (after that season ended),
the production parser and derivation produced:

```
events         : 38
competitions   : {'eng.1': 38}     — pure
statuses       : {'STATUS_FULL_TIME': 38}
unique IDs     : 38 of 38          — no duplicates
parsed records : 38 of 38          — every event mapped, none rejected
HOME  clean-sheet 0.3158  BTTS 0.6316  n=19
AWAY  clean-sheet 0.2632  BTTS 0.6842  n=19
```

19 home + 19 away = 38, the correct shape of a Premier League season. This
confirms the adapter handles real payloads, not just fixtures: correct venue
split, correct competition identity, no dropped records.

---

## August 2026 Behavior

TASK 25. The current 2026/27 season returns **zero** completed matches. It is
early August; the season has not started.

Consequences, reported honestly:

- Derived clean-sheet percentage: **unavailable** (not 0.0).
- The mandatory clean-sheet filter cannot be evaluated.
- **No recommendation** is produced.

What was explicitly **not** done:

- 2025/26 matches are **not** imported into 2026/27 statistics. Previous-season
  carryover is a modelling decision, not a data-plumbing shortcut.
- Friendlies are **not** used to pad the sample, even though preseason is the
  only football available in August
  (`test_friendlies_are_not_used_to_pad_the_sample`).
- Thresholds were **not** weakened to compensate.

Zero recommendations is the correct output for this state.

---

## Historical Safety Boundary

**This Epic does NOT make historical backtesting safe. LEAK-001 remains OPEN.**

The cutoff makes *match history* point-in-time correct. It does nothing for the
other model inputs. Evaluating a fixture from March 2026 today would use:

| Input | Point-in-time correct? |
|---|---|
| Match history (this Epic) | ✅ Yes — cutoff enforced |
| Team aggregate statistics (λ inputs) | ❌ **No** — current-season-only, present-day values |
| League average goals | ❌ **No** — present-day standings |
| Odds | ❌ No — current market |

POISSON_V1's λ would be computed from a full season of results that had not been
played at the fixture's kickoff. The clean-sheet filter would be honest while
the probability it filters was computed with hindsight — arguably *more*
dangerous than an obviously broken pipeline, because the result looks
trustworthy.

Backtesting remains unsafe until **every** input is point-in-time correct. That
requires stored fixture-level history, which is a future Epic.

---

## GG-024 Status

**OPEN — partially informed, not resolved.**

New finding: the *schedule* endpoint honours `season=` and serves previous
seasons (verified by disjoint event IDs).

GG-024 is about the **team-statistics** endpoint, which remains current-season-
only. That endpoint supplies the goals-scored/conceded aggregates POISSON_V1
needs, so historical λ inputs remain unavailable. A schedule endpoint that
supports history does not fix a statistics endpoint that does not.

---

## GG-002-B Status

**Partially resolved — narrowed, not closed.**

| Filter | Before | After |
|---|---|---|
| Clean-sheet percentage | No feed | ✅ **Derived from match records** |
| Knockout first leg | No feed | ❌ Still unavailable |
| Heavy-favourite mismatch | No feed | ❌ Still unavailable |

The clean-sheet third is resolved with real data. The other two need a
competition-format feed and a market/odds-derived favouritism signal
respectively. Neither was fabricated.

---

## POISSON_V1 Verification

`poisson.py` **unchanged** — byte-identical:

```
3cf9f2a19604cac40d41f0191272a0a123ba302ff9b79fab11a3bc8002f771ce  poisson.py
```

`git diff --stat -- poisson.py` is empty. The golden regression suite passes
unchanged (38 tests) with no expectation edited. λ formulas, BTTS probability
formula and GG probability thresholds are untouched.

---

## Threshold Verification

Byte-identical, `git diff` empty for all:

```
29f9d03627c29e05f1394c25922390e6e6061e4faf87c5fd0c28b4bcae94555b  config.py
fd96f4eaacb9650b228d3557c2561e3d6bfd3f8180628594c248946942e4ecba  filters.py
69e241abf05a92aa5b44be6235bc36b03c3558cd5fedaf2c1f8adc22318fd85e  decision.py
```

`MAX_CLEAN_SHEET_PCT`, `MIN_AVG_GOALS`, `EDGE_THRESHOLD`, `MIN_ODDS` and every
decision threshold are unchanged. No new filter or threshold was created.
`run3/` is untouched.

---

## Issues Closed

None fully. Progress recorded against GG-002-B (clean-sheet third resolved).

Deliberately **not** closed: LEAK-001, GG-024, GG-002-B, GG-005.

---

## Issues Remaining

- **LEAK-001** — OPEN. Aggregate statistics are not point-in-time correct.
- **GG-024** — OPEN. Team-statistics endpoint is still current-season-only.
- **GG-002-B** — OPEN. Knockout-first-leg and heavy-favourite filters still lack
  a feed.
- **GG-005** — OPEN. Recency/form weighting is unaddressed; history is used for
  rates, not for form.
- **New (documented, not filed as a defect):** the schedule `score` field is a
  `$ref` dict rather than a scalar. Handled, but it is a shape future providers
  must not assume away.

---

## Files Changed

Production:

| File | Change |
|---|---|
| `domain/match_records.py` | `event_id` + `competition` fields; `eligible_history()`; `derive_history()`; `DerivedHistory` |
| `domain/filter_stats.py` | Derived clean-sheet/BTTS, sample sizes, provenance |
| `domain/__init__.py` | Exports for the new domain symbols |
| `espn.py` | Schedule adapter: parsing, status allowlist, dedup, per-run cache, transport reuse |
| `shared/match_history.py` | **New.** Single composition boundary |
| `main.py` | Calls `build_fixture_filter_stats` |
| `analyze_all.py` | Calls `build_fixture_filter_stats` |
| `scripts/espn_diagnostic.py` | Sections 6 and 7 (manual, not CI) |

Tests: `tests/unit/test_espn_schedule_provider.py` (new),
`tests/unit/test_match_history_derivation.py` (new),
`tests/integration/test_match_history_pipeline.py` (new).

Unchanged: `poisson.py`, `config.py`, `filters.py`, `decision.py`, `output.py`,
`shared/odds.py`, `run3/`.

---

## Validation

```
pytest        1321 passed, 2 skipped
golden        38 passed (tests/regression/test_poisson_v1_regression.py)
ruff check .  All checks passed!
mypy          Success: no issues found in 23 source files
```

The 2 skips are the pre-existing GG.md specification disagreements (D1, D3) from
Epic 1B.3, unrelated to this Epic. All tests are offline and deterministic.

---

## Recommended Next Step

The mechanism now exists to retrieve historical match records, and the schedule
endpoint genuinely serves previous seasons. The blocker for backtesting is no
longer match history — it is the **aggregate statistics** that feed POISSON_V1.

Recommended: an Epic that establishes point-in-time-correct λ inputs, most
plausibly by deriving team goal aggregates from the same match-level schedule
records rather than the current-season-only statistics endpoint. That would
address GG-024 and LEAK-001 at their shared root, and is the prerequisite for
any honest backtest.

Until then, backtesting must not be attempted, and results must not be presented
as validated performance.
