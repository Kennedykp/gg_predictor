# Epic 2B.2 — Historical Dataset Construction

Builds the deterministic, point-in-time-safe historical match dataset that Epic 2B.1's season
integrity layer made trustworthy. **No model mathematics is implemented here** — POISSON_V1,
thresholds, filters, decision logic and odds are untouched. This Epic produces *data*, plus an
explicit record of what that data is and is not fit for.

---

## Objective

Turn verified ESPN league-seasons into a reproducible on-disk dataset of historical matches, where
every record carries enough provenance to answer *where did this come from* and every record is
labelled with whether a regular-season team-strength model may learn from it.

The prerequisite was Epic 2B.1: before that fix, 221 real fixtures were deleted and the same 221
injected into the following season. Building a dataset on that retrieval would have produced a corpus
that was **confidently wrong** — 14 of 20 audited league-seasons looked perfect.

---

## What was built

| File | Role |
|---|---|
| `domain/historical.py` | The observation contract: `HistoricalMatch`, eligibility classification, serialization, ordering, point-in-time query |
| `historical_dataset.py` | Deterministic builder + manifest writer (CLI) |
| `espn.py` (extended) | `parse_scoreboard_history()`, `get_league_history()` — historical adapter over the 2B.1 identity chokepoint |
| `research/audit_historical_dataset.py` | Bounded coverage audit (research tooling, not production) |
| `tests/unit/test_historical_dataset.py` | 30 offline tests |

---

## The observation contract

`HistoricalMatch` is a frozen dataclass describing **one observed event**, distinct from
`MatchRecord` (the model-facing view). It retains, per record: `provider`, `league`, `season`,
`event_id`, `kickoff` (timezone-aware UTC), both team ids and names, both scores, `status`, and
`season_phase`.

Three deliberate properties:

- **`__post_init__` validates rather than coerces.** A record cannot be constructed with a naive
  datetime, a negative score, or a score on one side only. Invalid input raises; it is never repaired.
- **Scores are `Optional[int]`, never `0`.** A fixture without a result has `None`, not zero. This is
  the GG-001 lesson applied to history: `0-0` is a real result and must stay distinguishable from
  "not played". `has_result` is an explicit `is not None` check on both sides.
- **`event_id` is the identity.** De-duplication is on the provider's own id, never on
  `(date, teams)` — which would collapse two legitimately distinct fixtures that coincided.

---

## Model eligibility — the GG-026 decision, recorded

Epic 2B.1 deliberately refused to decide whether promotion/relegation playoff fixtures belong in a
regular-season dataset, because it is a statistical policy and not a parsing default. **Epic 2B.2
records the decision**, at dataset level, from `season_phase`, outside provider parsing — exactly
where GG-026 said it belonged.

**Decision: excluded from model training, retained in the dataset.**

`classify_model_eligibility(phase)` returns one of three verdicts with a human-readable reason:

| Verdict | Meaning |
|---|---|
| `ELIGIBLE` | Ordinary league programme; a team-strength model may learn from it |
| `INELIGIBLE` | Named postseason tie (playoff / promotion / relegation / final) — different competitive context |
| `UNCERTAIN` | Phase absent or unrecognised — **excluded from the model view and reported** |

Two design points matter more than the verdicts themselves:

**The rule matches postseason markers; it never says `phase != "regular-season"`.** That obvious
inverse rule is a trap GG-026 explicitly warned about: ESPN labels **303 ordinary ger.1 2010/11
fixtures** `group-stage`, so the inverse rule would delete a legitimate Bundesliga season to exclude
~7 playoff matches. A regression test asserts that season survives intact.

**Exclusion is a view, not a deletion.** `model_dataset()` narrows at the point of use; the ineligible
records stay on disk with their reason attached. The policy is therefore reversible by a later
modelling Epic without re-fetching a byte — and auditable, because the reason travels with the record.

**`UNCERTAIN` is never silently trained on.** An unrecognised phase is excluded *and* surfaced in the
build manifest, so "I do not know what this is" cannot quietly become evidence.

---

## Determinism

The same inputs must produce byte-identical files, or checksums are theatre.

- `sort_key()` orders by `(league, season, kickoff, event_id)` — the trailing `event_id` breaks ties
  between simultaneous kickoffs, which is what makes the ordering **total** rather than merely sorted.
- JSONL output with `sort_keys=True`; kickoffs serialised as ISO-8601 UTC.
- `to_json_dict` / `from_json_dict` round-trip exactly, verified by test.
- The manifest records a SHA-256 per season file plus the identity-rule and contract versions, so a
  dataset built under different semantics is detectable rather than silently mixed.

---

## Point-in-time safety

`matches_before(matches, cutoff, ...)` enforces the Epic 1B.5 invariant **unchanged**:
`record.kickoff < cutoff`, strictly.

- Strict `<`, never `<=` — a match kicking off exactly at the cutoff has not been played.
- Requires a timezone-aware cutoff; a naive datetime raises rather than comparing ambiguously.
- Records without a result are excluded from the model view regardless of date: a fixture that was
  scheduled before the cutoff but never played carries no information.

This composes with, and does not replace, the season identity guarantee. Both hold independently.

---

## Coverage audit — 5 leagues × 4 boundary seasons

`research/audit_historical_dataset.py` replays payloads already cached by the Epic 2A audit through
the production parser: **zero network requests** on a warm cache. It mirrors `get_league_history()`
deliberately — including replaying **both** discovery windows and de-duplicating at the seam. (An
earlier version of the audit read only the primary window and under-reported the COVID seasons by
exactly the amount 2B.1 recovers; the audit was corrected, not the finding.)

| League | 2018/19 | 2019/20 | 2020/21 | 2023/24 | Expected |
|---|---|---|---|---|---|
| eng.1 | 380 | 380 | 380 | 380 | 380 |
| ger.1 | 306 | 306 | 306 | 306 | 306 |
| ita.1 | 380 | 380 | 380 | 380 | 380 |
| esp.1 | 380 | 380 | 380 | 380 | 380 |
| fra.1 | **384** | 380 | 380 | 306 | 380 / 306 |

**Every season matches its expected count**, with one explained exception (below). The two integrity
guarantees are visible in the data:

- **COVID tails preserved.** eng.1 2019/20 runs to **2020-07-26**, ita.1 to **2020-08-02**, esp.1 to
  **2020-07-19**. The pre-2B.1 June-30 window deleted these.
- **No contamination downstream.** 2020/21 first kickoffs are 2020-09-12 (eng.1), 2020-09-19 (ita.1),
  2020-09-12 (esp.1) — no 2019/20 tail leaked in, and the wrong-season events in the discovery
  windows were refused (eng.1 66, ita.1 98, esp.1 57 — the exact figures GG-025 recorded).

`WRONG_SEASON` rejections are **confirmations, not anomalies**: the second discovery window *is* the
following season's window, so events ESPN attributes elsewhere are supposed to be refused. A run
reporting zero such rejections would be the surprise. The audit prints them in a separate
CONFIRMATIONS section for exactly this reason.

---

## Genuine historical anomalies — retained, not repaired

Both remaining anomalies were investigated against the actual records before being accepted as real.

**fra.1 2018/19 returns 384, not 380.** The four extra records are the Ligue 1/Ligue 2 promotion
playoff:

| Event | Date | Fixture | Status | Phase |
|---|---|---|---|---|
| 540289 | 2019-05-21 | Paris FC 1–1 Lens | `STATUS_FINAL_PEN` | `promotion-playoff-quarterfinals` |
| 540288 | 2019-05-24 | Troyes 1–2 Lens | `STATUS_FINAL_AET` | `promotion-playoff-semifinals` |
| 540463 | 2019-05-30 | Lens 1–1 Dijon | `STATUS_FULL_TIME` | `promotion-playoff-finals` |
| 540462 | 2019-06-02 | Dijon 3–1 Lens | `STATUS_FULL_TIME` | `promotion-playoff-finals` |

All four are correctly `INELIGIBLE`, all four remain on disk. The count is right *and* the model view
is right, because they are different questions.

**fra.1 2019/20 yields 380 accepted but only 279 model-eligible.** The remaining **101 are
`STATUS_CANCELED`** — Ligue 1 was abandoned in March 2020 and never resumed; the last eligible
kickoff is **2020-03-08**. This is precisely the anomaly Epic 2A predicted. The cancelled fixtures are
stored as observed (no result, `has_result` false) and excluded from the model view by absence of a
result, not by a special case. **The season is never padded to 380.**

A season having fewer than today's expected match count is a fact about football, not evidence of
corruption. The audit reports the gap and stops there.

---

## STATUS_FINAL_PEN — answered

Epic 2A flagged `STATUS_FINAL_PEN` events without knowing what they were. They are **postseason ties**
(see Paris FC–Lens above), not league matches.

Consequently "has a final score" is **not** treated as "valid regular-season league match". Match
completion semantics were left exactly as they were; eligibility is a **separate axis** layered on
top. Widening completion semantics would have changed the statistical definition of league history,
which is out of scope for a data Epic.

---

## Fail-closed behaviour

The dataset inherits 2B.1's refusals and adds none of its own leniency:

- A league-season whose identity cannot be verified is **absent**, not partial. A season assembled
  from one of two discovery windows is a partial season presented as a whole one — the exact defect
  2B.1 removed — so a failed window fails the whole request.
- A scoreboard response at the event limit is refused rather than returned possibly-truncated.
- `None` (provider failure) and `[]` (ESPN genuinely has nothing) stay distinct, per the Epic 1B.2
  error semantics. The build reports failures separately from empty seasons.
- Nothing is inferred from kickoff date, team membership, calendar year or the requested season, and
  `0` is never substituted for a missing score.

---

## Validation

- **pytest:** 1435 passed, 2 skipped (the 2 skips are the unresolved D1/D3 spec decisions, unchanged).
- **Golden regression (POISSON_V1):** 38 passed — the frozen baseline file was not touched.

- **ruff:** clean.
- **mypy:** clean.
- **Production safety:** `git diff` on `poisson.py`, `config.py`, `filters.py`, `decision.py` and
  `run3/` is **empty**. The only modified production file is `espn.py` (additive: historical adapter).

---

## Remaining risks / open decisions

1. **LEAK-001 is still open.** Model and filter inputs are now point-in-time correct, but **odds are
   still today's market**. A backtest of *recommendations* remains invalid until historical odds
   exist. Clean data does not by itself make a backtest trustworthy.
2. **The eligibility policy is recorded, not validated.** Excluding playoff fixtures is a defensible
   modelling judgement; it has not been *measured* against a holdout. That belongs to a modelling Epic.
3. **Pre-2010 history is unavailable** (GG-027): eng.1 2009/10 is corrupt at source and correctly
   refused. If early history is ever needed it must come from a second provider and be cross-checked.
4. **`UNCERTAIN` phases are league-specific.** New leagues may introduce phase strings the classifier
   has not seen; they will be excluded and reported rather than guessed, but each new league warrants
   an audit pass.

---

## Recommended next Epic

**Epic 2B.3 — historical odds, or model evaluation harness (not both).**

The dataset is now trustworthy enough to train and evaluate on, but *recommendation* backtesting is
still blocked on odds provenance alone. The honest sequencing is to close LEAK-001's last input
before producing any accuracy figure — a confident number computed against today's prices would be
the same class of error this Epic exists to prevent.
