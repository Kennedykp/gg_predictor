"""
GG-004 — home/away match counts must never be fabricated by halving.

Epic 1B.2. Before the fix `espn.get_team_stats` read:

    if not home_matches: home_matches = matches_played / 2
    if not away_matches: away_matches = matches_played / 2

so whenever ESPN omitted `homeGamesPlayed`/`awayGamesPlayed` the provider
invented an even split. That is a fabricated divisor, and because it is the
denominator of every per-match rate handed to lambda, it silently distorted
every published probability.

Two independent facts make the halving wrong:

  1. Real schedules are genuinely uneven. Live-verified on the ESPN team
     endpoint: Aalesund (nor.1) played 9 home vs 6 away, AIK 7 vs 8,
     Athletico-PR 11 vs 10. An even split is the exception, not the rule.
  2. An odd `gamesPlayed` cannot be halved into whole matches at all.
     Aalesund's 15 becomes 7.5 "home matches", which is not a possible number
     of matches ever played by anyone.

ESPN does supply the real split counts once a season is under way, so the fix
uses them and treats absence as absence. These tests pin that: real counts are
used, missing counts yield `None`, and a genuine zero is still real data.

The provider is exercised through `espn._make_request`, which is monkeypatched.
No network access, no API key, fully deterministic.
"""

from typing import Any, Dict, List, Optional

import pytest

import espn

# ---------------------------------------------------------------------------
# The live-verified uneven case (docs/REPO_AUDIT.md: nor.1 Aalesund).
#
# 15 matches played, split 9 home / 6 away. This shape is the whole point of
# GG-004: 15 is odd, so `matches_played / 2` yields 7.5 home matches - a
# divisor describing a match count that cannot exist.
# ---------------------------------------------------------------------------
UNEVEN_ODD_STATS: List[Dict[str, Any]] = [
    {"name": "gamesPlayed", "value": 15},
    {"name": "pointsFor", "value": 24},
    {"name": "pointsAgainst", "value": 35},
    {"name": "homeGamesPlayed", "value": 9},
    {"name": "awayGamesPlayed", "value": 6},
    {"name": "homePointsFor", "value": 18},
    {"name": "homePointsAgainst", "value": 21},
    {"name": "awayPointsFor", "value": 6},
    {"name": "awayPointsAgainst", "value": 14},
]

# An exactly-even 10/10 season. Used as the base for the "what would halving
# have produced?" comparisons, because here - and only here - halving happens
# to agree with reality, which is precisely why the defect survived so long.
EVEN_STATS: List[Dict[str, Any]] = [
    {"name": "gamesPlayed", "value": 20},
    {"name": "pointsFor", "value": 30},
    {"name": "pointsAgainst", "value": 20},
    {"name": "homeGamesPlayed", "value": 10},
    {"name": "awayGamesPlayed", "value": 10},
    {"name": "homePointsFor", "value": 18},
    {"name": "homePointsAgainst", "value": 8},
    {"name": "awayPointsFor", "value": 12},
    {"name": "awayPointsAgainst", "value": 12},
]


def build_payload(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap a stats list in the ESPN team-endpoint envelope."""
    return {"team": {"record": {"items": [{"type": "total", "stats": stats}]}}}


def stats_without(base: List[Dict[str, Any]], *names: str) -> List[Dict[str, Any]]:
    """`base` with the named statistics absent — an incomplete API response."""
    return [s for s in base if s["name"] not in names]


def stats_with(base: List[Dict[str, Any]], **overrides: Any) -> List[Dict[str, Any]]:
    """`base` with named statistics overridden — e.g. a genuine zero."""
    return [{**s, "value": overrides[s["name"]]} if s["name"] in overrides else s for s in base]


@pytest.fixture
def espn_returning(monkeypatch):
    """Point `espn._make_request` at a fixed payload instead of the network."""

    def _install(payload: Optional[Dict[str, Any]]):
        monkeypatch.setattr(espn, "_make_request", lambda *a, **k: payload)

    return _install


# ---------------------------------------------------------------------------
# (1) The real counts ESPN supplied are the ones used
# ---------------------------------------------------------------------------


class TestRealSplitCountsAreUsedAsDivisors:
    """
    Each rate must be `split goals / split matches`, using the counts ESPN
    actually reported — never a count derived from the season total.
    """

    def test_home_and_away_rates_use_the_reported_counts(self, espn_returning):
        """18 home goals over 9 home matches, 6 away goals over 6 away matches."""
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] == pytest.approx(18 / 9)
        assert stats["home_goals_scored"] == 2.0
        assert stats["away_goals_scored"] == pytest.approx(6 / 6)
        assert stats["away_goals_scored"] == 1.0

    def test_rates_are_not_derived_from_half_the_season_total(self, espn_returning):
        """
        The explicit negative. Halving 15 gives 7.5, so the fabricated rates
        would have been 18/7.5 = 2.4 home and 6/7.5 = 0.8 away. Neither is the
        answer; both real divisors are used instead.
        """
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        halved = 15 / 2
        assert stats["home_goals_scored"] != pytest.approx(18 / halved)
        assert stats["away_goals_scored"] != pytest.approx(6 / halved)

    def test_conceded_rates_also_use_the_real_counts(self, espn_returning):
        """The same divisor rule governs goals conceded, not just goals scored."""
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_conceded"] == pytest.approx(21 / 9)
        assert stats["away_goals_conceded"] == pytest.approx(14 / 6)


# ---------------------------------------------------------------------------
# (2) The decisive case: an odd total cannot be halved
# ---------------------------------------------------------------------------


class TestOddTotalCannotBeHalvedIntoMatches:
    """
    The single clearest demonstration that halving was never merely imprecise —
    it was arithmetically impossible.

    Aalesund played 15 matches. `matches_played / 2` is 7.5, and no team has
    ever played 7.5 home matches: a match is an indivisible event, so 7.5 is
    not an approximation of a real quantity but a category error being used as
    a divisor. The real, live-verified split is 9 home and 6 away — genuinely
    uneven, as real schedules are, because fixture lists alternate home and
    away in blocks and are reordered by cup runs, TV picks and postponements.
    Halving is therefore wrong even when the total happens to be even; the odd
    total merely makes the wrongness impossible to overlook.
    """

    def test_odd_total_produces_the_real_rate_not_the_halved_one(self, espn_returning):
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] != pytest.approx(18 / 7.5)
        assert stats["home_goals_scored"] == pytest.approx(18 / 9)

    def test_the_two_answers_genuinely_differ(self, espn_returning):
        """
        Guard against a vacuous assertion: 18/7.5 = 2.4 and 18/9 = 2.0 really
        are different numbers, so the test above can actually fail.
        """
        assert 18 / 7.5 != 18 / 9
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] == pytest.approx(2.0)

    def test_reported_counts_sum_to_the_season_total(self, espn_returning):
        """9 + 6 = 15. The uneven split is complete; it is simply not even."""
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_matches"] + stats["away_matches"] == stats["matches_played"]


# ---------------------------------------------------------------------------
# (3) An uneven split changes the answer
# ---------------------------------------------------------------------------


class TestUnevenSplitChangesTheAnswer:
    """
    A 20-match season split 14 home / 6 away — ordinary mid-season, and the
    exact shape the old code could not represent. Halving asserts 10/10, so
    both rates move in opposite directions at once.
    """

    UNEVEN_EVEN_TOTAL = {"homeGamesPlayed": 14, "awayGamesPlayed": 6, "homePointsFor": 21}

    def test_real_split_is_returned_not_the_halved_split(self, espn_returning):
        """21 home goals: 21/14 = 1.5 real, versus 21/10 = 2.1 halved."""
        espn_returning(build_payload(stats_with(EVEN_STATS, **self.UNEVEN_EVEN_TOTAL)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] == pytest.approx(21 / 14)
        assert stats["home_goals_scored"] == pytest.approx(1.5)
        assert stats["home_goals_scored"] != pytest.approx(21 / 10)

    def test_the_away_rate_moves_the_other_way(self, espn_returning):
        """
        12 away goals: 12/6 = 2.0 real, versus 12/10 = 1.2 halved. Halving
        does not merely add noise — it understates one side while overstating
        the other, and both errors feed the same fixture's lambda.
        """
        espn_returning(build_payload(stats_with(EVEN_STATS, **self.UNEVEN_EVEN_TOTAL)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["away_goals_scored"] == pytest.approx(12 / 6)
        assert stats["away_goals_scored"] == pytest.approx(2.0)
        assert stats["away_goals_scored"] != pytest.approx(12 / 10)

    def test_halved_and_real_rates_are_demonstrably_different(self, espn_returning):
        """The comparison is only meaningful if the two differ. 1.5 != 2.1."""
        assert 21 / 14 != 21 / 10
        espn_returning(build_payload(stats_with(EVEN_STATS, **self.UNEVEN_EVEN_TOTAL)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert abs(stats["home_goals_scored"] - 21 / 10) == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# (4) Missing counts are unavailable, never halved — the key regression
# ---------------------------------------------------------------------------


class TestMissingCountsAreUnavailableNotHalved:
    """
    The regression this file exists to prevent.

    When `homeGamesPlayed` is absent the old code silently substituted
    `matches_played / 2` and produced a fully-formed, model-usable number with
    no indication that its divisor had been invented. With 20 matches played
    and 18 home goals it reported 18/10 = 1.8, which is indistinguishable from
    a real rate. The count is now absent, so the rate is absent.
    """

    def test_absent_home_games_played_yields_none(self, espn_returning):
        """Was 18/10 = 1.8 before the fix — fabricated, and impossible to spot."""
        espn_returning(build_payload(stats_without(EVEN_STATS, "homeGamesPlayed")))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None
        assert stats["home_goals_scored"] != pytest.approx(1.8)
        assert stats["home_matches"] is None

    def test_absent_away_games_played_yields_none(self, espn_returning):
        espn_returning(build_payload(stats_without(EVEN_STATS, "awayGamesPlayed")))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["away_goals_scored"] is None
        assert stats["away_goals_conceded"] is None
        assert stats["away_matches"] is None

    def test_absent_count_does_not_invalidate_the_other_side(self, espn_returning):
        """A partial response must not discard the split that did arrive."""
        espn_returning(build_payload(stats_without(EVEN_STATS, "homeGamesPlayed")))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None
        assert stats["away_goals_scored"] == pytest.approx(12 / 10)
        assert stats["away_matches"] == 10

    def test_season_total_still_available_when_a_split_is_not(self, espn_returning):
        """
        `matches_played` and `total_goals_avg` come from season totals, which
        were received. Only the split rate is unavailable — the record is not
        discarded wholesale.
        """
        espn_returning(build_payload(stats_without(EVEN_STATS, "homeGamesPlayed")))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["matches_played"] == 20
        assert stats["total_goals_avg"] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# (5) Zero matches in a split means undefined, not zero
# ---------------------------------------------------------------------------


class TestZeroMatchesInSplitIsUndefined:
    """
    A split with zero matches played has an UNDEFINED rate, not a zero one.

    The distinction is a claim about the world, not a formatting preference.
    Reporting 0.0 asserts "this team scores zero goals per home match" — a
    strong, falsifiable statement about observed performance. The truth is
    "this team has not played at home yet", which supports no rate at all.
    Division by zero is undefined precisely because no value is justified.
    Feeding 0.0 into lambda would model a team as incapable of scoring at home
    on the strength of no evidence whatsoever.
    """

    ZERO_HOME_SPLIT = {"homeGamesPlayed": 0, "homePointsFor": 0}

    def test_zero_home_matches_yields_none_not_zero(self, espn_returning):
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.ZERO_HOME_SPLIT)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None
        assert stats["home_goals_scored"] != 0.0

    def test_zero_home_matches_also_blocks_the_conceded_rate(self, espn_returning):
        """The divisor is shared, so both home rates are undefined together."""
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.ZERO_HOME_SPLIT)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_conceded"] is None

    def test_zero_count_is_still_reported_as_the_real_count(self, espn_returning):
        """
        The rate is undefined, but the count itself is real data: the team has
        played exactly zero home matches. Zero and absent stay distinguishable.
        """
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.ZERO_HOME_SPLIT)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_matches"] == 0
        assert stats["home_matches"] is not None

    def test_away_side_unaffected_by_an_unplayed_home_split(self, espn_returning):
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.ZERO_HOME_SPLIT)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["away_goals_scored"] == pytest.approx(6 / 6)
        assert stats["away_matches"] == 6


# ---------------------------------------------------------------------------
# (6) The real counts are exposed on the returned record
# ---------------------------------------------------------------------------


class TestSplitCountsAreExposedToCallers:
    """
    Epic 1B.2 publishes `home_matches`/`away_matches` so a caller can see the
    split it was given rather than having to trust it. A fabricated divisor is
    only invisible while it stays inside the function.
    """

    def test_returned_counts_equal_the_espn_values(self, espn_returning):
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_matches"] == 9
        assert stats["away_matches"] == 6

    def test_exposed_counts_are_not_half_the_total(self, espn_returning):
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_matches"] != 7.5
        assert stats["away_matches"] != 7.5

    def test_exposed_counts_reconcile_with_the_published_rates(self, espn_returning):
        """
        The published rate must be reproducible from the published counts.
        If they disagreed, the exposed count would be decoration rather than
        the divisor actually used.
        """
        espn_returning(build_payload(UNEVEN_ODD_STATS))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] == pytest.approx(18 / stats["home_matches"])
        assert stats["away_goals_scored"] == pytest.approx(6 / stats["away_matches"])


# ---------------------------------------------------------------------------
# (7) A genuine zero, with matches played, is real data
# ---------------------------------------------------------------------------


class TestGenuineZeroWithMatchesPlayedIsPreserved:
    """
    The other half of the contract, and the guard against over-correcting.

    A team that has played 5 home matches and scored in none of them really
    does average 0.0 goals per home match. That is an observation backed by 5
    matches of evidence, and it must be modelled. Treating every zero as
    missing would discard exactly the low-scoring teams a GG/BTTS model most
    needs to identify.
    """

    GENUINE_ZERO = {"homeGamesPlayed": 5, "homePointsFor": 0}

    def test_zero_goals_over_five_matches_is_exactly_zero(self, espn_returning):
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.GENUINE_ZERO)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_goals_scored"] == 0.0
        assert stats["home_goals_scored"] is not None

    def test_genuine_zero_is_distinguishable_from_unavailable(self, espn_returning):
        """
        Three outcomes, three distinct results — the distinction the whole
        data-contract line of work exists to make: 0 matches -> None,
        5 matches and 0 goals -> 0.0, absent count -> None.
        """
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.GENUINE_ZERO)))
        genuine_zero = espn.get_team_stats("1", "nor.1")

        espn_returning(
            build_payload(stats_with(UNEVEN_ODD_STATS, homeGamesPlayed=0, homePointsFor=0))
        )
        unplayed = espn.get_team_stats("1", "nor.1")

        espn_returning(build_payload(stats_without(UNEVEN_ODD_STATS, "homeGamesPlayed")))
        absent = espn.get_team_stats("1", "nor.1")

        assert genuine_zero is not None and unplayed is not None and absent is not None
        assert genuine_zero["home_goals_scored"] == 0.0
        assert unplayed["home_goals_scored"] is None
        assert absent["home_goals_scored"] is None
        assert genuine_zero["home_goals_scored"] != unplayed["home_goals_scored"]

    def test_genuine_zero_still_uses_the_real_divisor(self, espn_returning):
        """
        0/5 and 0/7.5 are both 0.0, so the value alone cannot prove the right
        divisor was used. The exposed count can.
        """
        espn_returning(build_payload(stats_with(UNEVEN_ODD_STATS, **self.GENUINE_ZERO)))
        stats = espn.get_team_stats("1", "nor.1")
        assert stats is not None
        assert stats["home_matches"] == 5
        assert stats["home_goals_conceded"] == pytest.approx(21 / 5)
