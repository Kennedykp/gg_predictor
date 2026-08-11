"""
Historical dataset builder tests (Epic 2B.2).

Entirely offline. Every payload here is a minimised copy of a real ESPN
scoreboard response, so what is asserted is the behaviour against the shapes the
provider actually produces - including the awkward ones Epic 2A found.

The invariants under test, in priority order:

  1. a record's season is the PROVIDER's, never the request's
  2. an unusable event is refused with a reason, never repaired
  3. zero is never substituted for a missing result
  4. two builds of the same data are byte-identical
  5. the point-in-time boundary is strict (`<`, not `<=`)
  6. genuine historical anomalies survive the build unchanged
"""

from datetime import datetime, timedelta, timezone

import pytest

import espn
import historical_dataset as hd
from domain.historical import (
    HistoricalMatch,
    ModelEligibility,
    matches_before,
    model_dataset,
    to_jsonl_line,
)

# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _event(
    event_id,
    kickoff,
    season_year,
    slug,
    home=("1", "Arsenal"),
    away=("2", "Chelsea"),
    home_score="2",
    away_score="1",
    status="STATUS_FULL_TIME",
    completed=True,
    state="post",
    league_id="700",
):
    """One scoreboard event, shaped exactly as ESPN returns it."""
    competitors = []
    for (team_id, name), score, home_away in (
        (home, home_score, "home"),
        (away, away_score, "away"),
    ):
        competitor = {
            "id": team_id,
            "homeAway": home_away,
            "team": {"id": team_id, "displayName": name},
        }
        if score is not None:
            competitor["score"] = {"value": float(score), "displayValue": str(score)}
        competitors.append(competitor)

    return {
        "id": event_id,
        "uid": f"s:600~l:{league_id}~e:{event_id}",
        "date": kickoff,
        "season": {"year": season_year, "slug": slug},
        "competitions": [
            {
                "id": event_id,
                "date": kickoff,
                "status": {
                    "type": {
                        "name": status,
                        "state": state,
                        "completed": completed,
                    }
                },
                "competitors": competitors,
            }
        ],
    }


def _payload(events, slug="eng.1", league_id="700"):
    return {"leagues": [{"id": league_id, "slug": slug}], "events": events}


def _match(event_id="1", season=2019, kickoff=None, phase="regular-season", **kwargs):
    """A valid HistoricalMatch, for tests that do not need a payload."""
    defaults = dict(
        event_id=event_id,
        competition="eng.1",
        season=season,
        kickoff=kickoff or datetime(2020, 1, 1, 15, 0, tzinfo=timezone.utc),
        home_team_id="1",
        away_team_id="2",
        completed=True,
        home_goals=2,
        away_goals=1,
        season_phase=phase,
        provider="espn",
    )
    defaults.update(kwargs)
    return HistoricalMatch(**defaults)


# ---------------------------------------------------------------------------
# Season provenance
# ---------------------------------------------------------------------------


class TestSeasonProvenance:
    """The stored season is what ESPN said, not what we asked for."""

    def test_stored_season_comes_from_the_payload(self):
        payload = _payload([_event("1", "2020-07-26T15:00Z", 2019, "regular-season")])
        readout = espn.parse_scoreboard_history(payload, "eng.1", 2019)

        assert len(readout.matches) == 1
        assert readout.matches[0].season == 2019

    def test_covid_extended_fixture_is_kept(self):
        """2020-07-26 is past June 30 and still belongs to 2019/20."""
        payload = _payload([_event("1", "2020-07-26T15:00Z", 2019, "regular-season")])
        readout = espn.parse_scoreboard_history(payload, "eng.1", 2019)

        assert len(readout.matches) == 1
        assert readout.matches[0].kickoff.month == 7

    def test_previous_season_event_is_refused_with_a_reason(self):
        """
        The Epic 2A contamination case. A 2019/20 fixture sits inside the
        2020/21 discovery window; it must be refused, and the reason must say
        it was the season - not something vague.
        """
        payload = _payload([_event("1", "2020-07-26T15:00Z", 2019, "regular-season")])
        readout = espn.parse_scoreboard_history(payload, "eng.1", 2020)

        assert readout.matches == []
        assert "WRONG_SEASON" in readout.rejected

    def test_requested_season_is_never_substituted_when_metadata_is_missing(self):
        event = _event("1", "2020-01-01T15:00Z", 2019, "regular-season")
        del event["season"]
        readout = espn.parse_scoreboard_history(_payload([event]), "eng.1", 2019)

        assert readout.matches == []
        assert readout.rejected_total == 1


# ---------------------------------------------------------------------------
# Fail-closed on results
# ---------------------------------------------------------------------------


class TestResultIntegrity:
    """A missing score is missing. It is never a nil-nil."""

    def test_completed_without_score_is_refused(self):
        event = _event("1", "2020-01-01T15:00Z", 2019, "regular-season", home_score=None)
        readout = espn.parse_scoreboard_history(_payload([event]), "eng.1", 2019)

        assert readout.matches == []
        assert readout.rejected.get("COMPLETED_WITHOUT_SCORE") == 1

    def test_postponed_fixture_is_kept_without_a_result(self):
        """
        Real history. The fixture existed and was not played, so it is stored
        with completed=False and no score rather than dropped or zeroed.
        """
        event = _event(
            "1",
            "2020-03-14T15:00Z",
            2019,
            "regular-season",
            status="STATUS_POSTPONED",
            completed=False,
            state="pre",
            home_score=None,
            away_score=None,
        )
        readout = espn.parse_scoreboard_history(_payload([event]), "eng.1", 2019)

        assert len(readout.matches) == 1
        match = readout.matches[0]
        assert match.completed is False
        assert match.home_goals is None
        assert match.away_goals is None
        assert match.has_result is False

    def test_partial_score_on_an_unfinished_fixture_is_discarded(self):
        """An in-progress scoreline is not a result, even though it is a number."""
        event = _event(
            "1",
            "2020-03-14T15:00Z",
            2019,
            "regular-season",
            status="STATUS_FIRST_HALF",
            completed=False,
            state="in",
            home_score="1",
            away_score="0",
        )
        readout = espn.parse_scoreboard_history(_payload([event]), "eng.1", 2019)

        assert readout.matches[0].home_goals is None
        assert readout.matches[0].away_goals is None

    def test_contract_refuses_a_completed_match_with_no_score(self):
        with pytest.raises(ValueError, match="Zero is never substituted"):
            _match(completed=True, home_goals=None, away_goals=None)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_builds_are_byte_identical(self, tmp_path):
        events = [
            _event("3", "2020-01-03T15:00Z", 2019, "regular-season"),
            _event("1", "2020-01-01T15:00Z", 2019, "regular-season"),
            _event("2", "2020-01-01T15:00Z", 2019, "regular-season"),
        ]

        def fetch(league, season):
            return espn.parse_scoreboard_history(_payload(events), league, season)

        first = hd.build_dataset(["eng.1"], [2019], fetch=fetch)
        second = hd.build_dataset(["eng.1"], [2019], fetch=fetch)

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        checks_a = hd.write_dataset(first, dir_a)
        checks_b = hd.write_dataset(second, dir_b)

        assert checks_a == checks_b
        assert (dir_a / "eng.1_2019.jsonl").read_bytes() == (dir_b / "eng.1_2019.jsonl").read_bytes()

    def test_simultaneous_kickoffs_are_ordered_by_event_id(self):
        """Kickoff alone is not a total order, so ties must break deterministically."""
        events = [
            _event("2", "2020-01-01T15:00Z", 2019, "regular-season"),
            _event("1", "2020-01-01T15:00Z", 2019, "regular-season"),
        ]
        readout = espn.parse_scoreboard_history(_payload(events), "eng.1", 2019)
        build = hd.build_league_season(
            "eng.1", 2019, fetch=lambda league, season: readout
        )

        assert [m.event_id for m in build.matches] == ["1", "2"]

    def test_round_trip_through_disk_preserves_every_record(self, tmp_path):
        events = [
            _event("1", "2020-01-01T15:00Z", 2019, "regular-season"),
            _event(
                "2",
                "2020-03-14T15:00Z",
                2019,
                "regular-season",
                status="STATUS_POSTPONED",
                completed=False,
                state="pre",
                home_score=None,
                away_score=None,
            ),
        ]
        readout = espn.parse_scoreboard_history(_payload(events), "eng.1", 2019)
        report = hd.BuildReport(
            builds=[hd.SeasonBuild("eng.1", 2019, readout.matches, {})]
        )
        hd.write_dataset(report, tmp_path)

        loaded = hd.load_dataset(tmp_path)
        assert [hd_match.event_id for hd_match in loaded] == ["1", "2"]
        assert loaded[1].completed is False
        assert loaded[1].home_goals is None


# ---------------------------------------------------------------------------
# Provider failure vs empty season
# ---------------------------------------------------------------------------


class TestErrorSemantics:
    def test_provider_failure_is_not_an_empty_season(self, tmp_path):
        report = hd.build_dataset(["eng.1"], [2019], fetch=lambda league, season: None)

        assert report.builds[0].failed is True
        assert report.failures

        checksums = hd.write_dataset(report, tmp_path)
        assert checksums == {}
        assert not list(tmp_path.glob("*.jsonl"))

    def test_empty_season_is_written_as_an_empty_file(self, tmp_path):
        """
        ESPN answering with no events is a real answer and gets a real file.
        Distinguishable from a failure, which gets no file at all.
        """
        readout = espn.parse_scoreboard_history(_payload([]), "eng.1", 2019)
        report = hd.BuildReport(builds=[hd.SeasonBuild("eng.1", 2019, readout.matches, {})])
        checksums = hd.write_dataset(report, tmp_path)

        assert "eng.1_2019.jsonl" in checksums
        assert (tmp_path / "eng.1_2019.jsonl").read_text() == ""

    def test_manifest_records_the_failure(self):
        report = hd.build_dataset(["eng.1"], [2019], fetch=lambda league, season: None)
        manifest = hd.build_manifest(report, {})

        season = manifest["seasons"][0]
        assert season["provider_failed"] is True
        assert season["file"] is None
        assert manifest["totals"]["league_seasons_failed"] == 1


# ---------------------------------------------------------------------------
# Eligibility (labelled, not deleted)
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_playoff_is_stored_but_excluded_from_the_model_view(self):
        events = [
            _event("1", "2020-01-01T15:00Z", 2019, "regular-season"),
            _event("2", "2020-05-30T15:00Z", 2019, "playoffs"),
        ]
        readout = espn.parse_scoreboard_history(_payload(events), "eng.1", 2019)

        assert len(readout.matches) == 2
        assert len(model_dataset(readout.matches)) == 1

    def test_group_stage_bundesliga_season_is_not_deleted(self):
        """
        303 ger.1 2010/11 fixtures carry phase 'group-stage'. A naive
        phase != 'regular-season' rule would delete a whole real season.
        """
        payload = _payload(
            [_event("1", "2010-08-20T18:30Z", 2010, "group-stage", league_id="10")],
            slug="ger.1",
            league_id="10",
        )

        readout = espn.parse_scoreboard_history(payload, "ger.1", 2010)

        assert len(readout.matches) == 1
        assert readout.matches[0].eligibility.verdict is ModelEligibility.ELIGIBLE
        assert len(model_dataset(readout.matches)) == 1

    def test_unrecognised_phase_is_uncertain_and_not_trained_on(self):
        payload = _payload([_event("1", "2020-01-01T15:00Z", 2019, "mystery-round")])
        readout = espn.parse_scoreboard_history(payload, "eng.1", 2019)

        assert len(readout.matches) == 1
        assert readout.matches[0].eligibility.verdict is ModelEligibility.UNCERTAIN
        assert model_dataset(readout.matches) == []


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------


class TestPointInTime:
    def test_boundary_is_strict(self):
        kickoff = datetime(2020, 1, 1, 15, 0, tzinfo=timezone.utc)
        match = _match(event_id="1", kickoff=kickoff)

        assert matches_before([match], kickoff) == []
        assert matches_before([match], kickoff + timedelta(seconds=1)) == [match]

    def test_a_fixture_cannot_inform_itself(self):
        kickoff = datetime(2020, 1, 1, 15, 0, tzinfo=timezone.utc)
        earlier = _match(event_id="1", kickoff=kickoff - timedelta(days=7))
        target = _match(event_id="2", kickoff=kickoff)

        visible = matches_before([earlier, target], kickoff)
        assert [m.event_id for m in visible] == ["1"]

    def test_naive_cutoff_is_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            matches_before([_match()], datetime(2020, 1, 1, 15, 0))

    def test_other_seasons_can_be_scoped_out(self):
        cutoff = datetime(2021, 1, 1, tzinfo=timezone.utc)
        old = _match(event_id="1", season=2018, kickoff=datetime(2019, 1, 1, tzinfo=timezone.utc))
        new = _match(event_id="2", season=2019, kickoff=datetime(2020, 1, 1, tzinfo=timezone.utc))

        assert len(matches_before([old, new], cutoff)) == 2
        assert len(matches_before([old, new], cutoff, season=2019)) == 1


# ---------------------------------------------------------------------------
# Genuine historical anomalies
# ---------------------------------------------------------------------------


class TestHistoricalAnomalies:
    def test_short_season_is_not_padded(self):
        """fra.1 2019/20 was abandoned. 279 stays 279."""
        events = [
            _event(
                str(i),
                f"2020-01-{(i % 28) + 1:02d}T15:00Z",
                2019,
                "regular-season",
                league_id="9",
            )
            for i in range(1, 12)
        ]
        payload = _payload(events, slug="fra.1", league_id="9")

        readout = espn.parse_scoreboard_history(payload, "fra.1", 2019)

        build = hd.build_league_season("fra.1", 2019, fetch=lambda league, season: readout)
        manifest = hd.build_manifest(hd.BuildReport(builds=[build]), {})

        assert manifest["seasons"][0]["records"] == 11
        assert manifest["totals"]["records"] == 11

    def test_repeated_pairing_is_reported_not_removed(self):
        """A relegation playoff can repeat a pairing. Both rows are real."""
        events = [
            _event("1", "2022-09-01T15:00Z", 2022, "regular-season"),
            _event("2", "2023-01-15T15:00Z", 2022, "regular-season"),
        ]
        readout = espn.parse_scoreboard_history(_payload(events), "eng.1", 2022)
        report = hd.BuildReport(builds=[hd.SeasonBuild("eng.1", 2022, readout.matches, {})])
        manifest = hd.build_manifest(report, {})

        assert len(readout.matches) == 2
        assert manifest["repeated_pairings"]
        assert manifest["duplicate_event_ids"] == {}

    def test_no_duplicate_event_ids_in_a_clean_build(self):
        events = [
            _event("1", "2020-01-01T15:00Z", 2019, "regular-season"),
            _event("2", "2020-01-02T15:00Z", 2019, "regular-season", away=("3", "Spurs")),
        ]
        readout = espn.parse_scoreboard_history(_payload(events), "eng.1", 2019)
        report = hd.BuildReport(builds=[hd.SeasonBuild("eng.1", 2019, readout.matches, {})])

        assert hd.build_manifest(report, {})["duplicate_event_ids"] == {}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_reports_rejection_reasons(self):
        events = [
            _event("1", "2020-01-01T15:00Z", 2019, "regular-season"),
            _event("2", "2020-01-02T15:00Z", 2020, "regular-season"),
        ]
        readout = espn.parse_scoreboard_history(_payload(events), "eng.1", 2019)
        build = hd.build_league_season("eng.1", 2019, fetch=lambda league, season: readout)
        manifest = hd.build_manifest(hd.BuildReport(builds=[build]), {})

        assert manifest["seasons"][0]["rejected"]["WRONG_SEASON"] == 1

    def test_manifest_carries_versions_and_provider(self):
        manifest = hd.build_manifest(hd.BuildReport(), {})

        assert manifest["schema_version"]
        assert manifest["eligibility_rule_version"]
        assert manifest["provider"] == "espn"

    def test_built_at_does_not_affect_file_checksums(self, tmp_path):
        """Provenance timestamp must not make two identical builds differ."""
        readout = espn.parse_scoreboard_history(
            _payload([_event("1", "2020-01-01T15:00Z", 2019, "regular-season")]),
            "eng.1",
            2019,
        )
        report = hd.BuildReport(builds=[hd.SeasonBuild("eng.1", 2019, readout.matches, {})])
        checksums = hd.write_dataset(report, tmp_path)

        early = hd.build_manifest(report, checksums, built_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        late = hd.build_manifest(report, checksums, built_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

        assert early["built_at"] != late["built_at"]
        assert early["seasons"][0]["checksum"] == late["seasons"][0]["checksum"]


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_field_order_is_fixed(self):
        line = to_jsonl_line(_match())
        assert line.startswith('{"event_id":')

    def test_unknown_field_is_refused_on_read(self):
        from domain.historical import from_json_dict

        with pytest.raises(ValueError, match="unrecognised field"):
            from_json_dict({"event_id": "1", "surprise": True})

    def test_kickoff_survives_a_round_trip_in_utc(self):
        from domain.historical import from_jsonl_line as parse

        original = _match(kickoff=datetime(2020, 7, 26, 15, 30, tzinfo=timezone.utc))
        restored = parse(to_jsonl_line(original))

        assert restored.kickoff == original.kickoff
        assert restored.kickoff.tzinfo is not None
