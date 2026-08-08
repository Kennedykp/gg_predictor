"""
GG-001 — missing statistics must not become `0`.

Epic 1B.1. These tests were written BEFORE the fix and run against the unmodified
provider to prove the defect was real rather than inferred from the audit. The
originally-observed buggy values are recorded in each docstring so the change in
behaviour is auditable.

The provider is exercised through `espn._make_request`, which is monkeypatched.
No network access, no API key, fully deterministic.
"""

from typing import Any, Dict, List, Optional

import pytest

import espn

# A complete ESPN "total" record. Every statistic the provider reads is present.
FULL_STATS: List[Dict[str, Any]] = [
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


def stats_without(*names: str) -> List[Dict[str, Any]]:
    """FULL_STATS with the named statistics absent — an incomplete API response."""
    return [s for s in FULL_STATS if s["name"] not in names]


def stats_with(name: str, value: Any) -> List[Dict[str, Any]]:
    """FULL_STATS with one statistic overridden — e.g. a genuine zero."""
    return [{**s, "value": value} if s["name"] == name else s for s in FULL_STATS]


@pytest.fixture
def espn_returning(monkeypatch):
    """Point `espn._make_request` at a fixed payload instead of the network."""

    def _install(payload: Optional[Dict[str, Any]]):
        monkeypatch.setattr(espn, "_make_request", lambda *a, **k: payload)

    return _install


# ---------------------------------------------------------------------------
# The core distinction: absent statistic vs genuine zero
# ---------------------------------------------------------------------------


class TestMissingStatisticIsNotZero:
    """
    BEFORE Epic 1B.1 every assertion below produced `0` instead of `None`.
    `espn.get_stat` was:

        next((s.get("value", 0) for s in stats_list if s.get("name") == name), 0)

    so an absent statistic and a genuine zero were indistinguishable.
    """

    @pytest.mark.parametrize(
        "absent_stat, affected_field",
        [
            ("homePointsFor", "home_goals_scored"),
            ("homePointsAgainst", "home_goals_conceded"),
            ("awayPointsFor", "away_goals_scored"),
            ("awayPointsAgainst", "away_goals_conceded"),
        ],
    )
    def test_absent_statistic_becomes_none(self, espn_returning, absent_stat, affected_field):
        """Was `0.0` before the fix — a fabricated, model-usable number."""
        espn_returning(build_payload(stats_without(absent_stat)))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats[affected_field] is None

    def test_absent_goals_for_makes_total_goals_avg_unavailable(self, espn_returning):
        """
        Was (0 + 20) / 20 = 1.0 before the fix — a plausible-looking average
        built from a statistic that was never received.
        """
        espn_returning(build_payload(stats_without("pointsFor")))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["total_goals_avg"] is None

    def test_only_the_absent_statistic_is_affected(self, espn_returning):
        """A partial response must not invalidate the statistics that did arrive."""
        espn_returning(build_payload(stats_without("homePointsFor")))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None
        assert stats["home_goals_conceded"] == pytest.approx(0.8)
        assert stats["away_goals_scored"] == pytest.approx(1.2)
        assert stats["away_goals_conceded"] == pytest.approx(1.2)

    def test_stat_present_but_value_key_absent_is_unavailable(self, espn_returning):
        """
        ESPN can return the entry without a `value`. No number was received, so
        this is unavailable — previously it defaulted to 0.
        """
        stats_list = [
            {"name": "homePointsFor"} if s["name"] == "homePointsFor" else s for s in FULL_STATS
        ]
        espn_returning(build_payload(stats_list))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None

    def test_explicit_null_value_is_unavailable(self, espn_returning):
        espn_returning(build_payload(stats_with("homePointsFor", None)))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None


class TestGenuineZeroIsPreserved:
    """
    The other half of the contract. A team that genuinely scored 0 is real data
    and must survive as 0.0 — the fix must not over-correct into treating every
    zero as missing.
    """

    def test_genuine_zero_goals_scored_stays_zero(self, espn_returning):
        espn_returning(build_payload(stats_with("homePointsFor", 0)))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["home_goals_scored"] == 0.0
        assert stats["home_goals_scored"] is not None

    def test_genuine_zero_goals_conceded_stays_zero(self, espn_returning):
        espn_returning(build_payload(stats_with("awayPointsAgainst", 0)))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["away_goals_conceded"] == 0.0

    def test_zero_and_missing_are_now_distinguishable(self, espn_returning):
        """The single assertion this entire sub-epic exists to make true."""
        espn_returning(build_payload(stats_with("homePointsFor", 0)))
        genuine_zero = espn.get_team_stats("1", "eng.1")

        espn_returning(build_payload(stats_without("homePointsFor")))
        missing = espn.get_team_stats("1", "eng.1")

        assert genuine_zero is not None and missing is not None
        assert genuine_zero["home_goals_scored"] == 0.0
        assert missing["home_goals_scored"] is None
        assert genuine_zero["home_goals_scored"] != missing["home_goals_scored"]


class TestCompleteResponseUnchanged:
    """
    Regression guard. When ESPN returns everything, the provider must produce
    exactly the values it produced before Epic 1B.1. These numbers were captured
    from the pre-fix implementation.
    """

    def test_all_rates_match_pre_fix_values(self, espn_returning):
        espn_returning(build_payload(FULL_STATS))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats == {
            "team_id": "1",
            "league_id": "eng.1",
            "home_goals_scored": 1.8,
            "away_goals_scored": 1.2,
            "home_goals_conceded": 0.8,
            "away_goals_conceded": 1.2,
            # TRANSITIONED (Epic 1B.3, GG-002). These asserted 0, which was the
            # hardcoded value, not a measurement. ESPN's standings record cannot
            # yield a clean-sheet count - conceding 5 over 5 games is consistent
            # with 0 or 4 clean sheets - so the honest value is None.
            "home_clean_sheet_pct": None,
            "away_clean_sheet_pct": None,
            "total_goals_avg": 2.5,
            "matches_played": 20,

            # Epic 1B.2: the real ESPN split counts, now exposed. Added keys
            # only - every rate above is byte-identical to the pre-fix values.
            "home_matches": 10,
            "away_matches": 10,
        }


class TestMatchesPlayedGating:
    """`matches_played` is the divisor for every rate, so it gates the whole record."""

    def test_absent_games_played_returns_none(self, espn_returning):
        """Pre-fix this worked by accident: absent -> 0 -> `== 0` -> None."""
        espn_returning(build_payload(stats_without("gamesPlayed")))
        assert espn.get_team_stats("1", "eng.1") is None

    def test_zero_games_played_returns_none(self, espn_returning):
        espn_returning(build_payload(stats_with("gamesPlayed", 0)))
        assert espn.get_team_stats("1", "eng.1") is None

    def test_no_record_items_returns_none(self, espn_returning):
        espn_returning({"team": {"record": {"items": []}}})
        assert espn.get_team_stats("1", "eng.1") is None

    def test_failed_request_returns_none(self, espn_returning):
        espn_returning(None)
        assert espn.get_team_stats("1", "eng.1") is None


@pytest.mark.characterization
class TestLegacyHomeAwaySplitHalving:
    """
    TRANSITIONED in Epic 1B.2 — GG-004 is now RESOLVED.

    These two tests previously pinned the legacy behaviour: when ESPN omitted the
    home/away match counts the provider substituted `matches_played / 2`. That
    was fabricated data, and the class was written to keep it visible until this
    sub-epic could remove it.

    The assertions are inverted rather than deleted, so the file still documents
    the defect and now proves it cannot return. ESPN does supply
    homeGamesPlayed/awayGamesPlayed (confirmed live), so a missing count means
    the rate is genuinely unavailable.
    """

    def test_absent_home_games_played_is_no_longer_halved(self, espn_returning):
        """The divisor is absent, so the rate is unavailable - not 18/(20/2)."""
        espn_returning(build_payload(stats_without("homeGamesPlayed")))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None
        assert stats["home_goals_scored"] != pytest.approx(1.8)

    def test_missing_split_is_unavailable_rather_than_an_assumed_even_split(
        self, espn_returning
    ):
        """
        A team having played 14 home and 6 away matches is normal mid-season, so
        assuming 10/10 silently distorted both rates. The provider now declines
        to guess: absence is reported as absence and the fixture is refused
        upstream by validate_poisson_inputs().
        """
        uneven = [s for s in FULL_STATS if s["name"] not in ("homeGamesPlayed",)]
        espn_returning(build_payload(uneven))
        stats = espn.get_team_stats("1", "eng.1")
        assert stats is not None
        assert stats["home_goals_scored"] is None
        assert stats["home_matches"] is None
        # The away side was untouched and must still be computed normally.
        assert stats["away_goals_scored"] == pytest.approx(1.2)
