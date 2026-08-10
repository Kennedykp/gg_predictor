"""
Historical season identity (Epic 2B.1).

WHAT THIS FILE PROTECTS
-----------------------
One sentence: a fixture belongs to a season because ESPN says so, never because
its kickoff fell inside a window we constructed.

The defect these tests lock out was not hypothetical. Measured against the Epic
2A cache of 140 league-seasons, the July-June window produced:

    eng.1 2019/20   314 of 380 fixtures   (66 deleted)
    ita.1 2019/20   282 of 380 fixtures   (98 deleted)
    esp.1 2019/20   323 of 380 fixtures   (57 deleted)

and the same 66/98/57 fixtures then reappeared inside the NEXT season's window,
so eng.1 2020/21 returned 446 fixtures - 380 real ones plus 66 belonging to the
season before, three of whose clubs had been relegated and never played a minute
of 2020/21.

Every payload below is shaped like the real thing, and the headline fixtures are
real: event 541466 is Everton 1-3 Bournemouth, kicked off 2020-07-26T15:00Z,
labelled `season.year = 2019, slug = '2019-20-english-premier-league'`.

HOW TO READ THE STRUCTURE
-------------------------
`TestTheOldRuleIsWrong` is the before-fix reproduction, kept permanently rather
than deleted after the fix. It states the OLD rule as an explicit function and
demonstrates it giving wrong answers on real data. That keeps the proof in the
repository instead of in a commit message, and means the reasoning survives even
if someone later proposes "why not just widen the window?".
"""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

import espn
from domain.match_records import MatchRecord, Venue, eligible_history
from domain.season_identity import (
    SeasonIdentity,
    SeasonVerdict,
    classify_event_season,
    season_year_from_label,
)

EPL = "eng.1"
EPL_LEAGUE_ID = "700"

# Real events, transcribed from the Epic 2A cache.
BOURNEMOUTH = "349"      # relegated at the end of 2019/20
NEWCASTLE = "361"
EVERTON = "368"
LEEDS = "357"            # promoted for 2020/21
FULHAM = "370"


def espn_event(
    event_id: str,
    kickoff: str,
    home_id: str,
    away_id: str,
    home_goals: int,
    away_goals: int,
    season_year: Any = 2019,
    season_slug: Any = "2019-20-english-premier-league",
    league_id: str = EPL_LEAGUE_ID,
    league_slug: Optional[str] = None,
    status: str = "STATUS_FULL_TIME",
    completed: bool = True,
    state: str = "post",
    omit_season: bool = False,
) -> Dict[str, Any]:
    """One ESPN scoreboard event, shaped exactly as the live feed shapes it."""
    event: Dict[str, Any] = {
        "id": event_id,
        "uid": f"s:600~l:{league_id}~e:{event_id}",
        "date": kickoff,
        "competitions": [
            {
                "id": event_id,
                "status": {"type": {"name": status, "state": state, "completed": completed}},
                "competitors": [
                    {
                        "id": home_id,
                        "homeAway": "home",
                        "score": str(home_goals),
                        "team": {"id": home_id},
                    },
                    {
                        "id": away_id,
                        "homeAway": "away",
                        "score": str(away_goals),
                        "team": {"id": away_id},
                    },
                ],
            }
        ],
    }
    if not omit_season:
        season: Dict[str, Any] = {}
        if season_year is not None:
            season["year"] = season_year
        if season_slug is not None:
            season["slug"] = season_slug
        event["season"] = season
    if league_slug is not None:
        event["league"] = {"slug": league_slug}
    return event


def scoreboard(events: List[Dict[str, Any]], slug: str = EPL, league_id: str = EPL_LEAGUE_ID):
    return {"events": events, "leagues": [{"slug": slug, "id": league_id}]}


# --- The real 2019/20 shape, split across the two calendar windows -----------
#
# Within the July-June window: an ordinary March fixture.
MARCH_2020 = espn_event("541611", "2020-03-07T15:00Z", EVERTON, NEWCASTLE, 2, 2)

# OUTSIDE it, because COVID pushed the season into July: Bournemouth's real
# penultimate fixture. This is the match the old window deleted from 2019/20 and
# then imported into 2020/21.
JULY_2020 = espn_event("541466", "2020-07-26T15:00Z", EVERTON, BOURNEMOUTH, 1, 3)

# The genuine 2020/21 season, which began in September 2020 and so shares a
# calendar window with the tail above.
SEPT_2020 = espn_event(
    "573721",
    "2020-09-12T11:30Z",
    FULHAM,
    LEEDS,
    3,
    4,
    season_year=2020,
    season_slug="2020-21-english-premier-league",
)


def windowed_feed(monkeypatch, by_window: Dict[str, List[Dict[str, Any]]], league: str = EPL):
    """
    Serve DIFFERENT events per `dates=` window, as the real endpoint does.

    A stub that answered every window identically could not reproduce the defect
    at all: the entire bug lives in which window a fixture shows up in.
    """
    calls: List[dict] = []

    def fake_fetch(url: str, params: Optional[dict] = None):
        params = params or {}
        calls.append(dict(params))
        if "/scoreboard" not in url:
            return espn.FetchResult(error=espn.ESPNError.HTTP_ERROR, detail=url)
        events = by_window.get(str(params.get("dates")), [])
        return espn.FetchResult(data=scoreboard(events, slug=league))

    monkeypatch.setattr(espn, "_fetch", fake_fetch)
    espn.clear_league_cache()
    espn.clear_schedule_cache()
    return calls


# Windows produced for season 2019 and 2020 respectively.
W2019 = "20190701-20200630"
W2020 = "20200701-20210630"
W2021 = "20210701-20220630"


# ===========================================================================
# PHASE 2 - THE OLD RULE, REPRODUCED AND SHOWN TO BE WRONG
# ===========================================================================
class TestTheOldRuleIsWrong:
    """
    The before-fix reproduction, kept permanently.

    `date_window_membership` below IS the old implementation's rule, stated
    plainly. Each test feeds it real data and shows it answering incorrectly,
    then shows the new rule answering correctly on the same input. Both halves
    matter: the first proves there was a defect, the second proves this Epic
    addressed THAT defect rather than something adjacent.
    """

    @staticmethod
    def date_window_membership(kickoff: str, season: int) -> bool:
        """The pre-2B.1 rule: in the window, therefore in the season."""
        start = datetime(season, 7, 1, tzinfo=timezone.utc)
        end = datetime(season + 1, 7, 1, tzinfo=timezone.utc)
        moment = espn.parse_kickoff(kickoff)
        assert moment is not None
        return start <= moment < end

    def test_old_rule_deletes_the_covid_extended_season(self):
        """
        Everton 1-3 Bournemouth, 2020-07-26, is a 2019/20 Premier League match.
        ESPN says so. The July-June window says otherwise, and the window was
        what the code believed.
        """
        assert self.date_window_membership("2020-07-26T15:00Z", 2019) is False

        identity = espn.extract_season_identity(JULY_2020, payload_competition=EPL)
        assert identity.season_year == 2019
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.ACCEPTED
        )

    def test_old_rule_imports_it_into_the_following_season(self):
        """
        The same match, offered to 2020/21. The window accepts it - that is the
        contamination, and it is the same defect seen from the other side.
        """
        assert self.date_window_membership("2020-07-26T15:00Z", 2020) is True

        identity = espn.extract_season_identity(JULY_2020, payload_competition=EPL)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2020)
            is SeasonVerdict.WRONG_SEASON
        )

    def test_no_window_boundary_can_separate_the_two_seasons(self):
        """
        Why "just move June 30 later" cannot work, stated as an assertion.

        2019/20 ran to 2020-07-26 and 2020/21 began 2020-09-12, so a boundary
        would have to sit between them - but ita.1 2019/20 ran to 2020-08-02 and
        other leagues had already restarted by then. The seasons OVERLAP in
        calendar time, so no single date separates them, in either direction.
        """
        last_2019 = espn.parse_kickoff(JULY_2020["date"])
        first_2020 = espn.parse_kickoff(SEPT_2020["date"])
        assert last_2019 is not None and first_2020 is not None

        # Any boundary late enough to keep 2019/20 whole...
        boundary = datetime(2020, 8, 31, tzinfo=timezone.utc)
        assert last_2019 < boundary          # ...admits the July tail, good...
        # ...and the identical window built for 2020 admits it a second time.
        assert self.date_window_membership(JULY_2020["date"], 2020) is True

        # Only the metadata separates them, and it does so exactly.
        tail = espn.extract_season_identity(JULY_2020, payload_competition=EPL)
        opener = espn.extract_season_identity(SEPT_2020, payload_competition=EPL)
        assert tail.season_year == 2019
        assert opener.season_year == 2020


# ===========================================================================
# PHASE 4 / 5 - THE TWO REGRESSION TARGETS, END TO END
# ===========================================================================
class TestCovidExtendedSeasonIsPreserved:
    def test_july_fixtures_are_recovered_into_2019_20(self, monkeypatch):
        """
        The 2019/20 request must return BOTH the in-window March fixture and the
        out-of-window July one. Discovery reaches into the next calendar window;
        validation decides what comes back.
        """
        windowed_feed(monkeypatch, {W2019: [MARCH_2020], W2020: [JULY_2020, SEPT_2020]})

        records = espn.get_league_match_records(EPL, season=2019)

        assert records is not None
        assert {r.event_id for r in records} == {"541611", "541466"}
        assert all(r.season == 2019 for r in records)

    def test_discovery_looks_past_june_30(self, monkeypatch):
        """The second window is requested, not assumed away."""
        calls = windowed_feed(monkeypatch, {W2019: [MARCH_2020], W2020: [JULY_2020]})

        espn.get_league_match_records(EPL, season=2019)

        assert [c["dates"] for c in calls] == [W2019, W2020]

    def test_the_recovered_fixture_keeps_its_real_scoreline(self, monkeypatch):
        """Recovered, not reconstructed: Everton 1-3 Bournemouth, exactly."""
        windowed_feed(monkeypatch, {W2019: [], W2020: [JULY_2020]})

        records = espn.get_league_match_records(EPL, season=2019)

        assert records is not None
        assert len(records) == 1
        record = records[0]
        assert (record.venue, record.goals_for, record.goals_against) == (Venue.HOME, 1, 3)
        assert record.team_id == EVERTON and record.opponent_id == BOURNEMOUTH


class TestPreviousSeasonCannotContaminate:
    def test_2020_21_excludes_the_2019_20_tail(self, monkeypatch):
        """
        The Epic 2A anomaly, preserved as a regression fixture. Both fixtures sit
        in the 2020/21 discovery window; only one belongs to 2020/21.
        """
        windowed_feed(monkeypatch, {W2020: [JULY_2020, SEPT_2020], W2021: []})

        records = espn.get_league_match_records(EPL, season=2020)

        assert records is not None
        assert [r.event_id for r in records] == ["573721"]
        assert all(r.season == 2020 for r in records)

    def test_a_relegated_club_cannot_enter_the_season_it_never_played(self, monkeypatch):
        """
        PHASE 17, team continuity. Bournemouth (349) were relegated in 2019/20
        and played no 2020/21 fixture. Under the old rule their July 2020
        matches were served as 2020/21 data - the clearest possible proof that
        calendar proximity is not membership, since no amount of squinting makes
        a relegated club part of the next season.
        """
        windowed_feed(monkeypatch, {W2020: [JULY_2020, SEPT_2020], W2021: []})

        records = espn.get_league_match_records(EPL, season=2020)

        assert records is not None
        involved = {r.team_id for r in records} | {r.opponent_id for r in records}
        assert BOURNEMOUTH not in involved
        assert {LEEDS, FULHAM} <= involved

    def test_the_same_event_is_accepted_by_its_own_season(self, monkeypatch):
        """
        Symmetry check: rejection from 2020/21 is not the fixture being lost,
        it is the fixture being filed correctly. Without this, a bug that
        dropped everything would pass the test above.
        """
        windowed_feed(monkeypatch, {W2019: [], W2020: [JULY_2020, SEPT_2020]})

        records = espn.get_league_match_records(EPL, season=2019)

        assert records is not None
        assert [r.event_id for r in records] == ["541466"]


# ===========================================================================
# PHASES 6, 7, 11 - THE IDENTITY RULE ITSELF
# ===========================================================================
class TestSeasonIdentityRule:
    def test_matching_season_is_accepted(self):
        identity = SeasonIdentity(competition=EPL, season_year=2019, season_label=None)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.ACCEPTED
        )

    def test_other_season_is_rejected(self):
        identity = SeasonIdentity(competition=EPL, season_year=2018, season_label=None)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.WRONG_SEASON
        )

    def test_missing_season_year_fails_closed(self):
        """
        PHASE 7. The requested season is NOT substituted, the kickoff is NOT
        consulted, and the event is not admitted on the grounds that it arrived
        from the right URL. No metadata, no membership.
        """
        identity = SeasonIdentity(competition=EPL, season_year=None, season_label=None)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.UNVERIFIABLE
        )

    def test_a_label_alone_is_not_enough(self):
        """
        A slug that names the right season, with no year, is still unverifiable.
        Epic 1B.5 found ESPN echoing surprising season values, so a single
        unconfirmed field does not carry the decision.
        """
        identity = SeasonIdentity(
            competition=EPL, season_year=None, season_label="2019-20-english-premier-league"
        )
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.UNVERIFIABLE
        )

    def test_year_and_label_contradiction_fails_closed(self):
        """
        PHASE 11, and a real case rather than an invented one: eng.1's 2009
        window contains 380 events labelled `year=2009, slug='2013-2014-...'`
        whose scorelines match neither season. When a payload contradicts
        itself, there is no principled way to pick a winner, so nothing is
        picked.
        """
        identity = SeasonIdentity(
            competition=EPL, season_year=2009, season_label="2013-2014-barclays-premier-league"
        )
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2009)
            is SeasonVerdict.UNVERIFIABLE
        )

    def test_agreeing_label_is_accepted(self):
        identity = SeasonIdentity(
            competition=EPL, season_year=2019, season_label="2019-20-english-premier-league"
        )
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.ACCEPTED
        )

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("2009-2010-barclays-premier-league", 2009),
            ("2019-20-english-premier-league", 2019),
            ("20062007-english-league-championship", 2006),
            ("201819-german-2-bundesliga", 2018),
            ("2019-20 English Premier League", 2019),
            # Phase labels carry no season and must stay silent, not guess.
            ("regular-season", None),
            ("group-stage", None),
            ("promotion-final", None),
            ("", None),
            (None, None),
        ],
    )
    def test_label_parsing_says_nothing_when_it_knows_nothing(self, label, expected):
        assert season_year_from_label(label) == expected

    @pytest.mark.parametrize("year", [None, "", "n/a", 2019.5, True, [], {}])
    def test_malformed_season_metadata_fails_closed(self, year):
        """
        PHASE 25.9. Note `True`: in Python `isinstance(True, int)` holds, so a
        boolean would otherwise sail through as the year 1.
        """
        identity = SeasonIdentity(competition=EPL, season_year=year, season_label=None)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is not SeasonVerdict.ACCEPTED
        )

    def test_zero_is_never_substituted_for_a_missing_season(self):
        """
        PHASE 25.12. The house rule: a missing value is missing, never 0. Season
        0 is not a season, and must not be treated as one either.
        """
        identity = SeasonIdentity(competition=EPL, season_year=0, season_label=None)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=0)
            is SeasonVerdict.ACCEPTED
        )
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.WRONG_SEASON
        )
        missing = SeasonIdentity(competition=EPL, season_year=None, season_label=None)
        assert (
            classify_event_season(missing, expected_competition=EPL, requested_season=0)
            is SeasonVerdict.UNVERIFIABLE
        )


# ===========================================================================
# PHASE 8 - COMPETITION IS A SEPARATE INVARIANT
# ===========================================================================
class TestCompetitionIdentity:
    def test_right_season_wrong_competition_is_rejected(self):
        """
        A cup tie played in 2019/20 is a 2019/20 event. It is not an eng.1
        event. Season identity cannot answer that question, which is why it is
        asked separately.
        """
        identity = SeasonIdentity(competition="uefa.champions", season_year=2019)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.WRONG_COMPETITION
        )

    def test_unstated_competition_fails_closed(self):
        identity = SeasonIdentity(competition=None, season_year=2019)
        assert (
            classify_event_season(identity, expected_competition=EPL, requested_season=2019)
            is SeasonVerdict.UNVERIFIABLE
        )

    def test_wrong_competition_is_distinguishable_from_wrong_season(self):
        """
        Three different facts, three different verdicts. Collapsing them into a
        boolean would make a competition leak look like an ordinary season
        boundary in every diagnostic.
        """
        verdicts = {
            classify_event_season(
                SeasonIdentity(competition="uefa.champions", season_year=2019),
                expected_competition=EPL,
                requested_season=2019,
            ),
            classify_event_season(
                SeasonIdentity(competition=EPL, season_year=2018),
                expected_competition=EPL,
                requested_season=2019,
            ),
            classify_event_season(
                SeasonIdentity(competition=EPL, season_year=None),
                expected_competition=EPL,
                requested_season=2019,
            ),
        }
        assert len(verdicts) == 3

    def test_a_foreign_league_event_in_the_payload_is_dropped(self, monkeypatch):
        """
        End to end: an event whose `uid` names another league (l:701) is not
        admitted merely because this response claims to be eng.1.
        """
        intruder = espn_event(
            "999999", "2020-03-08T15:00Z", EVERTON, NEWCASTLE, 1, 1, league_id="701"
        )
        windowed_feed(monkeypatch, {W2019: [MARCH_2020, intruder], W2020: []})

        records = espn.get_league_match_records(EPL, season=2019)

        assert records is not None
        assert [r.event_id for r in records] == ["541611"]

    def test_schedule_endpoint_checks_the_events_own_league(self):
        """
        The team-schedule endpoint DOES carry a per-event league slug, so it is
        checked directly rather than via the payload header.
        """
        cup_tie = espn_event(
            "700001",
            "2020-02-11T20:00Z",
            EVERTON,
            NEWCASTLE,
            2,
            0,
            league_slug="eng.fa",
        )
        league_match = espn_event(
            "541611", "2020-03-07T15:00Z", EVERTON, NEWCASTLE, 2, 2, league_slug=EPL
        )

        records = espn.parse_schedule_events(
            {"events": [cup_tie, league_match]}, EVERTON, EPL, season=2019
        )

        assert [r.event_id for r in records] == ["541611"]


# ===========================================================================
# PHASE 20 - CACHE SAFETY
# ===========================================================================
class TestCacheCannotMixSeasons:
    def test_two_seasons_do_not_share_an_entry(self, monkeypatch):
        windowed_feed(
            monkeypatch,
            {W2019: [MARCH_2020], W2020: [JULY_2020, SEPT_2020], W2021: []},
        )

        first = espn.get_league_match_records(EPL, season=2019)
        second = espn.get_league_match_records(EPL, season=2020)

        assert first is not None and second is not None
        assert {r.event_id for r in first} == {"541611", "541466"}
        assert {r.event_id for r in second} == {"573721"}

    def test_a_cache_hit_cannot_bypass_validation(self, monkeypatch):
        """
        The cache stores POST-validation records, so a hit returns something
        that already passed. Proven by asking for 2020 first - the season whose
        window contains the contamination - and confirming the stored value is
        the validated one, not the raw window.
        """
        windowed_feed(monkeypatch, {W2020: [JULY_2020, SEPT_2020], W2021: []})

        espn.get_league_match_records(EPL, season=2020)
        cached = espn.get_league_match_records(EPL, season=2020)

        assert cached is not None
        assert [r.event_id for r in cached] == ["573721"]

    def test_the_second_read_makes_no_request(self, monkeypatch):
        calls = windowed_feed(monkeypatch, {W2019: [MARCH_2020], W2020: []})

        espn.get_league_match_records(EPL, season=2019)
        before = len(calls)
        espn.get_league_match_records(EPL, season=2019)

        assert len(calls) == before

    def test_team_schedule_cache_is_season_scoped(self, monkeypatch):
        """
        The same club, two seasons, two entries. A shared entry would let one
        season's form answer for another - the same defect as the league cache,
        one level down.
        """
        by_season: Dict[int, List[Dict[str, Any]]] = {
            2019: [
                espn_event(
                    "541466", "2020-07-26T15:00Z", EVERTON, BOURNEMOUTH, 1, 3, league_slug=EPL
                )
            ],
            2020: [
                espn_event(
                    "573721",
                    "2020-09-12T11:30Z",
                    EVERTON,
                    LEEDS,
                    3,
                    0,
                    season_year=2020,
                    season_slug="2020-21-english-premier-league",
                    league_slug=EPL,
                )
            ],
        }

        def fake_fetch(url: str, params: Optional[dict] = None):
            season = int((params or {}).get("season", 0))
            return espn.FetchResult(data={"events": by_season.get(season, [])})

        monkeypatch.setattr(espn, "_fetch", fake_fetch)
        espn.clear_schedule_cache()

        older = espn.get_team_match_records(EVERTON, EPL, season=2019)
        newer = espn.get_team_match_records(EVERTON, EPL, season=2020)

        assert older is not None and newer is not None
        assert [r.event_id for r in older] == ["541466"]
        assert [r.event_id for r in newer] == ["573721"]


# ===========================================================================
# PHASE 22 - POINT-IN-TIME IS UNCHANGED AND STILL INDEPENDENT
# ===========================================================================
class TestPointInTimeStillHolds:
    def test_cutoff_still_excludes_later_matches(self, monkeypatch):
        """
        Season validation admits a record; the cutoff still decides whether it
        may be USED for a given fixture. Two protections, both required.
        """
        windowed_feed(monkeypatch, {W2019: [MARCH_2020], W2020: [JULY_2020]})

        records = espn.get_league_match_records(EPL, season=2019)
        assert records is not None

        eligible = eligible_history(
            records,
            target_kickoff=datetime(2020, 3, 8, tzinfo=timezone.utc),
            venue=Venue.HOME,
            competition=EPL,
        )

        # The July match is in the season but in the future. It is not evidence.
        assert [r.event_id for r in eligible] == ["541611"]

    def test_a_correct_season_does_not_license_a_leak(self, monkeypatch):
        """The cutoff is strict `<`, and belonging to the season does not relax it."""
        windowed_feed(monkeypatch, {W2019: [MARCH_2020], W2020: []})
        records = espn.get_league_match_records(EPL, season=2019)
        assert records is not None

        at_exactly_kickoff = eligible_history(
            records,
            target_kickoff=espn.parse_kickoff(MARCH_2020["date"]),
            venue=Venue.HOME,
            competition=EPL,
        )
        assert at_exactly_kickoff == []


# ===========================================================================
# PHASE 23 - THE CURRENT SEASON MUST STILL WORK
# ===========================================================================
class TestCurrentSeasonBehaviour:
    def test_current_season_still_issues_one_request(self, monkeypatch):
        """
        August 2026 is the season this project has to run in. A historical fix
        that doubled every live request - or worse, filtered out today's
        fixtures - would be a bad trade. The forward window cannot contain a
        played match, so it is not requested.
        """
        current = espn.resolve_season(EPL)
        calls = windowed_feed(monkeypatch, {espn._season_date_range(current): [MARCH_2020]})

        espn.get_league_match_records(EPL)

        assert len(calls) == 1

    def test_discovery_windows_skip_the_future(self):
        assert espn._season_discovery_windows(2019, date(2026, 8, 9)) == [W2019, W2020]
        assert espn._season_discovery_windows(2026, date(2026, 8, 9)) == ["20260701-20270630"]

    def test_a_current_season_fixture_is_accepted(self, monkeypatch):
        current = espn.resolve_season(EPL)
        opener = espn_event(
            "600001",
            f"{current}-08-15T14:00Z",
            EVERTON,
            NEWCASTLE,
            1,
            0,
            season_year=current,
            season_slug=f"{current}-{str(current + 1)[-2:]}-english-premier-league",
        )
        windowed_feed(monkeypatch, {espn._season_date_range(current): [opener]})

        records = espn.get_league_match_records(EPL)

        assert records is not None
        assert [r.event_id for r in records] == ["600001"]

    def test_scheduled_fixtures_are_still_not_results(self, monkeypatch):
        """
        Season validity does not make an unplayed fixture a result. The
        completion rules are untouched by this Epic.
        """
        current = espn.resolve_season(EPL)
        upcoming = espn_event(
            "600002",
            f"{current}-08-22T14:00Z",
            EVERTON,
            NEWCASTLE,
            0,
            0,
            season_year=current,
            status="STATUS_SCHEDULED",
            state="pre",
            completed=False,
        )
        windowed_feed(monkeypatch, {espn._season_date_range(current): [upcoming]})

        assert espn.get_league_match_records(EPL) == []


# ===========================================================================
# PHASES 15, 16, 24 - ANOMALIES, COUNTS AND ERRORS
# ===========================================================================
class TestAnomaliesAreNotRepaired:
    def test_a_short_season_stays_short(self, monkeypatch):
        """
        PHASE 15. fra.1 2019/20 was abandoned after 279 of 380 fixtures. That is
        history, not corruption, and nothing here may pad it back to 380.
        """
        played = [
            espn_event(
                str(700100 + i),
                f"2020-02-{(i % 28) + 1:02d}T15:00Z",
                EVERTON,
                NEWCASTLE,
                1,
                1,
                season_year=2019,
                season_slug=None,
            )
            for i in range(5)
        ]
        abandoned = espn_event(
            "700200",
            "2020-04-05T15:00Z",
            EVERTON,
            NEWCASTLE,
            0,
            0,
            season_year=2019,
            season_slug=None,
            status="STATUS_CANCELED",
            state="post",
            completed=False,
        )
        windowed_feed(monkeypatch, {W2019: played + [abandoned], W2020: []}, league="fra.1")

        records = espn.get_league_match_records("fra.1", season=2019)

        assert records is not None
        assert len(records) == 5

    def test_expected_match_count_is_not_the_test(self, monkeypatch):
        """
        PHASE 16. A season is not validated by arriving at 380. Here 380 events
        are returned, 66 of which belong to the previous season - exactly the
        Epic 2A shape - and the count alone would have called it healthy.
        """
        contaminated = [
            espn_event(
                str(800000 + i),
                "2020-07-15T15:00Z",
                EVERTON,
                BOURNEMOUTH,
                1,
                0,
                season_year=2019,
            )
            for i in range(66)
        ]
        genuine = [
            espn_event(
                str(900000 + i),
                "2020-09-15T15:00Z",
                FULHAM,
                LEEDS,
                1,
                0,
                season_year=2020,
                season_slug="2020-21-english-premier-league",
            )
            for i in range(314)
        ]
        windowed_feed(monkeypatch, {W2020: contaminated + genuine, W2021: []})

        records = espn.get_league_match_records(EPL, season=2020)

        assert records is not None
        assert len(records) == 314
        assert all(r.season == 2020 for r in records)


class TestErrorSemantics:
    def test_an_empty_season_is_not_a_failure(self, monkeypatch):
        """PHASE 24. A league that has not kicked off returns [], not None."""
        windowed_feed(monkeypatch, {W2019: [], W2020: []})

        assert espn.get_league_match_records(EPL, season=2019) == []

    def test_a_season_of_only_wrong_season_events_is_still_empty_not_failed(self, monkeypatch):
        """
        Nothing survived validation, but the provider worked perfectly. Reporting
        a failure here would hide a real integrity signal behind a network-shaped
        error.
        """
        windowed_feed(monkeypatch, {W2019: [SEPT_2020], W2020: [SEPT_2020]})

        assert espn.get_league_match_records(EPL, season=2019) == []

    def test_a_failed_discovery_window_fails_the_whole_request(self, monkeypatch):
        """
        A partial season presented as a whole one is the defect this Epic
        exists to remove. If a window cannot be read, its contents are unknown,
        so the answer is "unknown" rather than "here is the other half".
        """
        def fake_fetch(url: str, params: Optional[dict] = None):
            if str((params or {}).get("dates")) == W2019:
                return espn.FetchResult(data=scoreboard([MARCH_2020]))
            return espn.FetchResult(error=espn.ESPNError.SERVER_ERROR, detail="stubbed")

        monkeypatch.setattr(espn, "_fetch", fake_fetch)
        espn.clear_league_cache()

        assert espn.get_league_match_records(EPL, season=2019) is None

    def test_a_failure_is_not_cached(self, monkeypatch):
        state = {"fail": True}

        def fake_fetch(url: str, params: Optional[dict] = None):
            if state["fail"]:
                return espn.FetchResult(error=espn.ESPNError.SERVER_ERROR, detail="stubbed")
            return espn.FetchResult(data=scoreboard([MARCH_2020]))

        monkeypatch.setattr(espn, "_fetch", fake_fetch)
        espn.clear_league_cache()

        assert espn.get_league_match_records(EPL, season=2019) is None
        state["fail"] = False
        records = espn.get_league_match_records(EPL, season=2019)
        assert records is not None and len(records) == 1


# ===========================================================================
# PHASES 18, 19 - PROVENANCE ON THE NORMALIZED RECORD
# ===========================================================================
class TestProvenance:
    def test_a_record_carries_the_stated_season_not_the_requested_one(self, monkeypatch):
        """
        The distinction that makes provenance worth having: `season` is what
        ESPN said. For the July 2020 fixture the two answers differ from any
        date-derived guess, which is precisely why the field is stored.
        """
        windowed_feed(monkeypatch, {W2019: [], W2020: [JULY_2020]})

        records = espn.get_league_match_records(EPL, season=2019)

        assert records is not None
        record = records[0]
        assert record.season == 2019
        assert record.kickoff is not None and record.kickoff.year == 2020
        assert record.provider == "espn"
        assert record.competition == EPL
        assert record.event_id == "541466"

    def test_phase_is_recorded_but_never_filters(self, monkeypatch):
        """
        PHASE 9. 303 ordinary ger.1 2010/11 fixtures are labelled
        'group-stage', so filtering on the phase slug would delete a legitimate
        season. It is provenance, and the modelling decision is left open.
        """
        group_stage = espn_event(
            "260001",
            "2011-02-05T14:30Z",
            EVERTON,
            NEWCASTLE,
            2,
            1,
            season_year=2010,
            season_slug="group-stage",
        )
        windowed_feed(monkeypatch, {"20100701-20110630": [group_stage], "20110701-20120630": []},
                      league="ger.1")

        records = espn.get_league_match_records("ger.1", season=2010)

        assert records is not None
        assert len(records) == 1
        assert records[0].season_phase == "group-stage"

    def test_records_without_provenance_say_so(self):
        """A record that was never told its season reports None, not a guess."""
        record = MatchRecord(venue=Venue.HOME, goals_for=1, goals_against=0, completed=True)
        assert record.season is None
        assert record.season_phase is None
        assert record.provider is None


# ===========================================================================
# EXTRACTION - THE PROVIDER-SPECIFIC HALF
# ===========================================================================
class TestExtraction:
    def test_scoreboard_shape(self):
        identity = espn.extract_season_identity(JULY_2020, payload_competition=EPL)
        assert identity == SeasonIdentity(
            competition=EPL,
            season_year=2019,
            season_label="2019-20-english-premier-league",
            phase="2019-20-english-premier-league",
        )

    def test_schedule_shape_uses_display_name(self):
        """The schedule endpoint sends `displayName` where the scoreboard sends a slug."""
        event = {
            "season": {"year": 2019, "displayName": "2019-20 English Premier League"},
            "league": {"slug": EPL},
        }
        identity = espn.extract_season_identity(event)
        assert identity.season_year == 2019
        assert identity.season_label == "2019-20 English Premier League"
        assert identity.phase is None

    def test_a_string_year_is_still_a_stated_year(self):
        assert espn.extract_season_identity({"season": {"year": "2019"}}).season_year == 2019

    @pytest.mark.parametrize("raw", [None, "", "n/a", 2019.5, {}, []])
    def test_unusable_years_become_none(self, raw):
        assert espn.extract_season_identity({"season": {"year": raw}}).season_year is None

    def test_a_missing_season_block_becomes_none(self):
        identity = espn.extract_season_identity({"id": "1"})
        assert identity.season_year is None
        assert identity.season_label is None

    def test_the_events_own_league_wins_over_the_payload_header(self):
        """
        If a response labelled eng.1 contains an event that says it is eng.fa,
        the event is believed - and then rejected. The header is the weaker
        claim because it describes the response, not the match.
        """
        event = {"season": {"year": 2019}, "league": {"slug": "eng.fa"}}
        assert espn.extract_season_identity(event, payload_competition=EPL).competition == "eng.fa"

    @pytest.mark.parametrize(
        "uid,expected",
        [("s:600~l:700~e:541530", "700"), ("s:600~l:701~e:1", "701"), ("", None), (None, None),
         ("s:600~e:1", None)],
    )
    def test_uid_league_extraction(self, uid, expected):
        assert espn._uid_league_id(uid) == expected
