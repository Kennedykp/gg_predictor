# GG Predictor — Test Suite

Added in **Epic 1A**. Purpose: freeze current behaviour before Epic 1B changes the data layer.

## Layout

```
tests/
├── unit/                                  behaviour of individual pure functions
│   ├── test_poisson.py                    POISSON_V1 inputs, validation, edge cases
│   ├── test_poisson_invariants.py         mathematical properties
│   ├── test_filters.py                    hard-filter behaviour (pure function)
│   └── test_decision.py                   edge / bet-decision behaviour
└── regression/
    ├── test_poisson_v1_regression.py      GOLDEN frozen input/output pairs
    └── test_spec_agreement.py             code vs GG.md formulas
```

## Markers

| Marker | Meaning |
|---|---|
| `golden` | Frozen POISSON_V1 values. **A failure means the model changed.** |
| `characterization` | Documents **current** behaviour, including known-wrong legacy behaviour. Not a statement of desired behaviour. |
| `invariant` | A mathematical property that genuinely follows from the current implementation. |
| `spec` | Verifies agreement between the code and `GG.md`. |

Run a subset with e.g. `pytest -m golden`.

## Important

Tests marked `characterization` deliberately assert behaviour that Epic 0 identified as **wrong**
(for example: a missing statistic arriving as `0.0` is accepted as real data). They exist so that when
Epic 1B changes that behaviour, the change is **visible and deliberate** rather than silent. When the
behaviour is intentionally fixed, these tests are expected to be updated as part of that work.

Every test in this suite is **offline and deterministic** — no network, no API keys, no `.env`, no
clock or date dependency.

## Not covered

Providers (`espn.py`), odds clients and entry points have no tests yet — they require HTTP mocking and
belong to Epic 1B. Run-3 is out of scope and untouched.
