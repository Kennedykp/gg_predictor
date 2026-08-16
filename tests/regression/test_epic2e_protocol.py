"""
Epic 2E - the protocol, enforced structurally rather than trusted.

Epic 2E rests on two claims that would each invalidate the whole epic if false:

    1. the shot statistics are PER-MATCH observations, not season aggregates
    2. `competitor.form` - which is CONTAMINATED - never reaches a feature

Claim 2 is the dangerous one. `form` looks exactly like a legitimate feature and
is sitting in the same dictionary as the fields Epic 2E does use. Measured, not
assumed: a fra.1 fixture played on 2025-08-15, the opening weekend, carries
`form='LWLWW'`. Five results that did not exist yet. ESPN populates it as of
RETRIEVAL - 2026-08-09 for this cache - so it is end-of-season information
sitting on a matchday-1 fixture. Using it would recreate LEAK-001 exactly.

A comment saying "do not read form" is not a control, because the next person to
touch the extractor will not read the comment. These tests are the control: they
rewrite `form` to absurd values and require every output to be bit-identical.

The quarantine tests exist for the same reason as Epic 2D's: a leaky probe that
escapes into a candidate table is worse than no probe at all, because its number
looks like a result.
"""

from __future__ import annotations

import ast
import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.historical import HistoricalMatch
from research import epic2e_experiment as exp
from research import epic2e_shot_stats as shots

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _competitor(
    *,
    team_id: str = "1",
    home_away: str = "home",
    score: str = "2",
    shots_total: str = "14",
    on_target: str = "5",
    possession: str = "55.4",
    form: str = "WWWWW",
) -> dict:
    """A competitor block shaped like ESPN's, including the banned fields."""
    return {
        "id": team_id,
        "homeAway": home_away,
        "score": score,
        "form": form,
        "records": [{"name": "All Splits", "summary": "10-4-4"}],
        "statistics": [
            {"name": "totalShots", "displayValue": shots_total},
            {"name": "shotsOnTarget", "displayValue": on_target},
            {"name": "possessionPct", "displayValue": possession},
            {"name": "wonCorners", "displayValue": "7"},
            {"name": "shotAssists", "displayValue": "10"},
            {"name": "totalGoals", "displayValue": score},
        ],
    }


def _event(*, event_id: str = "401", season: int = 2018, **kwargs) -> dict:
    return {
        "id": event_id,
        "date": "2018-08-11T14:00Z",
        "season": {"year": season},
        "competitions": [
            {
                "id": event_id,
                "competitors": [
                    _competitor(team_id="1", home_away="home", **kwargs),
                    _competitor(team_id="2", home_away="away", score="1", **kwargs),
                ],
            }
        ],
    }


class TestContaminatedFormIsUnreachable:
    """
    The central leakage control. `form` and `records` must not influence anything.

    Not "must not be used deliberately" - must not be *reachable*. The allowlist
    in `_permitted_view` is what makes that true, and these tests are what keep
    it true.
    """

    def test_permitted_view_drops_the_contaminated_fields(self) -> None:
        view = shots._permitted_view(_competitor())
        for banned in shots.BANNED_COMPETITOR_KEYS:
            assert banned not in view, (
                f"{banned!r} survived the allowlist. It is populated as of cache "
                "retrieval, i.e. after the season ended, and would leak the answer."
            )

    def test_form_is_not_in_the_allowlist(self) -> None:
        assert "form" not in shots.PERMITTED_COMPETITOR_KEYS
        assert "records" not in shots.PERMITTED_COMPETITOR_KEYS

    def test_rewriting_form_changes_no_extracted_value(self) -> None:
        """
        The proof. Same fixture, wildly different `form`, identical output.

        If any future edit reads `form`, this test fails immediately.
        """
        honest = _event()
        tampered = copy.deepcopy(honest)
        for competitor in tampered["competitions"][0]["competitors"]:
            competitor["form"] = "LLLLLLLLLL"
            competitor["records"] = [{"name": "All Splits", "summary": "0-0-38"}]

        assert shots.profile_from_event(honest, "eng.1") == shots.profile_from_event(
            tampered, "eng.1"
        )

    def test_deleting_form_entirely_changes_nothing(self) -> None:
        """Absence must be as harmless as presence: no code path depends on it."""
        with_form = _event()
        without = copy.deepcopy(with_form)
        for competitor in without["competitions"][0]["competitors"]:
            del competitor["form"]
            del competitor["records"]

        assert shots.profile_from_event(with_form, "eng.1") == shots.profile_from_event(
            without, "eng.1"
        )

    def test_no_source_line_in_epic2e_mentions_the_banned_fields(self) -> None:
        """
        A grep-level backstop, scoped to string literals and attribute access.

        The words appear in prose in both modules (explaining the ban), so the
        check is on the parsed source: no string literal equal to a banned name,
        and no attribute of that name.
        """
        for module in ("research/epic2e_shot_stats.py", "research/epic2e_experiment.py"):
            tree = ast.parse((REPO_ROOT / module).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # The allowlist/banlist constants themselves are the exception:
                    # they must name the fields in order to exclude them.
                    if node.value in shots.BANNED_COMPETITOR_KEYS:
                        assert module.endswith("epic2e_shot_stats.py"), (
                            f"{module} contains the literal {node.value!r}; only the "
                            "sidecar's banlist may name it"
                        )
                if isinstance(node, ast.Attribute):
                    assert node.attr not in shots.BANNED_COMPETITOR_KEYS, (
                        f"{module} accesses .{node.attr}, which is contaminated"
                    )


class TestMissingIsNotZero:
    """GG-001: a provider's zero is not an observation of zero."""

    def test_zero_possession_is_unavailable_not_a_measurement(self) -> None:
        event = _event(possession="0.0", shots_total="0", on_target="0")
        profile = shots.profile_from_event(event, "eng.1")
        assert profile is not None
        assert not profile.available, (
            "0.0% possession in a played match is physically impossible; it is the "
            "missing-as-zero signature and must not be treated as data"
        )
        assert profile.home.shots is None, "an unavailable line must not carry numbers"

    def test_a_genuine_zero_shot_line_is_still_available(self) -> None:
        """
        The distinction that matters: a team CAN take zero shots on target.

        Availability turns on possession, so a real 0-on-target performance with
        real possession is kept. Conflating the two would silently discard
        exactly the low-scoring fixtures BTTS cares about most.
        """
        event = _event(possession="42.0", shots_total="3", on_target="0")
        profile = shots.profile_from_event(event, "eng.1")
        assert profile is not None
        assert profile.available
        assert profile.home.shots_on_target == 0

    def test_one_sided_availability_refuses_the_fixture(self) -> None:
        event = _event()
        event["competitions"][0]["competitors"][1] = _competitor(
            team_id="2", home_away="away", score="1", possession="0.0",
            shots_total="0", on_target="0",
        )
        profile = shots.profile_from_event(event, "eng.1")
        assert profile is not None
        assert not profile.available

    def test_impossible_line_is_refused(self) -> None:
        """shotsOnTarget > totalShots cannot happen; such a block is not trusted."""
        event = _event(shots_total="3", on_target="9")
        profile = shots.profile_from_event(event, "eng.1")
        assert profile is not None
        assert not profile.available

    def test_available_line_cannot_be_constructed_without_numbers(self) -> None:
        with pytest.raises(ValueError):
            shots.TeamShotLine(team_id="1", is_home=True, available=True)


class TestSeasonIdentityIsThePayloads:
    """Epic 2B.1's rule applies to the sidecar too: never trust the request."""

    def test_season_comes_from_the_payload(self) -> None:
        profile = shots.profile_from_event(_event(season=2019), "eng.1")
        assert profile is not None
        assert profile.season == 2019

    def test_event_without_a_season_is_refused(self) -> None:
        event = _event()
        del event["season"]
        assert shots.profile_from_event(event, "eng.1") is None


class TestQuarantine:
    """A leaky probe must be impossible to mistake for a model."""

    @pytest.mark.parametrize(
        "arm",
        [exp.GoalOracleLeaky, exp.ShotOracleLeaky, exp.ShotOracleActual],
    )
    def test_model_id_and_version_announce_the_leak(self, arm) -> None:
        assert "LEAKY" in arm.model_id
        assert "quarantined" in arm.model_version

    @pytest.mark.parametrize(
        "arm",
        [exp.GoalOracleLeaky, exp.ShotOracleLeaky, exp.ShotOracleActual],
    )
    def test_docstring_says_it_is_not_a_model(self, arm) -> None:
        doc = arm.__doc__ or ""
        assert "NOT A MODEL" in doc
        assert "QUARANTINED" in doc

    def test_no_leaky_arm_is_in_the_harness_registry(self) -> None:
        import evaluation_harness

        registry = getattr(evaluation_harness, "MODEL_REGISTRY", None)
        if registry is None:
            pytest.skip("harness exposes no registry")
        names = [str(name).upper() for name in registry]
        assert not any("ORACLE" in name for name in names)

    def test_production_does_not_import_the_research_modules(self) -> None:
        """
        The isolation requirement: production gains nothing from this experiment.

        Also asserts the corollary the approval called out - production still has
        no shot parsing, so the cache containing the fields has not quietly
        become a production dependency.
        """
        for module in ("espn.py", "poisson.py", "decision.py", "filters.py", "main.py"):
            source = (REPO_ROOT / module).read_text()
            assert "epic2e" not in source, f"{module} imports Epic 2E research code"

        espn_source = (REPO_ROOT / "espn.py").read_text().lower()
        for token in ("shotsontarget", "totalshots", "possessionpct"):
            assert token not in espn_source, (
                f"espn.py now references {token!r}; Epic 2E must not add shot "
                "parsing to production"
            )


class TestBurnedSeasonsAreComplete:
    """
    The holdout is only untouched if the burned list is honest about the rest.

    Epic 2D's own constant omitted its validation and holdout seasons. That file
    is historical and is deliberately not edited; this list is the complete one.
    """

    @pytest.mark.parametrize("season", [2018, 2019, 2020, 2021, 2022, 2023, 2024])
    def test_every_previously_scored_season_is_recorded(self, season: int) -> None:
        assert season in exp.BURNED_SEASONS

    def test_holdout_is_not_burned(self) -> None:
        assert exp.HOLDOUT_SEASON not in exp.BURNED_SEASONS

    def test_stage0_does_not_use_the_holdout(self) -> None:
        assert exp.HOLDOUT_SEASON not in exp.STAGE0_SEASONS

    def test_building_a_dataset_that_would_touch_the_holdout_raises(self) -> None:
        """
        Loading 2026 would pull 2025 in as the previous season. Refused loudly.

        A silent inclusion is the failure mode that matters: the reported ceiling
        would then have been measured partly on the untouched holdout.
        """
        with pytest.raises(AssertionError, match="holdout"):
            exp.build_dataset(["eng.1"], [exp.HOLDOUT_SEASON + 1])

    def test_the_gate_threshold_is_the_pre_registered_one(self) -> None:
        """
        The threshold was fixed before the result was seen and must not move.

        Pinned in a test so that changing it after seeing a number requires
        editing this assertion, which is a visible act in review.
        """
        assert exp.STAGE0_GATE == 0.60
        assert exp.EPIC2D_PUBLISHED_CEILING == 0.5679


class TestShotSurrogateIsFaithful:
    """The surrogate must swap the counts and change nothing else."""

    def _match(self, event_id: str = "401") -> HistoricalMatch:
        return HistoricalMatch(
            event_id=event_id,
            competition="eng.1",
            season=2018,
            kickoff=datetime(2018, 8, 11, 14, 0, tzinfo=timezone.utc),
            home_team_id="1",
            away_team_id="2",
            completed=True,
            home_goals=2,
            away_goals=1,
        )

    def test_goals_are_replaced_by_shots_on_target(self) -> None:
        match = self._match()
        profiles = {"401": shots.profile_from_event(_event(), "eng.1")}
        surrogate = exp._shot_surrogate(match, profiles)
        assert surrogate is not None
        assert surrogate.home_goals == 5
        assert surrogate.away_goals == 5
        # identity preserved, so the fit sees the same fixture
        assert surrogate.event_id == match.event_id
        assert surrogate.kickoff == match.kickoff
        assert surrogate.home_team_id == match.home_team_id

    def test_unavailable_fixture_is_dropped_not_zeroed(self) -> None:
        match = self._match()
        profiles = {
            "401": shots.profile_from_event(
                _event(possession="0.0", shots_total="0", on_target="0"), "eng.1"
            )
        }
        assert exp._shot_surrogate(match, profiles) is None

    def test_missing_profile_is_dropped(self) -> None:
        assert exp._shot_surrogate(self._match(), {}) is None


class TestAbsoluteIntervalConstruction:
    """
    The CI trick must rest on a real property, not a coincidence.

    `absolute AUC CI = 0.5 + paired-delta CI against a constant arm` is only
    valid if a constant predictor scores exactly 0.5 under this AUC
    implementation, in every resample.
    """

    def test_constant_predictor_scores_exactly_one_half(self) -> None:
        from domain.discrimination import auc_from_labelled

        assert auc_from_labelled([(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]) == 0.5

    def test_constant_arm_emits_one_probability_for_every_fixture(self) -> None:
        arm = exp.ConstantArm()
        from evaluation_harness import PredictionContext

        context = PredictionContext(
            competition="eng.1",
            season=2018,
            event_id="401",
            kickoff=datetime(2018, 8, 11, 14, 0, tzinfo=timezone.utc),
            home_team_id="1",
            away_team_id="2",
            history=[],
        )
        assert arm.predict(context).probability == 0.5
