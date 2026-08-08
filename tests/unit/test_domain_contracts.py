"""
Data contract tests (Epic 1B.1).

Covers the two rules the contracts exist to enforce:

  1. Unknown data is NOT zero, and the two are distinguishable.
  2. A required POISSON_V1 input that is unavailable stops the prediction. No
     substituted zero, no substituted league average, no borrowed figures.
"""

import dataclasses

import pytest

from domain import (
    LEGACY_FALLBACK_LEAGUE_AVERAGE,
    REQUIRED_POISSON_INPUTS,
    DataQuality,
    Fixture,
    LeagueAverageSource,
    LeagueStats,
    TeamStats,
    is_available,
    validate_poisson_inputs,
)


def complete_team(team_id: str = "1") -> TeamStats:
    return TeamStats(
        team_id=team_id,
        league_id="eng.1",
        home_goals_scored=1.8,
        home_goals_conceded=0.8,
        away_goals_scored=1.2,
        away_goals_conceded=1.2,
        home_clean_sheet_pct=0.2,
        away_clean_sheet_pct=0.25,
        total_goals_avg=2.5,
        matches_played=20,
    )


class TestIsAvailable:
    def test_zero_is_available(self):
        """The crux of GG-001: 0.0 is data, not absence."""
        assert is_available(0.0) is True
        assert is_available(0) is True

    def test_none_is_not_available(self):
        assert is_available(None) is False

    def test_normal_value_is_available(self):
        assert is_available(1.8) is True

    def test_truthiness_would_have_got_this_wrong(self):
        """Documents why `if value:` is banned in this codebase."""
        value = 0.0
        assert not value  # truthiness says "absent"
        assert is_available(value)  # availability says "present"


class TestDataQuality:
    def test_no_missing_is_complete(self):
        assert DataQuality.from_missing(()) is DataQuality.COMPLETE
        assert DataQuality.from_missing(()).is_complete

    def test_any_missing_is_incomplete(self):
        quality = DataQuality.from_missing(("home_goals_scored",))
        assert quality is DataQuality.INCOMPLETE
        assert not quality.is_complete


class TestTeamStats:
    def test_defaults_are_unavailable_not_zero(self):
        """An unpopulated contract must not look like a goalless team."""
        stats = TeamStats(team_id="1", league_id="eng.1")
        assert stats.home_goals_scored is None
        assert stats.away_goals_conceded is None
        assert stats.total_goals_avg is None

    def test_complete_team_has_no_missing_fields(self):
        team = complete_team()
        assert team.missing_as_home() == ()
        assert team.missing_as_away() == ()
        assert team.quality_as_home() is DataQuality.COMPLETE
        assert team.quality_as_away() is DataQuality.COMPLETE

    def test_missing_home_split_reported_only_for_home_role(self):
        """A team playing away is unaffected by an absent home split."""
        team = TeamStats(
            team_id="1",
            league_id="eng.1",
            away_goals_scored=1.2,
            away_goals_conceded=1.2,
        )
        assert team.missing_as_home() == ("home_goals_scored", "home_goals_conceded")
        assert team.missing_as_away() == ()
        assert team.quality_as_home() is DataQuality.INCOMPLETE
        assert team.quality_as_away() is DataQuality.COMPLETE

    def test_genuine_zeros_are_complete(self):
        """A team that has scored and conceded nothing has complete data."""
        team = TeamStats(
            team_id="1",
            league_id="eng.1",
            home_goals_scored=0.0,
            home_goals_conceded=0.0,
            away_goals_scored=0.0,
            away_goals_conceded=0.0,
        )
        assert team.quality_as_home() is DataQuality.COMPLETE
        assert team.quality_as_away() is DataQuality.COMPLETE

    def test_from_provider_dict_maps_espn_output(self):
        team = TeamStats.from_provider_dict(
            {
                "team_id": "359",
                "league_id": "eng.1",
                "home_goals_scored": 1.8,
                "home_goals_conceded": 0.8,
                "away_goals_scored": 1.2,
                "away_goals_conceded": 1.2,
                "home_clean_sheet_pct": 0,
                "away_clean_sheet_pct": 0,
                "total_goals_avg": 2.5,
                "matches_played": 20,
            }
        )
        assert team.team_id == "359"
        assert team.home_goals_scored == 1.8
        assert team.quality_as_home() is DataQuality.COMPLETE

    def test_from_provider_dict_keeps_none_as_none(self):
        team = TeamStats.from_provider_dict(
            {"team_id": "1", "league_id": "eng.1", "home_goals_scored": None}
        )
        assert team.home_goals_scored is None
        assert team.quality_as_home() is DataQuality.INCOMPLETE

    def test_absent_key_is_none_not_zero(self):
        team = TeamStats.from_provider_dict({"team_id": "1", "league_id": "eng.1"})
        assert team.home_goals_scored is None

    def test_is_immutable(self):
        """A record of what an API returned must not be editable after the fact."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            complete_team().home_goals_scored = 99.0


class TestLeagueStats:
    def test_default_is_unavailable(self):
        league = LeagueStats(league_id="eng.1")
        assert league.average_goals is None
        assert league.quality() is DataQuality.INCOMPLETE
        assert not league.is_trustworthy

    def test_calculated_is_trustworthy(self):
        league = LeagueStats.calculated("eng.1", 1.42)
        assert league.average_goals == 1.42
        assert league.source is LeagueAverageSource.CALCULATED
        assert league.is_trustworthy
        assert league.quality() is DataQuality.COMPLETE

    def test_legacy_fallback_is_usable_but_not_trustworthy(self):
        """
        The pipeline still runs on 1.35 (no published numbers change), but the
        value is labelled so nothing downstream can treat it as measurement.
        """
        league = LeagueStats.legacy_fallback("eng.1")
        assert league.average_goals == LEGACY_FALLBACK_LEAGUE_AVERAGE == 1.35
        assert league.source is LeagueAverageSource.LEGACY_FALLBACK
        assert not league.is_trustworthy
        assert league.quality() is DataQuality.COMPLETE

    def test_unattributed_is_not_trustworthy(self):
        """GG-003: origin unknowable at this layer, so not assertable as measured."""
        league = LeagueStats.unattributed("eng.1", 1.35)
        assert league.source is LeagueAverageSource.UNATTRIBUTED
        assert not league.is_trustworthy
        assert league.quality() is DataQuality.COMPLETE

    def test_unattributed_none_becomes_unavailable(self):
        league = LeagueStats.unattributed("eng.1", None)
        assert league.source is LeagueAverageSource.UNAVAILABLE
        assert league.quality() is DataQuality.INCOMPLETE

    def test_a_real_average_of_1_35_is_not_assumed_to_be_the_fallback(self):
        """Source is tracked explicitly rather than guessed from the value."""
        assert LeagueStats.calculated("eng.1", 1.35).is_trustworthy


class TestValidatePoissonInputs:
    def test_complete_inputs_pass(self):
        result = validate_poisson_inputs(
            league=LeagueStats.calculated("eng.1", 1.35),
            home_team=complete_team("1"),
            away_team=complete_team("2"),
        )
        assert result.is_complete
        assert result.missing == ()
        assert result.inputs is not None
        assert result.inputs.league_avg_goals == 1.35
        assert result.inputs.home_goals_scored_home == 1.8
        assert result.inputs.away_goals_scored_away == 1.2
        assert result.reason() == ""

    def test_exactly_five_required_inputs(self):
        assert len(REQUIRED_POISSON_INPUTS) == 5

    def test_correct_side_is_taken_from_each_team(self):
        """
        The home team contributes its HOME split, the away team its AWAY split.
        A swap here would silently change every probability.
        """
        home = TeamStats(
            team_id="1",
            league_id="eng.1",
            home_goals_scored=2.0,
            home_goals_conceded=0.5,
            away_goals_scored=99.0,  # must be ignored
            away_goals_conceded=99.0,
        )
        away = TeamStats(
            team_id="2",
            league_id="eng.1",
            home_goals_scored=99.0,  # must be ignored
            home_goals_conceded=99.0,
            away_goals_scored=1.0,
            away_goals_conceded=1.5,
        )
        inputs = validate_poisson_inputs(LeagueStats.calculated("eng.1", 1.35), home, away).inputs
        assert inputs is not None
        assert inputs.home_goals_scored_home == 2.0
        assert inputs.home_goals_conceded_home == 0.5
        assert inputs.away_goals_scored_away == 1.0
        assert inputs.away_goals_conceded_away == 1.5

    def test_missing_league_average_blocks_prediction(self):
        result = validate_poisson_inputs(
            league=LeagueStats.unavailable("eng.1"),
            home_team=complete_team("1"),
            away_team=complete_team("2"),
        )
        assert not result.is_complete
        assert result.missing == ("league_avg_goals",)
        assert result.inputs is None

    @pytest.mark.parametrize(
        "field, expected",
        [
            ("home_goals_scored", "home_goals_scored_home"),
            ("home_goals_conceded", "home_goals_conceded_home"),
        ],
    )
    def test_missing_home_split_blocks_prediction(self, field, expected):
        home = TeamStats(**{**complete_team("1").__dict__, field: None})
        result = validate_poisson_inputs(
            LeagueStats.calculated("eng.1", 1.35), home, complete_team("2")
        )
        assert not result.is_complete
        assert result.missing == (expected,)
        assert result.inputs is None

    @pytest.mark.parametrize(
        "field, expected",
        [
            ("away_goals_scored", "away_goals_scored_away"),
            ("away_goals_conceded", "away_goals_conceded_away"),
        ],
    )
    def test_missing_away_split_blocks_prediction(self, field, expected):
        away = TeamStats(**{**complete_team("2").__dict__, field: None})
        result = validate_poisson_inputs(
            LeagueStats.calculated("eng.1", 1.35), complete_team("1"), away
        )
        assert not result.is_complete
        assert result.missing == (expected,)

    def test_all_missing_inputs_are_reported_together(self):
        """One pass should show every gap, not just the first."""
        bare = TeamStats(team_id="1", league_id="eng.1")
        result = validate_poisson_inputs(LeagueStats.unavailable("eng.1"), bare, bare)
        assert result.missing == REQUIRED_POISSON_INPUTS

    def test_genuine_zeros_pass_validation(self):
        """
        Availability, not plausibility. A goalless team is real data and must be
        modelled; over-correcting into "0 means missing" would be a new bug.
        """
        zeros = TeamStats(
            team_id="1",
            league_id="eng.1",
            home_goals_scored=0.0,
            home_goals_conceded=0.0,
            away_goals_scored=0.0,
            away_goals_conceded=0.0,
        )
        result = validate_poisson_inputs(LeagueStats.calculated("eng.1", 1.35), zeros, zeros)
        assert result.is_complete
        assert result.inputs is not None
        assert result.inputs.home_goals_scored_home == 0.0

    def test_reason_names_the_missing_inputs(self):
        bare = TeamStats(team_id="1", league_id="eng.1")
        reason = validate_poisson_inputs(
            LeagueStats.calculated("eng.1", 1.35), bare, complete_team("2")
        ).reason()
        assert "home_goals_scored_home" in reason
        assert "home_goals_conceded_home" in reason

    def test_no_value_is_substituted_when_data_is_missing(self):
        """
        The core guarantee. On incomplete data there are no inputs at all - so no
        zero, no 1.35, and no other team's figures can reach the model.
        """
        bare = TeamStats(team_id="1", league_id="eng.1")
        result = validate_poisson_inputs(LeagueStats.unavailable("eng.1"), bare, bare)
        assert result.inputs is None


class TestFixtureContract:
    def test_from_provider_dict_maps_espn_fixture(self):
        fixture = Fixture.from_provider_dict(
            {
                "fixture_id": "700",
                "league_id": "eng.1",
                "league_name": "English Premier League",
                "home_team_id": "359",
                "home_team_name": "Arsenal",
                "away_team_id": "360",
                "away_team_name": "Chelsea",
                "datetime": "2026-02-08T15:00Z",
                "status": "STATUS_SCHEDULED",
            }
        )
        assert fixture.fixture_id == "700"
        assert fixture.label == "Arsenal vs Chelsea"
        assert fixture.kickoff == "2026-02-08T15:00Z"

    def test_kickoff_is_left_as_provider_string(self):
        """Parsing would silently choose a timezone policy; that is separate work."""
        fixture = Fixture.from_provider_dict({"datetime": "2026-02-08T15:00Z"})
        assert isinstance(fixture.kickoff, str)
