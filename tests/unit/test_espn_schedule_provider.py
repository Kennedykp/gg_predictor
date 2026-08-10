"""
ESPN schedule adapter (Epic 1B.4, TASK 22).

Offline and deterministic. `_fetch` is monkeypatched at the espn module seam, so
nothing here touches the network - the payloads below are minimal hand-written
copies of the live response shape recorded in docs/EPIC_1B4_MATCH_HISTORY.md.

What these tests are actually protecting:

  1. A match that did not happen before the target kickoff cannot become
     evidence for it.
  2. A match this adapter cannot fully understand is DROPPED, never guessed at.
     Every skip path is asserted individually, because the failure mode of a
     lenient parser is a plausible-looking wrong number, not a crash.
  3. Home/away perspective is never reversed.
"""

from datetime import datetime, timedelta, timezone

import pytest

import espn
from domain.match_records import Venue

# The target fixture every test measures against.
TARGET_KICKOFF = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)
TEAM = "83"        # the team whose perspective we derive
OPPONENT = "360"
LEAGUE = "esp.1"
# The season these tests request. `resolve_season` maps the current date to it,
# and every stub event is labelled with it, because Epic 2B.1 requires an event
# to state its own season before it can be believed.
SEASON = 2026


def _event(
    event_id="401001",
    kickoff="2026-08-01T14:00Z",
    team_home_away="home",
    team_score=2,
    opponent_score=0,
    status_name="STATUS_FULL_TIME",
    state="post",
    completed=True,
    league_slug=LEAGUE,
    team_id=TEAM,
    opponent_id=OPPONENT,
    include_team_score=True,
    include_opponent_score=True,
):
    """
    One ESPN schedule event, shaped like the live payload.

    Every field a test needs to corrupt is a parameter, so each test states the
    single thing it is varying and nothing else.
    """
    ours = {"id": team_id, "homeAway": team_home_away, "team": {"id": team_id}}
    if include_team_score:
        ours["score"] = {"value": team_score, "displayValue": str(team_score)}

    theirs = {
        "id": opponent_id,
        "homeAway": "away" if team_home_away == "home" else "home",
        "team": {"id": opponent_id},
    }
    if include_opponent_score:
        theirs["score"] = {"value": opponent_score, "displayValue": str(opponent_score)}

    return {
        "id": event_id,
        "date": kickoff,
        "league": {"slug": league_slug},
        # Epic 2B.1: real events always state their season, and the provider now
        # requires it. SEASON is the module constant these tests request.
        "season": {"year": SEASON, "displayName": f"{SEASON}-{str(SEASON + 1)[-2:]} Test League"},
        "competitions": [
            {
                "id": event_id,
                "status": {
                    "type": {"name": status_name, "state": state, "completed": completed}
                },
                "competitors": [ours, theirs],
            }
        ],
    }


def _payload(*events):
    return {"events": list(events)}


def _parse(*events, team_id=TEAM):
    return espn.parse_schedule_events(_payload(*events), team_id, LEAGUE)


@pytest.fixture(autouse=True)
def _clear_cache():
    """The schedule cache is process-wide; leaking it across tests hides bugs."""
    espn.clear_schedule_cache()
    yield
    espn.clear_schedule_cache()


# ---------------------------------------------------------------------------
# Valid matches and perspective
# ---------------------------------------------------------------------------
class TestValidMatches:
    def test_completed_home_match_is_parsed(self):
        records = _parse(_event(team_home_away="home", team_score=2, opponent_score=0))

        assert len(records) == 1
        record = records[0]
        assert record.venue == Venue.HOME
        assert record.goals_for == 2
        assert record.goals_against == 0
        assert record.completed is True
        assert record.event_id == "401001"
        assert record.competition == LEAGUE
        assert record.team_id == TEAM
        assert record.opponent_id == OPPONENT

    def test_completed_away_match_is_parsed(self):
        records = _parse(_event(team_home_away="away", team_score=1, opponent_score=3))

        assert len(records) == 1
        assert records[0].venue == Venue.AWAY
        assert records[0].goals_for == 1
        assert records[0].goals_against == 3

    def test_asymmetric_score_home_perspective(self):
        """
        TASK 6. Home Team 3-1 Away Team, read from the HOME team.

        Asymmetric on purpose: with 2-2 a reversed mapping is invisible.
        """
        records = _parse(_event(team_home_away="home", team_score=3, opponent_score=1))

        assert (records[0].goals_for, records[0].goals_against) == (3, 1)

    def test_asymmetric_score_away_perspective(self):
        """The same 3-1 match read from the AWAY team must invert to 1-3."""
        records = _parse(
            _event(team_home_away="away", team_score=1, opponent_score=3),
        )

        assert (records[0].goals_for, records[0].goals_against) == (1, 3)

    def test_perspective_cannot_be_reversed(self):
        """
        The identical event parsed from each side. Reversing the mapping would
        make these two equal, so this fails loudly if perspective is ever
        flattened or read from array order.
        """
        home_view = _parse(_event(team_home_away="home", team_score=3, opponent_score=1))
        away_view = espn.parse_schedule_events(
            _payload(_event(team_home_away="away", team_score=1, opponent_score=3)),
            TEAM,
            LEAGUE,
        )

        assert home_view[0].goals_for == away_view[0].goals_against == 3
        assert home_view[0].goals_against == away_view[0].goals_for == 1
        assert home_view[0].venue != away_view[0].venue


# ---------------------------------------------------------------------------
# Completion policy (TASK 7)
# ---------------------------------------------------------------------------
class TestCompletionPolicy:
    @pytest.mark.parametrize(
        "status_name,state,completed",
        [
            ("STATUS_SCHEDULED", "pre", False),
            ("STATUS_IN_PROGRESS", "in", False),
            ("STATUS_HALFTIME", "in", False),
            ("STATUS_POSTPONED", "pre", False),
            ("STATUS_CANCELED", "pre", False),
            ("STATUS_ABANDONED", "post", True),
            ("STATUS_SUSPENDED", "in", False),
            ("STATUS_UNKNOWN_FUTURE_VALUE", "post", True),
        ],
    )
    def test_non_final_statuses_are_excluded(self, status_name, state, completed):
        """
        None of these produced a trustworthy final score.

        ABANDONED is the important one: it reports state `post` AND
        `completed: true`, so a check on either flag alone would let a partial
        score through as a result. An unrecognised future status is excluded for
        the same reason - unknown means excluded, never assumed final.
        """
        records = _parse(_event(status_name=status_name, state=state, completed=completed))

        assert records == []

    @pytest.mark.parametrize(
        "status_name",
        ["STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"],
    )
    def test_final_statuses_are_included(self, status_name):
        records = _parse(_event(status_name=status_name, state="post", completed=True))

        assert len(records) == 1

    def test_completed_flag_false_is_excluded_even_when_post(self):
        records = _parse(_event(status_name="STATUS_FULL_TIME", state="post", completed=False))

        assert records == []


# ---------------------------------------------------------------------------
# Malformed / incomplete payloads
# ---------------------------------------------------------------------------
class TestMalformedPayloads:
    def test_missing_own_score_is_dropped_not_zeroed(self):
        """
        The central rule. A missing score must never be read as 0, which would
        manufacture a clean sheet out of an absent field.
        """
        records = _parse(_event(include_team_score=False))

        assert records == []

    def test_missing_opponent_score_is_dropped(self):
        records = _parse(_event(include_opponent_score=False))

        assert records == []

    @pytest.mark.parametrize("bad_score", ["", "abc", None, "2.5", -1, 1.5, True])
    def test_malformed_scores_are_dropped(self, bad_score):
        event = _event()
        event["competitions"][0]["competitors"][0]["score"] = {"value": bad_score}

        assert espn.parse_schedule_events(_payload(event), TEAM, LEAGUE) == []

    def test_bare_string_score_is_accepted(self):
        """Some ESPN endpoints return the score as a bare string rather than an object."""
        event = _event()
        event["competitions"][0]["competitors"][0]["score"] = "4"
        event["competitions"][0]["competitors"][1]["score"] = "2"

        records = espn.parse_schedule_events(_payload(event), TEAM, LEAGUE)

        assert (records[0].goals_for, records[0].goals_against) == (4, 2)

    def test_missing_team_id_is_dropped(self):
        event = _event()
        event["competitions"][0]["competitors"][0].pop("id")
        event["competitions"][0]["competitors"][0]["team"] = {}

        assert espn.parse_schedule_events(_payload(event), TEAM, LEAGUE) == []

    def test_missing_kickoff_is_dropped(self):
        """Without a kickoff the record can never be proven to precede the target."""
        event = _event()
        event.pop("date")

        assert espn.parse_schedule_events(_payload(event), TEAM, LEAGUE) == []

    def test_unparseable_kickoff_is_dropped(self):
        assert _parse(_event(kickoff="not-a-date")) == []

    def test_missing_home_away_label_is_dropped(self):
        """Perspective is never inferred from position in the array."""
        event = _event()
        event["competitions"][0]["competitors"][0].pop("homeAway")

        assert espn.parse_schedule_events(_payload(event), TEAM, LEAGUE) == []

    def test_team_not_in_event_is_dropped(self):
        assert _parse(_event(), team_id="99999") == []

    def test_event_with_one_competitor_is_dropped(self):
        event = _event()
        event["competitions"][0]["competitors"].pop()

        assert espn.parse_schedule_events(_payload(event), TEAM, LEAGUE) == []

    def test_event_without_competitions_is_dropped(self):
        assert _parse({"id": "1", "date": "2026-08-01T14:00Z", "competitions": []}) == []

    def test_empty_schedule_returns_empty_list(self):
        assert espn.parse_schedule_events({"events": []}, TEAM, LEAGUE) == []

    def test_payload_without_events_key_returns_empty_list(self):
        assert espn.parse_schedule_events({}, TEAM, LEAGUE) == []


# ---------------------------------------------------------------------------
# Timezone (TASK 10)
# ---------------------------------------------------------------------------
class TestTimezone:
    def test_kickoff_is_timezone_aware_utc(self):
        records = _parse(_event(kickoff="2026-08-01T14:00Z"))

        assert records[0].kickoff.tzinfo is not None
        assert records[0].kickoff == datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)

    def test_offset_timestamp_normalises_to_the_same_instant(self):
        """
        `2026-08-01T15:00+01:00` and `2026-08-01T14:00Z` are the same moment.
        They must compare equal, or a cutoff would include/exclude by the
        machine's locale rather than by fact.
        """
        offset = _parse(_event(kickoff="2026-08-01T15:00+01:00"))[0]
        utc = _parse(_event(kickoff="2026-08-01T14:00Z"))[0]

        assert offset.kickoff == utc.kickoff


# ---------------------------------------------------------------------------
# get_team_history: cutoff, dedup, competition, failure
# ---------------------------------------------------------------------------
def _stub_fetch(monkeypatch, payload=None, error=None):
    """Replace the transport seam. No socket is opened."""
    def fake_fetch(url, params=None):
        if error is not None:
            return espn.FetchResult(error=error, detail="stubbed")
        return espn.FetchResult(data=payload)

    monkeypatch.setattr(espn, "_fetch", fake_fetch)


class TestTargetKickoffCutoff:
    def test_match_one_second_before_target_is_included(self, monkeypatch):
        kickoff = (TARGET_KICKOFF - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _stub_fetch(monkeypatch, _payload(_event(kickoff=kickoff)))

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 1

    def test_match_exactly_at_target_is_excluded(self, monkeypatch):
        """
        Strict `<`. A match kicking off at exactly T is not evidence about T -
        and in the degenerate case it IS T.
        """
        kickoff = TARGET_KICKOFF.strftime("%Y-%m-%dT%H:%M:%SZ")
        _stub_fetch(monkeypatch, _payload(_event(kickoff=kickoff)))

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 0
        assert history.clean_sheet_pct is None

    def test_match_after_target_is_excluded(self, monkeypatch):
        kickoff = (TARGET_KICKOFF + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _stub_fetch(monkeypatch, _payload(_event(kickoff=kickoff)))

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 0

    def test_target_fixture_cannot_enter_its_own_history(self, monkeypatch):
        """
        TASK 9 regression. Even if the feed reports the target fixture as
        completed AND dates it before itself, the event-id exclusion drops it.
        """
        target_id = "401999"
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id=target_id, kickoff="2026-08-01T14:00Z"),
                _event(event_id="401001", kickoff="2026-08-02T14:00Z"),
            ),
        )

        history = espn.get_team_history(
            TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF, exclude_event_id=target_id
        )

        assert history.sample_size == 1


class TestDeduplication:
    def test_duplicate_event_ids_are_counted_once(self, monkeypatch):
        """
        A repeated event must not double-weight its scoreline. Here the repeat
        is a clean sheet, so counting it twice would move the rate from 1/2 to
        2/3 - a wrong number rather than an obvious error.
        """
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id="A", kickoff="2026-08-01T14:00Z", team_score=1, opponent_score=0),
                _event(event_id="A", kickoff="2026-08-01T14:00Z", team_score=1, opponent_score=0),
                _event(event_id="B", kickoff="2026-08-02T14:00Z", team_score=1, opponent_score=1),
            ),
        )

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 2
        assert history.clean_sheet_pct == 0.5


class TestCompetitionContamination:
    def test_other_competition_is_excluded(self, monkeypatch):
        """
        A cup win with a clean sheet must not inflate a LEAGUE clean-sheet rate.
        Without the competition guard this would read 1/2 instead of 0/1.
        """
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id="L", kickoff="2026-08-01T14:00Z", team_score=1, opponent_score=1),
                _event(
                    event_id="C",
                    kickoff="2026-08-02T14:00Z",
                    team_score=3,
                    opponent_score=0,
                    league_slug="uefa.champions",
                ),
            ),
        )

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 1
        assert history.clean_sheet_pct == 0.0

    def test_unknown_competition_is_excluded(self, monkeypatch):
        """An event with no league slug cannot prove it is a league match."""
        event = _event()
        event.pop("league")
        _stub_fetch(monkeypatch, _payload(event))

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 0


class TestVenueSeparation:
    def test_away_matches_do_not_enter_home_history(self, monkeypatch):
        """TASK 12. Samples are never merged to increase n."""
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id="H", kickoff="2026-08-01T14:00Z", team_home_away="home",
                       team_score=1, opponent_score=1),
                _event(event_id="A", kickoff="2026-08-02T14:00Z", team_home_away="away",
                       team_score=2, opponent_score=0),
            ),
        )

        home = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)
        away = espn.get_team_history(TEAM, LEAGUE, Venue.AWAY, TARGET_KICKOFF)

        assert home.sample_size == 1
        assert home.clean_sheet_pct == 0.0
        assert away.sample_size == 1
        assert away.clean_sheet_pct == 1.0


class TestProviderFailure:
    @pytest.mark.parametrize(
        "error",
        [
            espn.ESPNError.TIMEOUT,
            espn.ESPNError.CONNECTION,
            espn.ESPNError.HTTP_ERROR,
            espn.ESPNError.SERVER_ERROR,
            espn.ESPNError.MALFORMED_JSON,
            espn.ESPNError.EMPTY_RESPONSE,
        ],
    )
    def test_provider_failure_returns_none_not_zero(self, monkeypatch, error):
        """
        TASK 19. The distinction that matters: a failed request yields None
        (UNAVAILABLE, blocks a recommendation), never 0.0 (a real statistic that
        PASSES the clean-sheet filter).
        """
        _stub_fetch(monkeypatch, error=error)

        assert espn.get_team_match_records(TEAM, LEAGUE) is None
        assert espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF) is None

    def test_empty_schedule_is_available_with_zero_sample(self, monkeypatch):
        """
        A successful response with no completed matches is DIFFERENT from a
        failure: the rate is unavailable in both cases, but this one is a fact
        about the team, not about the provider.
        """
        _stub_fetch(monkeypatch, {"events": []})

        records = espn.get_team_match_records(TEAM, LEAGUE)
        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert records == []
        assert history is not None
        assert history.sample_size == 0
        assert history.clean_sheet_pct is None


class TestDerivedRates:
    def test_genuine_zero_clean_sheet_rate(self, monkeypatch):
        """0.0 is a real measurement and must not be confused with None."""
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id="1", kickoff="2026-08-01T14:00Z", team_score=1, opponent_score=1),
                _event(event_id="2", kickoff="2026-08-02T14:00Z", team_score=2, opponent_score=1),
                _event(event_id="3", kickoff="2026-08-03T14:00Z", team_score=0, opponent_score=1),
            ),
        )

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.clean_sheet_pct == 0.0
        assert history.clean_sheet_pct is not None
        assert history.sample_size == 3

    def test_genuine_zero_btts_rate(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id="1", kickoff="2026-08-01T14:00Z", team_score=1, opponent_score=0),
                _event(event_id="2", kickoff="2026-08-02T14:00Z", team_score=0, opponent_score=0),
            ),
        )

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.both_teams_scored_pct == 0.0

    def test_genuine_hundred_percent_btts_rate(self, monkeypatch):
        _stub_fetch(
            monkeypatch,
            _payload(
                _event(event_id="1", kickoff="2026-08-01T14:00Z", team_score=1, opponent_score=1),
                _event(event_id="2", kickoff="2026-08-02T14:00Z", team_score=3, opponent_score=2),
            ),
        )

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.both_teams_scored_pct == 1.0

    def test_sample_size_of_one_is_reported_honestly(self, monkeypatch):
        """
        TASK 15. n=1 is calculated and reported, not silently rejected. No
        minimum-sample rule is introduced by this Epic.
        """
        _stub_fetch(
            monkeypatch,
            _payload(_event(kickoff="2026-08-01T14:00Z", team_score=2, opponent_score=0)),
        )

        history = espn.get_team_history(TEAM, LEAGUE, Venue.HOME, TARGET_KICKOFF)

        assert history.sample_size == 1
        assert history.clean_sheet_pct == 1.0


class TestCaching:
    def test_repeated_requests_hit_the_network_once(self, monkeypatch):
        """TASK 21. One team's schedule is fetched once per run."""
        calls = []

        def counting_fetch(url, params=None):
            calls.append((url, params))
            return espn.FetchResult(data=_payload(_event(kickoff="2026-08-01T14:00Z")))

        monkeypatch.setattr(espn, "_fetch", counting_fetch)

        espn.get_team_match_records(TEAM, LEAGUE, season=2026)
        espn.get_team_match_records(TEAM, LEAGUE, season=2026)
        espn.get_team_match_records(TEAM, LEAGUE, season=2026)

        assert len(calls) == 1

    def test_cache_key_separates_team_league_and_season(self, monkeypatch):
        """
        Every parameter that changes the response is in the key. Sharing a cache
        entry across seasons would silently answer a 2026 question with 2025
        matches.
        """
        calls = []

        def counting_fetch(url, params=None):
            calls.append((url, params))
            return espn.FetchResult(data={"events": []})

        monkeypatch.setattr(espn, "_fetch", counting_fetch)

        espn.get_team_match_records(TEAM, LEAGUE, season=2026)
        espn.get_team_match_records(TEAM, LEAGUE, season=2025)
        espn.get_team_match_records("999", LEAGUE, season=2026)
        espn.get_team_match_records(TEAM, "eng.1", season=2026)

        assert len(calls) == 4

    def test_failure_is_not_cached(self, monkeypatch):
        """A transient outage must not poison the rest of the run."""
        state = {"fail": True}

        def flaky_fetch(url, params=None):
            if state["fail"]:
                return espn.FetchResult(error=espn.ESPNError.TIMEOUT, detail="stub")
            return espn.FetchResult(data=_payload(_event(kickoff="2026-08-01T14:00Z")))

        monkeypatch.setattr(espn, "_fetch", flaky_fetch)

        assert espn.get_team_match_records(TEAM, LEAGUE, season=2026) is None

        state["fail"] = False
        assert espn.get_team_match_records(TEAM, LEAGUE, season=2026) is not None

    def test_cache_cannot_bypass_the_kickoff_cutoff(self, monkeypatch):
        """
        The cache holds RAW RECORDS, so two fixtures with different kickoffs
        derive different histories from one fetch. If it cached the derived
        statistic instead, the second fixture would inherit the first's cutoff.
        """
        calls = []

        def counting_fetch(url, params=None):
            calls.append(url)
            return espn.FetchResult(
                data=_payload(
                    _event(event_id="1", kickoff="2026-08-01T14:00Z",
                           team_score=1, opponent_score=0),
                    _event(event_id="2", kickoff="2026-08-10T14:00Z",
                           team_score=1, opponent_score=1),
                )
            )

        monkeypatch.setattr(espn, "_fetch", counting_fetch)

        early = espn.get_team_history(
            TEAM, LEAGUE, Venue.HOME, datetime(2026, 8, 5, tzinfo=timezone.utc)
        )
        late = espn.get_team_history(
            TEAM, LEAGUE, Venue.HOME, datetime(2026, 8, 20, tzinfo=timezone.utc)
        )

        assert len(calls) == 1
        assert early.sample_size == 1
        assert late.sample_size == 2


class TestRequestShape:
    def test_uses_https_and_the_league_scoped_schedule_path(self, monkeypatch):
        """
        TASK 20. Same transport, HTTPS, and the league-scoped path - which is
        what makes the response competition-pure in the first place.
        """
        captured = {}

        def capturing_fetch(url, params=None):
            captured["url"] = url
            captured["params"] = params
            return espn.FetchResult(data={"events": []})

        monkeypatch.setattr(espn, "_fetch", capturing_fetch)

        espn.get_team_match_records(TEAM, LEAGUE, season=2026)

        assert captured["url"].startswith("https://")
        assert captured["url"].endswith(f"/{LEAGUE}/teams/{TEAM}/schedule")
        assert captured["params"] == {"season": 2026}

    def test_season_defaults_to_the_resolved_current_season(self, monkeypatch):
        captured = {}

        def capturing_fetch(url, params=None):
            captured["params"] = params
            return espn.FetchResult(data={"events": []})

        monkeypatch.setattr(espn, "_fetch", capturing_fetch)
        espn.get_team_match_records(TEAM, LEAGUE)

        assert captured["params"]["season"] == espn.resolve_season(LEAGUE)
