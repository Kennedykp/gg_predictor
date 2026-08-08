"""
Domain contracts for the GG/BTTS pipeline.

Introduced by Epic 1B.1 to fix GG-001: a statistic an API never returned and a
statistic that is genuinely zero must not be represented identically.

    Unknown data is NOT zero.

Scope is deliberately narrow. These types describe only what the current GG
pipeline already handles — nothing speculative, no Pydantic, no new runtime
dependency. Plain stdlib dataclasses, because that is sufficient for typed,
immutable value objects and keeps the dependency surface unchanged.

The pipeline still passes provider dicts around; `from_provider_dict` adapters
bridge to these contracts without forcing a repo-wide restructure.
"""

from domain.availability import DataQuality, is_available, missing_fields
from domain.fixture import Fixture
from domain.stats import (
    LEGACY_FALLBACK_LEAGUE_AVERAGE,
    LeagueAverageSource,
    LeagueStats,
    TeamStats,
)
from domain.validation import (
    REQUIRED_POISSON_INPUTS,
    InputValidation,
    PoissonInputs,
    validate_poisson_inputs,
)

__all__ = [
    # availability
    "DataQuality",
    "is_available",
    "missing_fields",
    # entities
    "Fixture",
    "TeamStats",
    "LeagueStats",
    "LeagueAverageSource",
    "LEGACY_FALLBACK_LEAGUE_AVERAGE",
    # validation
    "PoissonInputs",
    "InputValidation",
    "validate_poisson_inputs",
    "REQUIRED_POISSON_INPUTS",
]
