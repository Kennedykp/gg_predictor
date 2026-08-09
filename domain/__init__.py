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

Epic 1B.3 adds the filter-side equivalents (GG-002): `FilterStats` carries the
inputs the hard filters compare against thresholds, and `evaluate_filters` is
the single boundary both entry points use, so they cannot interpret the same
statistic differently (GG-006).
"""

from domain.availability import DataQuality, is_available, missing_fields
from domain.filter_evaluation import (
    FILTER_DATA_UNAVAILABLE,
    FilterOutcome,
    FilterResult,
    evaluate_filters,
)
from domain.filter_stats import (
    REQUIRED_FILTER_FIELDS,
    FilterStats,
    StatSource,
    build_filter_stats,
)
from domain.fixture import Fixture
from domain.match_records import (
    DerivedHistory,
    MatchRecord,
    Venue,
    both_teams_scored_pct,
    clean_sheet_pct,
    completed_matches,
    derive_history,
    eligible_history,
)
from domain.poisson_inputs import (
    LeagueBaseline,
    PointInTimePoissonInputs,
    VenueGoalAverages,
    build_poisson_inputs,
    derive_league_baseline,
    derive_venue_averages,
)
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
    # filter inputs (Epic 1B.3)
    "FilterStats",
    "StatSource",
    "build_filter_stats",
    "REQUIRED_FILTER_FIELDS",
    # filter evaluation (Epic 1B.3)
    "evaluate_filters",
    "FilterResult",
    "FilterOutcome",
    "FILTER_DATA_UNAVAILABLE",
    # exact match-level derivations (Epic 1B.3)
    "MatchRecord",
    "Venue",
    "clean_sheet_pct",
    "both_teams_scored_pct",
    "completed_matches",
    # point-in-time match history (Epic 1B.4)
    "DerivedHistory",
    "derive_history",
    "eligible_history",
    # point-in-time model inputs (Epic 1B.5)
    #
    # `PointInTimePoissonInputs` is deliberately NOT named `PoissonInputs`:
    # `domain.validation` already exports that name for the validated tuple the
    # model is called with. Two same-named contracts differing only in
    # provenance would be a genuine hazard, since the whole point of this Epic
    # is that provenance is the difference.
    "PointInTimePoissonInputs",
    "VenueGoalAverages",
    "LeagueBaseline",
    "derive_venue_averages",
    "derive_league_baseline",
    "build_poisson_inputs",
]

