"""
GG-003 — the league average is a MEASUREMENT, or it is `None`. Never `1.35`.

`espn.get_league_avg_goals` used to answer the hardcoded `1.35` whenever it could
not compute a figure, and since the old path (`/apis/site/v2/.../standings`)
replies HTTP 200 with `{}`, it could never compute one. Every prediction ever
published divided by that constant. The constant was also simply wrong: the
live-verified EPL 2025-26 figure is 1.3750, not 1.35 — being close enough to look
plausible is what let it survive.

Two properties are pinned here:

  * UNITS — goals per TEAM per MATCH, i.e. `total goals / total team-games`. A
    standings table counts each fixture twice (once per team), so summing
    `gamesPlayed` yields team-games, not fixtures. This value is the denominator
    of BOTH lambdas in POISSON_V1, so feeding it the per-fixture figure (exactly
    double) would halve every lambda in the system.
  * ABSENCE — any payload the average cannot be computed from yields `None`, and
    specifically not `1.35`. Each absence test asserts both, because
    `result is None` alone would still pass if the fallback returned some other
    fabricated constant.

Offline and deterministic. `espn._make_request` is the monkeypatched seam, matching
tests/unit/test_espn_missing_data.py. No network, no API key.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

import espn
import poisson

# The fabricated constant this defect was about. Named so the assertions below
# read as an explicit refusal rather than an arbitrary inequality.
LEGACY_FABRICATED_AVERAGE = 1.35


# ---------------------------------------------------------------------------
# Minimal standings payloads
# ---------------------------------------------------------------------------
def entry(
    goals_for: Optional[float],
    goals_against: Optional[float],
    games_played: Optional[float],
    omit: Sequence[str] = (),
) -> Dict[str, Any]:
    """
    One standings row, in ESPN's shape.

    ESPN reports goals as `pointsFor` / `pointsAgainst` (a football table has no
    other "points for"). `omit` drops a statistic entirely, which is how a real
    partial response arrives — distinct from the statistic being present as null.
    """
    stats: List[Dict[str, Any]] = [
        {"name": "pointsFor", "value": goals_for},
        {"name": "pointsAgainst", "value": goals_against},
        {"name": "gamesPlayed", "value": games_played},
    ]
    return {"stats": [s for s in stats if s["name"] not in omit]}


def standings(*entries: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap rows in the ESPN standings envelope."""
    return {"children": [{"standings": {"entries": list(entries)}}]}


# Two teams, 10 games each. 15 + 5 = 20 goals scored, mirrored so 20 are also
# conceded, over 10 + 10 = 20 team-games. Hand-calculated: 20 / 20 = 1.0 exactly.
BALANCED_TWO_TEAM = standings(entry(15, 5, 10), entry(5, 15, 10))


def epl_2025_26_totals() -> Tuple[Dict[str, Any], float, float]:
    """
    A 20-team, 38-game table summing to the live-verified EPL 2025-26 totals.

    19 teams on 52 goals plus one on 57 gives 1045 scored; goals-against is the
    same list reversed, so the league balances (every goal is both scored and
    conceded). 20 x 38 = 760 team-games. Hand-calculated: 1045 / 760 = 1.375.
    """
    goals_for = [52.0] * 19 + [57.0]
    goals_against = list(reversed(goals_for))
    rows = [
        entry(gf, ga, 38) for gf, ga in zip(goals_for, goals_against, strict=True)
    ]
    return standings(*rows), sum(goals_for), 20 * 38.0


@pytest.fixture
def espn_returning(monkeypatch):
    """Point `espn._make_request` at a fixed payload instead of the network."""

    def _install(payload: Optional[Dict[str, Any]]):
        monkeypatch.setattr(espn, "_make_request", lambda *a, **k: payload)

    return _install


@pytest.fixture
def espn_capturing(monkeypatch):
    """
    Record every request, answering with a valid table.

    Returns the call list so a test can inspect the URL and query params the
    provider actually sent.
    """
    calls: List[Dict[str, Any]] = []

    def fake_request(url: str, params: Optional[dict] = None):
        calls.append({"url": url, "params": params})
        return BALANCED_TWO_TEAM

    monkeypatch.setattr(espn, "_make_request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# (1) Hand-calculated determinism
# ---------------------------------------------------------------------------
class TestHandCalculatedAverage:
    """
    Fixed inputs, arithmetic done by hand in the docstrings. No tolerance games:
    if the provider changes what it divides by, these numbers move.
    """

    def test_balanced_two_team_table_is_exactly_one(self, espn_returning):
        """(15 + 5) goals / (10 + 10) team-games = 1.0."""
        espn_returning(BALANCED_TWO_TEAM)
        assert espn.get_league_avg_goals("eng.1") == 1.0

    def test_epl_2025_26_totals_give_1_375(self, espn_returning):
        """
        The real figure the fabricated constant was standing in for:
        1045 / 760 = 1.375, versus the hardcoded 1.35.
        """
        payload, total_goals, total_team_games = epl_2025_26_totals()
        assert (total_goals, total_team_games) == (1045.0, 760.0)

        espn_returning(payload)
        result = espn.get_league_avg_goals("eng.1")

        assert result == pytest.approx(1.375)
        assert result == pytest.approx(total_goals / total_team_games)
        # The defect in one line: the old constant was not merely unsourced.
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_uneven_games_played_still_divides_by_the_real_total(self, espn_returning):
        """
        Mid-season tables are ragged. 30 goals / (12 + 8) team-games = 1.5 —
        dividing by a per-team average of games would give a different answer.
        """
        espn_returning(standings(entry(20, 10, 12), entry(10, 20, 8)))
        assert espn.get_league_avg_goals("eng.1") == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# (2) Units
# ---------------------------------------------------------------------------
class TestUnitsArePerTeamPerMatch:
    """
    WHY THIS MATTERS: this value is the denominator of both lambdas in POISSON_V1

        lambda_home = (home_GF_home * away_GA_away) / league_avg_goals
        lambda_away = (away_GF_away * home_GA_home) / league_avg_goals

    The numerators are per-team-per-match rates, so the denominator must be too:
    `total goals / total team-games`. A standings table double-counts fixtures,
    so `sum(gamesPlayed)` is TEAM-GAMES and the correct divisor is already there.

    The per-FIXTURE figure — `total goals / (team-games / 2)` — is exactly twice
    as large (EPL 2025-26: 2.7500 against 1.3750) and looks just as much like a
    league average. Using it would halve every lambda and depress every GG
    probability in the system, silently. poisson.py and GG.md both say "per team".
    """

    def test_result_equals_total_goals_over_total_team_games(self, espn_returning):
        payload, total_goals, total_team_games = epl_2025_26_totals()
        espn_returning(payload)

        result = espn.get_league_avg_goals("eng.1")

        assert result == pytest.approx(total_goals / total_team_games)

    def test_result_is_half_the_per_fixture_figure(self, espn_returning):
        """The units assertion stated as the ratio that distinguishes them."""
        payload, total_goals, total_team_games = epl_2025_26_totals()
        espn_returning(payload)

        result = espn.get_league_avg_goals("eng.1")
        per_fixture = total_goals / (total_team_games / 2)

        assert per_fixture == pytest.approx(2.75)
        assert result == pytest.approx(per_fixture / 2)
        assert result != pytest.approx(per_fixture)

    def test_balanced_table_is_not_the_per_fixture_figure(self, espn_returning):
        """20 goals over 20 team-games is 1.0 per team, 2.0 per fixture."""
        espn_returning(BALANCED_TWO_TEAM)

        result = espn.get_league_avg_goals("eng.1")

        assert result == pytest.approx(1.0)
        assert result == pytest.approx(2.0 / 2)

    def test_per_fixture_denominator_would_halve_both_lambdas(self, espn_returning):
        """
        Demonstrates the consequence rather than asserting it in prose: the same
        team rates through POISSON_V1 with the per-fixture divisor produce half
        the lambdas and a materially lower GG probability.
        """
        espn_returning(BALANCED_TWO_TEAM)
        per_team = espn.get_league_avg_goals("eng.1")
        assert per_team is not None

        correct = poisson.calculate_gg_probability(per_team, 1.5, 1.2, 1.3, 1.4)
        per_fixture_divisor = poisson.calculate_gg_probability(per_team * 2, 1.5, 1.2, 1.3, 1.4)
        assert correct is not None and per_fixture_divisor is not None

        assert per_fixture_divisor["lambda_home"] == pytest.approx(correct["lambda_home"] / 2)
        assert per_fixture_divisor["lambda_away"] == pytest.approx(correct["lambda_away"] / 2)
        assert per_fixture_divisor["gg_probability"] < correct["gg_probability"]


# ---------------------------------------------------------------------------
# (3) The crux: no 1.35 fallback
# ---------------------------------------------------------------------------
class TestNoHardcodedFallback:
    """
    GG-003. Every case below returned 1.35 before the fix, so every one produced
    a usable, wrong denominator from a response that carried no data at all.

    `{}` is the signature case: HTTP 200, success by status code, nothing by
    content. That is what the old standings path returned on every single call.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(None, id="request-failed"),
            pytest.param({}, id="http-200-empty-object"),
            pytest.param({"children": []}, id="no-children"),
            pytest.param({"children": [{"name": "Premier League"}]}, id="no-standings-key"),
            pytest.param({"children": [{"standings": {}}]}, id="no-entries-key"),
            pytest.param({"standings": {"entries": []}}, id="no-children-key"),
        ],
    )
    def test_unusable_payload_is_none_and_not_the_constant(self, espn_returning, payload):
        espn_returning(payload)

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        # Asserted separately: `is None` alone would still pass if the fallback
        # were replaced by some other fabricated number.
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_empty_entries_list_is_none(self, espn_returning):
        """A table with no rows is not a league averaging 1.35."""
        espn_returning(standings())

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_failure_is_distinguishable_from_a_real_average(self, espn_returning):
        """
        The distinction the sub-epic exists to make: a computed figure and an
        unobtainable one no longer look alike to a caller.
        """
        espn_returning(BALANCED_TWO_TEAM)
        computed = espn.get_league_avg_goals("eng.1")

        espn_returning(None)
        unavailable = espn.get_league_avg_goals("eng.1")

        assert computed == 1.0
        assert unavailable is None
        assert computed != unavailable


# ---------------------------------------------------------------------------
# (4) Preseason
# ---------------------------------------------------------------------------
class TestPreseason:
    def test_no_games_played_is_none_not_zero(self, espn_returning):
        """
        The table exists, nothing has been played. Genuinely unavailable — and
        `0` would be worse than useless as a divisor, since POISSON_V1 rejects it
        only by an explicit guard.
        """
        espn_returning(standings(entry(0, 0, 0), entry(0, 0, 0)))

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        assert result != 0
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_zero_games_played_does_not_raise(self, espn_returning):
        """Division by the summed team-games happens after this guard, not before."""
        espn_returning(standings(*[entry(0, 0, 0) for _ in range(20)]))

        assert espn.get_league_avg_goals("eng.1") is None

    def test_one_match_played_league_wide_is_still_computed(self, espn_returning):
        """
        The other half of the contract: a barely-started season is thin data, not
        missing data. 3 goals / 2 team-games = 1.5.
        """
        espn_returning(standings(entry(2, 1, 1), entry(1, 2, 1), entry(0, 0, 0)))

        assert espn.get_league_avg_goals("eng.1") == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# (5) Integrity check
# ---------------------------------------------------------------------------
class TestScoredConcededIntegrity:
    """
    Every goal is both scored by someone and conceded by someone, so league-wide
    `pointsFor` must equal `pointsAgainst`. A mismatch means the table is
    truncated or inconsistent — the totals are then not a league total, and the
    quotient is not a league average.
    """

    def test_mismatched_totals_are_rejected(self, espn_returning):
        """20 scored against 15 conceded: 5 goals unaccounted for, so refuse."""
        espn_returning(standings(entry(15, 5, 10), entry(5, 10, 10)))

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_a_truncated_table_is_rejected_rather_than_averaged(self, espn_returning):
        """
        The realistic failure: rows dropped from the response. The remaining rows
        would still divide cleanly, which is exactly why the check is needed.
        """
        espn_returning(standings(entry(30, 20, 12), entry(25, 22, 12), entry(18, 24, 12)))

        assert espn.get_league_avg_goals("eng.1") is None

    def test_small_discrepancy_within_tolerance_is_accepted(self, espn_returning):
        """
        Not over-corrected. A sub-goal difference is rounding in the feed, not a
        missing row, so the figure is still published: 20 / 20 = 1.0.
        """
        espn_returning(standings(entry(15, 5, 10), entry(5, 14.6, 10)))

        assert espn.get_league_avg_goals("eng.1") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# (6) Partial tables
# ---------------------------------------------------------------------------
class TestPartialTable:
    """
    One row missing a statistic understates a league-wide total, and the result
    still looks like a plausible average. Refuse the whole figure.
    """

    def test_entry_without_goals_for_is_rejected(self, espn_returning):
        espn_returning(standings(entry(15, 5, 10), entry(None, 15, 10, omit=("pointsFor",))))

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_entry_without_games_played_is_rejected(self, espn_returning):
        espn_returning(standings(entry(15, 5, 10), entry(5, 15, None, omit=("gamesPlayed",))))

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        assert result != LEGACY_FABRICATED_AVERAGE

    def test_entry_with_no_stats_at_all_is_rejected(self, espn_returning):
        espn_returning(standings(entry(15, 5, 10), {"stats": []}))

        assert espn.get_league_avg_goals("eng.1") is None

    def test_partial_table_is_not_averaged_over_the_rows_that_arrived(self, espn_returning):
        """
        Names the number being refused: 15 / 10 = 1.5 is computable from the one
        complete row and would pass every downstream sanity check.
        """
        espn_returning(standings(entry(15, 5, 10), entry(None, 15, 10, omit=("pointsFor",))))

        result = espn.get_league_avg_goals("eng.1")

        assert result is None
        assert result != pytest.approx(1.5)


# ---------------------------------------------------------------------------
# (7) Season parameter
# ---------------------------------------------------------------------------
class TestSeasonParameter:
    def test_explicit_season_is_passed_to_the_request(self, espn_capturing):
        """An explicit season must reach the query string, not be re-derived."""
        result = espn.get_league_avg_goals("eng.1", season_id=2025)

        assert result == 1.0
        assert len(espn_capturing) == 1
        assert espn_capturing[0]["params"] == {"season": 2025}

    def test_league_code_is_in_the_requested_url(self, espn_capturing):
        espn.get_league_avg_goals("eng.1", season_id=2025)

        assert "eng.1/standings" in espn_capturing[0]["url"]

    def test_season_is_always_sent_when_not_supplied(self, espn_capturing):
        """
        Omitting `season_id` resolves one rather than sending none — an unseasoned
        standings request is not guaranteed to answer with the current season.
        """
        espn.get_league_avg_goals("eng.1")

        params = espn_capturing[0]["params"]
        assert params is not None
        assert params["season"] == espn.resolve_season("eng.1")

    def test_a_different_season_is_forwarded_unchanged(self, espn_capturing):
        espn.get_league_avg_goals("ger.1", season_id=2024)

        assert espn_capturing[0]["params"] == {"season": 2024}
