"""
Shared record builders for the settlement/evaluation tests (Epic 2H-3).

These live here rather than in a test module because two test files need them,
and importing one test file from another gives that source file two module names
(`test_evaluation_input` and `tests.unit.test_evaluation_input`), which mypy
rejects outright.

Imported as `from helpers.settlement_fixtures import ...` - NOT `tests.helpers`.
`tests/` is on `sys.path` and has no `__init__.py`, so `helpers` is the only name
under which both pytest and mypy resolve this file. That also matches the
existing precedent in this suite, where three test files already do
`from conftest import ...`.

The builders return plain `dict`s on purpose. Both logs are JSONL on disk and
the code under test reads untrusted mappings, so a test that passed a typed
object would be testing a stricter input than production ever sees. Overrides
go through `**extra`, which means a test can also inject a field that should
NOT be there - the leakage tests rely on that.
"""

from __future__ import annotations

from typing import Any

# Kickoff is after creation, and settlement is after kickoff: the real ordering
# of a prediction's life. Tests that care about ordering assert on these.
KICKOFF = "2026-08-15T15:00:00+00:00"
CREATED = "2026-08-15T09:00:00+00:00"
SETTLED_AT = "2026-08-17T12:00:00+00:00"


def prediction(
    prediction_id: str = "pred-1",
    fixture_id: Any = "740123",
    competition: str | None = "eng.1",
    season: int | None = 2026,
    probability: float | None = 0.55,
    status: str = "SCORED",
    **extra: Any,
) -> dict[str, Any]:
    """
    A ledger record as `prediction_ledger` writes it (schema `2g.1`).

    `fixture_id` is deliberately `Any`: the ledger stores a string, but a
    provider can hand back an int, and the join has to survive that.
    """
    record: dict[str, Any] = {
        "schema_version": "2g.1",
        "prediction_id": prediction_id,
        "run_id": "run-1",
        "created_at": CREATED,
        "fixture_id": fixture_id,
        "competition": competition,
        "season": season,
        "kickoff": KICKOFF,
        "home_team_id": "359",
        "away_team_id": "360",
        "status": status,
        "probability": probability,
        "home_sample": 5,
        "away_sample": 5,
        "league_sample": 40,
        "provenance": {"model_id": "POISSON_V1", "model_version": "1.0.0"},
    }
    record.update(extra)
    return record


def settlement(
    prediction_id: str = "pred-1",
    fixture_id: Any = "740123",
    competition: str | None = "eng.1",
    season: int | None = 2026,
    home: int | None = 2,
    away: int | None = 1,
    outcome: str = "YES",
    status: str = "SETTLED",
    reason: str | None = None,
    settled_at: str = SETTLED_AT,
    **extra: Any,
) -> dict[str, Any]:
    """
    A settlement record as `settle_predictions` writes it (schema `2h.1`).

    Defaults to 2-1: both teams scored, so the outcome is YES. `matched_season`
    mirrors `season` by default, since the rollover drift of 2H-F3 is the
    exception rather than the norm.
    """
    record: dict[str, Any] = {
        "schema_version": "2h.1",
        "prediction_id": prediction_id,
        "fixture_id": fixture_id,
        "competition": competition,
        "season": season,
        "matched_season": season,
        "final_home_goals": home,
        "final_away_goals": away,
        "gg_outcome": outcome,
        "settlement_status": status,
        "unresolved_reason": reason,
        "settled_at": settled_at,
        "source": "espn/scoreboard",
    }
    record.update(extra)
    return record


def unresolved(reason: str = "POSTPONED", **kwargs: Any) -> dict[str, Any]:
    """
    A fixture settlement looked at and could not grade.

    Note the score is `None` and the outcome `UNKNOWN` - NOT 0-0. A real
    goalless draw is a NO and must stay scoreable; conflating the two would
    quietly discard evidence.
    """
    return settlement(
        home=None,
        away=None,
        outcome="UNKNOWN",
        status="UNRESOLVED",
        reason=reason,
        **kwargs,
    )
