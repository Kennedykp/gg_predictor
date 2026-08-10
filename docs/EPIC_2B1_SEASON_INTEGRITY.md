# Epic 2B.1 — Historical Season Integrity

Status: complete, awaiting review. Not committed.
Branch: `epic/2b1-season-integrity`

---

# Objective

Fix and prove historical season identity before any historical dataset,
backtest, calibration, cold-start implementation or model comparison is built.

Nothing downstream of this is worth building on a dataset that cannot say which
season a match belongs to. This Epic changes how fixtures are identified. It
changes no mathematics, no thresholds and no decisions.

---

# Defect

**Root cause, one sentence:** season membership was defined by a date window we
constructed, not by what the provider said about the event.

`espn.py` built a fixed July→June window from the requested season and treated
everything the window returned as belonging to that season:

```python
def _season_date_range(season: int) -> str:
    return f"{season}0701-{season + 1}0630"
```

The window was used as the *definition* of membership. It has no other check.
Two consequences, both measured, both real:

**1. Truncation.** Seasons that ran past 30 June were cut off. The COVID-extended
2019/20 season finished on 2020-07-26 (eng.1), 2020-08-02 (ita.1) and
2020-07-19 (esp.1). Those matchdays fall outside the window, so they were
silently deleted.

**2. Contamination.** The very same fixtures fall *inside* the window built for
the following season, so a request for 2020/21 returned them as 2020/21 data —
including three clubs that had been relegated and never played a minute of it.

Measured on the Epic 2A cache (5 production leagues × 4 seasons = 20
league-seasons):

| league | season | true | old rule returned | lost | wrongly admitted |
|---|---|---|---|---|---|
| eng.1 | 2019 | 380 | 314 | **66** | 0 |
| eng.1 | 2020 | 380 | 446 | 0 | **66** |
| ita.1 | 2019 | 380 | 282 | **98** | 0 |
| ita.1 | 2020 | 380 | 478 | 0 | **98** |
| esp.1 | 2019 | 380 | 323 | **57** | 0 |
| esp.1 | 2020 | 380 | 437 | 0 | **57** |

**221 real fixtures deleted; the same 221 injected into the wrong season.**
The remaining 14 league-seasons audited were unaffected — which is exactly what
makes this dangerous, because the defect is invisible in a normal season.

---

# Why Date Windows Are Not Season Identity

Three independent reasons, each sufficient on its own:

**Seasons overlap in calendar time.** eng.1 2019/20 ended 2020-07-26.
eng.1 2020/21 began 2020-09-12. ita.1 2019/20 ran to 2020-08-02, by which point
other leagues had already restarted. There is no date that separates them,
so *no* boundary — 30 June, 31 August, 30 September — can be correct for all
leagues in all seasons.

**A wider window makes contamination worse, not better.** Widening to capture
the July tail necessarily also widens the *next* season's window, which is what
imported the tail in the first place. The two failures are the same failure.

**ESPN will not serve a wider window anyway.** Measured:
`dates=20190701-20200630` → HTTP 200. The same range plus one day → **HTTP 400**.
The endpoint refuses ranges longer than 366 days, so "just use a 14-month
window" is not merely wrong, it is unavailable.

This is why Phase 13's prohibition is correct and why the fix is metadata-based.

---

# Before-Fix Reproduction

The new regression suite was run against the **pre-fix** provider
(`git stash push -- espn.py domain/match_records.py`). Behavioural failures,
quoted verbatim:

```
test_july_fixtures_are_recovered_into_2019_20
E   AssertionError: assert {'541611'} == {'541466', '541611'}
E     Extra items in the right set: '541466'

test_2020_21_excludes_the_2019_20_tail
E   AssertionError: assert ['541466', '573721'] == ['573721']
E     At index 0 diff: '541466' != '573721'

test_a_relegated_club_cannot_enter_the_season_it_never_played
E   AssertionError: assert '349' not in {'349', '357', '368', '370'}

test_expected_match_count_is_not_the_test
E   AssertionError: assert 380 == 314

test_a_foreign_league_event_in_the_payload_is_dropped
E   AssertionError: assert ['541611', '999999'] == ['541611']
```

`349` is AFC Bournemouth, appearing in a season they were relegated out of.
36 of 73 tests failed in total (the remainder being `AttributeError` on
functions that did not yet exist). The stash was popped and the fix restored.

These reproductions are **kept permanently** in
`TestTheOldRuleIsWrong`, which states the old rule as an explicit function and
asserts that it gives the wrong answer on real data. The proof lives in the
repository rather than in this document.

---

# ESPN Season Metadata Investigation

Read-only investigation over the 140 cached league-seasons captured in Epic 2A
(53,934 events, 7 leagues, 2006–2025), plus bounded live probes.

**What exists at event level**

| field | presence | verdict |
|---|---|---|
| `event.season.year` | **53,934 / 53,934 (100%)** | authoritative |
| `event.season.slug` | 100% (scoreboard) | corroborating, often a phase |
| `event.season.type` | 100%, opaque integer (e.g. `8906`) | unused |
| `event.uid` (`s:600~l:700~e:…`) | 100% | competition identity |
| `event.league.slug` | schedule endpoint only | competition identity |
| `event.season.displayName` | schedule endpoint only | corroborating |

**What the endpoints mean by `season`**

- League scoreboard: **no `season=` parameter exists.** Passing one is ignored;
  discovery is by `dates=` only. This is why discovery and validation had to be
  separated rather than delegated to the API.
- Team schedule: `season=` **is** honoured and filters server-side.
- The **payload-level** `season.year` is *not* the requested season. Measured: a
  request for the 2019 window answers with a top-level `season.year = 2026` —
  it describes the league's current season. Nothing in production reads it.

**`season.slug` is frequently a phase, not a season.** Observed values include
`regular-season` (4,968 in eng.2), `group-stage` (**303 ordinary ger.1 2010/11
Bundesliga fixtures**), `semi-finals`, `promotion-final`. Any rule that requires
a season-shaped slug, or filters on slug, would delete legitimate seasons.

---

# Authoritative Season Identity Rule

One chokepoint: `domain/season_identity.classify_event_season()`.

```
1. COMPETITION, checked first and independently.
   Not stated            -> UNVERIFIABLE
   Stated and different  -> WRONG_COMPETITION

2. SEASON MUST BE STATED.
   No usable season.year -> UNVERIFIABLE
   (not inferred from kickoff, not defaulted to the requested season)

3. LABEL MAY VETO, NEVER VOUCH.
   Label encodes a season AND contradicts season.year -> UNVERIFIABLE
   Label encodes no season                            -> silent

4. season.year == requested_season ? ACCEPTED : WRONG_SEASON
```

**Why `season.year` alone is not trusted (Phase 11).** Epic 1B.5 warned that
ESPN can echo surprising season values, and the corpus confirms it. In eng.1's
2009 window, 380 events carry `season.year = 2009` alongside
`season.slug = "2013-2014-barclays-premier-league"`. Spot-checked against real
results, that block is corrupt: it repeats the 2009-10 fixture list with **wrong
scores** (Chelsea 0-1 Hull, for a match that really finished 2-1). Trusting
`season.year` alone would have imported 380 fabricated results into the
historical dataset. The veto rejects exactly this block and nothing else:
45,657 events where the slug encodes a season agree with `season.year`; 380
disagree.

`SeasonVerdict` is a four-valued enum, not a boolean, because "wrong season",
"wrong competition" and "cannot tell" are three different facts and only the
third is a reason to investigate the provider.

---

# Discovery vs Validation

Now structurally separate, and visible in the code:

```
DISCOVERY   _season_discovery_windows(season)
            -> ["20190701-20200630", "20200701-20210630"]
            Deliberately broad. Finds candidates. Proves nothing.

VALIDATION  classify_event_season(identity, competition, season)
            -> ACCEPTED | WRONG_SEASON | WRONG_COMPETITION | UNVERIFIABLE
            The only thing that admits a fixture.
```

Discovery spans the season's own window **and the following one**, because that
is where an extended season's tail lives. The second window is skipped when it
lies entirely in the future, so current-season retrieval still costs one
request. Events are de-duplicated by event ID, since the two windows overlap.

The broad window is now harmless: it produces candidates that must still prove
their identity. Mutation M4 (below) confirms that replacing validation with a
date check — even a *generous* one — is caught immediately.

---

# 2019/20 Regression

Live, through the production provider, after the fix:

```
eng.1 season=2019 -> 380 records, all season==2019
                     66 kickoffs after 2020-06-30 preserved
                     event 541466 present
```

Representative recovered event, full provenance:

| field | value |
|---|---|
| event ID | `541466` |
| kickoff | `2020-07-26T15:00Z` |
| teams | Everton (368) 1–3 AFC Bournemouth (349) |
| requested season | 2019 |
| `season.year` | 2019 |
| `season.slug` | `2019-20-english-premier-league` |
| `uid` | `s:600~l:700~e:541466` → league 700 |
| status | `STATUS_FULL_TIME` |

Cached-corpus totals for the same fix: eng.1 314→380, ita.1 282→380,
esp.1 323→380. ger.1 unaffected (306, Bundesliga finished 27 June).

---

# 2020/21 Contamination Regression

Live, after the fix:

```
eng.1 season=2020 -> 380 records, all season==2020
                     relegated clubs 349/381/395 absent
                     distinct clubs: 20
                     541466 leaked in: False
```

Before the fix the same request returned **446** events. The Epic 2A anomaly is
preserved as a permanent regression fixture in
`TestPreviousSeasonCannotContaminate`, using the real event.

**Promotion/relegation boundary (Phase 17).** Derived from the corpus, then used
as the regression case:

- Relegated after 2019/20, absent from 2020/21: **Bournemouth (349), Norwich
  City (381), Watford (395)**
- Promoted into 2020/21: **Leeds United (357), Fulham (370), West Brom (383)**

`test_a_relegated_club_cannot_enter_the_season_it_never_played` asserts
Bournemouth cannot appear in 2020/21. It fails against the old code with
`assert '349' not in {'349', ...}`. A club that was relegated is the cleanest
possible disproof of date-based membership: no amount of calendar proximity
makes them part of the next season.

---

# Competition Identity

Treated as a **separate invariant**, checked independently and first.

- **Scoreboard events carry no `league` object.** Competition identity comes
  from `uid` (`s:600~l:700~e:…`), whose league id is compared with the id in the
  payload header. An event whose `uid` names a different league is dropped even
  though the payload claims to be eng.1.
- **Schedule events do carry `league.slug`**, which is checked directly.
- Where an event states its own competition, the **event wins over the payload
  header** — the header describes the response, the event describes the match.

A right-season/wrong-competition event returns `WRONG_COMPETITION`, not
`WRONG_SEASON`. Mutation M5 confirms this check is load-bearing.

---

# Playoff/Postseason Findings

**Investigated, deliberately not decided. This is a STOP-and-report item.**

How ESPN labels them: playoff fixtures sit in the **same competition** and the
**same `season.year`** as the league programme, distinguished only by
`season.slug` (`promotion-semifinals`, `relegation-playoff`, `final`, …).

Observed in the corpus:

| league | phase slugs | events |
|---|---|---|
| eng.2 | `semi-finals`, `promotion-semifinals`, `final`, … | ~100 |
| fra.1 | `promotionrelegation-playoffs`, `promotion-playoff-quarterfinals`, … | 6 |
| ita.1 | `relegation-playoff` | 1 (2022/23) |
| ger.1 | `group-stage` on **303 ordinary 2010/11 fixtures** | 303 |

**Why no policy was invented:** a naive "keep only `regular-season`" rule would
delete an entire legitimate Bundesliga season, because ESPN labelled it
`group-stage`. And whether a promotion playoff belongs in a regular-season
team-strength model is a *statistical* question, not a parsing question.

**What was done instead:** the phase is captured as
`MatchRecord.season_phase` (provenance) and **never filters**. Behaviour is
unchanged from before this Epic. The decision is now visible and can be taken
deliberately in 2B.2 with the data in hand.

**DECISION REQUIRED (product/statistical, not provider):** should
promotion/relegation playoff fixtures be included in league datasets used for
regular-season team-strength modelling? Affects ita.1 2022/23 (1 match),
fra.1 (6 matches across 20 seasons) and eng.2/ger.2 if wired in Phase 2.
Material impact on production leagues today: **≈7 matches**.

---

# STATUS_FINAL_PEN Findings

Exhaustively enumerated across all 7 leagues × 20 seasons: **9 events, all of
them playoff or postseason ties.**

```
fra.1  510814  2018-05-20  promotionrelegation-playoff-semifinals  Ajaccio v Le Havre
fra.1  540289  2019-05-21  promotion-playoff-quarterfinals        Paris FC v Lens
eng.2  216761  2007-05-15  semi-finals                            Derby v Southampton
eng.2  479233  2017-05-29  final                                  Huddersfield v Reading
… 5 more, all eng.2 playoff ties
```

**Zero occurrences in eng.1, ger.1, ita.1 or esp.1 regular-season play** — which
is correct, because league matches are not decided on penalties.

`STATUS_FINAL_PEN` is therefore a *playoff* signal, not a completion-semantics
problem. **Match completion semantics were left exactly as they were**, per
Phase 10. Widening or narrowing completion would change the statistical
definition of league history, which is out of scope; the finding is reported
rather than acted on. It is subsumed by the playoff decision above.

---

# Fail-Closed Behavior

An event that cannot prove its identity is **refused**. Specifically, the
implementation never:

- infers season from kickoff date
- substitutes the requested season for a missing one
- guesses from team membership or calendar year
- treats "arrived from the eng.1 URL" as evidence
- substitutes `0` for a missing season

Refusal cases: missing `season.year`; a non-integer year (`"n/a"`, `2019.5`,
`{}`, `[]`); a **boolean** year (Python's `isinstance(True, int)` would
otherwise smuggle it through as year 1); a label that contradicts the year;
a missing competition.

`0` is never used to repair identity: `season=0` is treated as a real value
distinct from "missing", and a missing season with `requested_season=0` is still
`UNVERIFIABLE`.

Measured on the 20 audited league-seasons: **0 events refused as unverifiable**.
The guard is strict but not destructive on real production data.

---

# MatchRecord / Domain Changes

Additive only — three optional fields, all defaulting to `None`:

```python
season: Optional[int] = None          # what the PROVIDER stated
season_phase: Optional[str] = None    # provenance only, never filters
provider: Optional[str] = None        # who said so
```

No existing field changed type, meaning or default. No derivation changed. All
1,405 tests pass, including the domain-contract suite. `season` is stored
rather than recomputed precisely because the one field it could be re-derived
from — the kickoff — is the field known to lie about it.

New module `domain/season_identity.py` holds the rule. It is
provider-independent: it accepts an extracted `SeasonIdentity`, never JSON, so
a second provider means a second extractor, not a second copy of the rules.

---

# Cache Safety

Both caches are keyed by season and competition:

- League cache: `(league, season)` → validated records
- Schedule cache: `(team_id, league, season)` → validated records

Cache entries store **post-validation** records, so a hit cannot bypass
validation — there is no path that returns raw events. Failures are never
cached, so a transient error does not become a permanent empty season.

Regression tests: two seasons never share an entry; a hit on the contaminated
season returns the validated 380, not the raw 446; the same club in two seasons
gets two entries; a failure is not cached.

---

# Point-in-Time Preservation

`record.kickoff < target_kickoff` is **unchanged**. All 10 Epic 1B.5 regression
tests pass **unmodified** — no test fixture required alteration.

Season correctness and point-in-time correctness are separate protections and
both are now asserted together: a July 2020 fixture is correctly *in* season
2019, and still correctly *excluded* from evidence for a March 2020 fixture.
Belonging to the right season does not relax the cutoff; the boundary remains
strict `<`.

---

# Historical Coverage After Fix

`python3 research/verify_season_integrity.py` (offline, Epic 2A cache):

```
league   season    new  done   old  lost  bogus
eng.1    2018      380   380   380     0      0
eng.1    2019      380   380   314    66      0
eng.1    2020      380   380   446     0     66
eng.1    2022      380   380   380     0      0
ger.1    2018-22   306   306   306     0      0   (all four seasons)
ita.1    2018      380   380   380     0      0
ita.1    2019      380   380   282    98      0
ita.1    2020      380   380   478     0     98
ita.1    2022      381   381   381     0      0
esp.1    2018      380   380   380     0      0
esp.1    2019      380   380   323    57      0
esp.1    2020      380   380   437     0     57
esp.1    2022      380   375   380     0      0
fra.1    2018      384   384   384     0      0
fra.1    2019      380   279   380     0      0
fra.1    2020      380   380   380     0      0
fra.1    2022      380   380   380     0      0

league-seasons audited: 20      old rule wrong in: 6
fixtures lost by old rule: 221  fixtures invented: 221
events refused as unverifiable: 0
```

Every affected season now returns its true length. Live confirmation through the
production provider matches exactly (380/380, 66 recovered, 0 leaked).

---

# Genuine Historical Anomalies

Deliberately **not** repaired. Each was investigated and confirmed as real:

- **fra.1 2019/20 — 380 fixtures, 279 completed.** Ligue 1 was ended early by
  government decree; the remaining 101 carry `STATUS_CANCELED`. The season is
  genuinely short and is left short.
- **fra.1 2018/19 — 384.** 380 league matches plus 4 promotion/relegation
  playoff fixtures inside the same competition.
- **ita.1 2022/23 — 381.** 380 plus one `relegation-playoff`
  (Spezia 1–3 Hellas Verona, 2023-06-11) — a genuine third meeting between two
  clubs, not a duplicate. Verified: no duplicate event IDs.
- **esp.1 2022/23 — 380 discovered, 375 completed.** Five February 2023 events
  are still `STATUS_SCHEDULED` in ESPN's data (fixtures that were rearranged).
  A provider-side gap, faithfully reported rather than filled.

A season having fewer than 380 matches is not evidence of corruption, and the
integrity layer does not treat it as such. Match count is used as *evidence*,
never as identity — `test_expected_match_count_is_not_the_test` builds a
380-event season that is 66 events contaminated and proves the count would have
called it healthy.

---

# Current-Season Verification

Unbroken, and no more expensive than before:

- `resolve_season("eng.1")` → 2026; discovery requests exactly
  `['20260701-20270630']` — **one window, one request**, because the following
  window is entirely in the future and cannot contain a played match.
- A current-season fixture with matching metadata is accepted.
- Scheduled (unplayed) fixtures are still not results; completion semantics are
  untouched.

The extra request only ever applies to historical seasons.

---

# Mutation-Test Result

Seven guards were individually weakened, the full suite run, then restored.
**All seven were killed:**

| # | mutation | result |
|---|---|---|
| M1 | accept requested season when metadata missing | **KILLED** |
| M2 | remove the label-contradiction veto | **KILLED** |
| M3 | remove season validation from scoreboard parser | **KILLED** |
| M4 | replace identity validation with date-only validation | **KILLED** |
| M5 | drop the competition check from the chokepoint | **KILLED** |
| M6 | shrink discovery back to one July–June window | **KILLED** |
| M7 | let the schedule parser skip validation | **KILLED** |

Suite restored and re-run afterwards: `1405 passed, 2 skipped`. M4 is the
important one — it proves date-only membership cannot be reintroduced even in a
generous form.

---

# Remaining Risks

1. **Playoff inclusion policy is undecided** (see above). ≈7 matches affect
   production leagues today. Requires a statistical decision, not a code change.
2. **eng.1 2009/10 is corrupt at source** — 380 events with contradictory
   metadata and wrong scores. Correctly refused as `UNVERIFIABLE`, so the season
   is unusable rather than wrong. Only matters if history before 2010 is wanted.
3. **`season.type` is an opaque integer** (`8906`, …). Not decoded, not used.
   Might be a cleaner phase signal than the slug; unverified.
4. **Only ESPN is validated.** Other provider modules exist but are unwired.
5. **Second-provider disagreement is out of scope** — `provider` is recorded so
   the question can be asked later.
6. **Live seasons in progress** return whatever ESPN currently knows; nothing
   here makes an incomplete season detectable as such.

---

# Files Changed

Created:

- `domain/season_identity.py` — the chokepoint (identity, verdicts, label rules)
- `tests/regression/test_season_integrity.py` — 73 tests
- `research/verify_season_integrity.py` — offline coverage audit
- `docs/EPIC_2B1_SEASON_INTEGRITY.md` — this document

Modified:

- `espn.py` — discovery/validation split, identity extraction, competition
  check, season-scoped caches, provenance on records
- `domain/match_records.py` — three optional provenance fields
- `tests/conftest.py` — stub payloads carry realistic season metadata
- `tests/unit/test_espn_schedule_provider.py` — local builder states its season
- `docs/TECHNICAL_DEBT.md` — GG-025 resolved; GG-026 and GG-027 opened


Unchanged (verified by hash against `HEAD`): `poisson.py`, `config.py`,
`filters.py`, `decision.py`, `odds_api.py`, `shared/match_history.py`,
`shared/odds.py`, `domain/poisson_inputs.py`, all of `run3/`.

---

# Validation Results

```
pytest                          1405 passed, 2 skipped        (was 1332)
  POISSON_V1 golden regression    38 passed  (test_poisson_v1_regression.py)
  all POISSON_V1 suites          808 passed  (+ unit/test_poisson, invariants)
  point-in-time (1B.5)            10 passed, unmodified
  season integrity (new)          73 passed

ruff check .                    All checks passed!
mypy                            Success: no issues found in 26 source files
mutation testing                7 mutations, 7 killed
live diagnostic                 read-only, 4 requests, matches offline results
```

The 2 skips are pre-existing spec-agreement items (D1, D3), unrelated.

**Confirmed unchanged:** POISSON_V1 mathematics; config thresholds; filter
semantics; decision logic; odds logic; Run-3. Verified by SHA against `HEAD`,
not by inspection.

---

# Recommended Next Epic

**Epic 2B.2 — Historical Dataset Construction**, now that season identity is
trustworthy. Before it starts, one decision is needed:

> **Do promotion/relegation playoff fixtures belong in the league dataset used
> for regular-season team-strength modelling?**

Recommendation: exclude them, on the grounds that they are a different
competitive context — but this must be an explicit, recorded modelling decision,
and it must be implemented at dataset-construction level using
`MatchRecord.season_phase`, **not** inside provider parsing. The `group-stage`
mislabelling of 303 ordinary Bundesliga fixtures shows why the rule cannot be a
simple slug match and needs case-by-case validation.

Cold-start mathematics, empirical Bayes, Dixon-Coles and backtesting all remain
out of scope until the dataset exists.
