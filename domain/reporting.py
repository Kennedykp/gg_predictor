"""
Epic 2H-5 — evaluation reporting: grouping, rollup and deterministic ordering.

WHAT THIS MODULE IS FOR
-----------------------
Epic 2H-3 grades the ledger and reports ONE number per model version. That is the
right default and the wrong granularity for the only question an operator
actually asks after a bad week: *where* was it bad? A single Brier of 0.21 across
five competitions and two seasons hides a competition that is systematically
mispriced, because the good leagues average it away.

The information needed to answer that has been present since 2H-3 —
`EvaluationInput.join_key` is `(competition, season, fixture_id)` — but nothing
reachable from the ledger-graded path exposed it. The only existing breakdown
(`run_evaluation.py --breakdown competition`) sits on the RESEARCH harness, which
recomputes probabilities through today's model. So the only way to get a
per-competition number was the one route guaranteed not to describe what was
actually published.

This module closes that gap without inventing anything: it regroups the same
inputs along already-stored dimensions and hands each group to the frozen
`summarise`.

WHAT THIS MODULE MUST NOT BECOME
--------------------------------
No metric is defined here. Brier, log loss and calibration come from
`domain/evaluation.py` untouched, and this module cannot compute a probability —
it only ever reads `PredictionRecord.probability`, which came off the ledger.
Adding a metric here would put evaluation mathematics in two places, and the
copy would be the one that drifts.

Pure: no filesystem, no network, no clock. Grouping is a function of its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from domain.evaluation import MetricSummary, summarise
from domain.evaluation_input import EvaluationInput, SettlementState, to_prediction_records

__all__ = [
    "REPORTING_SCHEMA_VERSION",
    "MIXED",
    "UNKNOWN_SEASON",
    "Dimension",
    "GroupCounts",
    "Group",
    "group_inputs",
    "count_states",
    "summarise_dimension",
    "summarise_dimensions",
]

REPORTING_SCHEMA_VERSION = "2h5.1"

# Shown when a group spans more than one model version.
#
# A competition's fixtures may have been predicted by two different model
# versions across a season. Naming either one would attribute that group's Brier
# to a model that produced only part of it — a wrong answer that looks precise.
# `MIXED` is a deliberately unusable value: it forces the reader to break the
# group down by model before drawing a conclusion about a model.
MIXED = "MIXED"

# Sort/label placeholder for a record whose season is absent.
#
# `JoinKey`'s season is Optional, and `None` cannot be compared with `int` in a
# sort key. Substituting 0 would file it as a real season and place it before
# every genuine one, so the absence is labelled and sorted last instead.
UNKNOWN_SEASON = "UNKNOWN"


class Dimension(str, Enum):
    """
    The axes a report may be cut along.

    All five are derived from fields the ledger ALREADY stores. There is
    deliberately no `team` or `evidence` dimension: the first is not on
    `EvaluationInput`, and the second exists on the research harness where it can
    be computed from sample counts. Adding either here would mean widening the
    evaluation input contract, which is a different Epic.
    """

    OVERALL = "overall"
    MODEL = "model"
    COMPETITION = "competition"
    SEASON = "season"
    COMPETITION_SEASON = "competition_season"


@dataclass(frozen=True)
class GroupCounts:
    """
    Settlement states within one group, kept separate from the metrics.

    `unresolved` (football: postponed, abandoned) and `missing` (our pipeline has
    not settled it yet) are counted apart for the same reason Epic 2H-4 split the
    lifecycle stages: collapsing them makes a broken settlement job look like bad
    weather. A group whose `missing` count is climbing is an operational fault,
    and no Brier score will tell you that.
    """

    total: int
    settled: int
    unresolved: int
    missing: int

    @property
    def accounted_for(self) -> bool:
        """True when every input landed in exactly one state."""
        return self.settled + self.unresolved + self.missing == self.total


@dataclass(frozen=True)
class Group:
    """One row of a breakdown: what was measured, over what, and how much of it."""

    dimension: Dimension
    key: Tuple[str, ...]
    label: str
    counts: GroupCounts
    summary: MetricSummary

    @property
    def is_reportable(self) -> bool:
        """
        True when at least one prediction in the group was actually scored.

        A group of entirely unresolved fixtures has a `None` Brier, which is
        honest but not a finding. Callers use this to distinguish "measured and
        poor" from "not yet measurable" — two situations that a bare `null` in a
        JSON field conflates.
        """
        return self.summary.scored > 0


def _season_label(season: Optional[int]) -> str:
    return UNKNOWN_SEASON if season is None else str(season)


def _key_of(item: EvaluationInput, dimension: Dimension) -> Tuple[str, ...]:
    """
    The group key for one input, read from already-stored fields.

    `join_key` is `(competition, season, fixture_id)` and is only ever produced
    for records that carry a competition, so the competition component is safe to
    index. Season is not, hence `_season_label`.
    """
    competition, season, _fixture_id = item.join_key

    if dimension is Dimension.OVERALL:
        return ()
    if dimension is Dimension.MODEL:
        return (item.provenance.model_id, item.provenance.model_version)
    if dimension is Dimension.COMPETITION:
        return (competition,)
    if dimension is Dimension.SEASON:
        return (_season_label(season),)
    if dimension is Dimension.COMPETITION_SEASON:
        return (competition, _season_label(season))

    # Unreachable for a real `Dimension`; explicit so a future member added to
    # the enum without a branch here fails loudly instead of silently grouping
    # everything into one bucket and reporting a plausible wrong number.
    raise ValueError(f"unsupported reporting dimension: {dimension!r}")


def _sort_key(key: Tuple[str, ...]) -> Tuple[object, ...]:
    """
    Deterministic ordering, with absent seasons last.

    Ordering is part of the contract: two runs over identical data must produce
    byte-identical reports, so nothing may depend on dict insertion order. The
    leading flag pushes `UNKNOWN` to the end of each level rather than sorting it
    among real values, where "UNKNOWN" would land between numbers as a string.
    """
    return tuple(part for pair in ((p == UNKNOWN_SEASON, p) for p in key) for part in pair)


def group_inputs(
    inputs: Sequence[EvaluationInput],
    dimension: Dimension,
) -> Dict[Tuple[str, ...], List[EvaluationInput]]:
    """
    Partition inputs by `dimension`, in deterministic key order.

    Every input lands in exactly one group and none is dropped: an unresolved
    fixture is grouped like any other, because a group that silently excluded it
    would report coverage as a fraction of whatever survived. `summarise` applies
    its own `is_scored` filter downstream.
    """
    groups: Dict[Tuple[str, ...], List[EvaluationInput]] = {}
    for item in inputs:
        groups.setdefault(_key_of(item, dimension), []).append(item)
    return {key: groups[key] for key in sorted(groups, key=_sort_key)}


def count_states(inputs: Iterable[EvaluationInput]) -> GroupCounts:
    """
    Tally settlement states without collapsing any of them.

    An unrecognised state raises rather than being bucketed as `missing`: a new
    `SettlementState` member must be counted deliberately, and inflating the
    operational-gap number is the specific way this would mislead.
    """
    settled = unresolved = missing = total = 0
    for item in inputs:
        total += 1
        if item.settlement_state is SettlementState.SETTLED:
            settled += 1
        elif item.settlement_state is SettlementState.UNRESOLVED:
            unresolved += 1
        elif item.settlement_state is SettlementState.MISSING:
            missing += 1
        else:
            raise ValueError(
                f"unhandled settlement state {item.settlement_state!r} on "
                f"prediction {item.prediction_id!r}"
            )
    return GroupCounts(total=total, settled=settled, unresolved=unresolved, missing=missing)


def _identity_of(group: Sequence[EvaluationInput]) -> Tuple[str, str]:
    """
    The model identity of a group, or `MIXED` where it is not single-valued.

    `summarise` requires a model id and version. For a non-model dimension the
    group may legitimately span versions, and this is where that gets stated
    rather than guessed.
    """
    ids = {item.provenance.model_id for item in group}
    versions = {item.provenance.model_version for item in group}
    return (
        next(iter(ids)) if len(ids) == 1 else MIXED,
        next(iter(versions)) if len(versions) == 1 else MIXED,
    )


def summarise_dimension(
    inputs: Sequence[EvaluationInput],
    dimension: Dimension,
    *,
    bin_count: int = 10,
) -> List[Group]:
    """
    One `Group` per bucket along `dimension`, in deterministic order.

    The metrics come from the frozen `summarise`; this function only decides
    which records go together. `to_prediction_records` passes ALL inputs through,
    including unresolved ones, exactly as `summarise_by_model` does — so
    `coverage` in a breakdown means the same thing it means overall.
    """
    groups: List[Group] = []
    for key, members in group_inputs(inputs, dimension).items():
        model_id, model_version = _identity_of(members)
        groups.append(
            Group(
                dimension=dimension,
                key=key,
                label=" / ".join(key) if key else dimension.value,
                counts=count_states(members),
                summary=summarise(
                    to_prediction_records(members),
                    model_id=model_id,
                    model_version=model_version,
                    bin_count=bin_count,
                ),
            )
        )
    return groups


def summarise_dimensions(
    inputs: Sequence[EvaluationInput],
    dimensions: Sequence[Dimension],
    *,
    bin_count: int = 10,
) -> Mapping[Dimension, List[Group]]:
    """
    Several breakdowns of the same inputs, keyed by dimension.

    Duplicates are collapsed and the caller's order is preserved: a report asking
    for `competition` twice should not contain it twice, and reordering the flags
    should not reorder the artifact.
    """
    seen: Dict[Dimension, List[Group]] = {}
    for dimension in dimensions:
        if dimension not in seen:
            seen[dimension] = summarise_dimension(inputs, dimension, bin_count=bin_count)
    return seen
