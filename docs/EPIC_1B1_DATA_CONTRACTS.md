# Epic 1B.1 — Data Contracts: missing data is no longer zero

Closes **GG-001**, the CRITICAL defect Epic 0 identified as the root cause that amplified every
other data problem in the system.

---

## The defect

`espn.get_stat()` ended with `, 0)` in two places:

```python
return next((s.get("value", 0) for s in stats_list if s.get("name") == name), 0)
```

A statistic ESPN never sent and a statistic genuinely equal to zero both arrived as `0`. Nothing
downstream could tell them apart, because the information was destroyed at the point of ingestion.

`poisson.py` guards `None` and negatives, but `0.0` passes its `val < 0` check — correctly, since a
genuine `0.0` is real data. So fabricated zeros flowed straight into the model.

### Why this was CRITICAL

`λ_home = (home_scored × away_conceded) / league_avg`. A single fabricated `0` sets `λ_home = 0`,
which makes `P(GG_YES) = 0.0`. In `analyze_all.py`:

```python
gg_no_prob = 1 - gg_yes_prob   # 1 - 0.0 = 1.0
```

The system published a **100%-confident `GG_NO`** — derived entirely from a statistic that never
arrived — and priced it against real odds. At a realistic price of 1.80 that classifies as
`STRONG_VALUE` with `system_recommendation: RECOMMEND_PLAY`.

A confident recommendation from absent data is worse than no recommendation, because nothing in the
output distinguishes it from a real one.

### Verified before changing anything

A regression test written against the **unmodified** code, feeding ESPN payloads with statistics
omitted: **9 failures**, including the 100%-confident `GG_NO` and `RECOMMEND_PLAY` on missing data.
The defect was reproduced first, then fixed.

---

## What changed

### 1. The provider reports absence as absence — `espn.py`

```python
for stat in stats_list:
    if stat.get("name") == name:
        return stat.get("value")   # present but no "value" key -> None
return None                        # entry absent entirely -> None
```

Three cases are now distinct:

| Case | Before | After |
|---|---|---|
| Entry absent from `stats_list` | `0` | `None` |
| Entry present, no `"value"` key | `0` | `None` |
| Entry present, value `0` | `0` | `0` — genuine zero, real data |

Per-match rates return `None` when their underlying total was never supplied, rather than dividing a
value that does not exist. `total_goals_avg` is `None` if either component is missing.

### 2. Typed contracts — `domain/`

| Module | Contents |
|---|---|
| `availability.py` | `is_available()`, `DataQuality`, `missing_fields()` |
| `stats.py` | `TeamStats`, `LeagueStats`, `LeagueAverageSource` |
| `fixture.py` | `Fixture` |
| `validation.py` | `validate_poisson_inputs()`, `PoissonInputs`, `ValidationResult` |

Every optional statistic is `Optional[float]`. `None` means not supplied; `0.0` means genuinely zero.

`is_available()` is an explicit `is not None` check rather than a truthiness test, because
`if value:` treats a genuine `0.0` as absent — the exact confusion this epic removes.

The dataclasses are frozen: they record what an API returned at a point in time, and mutating them
afterwards would make a prediction impossible to reproduce.

### 3. Validation before the model — `domain/validation.py`

`validate_poisson_inputs()` checks the five inputs POISSON_V1 requires and reports **all** gaps in one
pass, so one run shows every missing field rather than only the first.

The essential guarantee: when anything is missing, the result carries **no inputs at all**
(`inputs is None`). There is no substituted `0`, no substituted league average, no borrowed figures
from the other team. Incomplete data cannot reach the model, by construction rather than by
convention.

Sides are bound correctly — the home team contributes its home split, the away team its away split.
A transposition here would silently change every probability, so it is pinned by test.

### 4. Both entry points refuse instead of predicting

`main.py` → rejection reason naming the absent input; decision stays `NO BET`.
`analyze_all.py` → `model_probability: None`, `filter_status: MISSING_DATA`,
`system_recommendation: RECOMMEND_NO_PLAY`. The 100%-confident `GG_NO` path is closed.

**Filter inputs are guarded too.** They can now be `None`, and `None < 1.0` raises `TypeError` — the
fix would have crashed the run on the first incomplete fixture. An unevaluable filter rejects the
fixture as unreliable rather than comparing against an invented number. Thresholds are untouched.

---

## What deliberately did NOT change

**`poisson.py` was not touched.** POISSON_V1 is the frozen baseline. It still accepts `0.0` as valid,
which is correct — a genuine `0.0` *is* valid data. The defect was never in the model; it was that
something upstream fabricated the zero. Fixing it in the model would have hidden the real bug and
broken legitimate goalless-team predictions.

Also unchanged: `filters.py`, `decision.py`, `config.py`, `run3/`, `shared/` — confirmed by
`git diff --stat` returning empty for all of them. No formula, threshold or decision rule was altered.

### Known-fabricated values left in place

Each of these changes production output and needs its own sub-epic:

| ID | Value | Why left |
|---|---|---|
| GG-002 | `home/away_clean_sheet_pct = 0` | ESPN supplies no clean-sheet data. Marking it unavailable would reject **every** fixture. |
| GG-003 | League average `1.35` | Substituted inside the provider, so callers cannot attribute it — hence `UNATTRIBUTED`. |
| GG-004 | Home/away counts halved | Correcting the divisor changes every rate and every published probability. |

On GG-003: the honest option was to admit the layer cannot know. Inferring from the value
(`== 1.35 → fallback`) was considered and rejected — a real league average can legitimately be 1.35,
so that check would mislabel genuine data. `LeagueAverageSource.UNATTRIBUTED` records "obtained, origin
unknown", and is not trustworthy because it cannot be *shown* to be. Epic 1B.2 removes the state by
making the provider report its own source.

---

## Verification

| Check | Result |
|---|---|
| Full suite | **946 passed**, 4 skipped |
| Golden regression (POISSON_V1) | **51 passed, unchanged** — identical output for complete data |
| `ruff check .` | All checks passed |
| `mypy domain/` | Success, no issues |
| `git diff` on model/filters/decision/config/run3/shared | **empty** |

The golden suite passing unchanged is the important one: it proves complete data still produces
byte-identical predictions. Only the missing-data path behaves differently.

### New coverage

- `tests/unit/test_domain_contracts.py` — contract behaviour, including that a genuinely goalless
  team still validates (over-correcting into "0 means missing" would be a new bug).
- `tests/unit/test_espn_missing_data.py` — provider-level, per-statistic.
- `tests/integration/test_pipeline_missing_data.py` — end-to-end, with odds mocked at a deliberately
  generous 1.80 so that any fabricated probability reaching the odds layer again would classify as
  `STRONG_VALUE` and fail loudly rather than pass quietly.

Every test asserts both directions: missing data is refused, **and** complete data still predicts.

### Spec agreement

**D2 is resolved.** `GG.md` §6 — "if any of these are missing → NO BET" — was previously
unenforceable and is now enforced. The skipped placeholder in `tests/regression/test_spec_agreement.py`
has been replaced by a real passing assertion. D1, D3, D4 and D5 remain open product decisions.

---

## What this unblocks

`DataQuality` and `LeagueAverageSource` give later work somewhere to record provenance, which
backtesting and model comparison need — a Dixon-Coles-vs-Poisson comparison run on fabricated inputs
would produce confident numbers justifying a wrong choice.

Recommended next: **GG-003** (real league average — it is the denominator of both λ values, so one
constant currently scales every prediction the system makes), then GG-004, then GG-002.

Note that this epic does **not** address **LEAK-001**. `get_team_stats()` still takes no date
parameter, so historical backtesting remains invalid regardless of data quality.
