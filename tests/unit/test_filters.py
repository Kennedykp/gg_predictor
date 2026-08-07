"""
Unit / characterization tests for the GG hard filters (`filters.py`).

Epic 0 found that four of the five documented filters cannot currently fire in
production, because `main.py` hardcodes three flags and `espn.py` hardcodes the
clean-sheet rates to 0 (docs/TECHNICAL_DEBT.md GG-002).

That is a CALLER/PROVIDER wiring defect, not a defect in this module. These tests
therefore do two separate things:

  1. prove the pure `apply_filters` function behaves correctly when it IS given
     appropriate inputs - so the wiring fix in Epic 1B has a target to hit;
  2. document, as explicit characterization tests, that the values production
     currently supplies can never trigger a rejection.

No threshold and no caller was changed.
"""

import pytest

from config import MAX_CLEAN_SHEET_PCT, MIN_AVG_GOALS
from filters import apply_filters

# Inputs that pass every filter.
PASSING = {
    "home_avg_goals": 1.50,
    "away_avg_goals": 1.40,
    "home_clean_sheet_pct": 0.20,
    "away_clean_sheet_pct": 0.25,
}


class TestThresholdsUnchanged:
    """Guards against accidental threshold drift during future refactors."""

    def test_min_avg_goals_is_one(self):
        assert MIN_AVG_GOALS == 1.0

    def test_max_clean_sheet_pct_is_forty_percent(self):
        assert MAX_CLEAN_SHEET_PCT == 0.40


class TestPassingFixture:
    def test_normal_fixture_passes_with_no_reasons(self):
        passes, reasons = apply_filters(**PASSING)
        assert passes is True
        assert reasons == []

    def test_return_shape(self):
        passes, reasons = apply_filters(**PASSING)
        assert isinstance(passes, bool)
        assert isinstance(reasons, list)
        
    def test_optional_flags_default_to_permissive(self):
        # is_knockout_first_leg=False, is_heavy_favorite_mismatch=False,
        # has_reliable_data=True are the defaults.
        passes, reasons = apply_filters(1.5, 1.4, 0.2, 0.25)
        assert passes is True
        assert reasons == []


class TestEachRejectionConditionIndividually:
    """All five documented filters, each triggered on its own."""

    def test_home_below_min_avg_goals(self):
        args = dict(PASSING, home_avg_goals=0.80)
        passes, reasons = apply_filters(**args)
        assert passes is False
        assert len(reasons) == 1
        assert "Home team averages" in reasons[0]
        assert "0.80" in reasons[0]

    def test_away_below_min_avg_goals(self):
        args = dict(PASSING, away_avg_goals=0.55)
        passes, reasons = apply_filters(**args)
        assert passes is False
        assert len(reasons) == 1
        assert "Away team averages" in reasons[0]

    def test_home_above_max_clean_sheet_pct(self):
        args = dict(PASSING, home_clean_sheet_pct=0.55)
        passes, reasons = apply_filters(**args)
        assert passes is False
        assert len(reasons) == 1
        assert "Home team keeps" in reasons[0]
        assert "55.0%" in reasons[0]

    def test_away_above_max_clean_sheet_pct(self):
        args = dict(PASSING, away_clean_sheet_pct=0.60)
        passes, reasons = apply_filters(**args)
        assert passes is False
        assert len(reasons) == 1
        assert "Away team keeps" in reasons[0]

    def test_knockout_first_leg(self):
        passes, reasons = apply_filters(**PASSING, is_knockout_first_leg=True)
        assert passes is False
        assert reasons == ["First-leg knockout match"]

    def test_heavy_favorite_mismatch(self):
        passes, reasons = apply_filters(**PASSING, is_heavy_favorite_mismatch=True)
        assert passes is False
        assert reasons == ["Heavy favorite vs deep-defending underdog"]

    def test_unreliable_data(self):
        passes, reasons = apply_filters(**PASSING, has_reliable_data=False)
        assert passes is False
        assert reasons == ["Missing or unreliable data"]


class TestThresholdBoundaries:
    """Exact boundary semantics of each comparison."""

    def test_avg_goals_exactly_at_threshold_passes(self):
        # Comparison is `< MIN_AVG_GOALS`, so exactly 1.0 is allowed.
        args = dict(PASSING, home_avg_goals=1.0, away_avg_goals=1.0)
        passes, reasons = apply_filters(**args)
        assert passes is True
        assert reasons == []

    def test_avg_goals_just_below_threshold_fails(self):
        args = dict(PASSING, home_avg_goals=0.999999)
        passes, _ = apply_filters(**args)
        assert passes is False

    def test_clean_sheet_exactly_at_threshold_passes(self):
        # Comparison is `> MAX_CLEAN_SHEET_PCT`, so exactly 0.40 is allowed.
        args = dict(PASSING, home_clean_sheet_pct=0.40, away_clean_sheet_pct=0.40)
        passes, reasons = apply_filters(**args)
        assert passes is True
        assert reasons == []

    def test_clean_sheet_just_above_threshold_fails(self):
        args = dict(PASSING, home_clean_sheet_pct=0.400001)
        passes, _ = apply_filters(**args)
        assert passes is False

    def test_zero_avg_goals_fails(self):
        args = dict(PASSING, home_avg_goals=0.0)
        passes, _ = apply_filters(**args)
        assert passes is False


class TestCombinedRejections:
    def test_two_reasons_accumulate(self):
        args = dict(PASSING, home_avg_goals=0.5, away_clean_sheet_pct=0.7)
        passes, reasons = apply_filters(**args)
        assert passes is False
        assert len(reasons) == 2

    def test_all_reasons_accumulate_in_documented_order(self):
        passes, reasons = apply_filters(
            home_avg_goals=0.5,
            away_avg_goals=0.6,
            home_clean_sheet_pct=0.8,
            away_clean_sheet_pct=0.9,
            is_knockout_first_leg=True,
            is_heavy_favorite_mismatch=True,
            has_reliable_data=False,
        )
        assert passes is False
        assert len(reasons) == 7
        assert "Home team averages" in reasons[0]
        assert "Away team averages" in reasons[1]
        assert "Home team keeps" in reasons[2]
        assert "Away team keeps" in reasons[3]
        assert reasons[4] == "First-leg knockout match"
        assert reasons[5] == "Heavy favorite vs deep-defending underdog"
        assert reasons[6] == "Missing or unreliable data"

    def test_any_single_reason_is_enough_to_fail(self):
        passes, reasons = apply_filters(**PASSING, has_reliable_data=False)
        assert passes is False
        assert len(reasons) >= 1


class TestProductionWiringCannotTriggerFilters:
    """
    CHARACTERIZATION — docs/TECHNICAL_DEBT.md GG-002.

    Documents that the values production actually supplies make four of the five
    filters unreachable. These tests describe CURRENT wiring, not desired
    behaviour, and are expected to be updated when Epic 1B fixes the wiring.
    """

    @pytest.mark.characterization
    def test_hardcoded_zero_clean_sheet_can_never_trigger_rejection(self):
        # espn.get_team_stats() hardcodes both clean-sheet rates to 0.
        # The filter fires only when the value exceeds 0.40.
        assert not (0 > MAX_CLEAN_SHEET_PCT)
        args = dict(PASSING, home_clean_sheet_pct=0, away_clean_sheet_pct=0)
        passes, reasons = apply_filters(**args)
        assert passes is True
        assert reasons == []

    @pytest.mark.characterization
    def test_hardcoded_flags_can_never_trigger_rejection(self):
        # main.py passes is_knockout_first_leg=False,
        # is_heavy_favorite_mismatch=False, has_reliable_data=True literally.
        passes, reasons = apply_filters(
            **PASSING,
            is_knockout_first_leg=False,
            is_heavy_favorite_mismatch=False,
            has_reliable_data=True,
        )
        assert passes is True
        assert reasons == []

    @pytest.mark.characterization
    def test_combined_goals_average_lets_a_dire_team_through(self):
        """
        main.py supplies `total_goals_avg` = (goals_for + goals_against) / matches,
        i.e. BOTH teams' goals, into the parameter named `home_avg_goals`, which
        is documented in GG.md as goals *scored*.

        A side scoring 5 in 20 games while conceding 30 has a scoring rate of
        0.25 - far below the 1.0 threshold - yet its combined average is 1.75
        and it passes. This is GG-006 plus GG-002 acting together.
        """
        goals_for, goals_against, matches = 5, 30, 20
        combined_avg = (goals_for + goals_against) / matches
        scoring_rate = goals_for / matches

        assert scoring_rate < MIN_AVG_GOALS, "the team's actual scoring rate is far too low"
        assert combined_avg == 1.75

        args = dict(PASSING, home_avg_goals=combined_avg)
        passes, reasons = apply_filters(**args)
        assert passes is True, "current wiring lets this fixture through"
        assert reasons == []

    @pytest.mark.characterization
    def test_filter_would_reject_the_same_team_on_its_scoring_rate(self):
        """The counterpart to the test above: the pure function is not at fault."""
        args = dict(PASSING, home_avg_goals=5 / 20)
        passes, reasons = apply_filters(**args)
        assert passes is False
        assert "Home team averages" in reasons[0]


class TestDeterminism:
    def test_repeated_calls_are_identical(self):
        first = apply_filters(**PASSING)
        for _ in range(20):
            assert apply_filters(**PASSING) == first

    def test_reasons_list_is_not_shared_between_calls(self):
        # Guards against a mutable-default-argument style bug.
        _, first = apply_filters(**dict(PASSING, home_avg_goals=0.5))
        _, second = apply_filters(**dict(PASSING, home_avg_goals=0.5))
        assert first == second
        assert first is not second
