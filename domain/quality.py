"""
Epic 2J — evaluation quality gates: what may be CLAIMED, not what is measured.

THE DEFECT THIS CLOSES (2G-R5)
------------------------------
Before this module, the ledger-graded reporting path could emit this, and mark it
reportable:

    overall    scored 1/1    brier 0.0100  log loss 0.1054

Every number there is arithmetically correct and the sentence it forms is a lie.
A Brier of 0.0100 over ONE observation is not evidence of model quality; it is
evidence that one coin came up the way we guessed. There was no AUC, no baseline
to compare against, and nothing in the output that said "one".

2G-R5 requires that a Brier score never be presented as evidence of model quality
on its own. This module is the gate that enforces it. Three things must accompany
a Brier score before it may be read as a quality claim:

    1. AUC, when AUC is mathematically defined - or an explicit statement of WHY
       it is undefined. Never a substituted 0.5.
    2. The constant-predictor Brier, computed over the same settled observations.
    3. Enough observations to justify the decimal places being printed.

WHAT THIS MODULE DOES NOT DO
----------------------------
It defines NO metric. Brier, log loss and calibration come from the frozen
`domain/evaluation.py`; AUC and the constant-predictor benchmark come from the
frozen `domain/discrimination.py`. This module calls them and classifies the
result. If a metric were computed here it would exist in two places, and the copy
would be the one that drifts.

It also cannot change a probability. `PredictionRecord.probability` arrives from
the ledger via `domain/evaluation_input.py` and is only ever read. There is no
import path from here to `poisson`, to `evaluation_harness`, or to any provider:
this module grades the probability that was PUBLISHED, never one recomputed
today. `tests/regression/test_quality_gates_isolation.py` enforces that
statically.

THE GATE CONTROLS THE CLAIM, NOT THE EVIDENCE
---------------------------------------------
Below the threshold the metrics are still computed and still carried on the
assessment. They are merely refused the status of a quality claim. Deleting them
would be the opposite failure to the one this Epic fixes: a report that hides its
own weak evidence cannot be audited either. The distinction is between "here is
what we measured" and "this is how good the model is", and only the second needs
a licence.

Pure: no filesystem, no network, no clock, no randomness. The same records always
produce the same assessment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from domain.discrimination import summarise_discrimination
from domain.evaluation import MetricSummary, PredictionRecord, summarise

__all__ = [
    "QUALITY_SCHEMA_VERSION",
    "MINIMUM_REPORTABLE_N",
    "BASELINE_EQUALITY_TOLERANCE",
    "ReportStatus",
    "BaselineVerdict",
    "QualityAssessment",
    "assess",
    "assess_records",
]

QUALITY_SCHEMA_VERSION = "2j.1"


# ---------------------------------------------------------------------------
# The minimum-n policy
# ---------------------------------------------------------------------------

# The number of SCORED observations a group needs before its metrics may be
# presented as evidence of model quality.
#
# WHERE 100 COMES FROM. It is derived from a stated tolerance for statistical
# uncertainty, not chosen for roundness. The observed BTTS base rate is a
# proportion, and the standard error of a proportion is sqrt(p(1-p)/n), maximised
# at p = 0.5:
#
#     SE = 0.5 / sqrt(n)
#
# Requiring that the rate be resolved to within +/-0.05 - five percentage points,
# one standard error, the coarsest precision at which a two-decimal Brier score
# is not actively misleading - gives:
#
#     0.5 / sqrt(n) <= 0.05  =>  sqrt(n) >= 10  =>  n >= 100
#
# WHAT 100 DOES NOT BUY, stated here because the number invites over-reading.
# By Hanley-McNeil, the standard error of AUC at A = 0.5 with balanced classes
# and n = 100 is approximately 0.058. Epic 2D measured POISSON_V1 at AUC ~ 0.54,
# an effect of 0.04 above chance - SMALLER than the standard error at this
# threshold. Distinguishing that from a coin flip needs an order of magnitude
# more data (n in the high hundreds).
#
# So clearing this gate makes a report PRESENTABLE, not CERTIFIED. It is a floor
# on false precision, not a test of skill. The report says so, in the status
# field, rather than leaving the reader to infer it.
#
# It is deliberately NOT in `config.py`. That module is frozen and model-facing,
# and a threshold living there would imply a tunable that changes predictions.
# This one changes only what a human is shown. The named-default-in-a-domain-
# module shape follows `DEFAULT_SETTLEMENT_GRACE` in `domain/lifecycle.py`.
MINIMUM_REPORTABLE_N = 100

# Two Brier scores closer than this are called equal rather than ranked.
#
# `constant_predictor_brier` at a base rate of exactly 0.0 or 1.0 returns 0.0,
# and a model that also scored 0.0 there is genuinely tied. Comparing floats with
# `==` would let accumulated rounding in the two independent summations report a
# 1e-17 difference as "WORSE than baseline", which is a false claim about the
# model built entirely out of float noise.
BASELINE_EQUALITY_TOLERANCE = 1e-12


class ReportStatus(str, Enum):
    """
    Whether a group's metrics may be read as a statement about model quality.

    Four values, because there are four genuinely different situations and
    collapsing any pair of them loses the operational signal that distinguishes
    them:

    `NOT_MEASURABLE` - nothing was scored. No metric exists. This is the state of
    a competition whose fixtures are all still awaiting settlement, and it is an
    operational fact, not a model result. Kept separate from
    `INSUFFICIENT_SAMPLE` for the reason Epic 2H-4 split the lifecycle stages: a
    broken settlement job must not read as a weak model.

    `INSUFFICIENT_SAMPLE` - metrics exist but there are fewer than
    `MINIMUM_REPORTABLE_N` of them. This is the n=1 case that 2G-R5 exists to
    catch.

    `AUC_UNDEFINED` - the sample is large enough, but every outcome fell on one
    side, so there are no positive/negative pairs to rank. Brier and calibration
    remain meaningful; discrimination is unmeasured, and a Brier score is at its
    most misleading here because the constant predictor scores 0.0 by definition.

    `REPORTABLE` - enough observations, both classes present, AUC defined,
    baseline computed. The only value under which the numbers may be quoted as
    evidence of quality.
    """

    NOT_MEASURABLE = "NOT_MEASURABLE"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    AUC_UNDEFINED = "AUC_UNDEFINED"
    REPORTABLE = "REPORTABLE"


class BaselineVerdict(str, Enum):
    """
    The model's standing against the constant predictor, named rather than signed.

    A signed delta alone is read wrongly about half the time, because "lower is
    better" for Brier inverts the usual intuition that a positive difference is
    good news. Naming the direction removes the coin flip.
    """

    BETTER = "BETTER"
    EQUAL = "EQUAL"
    WORSE = "WORSE"
    UNDEFINED = "UNDEFINED"


@dataclass(frozen=True)
class QualityAssessment:
    """
    One group's metrics, its discrimination evidence, and its licence to claim.

    Frozen, and carries the underlying `MetricSummary` by reference rather than
    copying fields out of it: there is exactly one definition of Brier in this
    repository and this object points at it.

    `minimum_n` is stored rather than looked up at read time so that an artifact
    read months later states the threshold it was actually judged against, even
    if the policy has since moved.
    """

    summary: MetricSummary
    scored: int
    positives: int
    negatives: int
    auc: Optional[float]
    auc_undefined_reason: Optional[str]
    constant_brier: Optional[float]
    brier_delta: Optional[float]
    baseline_verdict: BaselineVerdict
    status: ReportStatus
    minimum_n: int

    @property
    def is_quality_claim(self) -> bool:
        """
        True only when these numbers may be presented as evidence of quality.

        This is the single predicate 2G-R5 reduces to, and the one the regression
        pin asserts against. Everything else on this object is evidence; this is
        the licence to draw a conclusion from it.
        """
        return self.status is ReportStatus.REPORTABLE

    @property
    def shortfall(self) -> int:
        """How many more scored observations the group needs. Zero once cleared."""
        return max(0, self.minimum_n - self.scored)

    @property
    def headline(self) -> str:
        """
        One line stating what this group does and does not establish.

        Exists so that the console renderer and the JSON artifact cannot drift
        into describing the same assessment differently - the phrasing is decided
        once, here, next to the status that determines it.
        """
        if self.status is ReportStatus.NOT_MEASURABLE:
            return "not yet measurable - nothing scored"
        if self.status is ReportStatus.INSUFFICIENT_SAMPLE:
            return (
                f"INSUFFICIENT_SAMPLE - {self.scored} scored, "
                f"{self.minimum_n} required; not evidence of model quality"
            )
        if self.status is ReportStatus.AUC_UNDEFINED:
            return f"AUC undefined - {self.auc_undefined_reason}; discrimination unmeasured"
        return "reportable"


def _auc_undefined_reason(scored: int, positives: int, negatives: int) -> Optional[str]:
    """
    Why AUC could not be computed, in the reader's terms.

    Mirrors `auc_from_labelled`'s own two refusal conditions exactly - no pairs at
    all, or no pairs that straddle the classes - so the explanation can never
    describe a different cause than the one that actually applied. Returns None
    when AUC is defined, which is what makes "reason present" and "auc absent"
    two views of one fact rather than two fields that can disagree.
    """
    if scored == 0:
        return "no scored predictions"
    if positives == 0:
        return f"single-class sample (0 YES, {negatives} NO)"
    if negatives == 0:
        return f"single-class sample ({positives} YES, 0 NO)"
    return None


def _verdict(delta: Optional[float]) -> BaselineVerdict:
    """
    Name the direction of `brier_delta`.

    Lower Brier is better, so a NEGATIVE delta means the model beat the constant
    predictor. That inversion is the whole reason this function exists instead of
    a comparison at the call site.
    """
    if delta is None:
        return BaselineVerdict.UNDEFINED
    if abs(delta) <= BASELINE_EQUALITY_TOLERANCE:
        return BaselineVerdict.EQUAL
    return BaselineVerdict.BETTER if delta < 0 else BaselineVerdict.WORSE


def _status(scored: int, auc_defined: bool, minimum_n: int) -> ReportStatus:
    """
    Classify a group, most disqualifying condition first.

    The order matters and is not arbitrary. Sample size is checked BEFORE
    discrimination because with n below the threshold the AUC is untrustworthy
    even when it is mathematically defined - reporting `AUC_UNDEFINED` for an
    n=3 group would imply that a bigger sample was the only thing missing, and
    reporting `REPORTABLE` because two classes happened to appear would be the
    original defect wearing a new field.
    """
    if scored == 0:
        return ReportStatus.NOT_MEASURABLE
    if scored < minimum_n:
        return ReportStatus.INSUFFICIENT_SAMPLE
    if not auc_defined:
        return ReportStatus.AUC_UNDEFINED
    return ReportStatus.REPORTABLE


def assess(
    records: Sequence[PredictionRecord],
    summary: MetricSummary,
    *,
    minimum_n: int = MINIMUM_REPORTABLE_N,
) -> QualityAssessment:
    """
    Attach discrimination evidence and a report status to an existing summary.

    Takes the ALREADY-COMPUTED `MetricSummary` rather than recomputing it. The
    caller in `domain/reporting.py` has one in hand, and calling `summarise`
    again here would both double the work and create a second place where
    `bin_count` is decided - two calibration tables for one group, differing by
    whichever argument the second caller forgot to pass.

    `summarise_discrimination` supplies AUC, the constant-predictor Brier and the
    class counts in one pass over the same records, so the positive/negative
    tally can never disagree with the AUC that was derived from it.
    """
    discrimination = summarise_discrimination(
        records,
        model_id=summary.model_id,
        model_version=summary.model_version,
    )

    # `summary.brier` and `constant_brier` are both None exactly when nothing was
    # scored, so the delta is None in precisely that case. Guarding on both is
    # not redundant: it is what lets mypy see the subtraction as total.
    delta: Optional[float] = None
    if summary.brier is not None and discrimination.constant_brier is not None:
        delta = summary.brier - discrimination.constant_brier

    return QualityAssessment(
        summary=summary,
        scored=discrimination.scored,
        positives=discrimination.positives,
        negatives=discrimination.negatives,
        auc=discrimination.auc,
        auc_undefined_reason=_auc_undefined_reason(
            discrimination.scored,
            discrimination.positives,
            discrimination.negatives,
        ),
        constant_brier=discrimination.constant_brier,
        brier_delta=delta,
        baseline_verdict=_verdict(delta),
        status=_status(discrimination.scored, discrimination.auc is not None, minimum_n),
        minimum_n=minimum_n,
    )


def assess_records(
    records: Sequence[PredictionRecord],
    *,
    model_id: str,
    model_version: str,
    bin_count: int = 10,
    minimum_n: int = MINIMUM_REPORTABLE_N,
) -> QualityAssessment:
    """
    Assess a record set from scratch, for callers holding no `MetricSummary`.

    Convenience only: it calls the frozen `summarise` and hands the result
    straight to `assess`, so a standalone caller and the reporting path cannot
    produce different assessments of the same records.
    """
    return assess(
        records,
        summarise(
            records,
            model_id=model_id,
            model_version=model_version,
            bin_count=bin_count,
        ),
        minimum_n=minimum_n,
    )
