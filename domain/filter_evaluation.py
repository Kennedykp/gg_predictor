"""
The single GG filter-evaluation boundary (Epic 1B.3, TASK 10 / 12 / 13).

Before this module `main.py` and `analyze_all.py` each built their own filter
arguments, and they disagreed: main.py passed `total_goals_avg` (goals scored
PLUS conceded) while analyze_all.py passed single-side scoring rates, into the
same parameter. Same fixture, same provider data, two different filter verdicts
(GG-006). Both entry points now call `evaluate_filters` and nothing else.

Two states that used to look identical are now distinct:

    FAILED       - the statistic was measured and it breached a threshold.
    UNEVALUATED  - the statistic was never obtained, so no verdict exists.

Both block a recommendation. They are not the same fact, and reporting them as
the same fact is what made GG-002 invisible: "passed filters" was indistinguishable
from "filters never ran".

`filters.apply_filters` keeps its thresholds and its comparisons untouched. This
module only decides what reaches it, and refuses to call it at all when an input
is missing rather than substituting a number.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple

from domain.filter_stats import FilterStats
from filters import apply_filters

__all__ = ["FilterOutcome", "FilterResult", "FILTER_DATA_UNAVAILABLE", "evaluate_filters"]


# Distinct from any threshold-breach reason, so downstream output can tell
# "rejected on the numbers" from "could not be judged".
FILTER_DATA_UNAVAILABLE = "FILTER_DATA_UNAVAILABLE"


class FilterOutcome(str, Enum):
    PASSED = "PASSED"            # every filter evaluated, none breached
    FAILED = "FAILED"            # every filter evaluated, at least one breached
    UNEVALUATED = "UNEVALUATED"  # required statistic missing: no verdict possible


@dataclass(frozen=True)
class FilterResult:
    """
    Outcome of one filter evaluation.

    `passed` is kept as a plain bool so existing callers and output code keep
    working unchanged. It is False for UNEVALUATED, which is the safe direction:
    an unevaluated filter must never look like a pass.
    """

    outcome: FilterOutcome
    reasons: List[str] = field(default_factory=list)
    unavailable_fields: Tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome is FilterOutcome.PASSED

    @property
    def was_evaluated(self) -> bool:
        """False when filter data was missing - the verdict is absent, not negative."""
        return self.outcome is not FilterOutcome.UNEVALUATED

    @property
    def allows_recommendation(self) -> bool:
        """
        Whether a bet may be recommended.

        Only an explicit PASS qualifies. TASK 10: unavailable filter data must
        never fall through to a recommendation.
        """
        return self.passed


def evaluate_filters(stats: FilterStats) -> FilterResult:
    """
    Evaluate the GG hard filters against validated statistics.

    Returns UNEVALUATED without calling `apply_filters` when any required
    statistic is missing. That ordering is deliberate: there is no value to pass
    for an absent statistic, and the two historical answers - 0.0 (a lie about
    the team) and 0.5 (a lie chosen because it passes) - are both fabrications.
    """
    unavailable = stats.unavailable_fields()
    if unavailable:
        return FilterResult(
            outcome=FilterOutcome.UNEVALUATED,
            reasons=[f"{FILTER_DATA_UNAVAILABLE}: {', '.join(unavailable)}"],
            unavailable_fields=unavailable,
        )

    # Narrowed by the check above; restated for mypy, which cannot see through
    # the tuple-based availability check.
    assert stats.home_avg_goals_scored is not None
    assert stats.away_avg_goals_scored is not None
    assert stats.home_clean_sheet_pct is not None
    assert stats.away_clean_sheet_pct is not None

    passes, reasons = apply_filters(
        home_avg_goals=stats.home_avg_goals_scored,
        away_avg_goals=stats.away_avg_goals_scored,
        home_clean_sheet_pct=stats.home_clean_sheet_pct,
        away_clean_sheet_pct=stats.away_clean_sheet_pct,
        is_knockout_first_leg=stats.is_knockout_first_leg,
        is_heavy_favorite_mismatch=stats.is_heavy_favorite_mismatch,
        # True here means "the required inputs are present", which the guard
        # above has just established. It is not an assumption.
        has_reliable_data=True,
    )

    return FilterResult(
        outcome=FilterOutcome.PASSED if passes else FilterOutcome.FAILED,
        reasons=list(reasons),
    )
