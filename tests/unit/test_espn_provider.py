"""
ESPN provider — season resolution, kickoff parsing, fixture eligibility, transport config.

Epic 1B.2 companion to `test_espn_missing_data.py`. That file covers the
"missing statistic must not become 0" contract (GG-001/GG-004); this one covers
the four remaining decisions the provider makes BEFORE any statistic is read:

    which season am I asking about   (resolve_season)
    when is kickoff, in what zone    (parse_kickoff)
    may this fixture be predicted    (is_predictable)
    where am I sending the request   (config URLs)

Every one of these failed silently in production rather than raising, which is
why each has an explicit test here.

Offline and deterministic: no network, no API key, no real clock. `date(...)` is
always passed explicitly, and `espn._make_request` — the single HTTP seam, kept
stable precisely so tests can monkeypatch it — is replaced with a raiser so any
accidental request fails the test instead of hitting ESPN.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import config
import espn


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Replace the one HTTP seam with a raiser — a network call here is a test bug."""

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "espn._make_request was called; every test in this file must be offline"
        )

    monkeypatch.setattr(espn, "_make_request", _forbidden)


# ---------------------------------------------------------------------------
# Season resolution (Epic 1B.2, TASK 10)
# ---------------------------------------------------------------------------


class TestResolveSeasonEuropeanLeagues:
    """
    ESPN names a season by the year it STARTS in, so 2025-26 EPL is `2025`.

    Asking for the wrong year returns a valid-looking table for the WRONG season,
    which is worse than an error: the standings parse cleanly and feed a stale
    league average straight into lambda.
    """

    def test_january_resolves_to_the_previous_year(self):
        """Mid-season January 2026 is still the 2025-26 season, i.e. `2025`."""
        assert espn.resolve_season("eng.1", today=date(2026, 1, 15)) == 2025

    def test_august_resolves_to_the_current_year(self):
        """August 2026 is the start of 2026-27, i.e. `2026` — not 2025."""
        assert espn.resolve_season("eng.1", today=date(2026, 8, 16)) == 2026

    def test_july_is_the_rollover_month(self):
        """
        Boundary. ESPN's 2025-26 block starts 2025-06-01, so by July the new
        season id is already addressable — month 7 must roll over, not month 8.
        """
        assert espn.resolve_season("eng.1", today=date(2026, 7, 1)) == 2026

    def test_june_is_still_the_old_season(self):
        """The other side of the same boundary — off-by-one here silently skews a whole month."""
        assert espn.resolve_season("eng.1", today=date(2026, 6, 30)) == 2025

    @pytest.mark.parametrize("month", [1, 2, 3, 4, 5, 6])
    def test_every_month_before_rollover_uses_the_previous_year(self, month):
        assert espn.resolve_season("eng.1", today=date(2026, month, 15)) == 2025

    @pytest.mark.parametrize("month", [7, 8, 9, 10, 11, 12])
    def test_every_month_from_rollover_uses_the_current_year(self, month):
        assert espn.resolve_season("eng.1", today=date(2026, month, 15)) == 2026

    def test_rollover_month_matches_configured_constant(self):
        """The boundary is config-driven; this pins the test to the same source of truth."""
        rollover = config.EUROPEAN_SEASON_ROLLOVER_MONTH
        assert espn.resolve_season("eng.1", today=date(2026, rollover, 1)) == 2026
        assert espn.resolve_season("eng.1", today=date(2026, rollover - 1, 1)) == 2025


class TestResolveSeasonCalendarYearLeagues:
    """
    Brazil/MLS/Nordics play inside one calendar year, so the season id is simply
    that year — applying the European rollover to them would request the wrong
    season for six months of every year.
    """

    def test_january_resolves_to_the_current_year(self):
        """The case that distinguishes the two conventions: bra.1 in January is 2026, not 2025."""
        assert espn.resolve_season("bra.1", today=date(2026, 1, 15)) == 2026

    def test_august_resolves_to_the_current_year(self):
        assert espn.resolve_season("bra.1", today=date(2026, 8, 16)) == 2026

    @pytest.mark.parametrize("month", [1, 6, 7, 8, 12])
    def test_no_rollover_in_any_month(self, month):
        """A calendar-year league must never roll over — the year is the year, all year."""
        assert espn.resolve_season("bra.1", today=date(2026, month, 15)) == 2026

    def test_january_differs_from_a_european_league_on_the_same_day(self):
        """The single assertion proving the two conventions are actually distinguished."""
        same_day = date(2026, 1, 15)
        assert espn.resolve_season("bra.1", today=same_day) == 2026
        assert espn.resolve_season("eng.1", today=same_day) == 2025

    @pytest.mark.parametrize("league_code", sorted(config.CALENDAR_YEAR_LEAGUES))
    def test_every_configured_calendar_league_uses_the_plain_year(self, league_code):
        assert espn.resolve_season(league_code, today=date(2026, 1, 15)) == 2026


class TestResolveSeasonUsesSuppliedDate:
    """`today` must be honoured — a test that silently read the wall clock would rot in July."""

    def test_result_tracks_the_supplied_date_not_the_real_clock(self):
        assert espn.resolve_season("eng.1", today=date(2019, 1, 15)) == 2018
        assert espn.resolve_season("eng.1", today=date(2031, 8, 15)) == 2031


# ---------------------------------------------------------------------------
# Kickoff parsing (GG-014)
# ---------------------------------------------------------------------------


class TestParseKickoff:
    """
    GG-014: a naive datetime here compares against LOCAL time.

    The host runs at UTC+1, so a 23:30Z kickoff read as naive lands on the wrong
    matchday. `tzinfo is not None` is therefore asserted explicitly on every
    success path — an equal-looking datetime with no zone is the actual bug.
    """

    def test_z_suffix_returns_timezone_aware_utc(self):
        """ESPN's own format: `Z` must become real UTC, not a stripped suffix."""
        parsed = espn.parse_kickoff("2025-08-16T11:30Z")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)
        assert parsed.hour == 11

    def test_z_suffix_equals_the_explicit_utc_datetime(self):
        """Full-value pin, so a future 'fix' cannot shift the instant while staying aware."""
        assert espn.parse_kickoff("2025-08-16T11:30Z") == datetime(
            2025, 8, 16, 11, 30, tzinfo=timezone.utc
        )

    def test_none_returns_none(self):
        """ESPN omits `date` on some events; absence stays absence."""
        assert espn.parse_kickoff(None) is None

    def test_garbage_string_returns_none(self):
        """An unparseable timestamp must not raise mid-fetch and lose the whole scoreboard."""
        assert espn.parse_kickoff("not-a-timestamp") is None

    @pytest.mark.parametrize(
        "raw",
        ["", "16/08/2025", "2025-13-45T99:99Z", "TBD", "2025-08-16T11:30ZZ"],
    )
    def test_unusable_values_return_none(self, raw):
        assert espn.parse_kickoff(raw) is None

    def test_existing_offset_is_preserved(self):
        """An already-offset timestamp is real information — do not overwrite it with UTC."""
        parsed = espn.parse_kickoff("2025-08-16T13:30+02:00")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(hours=2)
        assert parsed.hour == 13

    def test_preserved_offset_still_denotes_the_same_instant(self):
        """13:30+02:00 and 11:30Z are the same moment; comparisons across fixtures rely on it."""
        assert espn.parse_kickoff("2025-08-16T13:30+02:00") == espn.parse_kickoff(
            "2025-08-16T11:30Z"
        )

    def test_late_kickoff_keeps_its_utc_date(self):
        """The concrete GG-014 failure: a 23:30Z kickoff must not drift onto the next day."""
        parsed = espn.parse_kickoff("2025-08-16T23:30Z")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.date() == date(2025, 8, 16)
        assert parsed.hour == 23


# ---------------------------------------------------------------------------
# Fixture eligibility (GG-013)
# ---------------------------------------------------------------------------


class TestIsPredictable:
    """
    GG-013: a pre-match model must never be handed a match that already happened.

    A finished fixture's statistics contain that result, so the 'prediction'
    would be of a known outcome — it would score well and mean nothing.
    """

    def test_scheduled_match_is_predictable(self):
        assert espn.is_predictable({"state": "pre"}) is True

    def test_in_play_match_is_not_predictable(self):
        """Live matches already have goals on the board."""
        assert espn.is_predictable({"state": "in"}) is False

    def test_finished_match_is_not_predictable(self):
        """The GG-013 case — the outcome is known, so this is not a prediction."""
        assert espn.is_predictable({"state": "post"}) is False

    def test_postponed_match_is_not_predictable_despite_pre_state(self):
        """
        `state` alone is insufficient: ESPN still reports `pre` for a postponed
        match, so filtering on state only would keep a fixture that will not be played.
        """
        assert espn.is_predictable({"state": "pre", "is_postponed": True}) is False

    def test_scheduled_and_explicitly_not_postponed_is_predictable(self):
        """The flag must not be treated as merely 'present'."""
        assert espn.is_predictable({"state": "pre", "is_postponed": False}) is True

    @pytest.mark.parametrize("fixture", [{}, {"state": None}, {"state": "unknown"}])
    def test_unknown_state_is_not_predictable(self, fixture):
        """Unknown is not a licence to predict — default closed, not open."""
        assert espn.is_predictable(fixture) is False

    def test_uses_the_fixture_state_enum_value(self):
        """Pins the dict key contract to the enum rather than a loose literal."""
        assert espn.is_predictable({"state": espn.FixtureState.PRE.value}) is True
        assert espn.is_predictable({"state": espn.FixtureState.POST.value}) is False


# ---------------------------------------------------------------------------
# Enum contracts
# ---------------------------------------------------------------------------


class TestFixtureStateEnum:
    """These strings are ESPN's wire values — renaming one silently drops every fixture."""

    @pytest.mark.parametrize(
        "name, value",
        [("PRE", "pre"), ("IN", "in"), ("POST", "post"), ("UNKNOWN", "unknown")],
    )
    def test_member_exists_with_expected_value(self, name, value):
        assert getattr(espn.FixtureState, name).value == value

    def test_exact_member_set(self):
        """No more, no less — an extra state would need explicit handling in is_predictable."""
        assert {m.name for m in espn.FixtureState} == {"PRE", "IN", "POST", "UNKNOWN"}

    def test_is_a_string_enum_so_it_compares_to_raw_api_values(self):
        """`str` subclassing is why `fixture["state"] == FixtureState.PRE.value` works."""
        assert issubclass(espn.FixtureState, str)
        assert espn.FixtureState.PRE == "pre"


class TestESPNErrorEnum:
    """
    Epic 1B.2 TASK 14: "ESPN is down" and "no matches today" must not be the same
    observation. These named causes are what makes the two distinguishable.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "TIMEOUT",
            "CONNECTION",
            "SERVER_ERROR",
            "HTTP_ERROR",
            "MALFORMED_JSON",
            "EMPTY_RESPONSE",
        ],
    )
    def test_member_exists_and_value_matches_its_name(self, name):
        assert getattr(espn.ESPNError, name).value == name

    def test_exact_member_set(self):
        assert {m.name for m in espn.ESPNError} == {
            "TIMEOUT",
            "CONNECTION",
            "SERVER_ERROR",
            "HTTP_ERROR",
            "MALFORMED_JSON",
            "EMPTY_RESPONSE",
        }

    def test_empty_response_exists_for_the_gg003_signature(self):
        """HTTP 200 with a `{}` body needs its OWN name, or it passes as success."""
        assert espn.ESPNError.EMPTY_RESPONSE.value == "EMPTY_RESPONSE"

    def test_is_a_string_enum_so_it_is_loggable_and_serialisable(self):
        assert issubclass(espn.ESPNError, str)
        assert espn.ESPNError.HTTP_ERROR == "HTTP_ERROR"


# ---------------------------------------------------------------------------
# Transport configuration regressions (GG-020 plaintext, GG-003 wrong path)
# ---------------------------------------------------------------------------


class TestTransportURLRegressions:
    """
    GG-020 and GG-003 were both single-character-class config mistakes that
    produced no error at runtime. Pinned here because neither has a symptom:
    plaintext still returns data, and the wrong standings path still returns 200.
    """

    def test_base_url_is_https(self):
        """Plaintext ESPN is MITM-modifiable, and a tampered response feeds the model directly."""
        assert config.ESPN_BASE_URL.startswith("https://")

    def test_standings_base_url_is_https(self):
        assert config.ESPN_STANDINGS_BASE_URL.startswith("https://")

    @pytest.mark.parametrize(
        "url_name", ["ESPN_BASE_URL", "ESPN_STANDINGS_BASE_URL"]
    )
    def test_no_plaintext_http_anywhere_in_url(self, url_name):
        """`startswith` alone would miss an embedded `http://`; this closes that gap."""
        assert "http://" not in getattr(config, url_name)

    def test_standings_url_does_not_use_the_site_path(self):
        """
        GG-003 ROOT CAUSE. `/apis/site/v2/.../standings` answers HTTP 200 with a
        2-byte `{}` body, so nothing raised and every call fell through to the
        hardcoded 1.35. The working path is `/apis/v2/...` — no `/site/`.
        """
        assert "/site/" not in config.ESPN_STANDINGS_BASE_URL

    def test_standings_url_uses_the_verified_working_path(self):
        assert "/apis/v2/" in config.ESPN_STANDINGS_BASE_URL

    def test_scoreboard_and_standings_urls_are_not_the_same(self):
        """They are genuinely different paths; collapsing them reintroduces GG-003."""
        assert config.ESPN_BASE_URL != config.ESPN_STANDINGS_BASE_URL

    def test_scoreboard_url_still_uses_the_site_path(self):
        """Guard against 'fixing' GG-003 by stripping `/site/` from the working scoreboard URL too."""
        assert "/apis/site/v2/" in config.ESPN_BASE_URL

    @pytest.mark.parametrize(
        "url_name", ["ESPN_BASE_URL", "ESPN_STANDINGS_BASE_URL"]
    )
    def test_url_has_no_trailing_slash(self, url_name):
        """Callers build `f"{BASE}/{league}/..."`; a trailing slash yields a `//` path."""
        assert not getattr(config, url_name).endswith("/")
