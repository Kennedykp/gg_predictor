"""
Epic 2J regression firewall — the quality gate must never reach for the model.

WHY THIS FILE EXISTS
--------------------
`domain/quality.py` grades the probability that was PUBLISHED. The single most
plausible way for a future change to break that is not malice but convenience:
AUC over a sparse group looks unstable, someone reasons that a fresh probability
from today's model would be "more accurate", and imports `poisson` or calls
`evaluation_harness.replay()`. The resulting number would grade a model that
never made the prediction, against outcomes already known — hindsight presented
as track record, and the metric would look BETTER for it, which is what makes it
dangerous rather than merely wrong.

Runtime tests cannot catch that. A replay-contaminated evaluation returns
plausible floats and every arithmetic assertion still passes. So this suite
inspects the SOURCE: it parses the import graph with `ast` and asserts, as a
structural fact, that no forbidden module is reachable from the quality gate.

This mirrors `tests/regression/test_reporting_isolation.py` (2H-5) and
`test_evaluation_integration_isolation.py` (2H-3) deliberately. The pattern is
established; a different shape here would be a second thing to learn.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set

import pytest

import domain.quality as quality_module
from domain.quality import (
    MINIMUM_REPORTABLE_N,
    QualityAssessment,
    ReportStatus,
    assess_records,
)
from domain.reporting import Dimension, summarise_dimension

REPO_ROOT = Path(__file__).resolve().parents[2]

# The PURE modules, held to the strict transitive standard: nothing forbidden may
# be reachable from them at any depth.
PURE_MODULES = (
    "domain/quality.py",
    "domain/reporting.py",
)

# The CLI entry point, held to a direct-import standard plus an explicit
# allow-list below. See `ENTRY_POINT_KNOWN_TRANSITIVE`.
ENTRY_POINT_MODULES = ("report_evaluation.py",)

GUARDED_MODULES = PURE_MODULES + ENTRY_POINT_MODULES

# A KNOWN, PRE-EXISTING transitive edge, enumerated rather than ignored.
#
# `report_evaluation.py` imports `DEFAULT_SETTLEMENT_DIR` from
# `settle_predictions` — the constant `Path("data/settlements")`, so that the
# reporting CLI and the settlement CLI cannot disagree about where settlements
# live. `settle_predictions` needs a provider for its OWN work, so `espn` and
# `historical_dataset` become transitively reachable from the report.
#
# This predates Epic 2J and is not a leak: what crosses the boundary is a
# directory name, and `domain/reporting.py` — where the metrics are actually
# assembled — has no such path at any depth (asserted separately below). The 2H-5
# firewall checks direct imports only, which is why it never surfaced.
#
# It is pinned here rather than filtered out silently. The set is exact: if a
# FOURTH forbidden module becomes reachable, or if any of these becomes reachable
# from a pure module, the assertion fails and the change must be justified. The
# clean fix is a shared paths module, which is a refactor of frozen files and
# therefore not this Epic's to make; recorded in the 2J audit as a known risk.
ENTRY_POINT_KNOWN_TRANSITIVE = frozenset(
    {
        "espn",
        "historical_dataset",
        "domain.poisson_inputs",
    }
)


# Modules that recompute a probability, fetch live data, or replay history.
#
# `evaluation_harness` is the subtle one: it is legitimate RESEARCH code and its
# `replay()` is the correct tool for "how would today's model have done". It is
# forbidden HERE because its output is a probability that was never published.
FORBIDDEN_MODULES = frozenset(
    {
        "poisson",
        "evaluation_harness",
        "decision",
        "filters",
        "main",
        "espn",
        "api_football",
        "sofascore",
        "sportmonks",
        "odds_api",
        "historical_dataset",
        "domain.cold_start",
        "domain.goal_models",
        "domain.team_strength",
        "domain.poisson_inputs",
        "shared.odds",
    }
)

# Names that must not be called from the guarded modules even if the import were
# somehow legitimate. `replay` is the entry point to the contamination.
FORBIDDEN_CALLS = frozenset({"replay", "predict", "predict_btts", "recommend"})


def _module_name(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(relative.parts)
    return ".".join(parts)


def _imports_of(path: Path) -> Set[str]:
    """
    Every module named by an `import` in one file, absolute-resolved.

    Uses `ast` rather than a text search so that a forbidden name appearing in a
    docstring or comment — as it does, extensively, in these modules — is not
    mistaken for a real dependency. The explanations of WHY replay is excluded
    must not trip the test that enforces the exclusion.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import: resolve against the containing package.
                package = _module_name(path).rsplit(".", node.level)[0]
                found.add(f"{package}.{node.module}" if node.module else package)
            elif node.module:
                found.add(node.module)
    return found


def _local_path_of(module: str) -> Path | None:
    """The file backing a first-party module, or None for stdlib/third-party."""
    as_module = REPO_ROOT / (module.replace(".", "/") + ".py")
    if as_module.is_file():
        return as_module
    as_package = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    return as_package if as_package.is_file() else None


def _transitive_imports(entry: str) -> Dict[str, List[str]]:
    """
    Every first-party module reachable from `entry`, with the path that reached it.

    Transitive, not direct: an import of a clean-looking helper that itself
    imports `poisson` would defeat a one-level check, and that is exactly how
    this kind of leak arrives — through a module nobody thought of as model code.
    """
    start = _local_path_of(entry)
    assert start is not None, f"{entry} is not a first-party module"

    reached: Dict[str, List[str]] = {entry: [entry]}
    queue: List[str] = [entry]
    while queue:
        current = queue.pop()
        path = _local_path_of(current)
        if path is None:
            continue
        for imported in sorted(_imports_of(path)):
            if _local_path_of(imported) is None or imported in reached:
                continue
            reached[imported] = reached[current] + [imported]
            queue.append(imported)
    return reached


class TestImportFirewall:
    @pytest.mark.parametrize("module", PURE_MODULES)
    def test_pure_modules_reach_nothing_forbidden_at_any_depth(self, module: str) -> None:
        """
        The strict guarantee, where it actually matters.

        `domain/quality.py` and `domain/reporting.py` are where the metrics are
        assembled and the claim is decided. Nothing that could recompute a
        probability may be reachable from them at ANY depth.
        """
        entry = _module_name(REPO_ROOT / module)
        reached = _transitive_imports(entry)
        violations = {
            name: " -> ".join(chain)
            for name, chain in reached.items()
            if name in FORBIDDEN_MODULES
        }
        assert not violations, (
            f"{module} can reach model/replay code: {violations}. "
            "Evaluation must grade the published probability, never one recomputed today."
        )

    @pytest.mark.parametrize("module", ENTRY_POINT_MODULES)
    def test_entry_point_imports_nothing_forbidden_directly(self, module: str) -> None:
        """
        The CLI may not import model or replay code itself.

        Direct-import scope, matching the 2H-5 convention. The transitive picture
        is asserted separately and exactly, below.
        """
        direct = _imports_of(REPO_ROOT / module) & FORBIDDEN_MODULES
        assert not direct, f"{module} directly imports {direct}"

    @pytest.mark.parametrize("module", ENTRY_POINT_MODULES)
    def test_entry_point_transitive_reach_has_not_widened(self, module: str) -> None:
        """
        The known transitive edge is exactly what we documented — no more.

        This is the test that turns a pre-existing wart into a monitored one. It
        does not pass because the reach is empty; it passes because the reach is
        the enumerated set. Anything new fails, including — critically — `poisson`
        or `evaluation_harness` arriving by any route.
        """
        entry = _module_name(REPO_ROOT / module)
        reached = _transitive_imports(entry)
        forbidden = reached.keys() & FORBIDDEN_MODULES
        unexpected = {
            name: " -> ".join(reached[name])
            for name in forbidden - ENTRY_POINT_KNOWN_TRANSITIVE
        }
        assert not unexpected, (
            f"{module} gained a new path to model/replay code: {unexpected}"
        )

    @pytest.mark.parametrize("module", ENTRY_POINT_MODULES)
    def test_replay_is_unreachable_from_the_entry_point_even_transitively(
        self, module: str
    ) -> None:
        """
        The allow-list must never be read as tolerating replay.

        `espn` behind a path constant is a wart. A reachable `replay()` would be a
        different category of thing: a report that grades a probability nobody
        published. Asserted separately so that widening the allow-list cannot
        smuggle it in.
        """
        reached = set(_transitive_imports(_module_name(REPO_ROOT / module)))
        assert "evaluation_harness" not in reached
        assert "poisson" not in reached
        assert "run_evaluation" not in reached

    def test_the_allow_list_contains_no_probability_computing_module(self) -> None:
        # A guard on the guard's own exemptions: whatever is excused must be a
        # data-access or path artifact, never something that produces a number we
        # would grade. `domain.poisson_inputs` is named for the model but only
        # SHAPES provider data into model inputs; it computes no probability.
        assert "poisson" not in ENTRY_POINT_KNOWN_TRANSITIVE
        assert "evaluation_harness" not in ENTRY_POINT_KNOWN_TRANSITIVE
        assert "decision" not in ENTRY_POINT_KNOWN_TRANSITIVE


    @pytest.mark.parametrize("module", GUARDED_MODULES)
    def test_no_forbidden_call_appears(self, module: str) -> None:
        tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"), filename=module)
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        assert not called & FORBIDDEN_CALLS, f"{module} calls {called & FORBIDDEN_CALLS}"

    def test_the_gate_reaches_only_the_frozen_metric_modules(self) -> None:
        # Positive assertion: the gate's whole dependency set is the two frozen
        # metric modules and what they already needed. A new first-party
        # dependency here is a design change and should fail until reviewed.
        reached = set(_transitive_imports("domain.quality"))
        assert "domain.evaluation" in reached
        assert "domain.discrimination" in reached
        assert reached <= {
            "domain.quality",
            "domain.evaluation",
            "domain.discrimination",
            "domain.validation",
        }, f"unexpected dependency: {reached}"

    def test_the_firewall_would_notice_a_violation(self) -> None:
        # Guards the guard. If `_transitive_imports` silently returned nothing,
        # every test above would pass vacuously - so prove it detects a real,
        # known-bad edge: `run_evaluation.py` legitimately uses the harness.
        reached = _transitive_imports("run_evaluation")
        assert reached.keys() & FORBIDDEN_MODULES, (
            "the firewall found no forbidden module in a file known to contain one; "
            "the detector itself is broken"
        )


class TestNoModelStateInTheGate:
    def test_the_module_holds_no_provider_or_model_reference(self) -> None:
        # A module-level object could smuggle in behaviour the import graph misses.
        for name in dir(quality_module):
            if name.startswith("__"):
                continue
            value = getattr(quality_module, name)
            text = repr(type(value)) + repr(getattr(value, "__module__", ""))
            assert "poisson" not in text.lower()
            assert "harness" not in text.lower()

    def test_the_gate_is_deterministic_across_runs(self) -> None:
        # Anything time-, random- or network-dependent would show up here.
        from datetime import datetime, timezone

        from domain.evaluation import BttsOutcome, PredictionRecord


        records = [
            PredictionRecord(
                model_id="POISSON_V1",
                model_version="1.0.0",
                competition="eng.1",
                season=2026,
                event_id=f"7401{index:02d}",
                kickoff=datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc),
                home_team_id="H",
                away_team_id="A",
                outcome=BttsOutcome.YES if index % 2 else BttsOutcome.NO,
                probability=0.4 + (index % 5) / 100,
            )
            for index in range(120)
        ]
        first = assess_records(records, model_id="POISSON_V1", model_version="1.0.0")
        second = assess_records(records, model_id="POISSON_V1", model_version="1.0.0")
        assert first == second


class TestTwoGRFiveIsEnforcedEndToEnd:
    """
    The requirement, asserted through the real reporting path.

    Unit tests prove the gate classifies correctly. This proves the gate is
    actually WIRED — that a report built by `summarise_dimension` cannot present a
    lone Brier as a quality claim. A gate that is correct but unreachable would
    satisfy every test in `test_quality.py` and none of the requirement.
    """

    def _one_settled_input(self) -> list:
        from helpers.settlement_fixtures import prediction, settlement

        from domain.evaluation_input import join_for_evaluation

        inputs, _join = join_for_evaluation(
            [
                prediction(
                    prediction_id="p-1",
                    fixture_id="740001",
                    competition="eng.1",
                    season=2026,
                    probability=0.9,
                )
            ],
            [
                settlement(
                    prediction_id="p-1",
                    fixture_id="740001",
                    competition="eng.1",
                    season=2026,
                    home=2,
                    away=1,
                    outcome="YES",
                )
            ],
        )
        return inputs

    def test_a_brier_of_one_hundredth_over_one_observation_is_not_a_claim(self) -> None:
        group = summarise_dimension(self._one_settled_input(), Dimension.OVERALL)[0]
        assert group.summary.brier == pytest.approx(0.01)
        assert group.is_quality_claim is False
        assert group.status is ReportStatus.INSUFFICIENT_SAMPLE

    def test_every_group_carries_an_assessment(self) -> None:
        for dimension in Dimension:
            for group in summarise_dimension(self._one_settled_input(), dimension):
                assert isinstance(group.quality, QualityAssessment)

    def test_the_old_gate_and_the_new_one_disagree_on_purpose(self) -> None:
        # `is_reportable` says "a metric exists"; `is_quality_claim` says "it may
        # be believed". This n=1 group is the case that separates them, and the
        # separation is the fix.
        group = summarise_dimension(self._one_settled_input(), Dimension.OVERALL)[0]
        assert group.is_reportable is True
        assert group.is_quality_claim is False

    def test_the_default_policy_applies_without_being_asked_for(self) -> None:
        # The gate must be on by default. An opt-in gate protects nobody.
        group = summarise_dimension(self._one_settled_input(), Dimension.OVERALL)[0]
        assert group.quality.minimum_n == MINIMUM_REPORTABLE_N

    def test_metrics_remain_available_beneath_the_gate(self) -> None:
        # Transparency: the evidence is not deleted, only qualified.
        quality = summarise_dimension(self._one_settled_input(), Dimension.OVERALL)[0].quality
        assert quality.summary.brier is not None
        assert quality.constant_brier is not None
        assert quality.scored == 1
