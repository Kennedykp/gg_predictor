"""
Offline unit tests for the two-stage Odds API integration.

Verifies:
1. /events payload parsing: event_id and home/away teams extracted and indexed.
2. Team normalization & aliases: Wolves/Spurs/Forest resolve; both teams must match; partial matches return None.
3. Event not found: returns None and makes NO event-odds call (quota-safe).
4. Valid event BTTS payload: extracts Yes odds correctly.
5. Deterministic bookmaker selection: first available bookmaker rule preserved.
6. Missing BTTS market: returns None safely.
7. Event cache: repeated fixture lookups in the same league trigger exactly ONE /events request.
8. Odds cache: repeated lookups of the same event_id trigger exactly ONE event-odds request.
9. Cached None: unavailable BTTS result is cached; repeated lookup does not re-query API.
10. HTTP failures on /events: 401, 404, 429, 500, network error return None safely.
11. HTTP failures on /events/{id}/odds: 401, 404, 429, 500, network error return None safely.
12. Quota headers: x-requests-used, x-requests-remaining, x-requests-last tracked safely.
13. Public API compatibility: get_btts_odds(home, away, league_id) signature unchanged.
14. Model invariance: Poisson calculations, filters, N>=3 gate, and decision thresholds unchanged.
"""

import inspect
from typing import Any, Dict, List, Optional

import pytest
import requests

import odds_api
import shared.odds
from decision import make_decision
from poisson import calculate_gg_probability

SAMPLE_EPL_EVENTS = [
    {
        "id": "epl_game_1",
        "sport_key": "soccer_epl",
        "sport_title": "EPL",
        "commence_time": "2026-02-08T15:00:00Z",
        "home_team": "Wolves",
        "away_team": "Spurs",
    },
    {
        "id": "epl_game_2",
        "sport_key": "soccer_epl",
        "sport_title": "EPL",
        "commence_time": "2026-02-08T17:30:00Z",
        "home_team": "Nottm Forest",
        "away_team": "Arsenal",
    },
]

SAMPLE_EPL_GAME_1_ODDS = {
    "id": "epl_game_1",
    "sport_key": "soccer_epl",
    "home_team": "Wolves",
    "away_team": "Spurs",
    "bookmakers": [
        {
            "key": "bookmaker_a",
            "title": "Bookmaker A",
            "markets": [
                {
                    "key": "btts",
                    "outcomes": [
                        {"name": "Yes", "price": 1.95},
                        {"name": "No", "price": 1.85},
                    ],
                }
            ],
        },
        {
            "key": "bookmaker_b",
            "title": "Bookmaker B",
            "markets": [
                {
                    "key": "btts",
                    "outcomes": [
                        {"name": "Yes", "price": 2.10},
                        {"name": "No", "price": 1.75},
                    ],
                }
            ],
        },
    ],
}

SAMPLE_EPL_GAME_2_ODDS = {
    "id": "epl_game_2",
    "sport_key": "soccer_epl",
    "home_team": "Nottm Forest",
    "away_team": "Arsenal",
    "bookmakers": [
        {
            "key": "bookmaker_a",
            "title": "Bookmaker A",
            "markets": [
                {
                    "key": "btts",
                    "outcomes": [
                        {"name": "Yes", "price": 1.75},
                        {"name": "No", "price": 2.05},
                    ],
                }
            ],
        }
    ],
}


class FakeResponse:
    """Mock requests.Response for offline deterministic testing."""

    def __init__(
        self,
        json_data: Any,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
    ):
        self._json_data = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            http_err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            http_err.response = self
            raise http_err


def default_router(url: str, params: Optional[dict] = None, timeout: int = 30) -> FakeResponse:
    """Routes mocked responses based on endpoint."""
    if "/events" in url and "/odds" not in url:
        return FakeResponse(SAMPLE_EPL_EVENTS)
    elif "/events/epl_game_1/odds" in url:
        return FakeResponse(SAMPLE_EPL_GAME_1_ODDS)
    elif "/events/epl_game_2/odds" in url:
        return FakeResponse(SAMPLE_EPL_GAME_2_ODDS)
    return FakeResponse({})


@pytest.fixture(autouse=True)
def _clear_cache_between_tests(monkeypatch):
    """Ensure in-memory odds cache is cleared before and after each test."""
    odds_api.clear_cache()
    # Provide a test API key so _make_request proceeds to mock requests.get
    monkeypatch.setattr(shared.odds, "ODDS_API_KEY", "test_mock_api_key")
    yield
    odds_api.clear_cache()


class TestEventDiscoveryAndMatching:
    def test_events_payload_parsing(self, monkeypatch):
        """1. /events payload parsing extracts event_id and indexes teams correctly."""
        monkeypatch.setattr(requests, "get", default_router)

        events = odds_api.fetch_league_events("eng.1")
        assert len(events) == 2
        assert events[0]["id"] == "epl_game_1"
        assert events[0]["home_team"] == "Wolves"
        assert events[0]["away_team"] == "Spurs"
        assert events[0]["norm_home"] == "wolverhampton wanderers"
        assert events[0]["norm_away"] == "tottenham hotspur"

    def test_team_normalization_and_aliases(self, monkeypatch):
        """2. Existing team aliases work deterministically for event resolution."""
        monkeypatch.setattr(requests, "get", default_router)

        # Full ESPN names -> The Odds API "Wolves" and "Spurs"
        ev_id_1 = odds_api.find_event_id_for_match(
            "Wolverhampton Wanderers", "Tottenham Hotspur", "eng.1"
        )
        assert ev_id_1 == "epl_game_1"

        # "Nottingham Forest" -> "Nottm Forest"
        ev_id_2 = odds_api.find_event_id_for_match(
            "Nottingham Forest", "Arsenal", "eng.1"
        )
        assert ev_id_2 == "epl_game_2"

        # Partial matches (one team matches, other does not) must return None
        assert odds_api.find_event_id_for_match("Wolves", "Chelsea", "eng.1") is None
        assert odds_api.find_event_id_for_match("Chelsea", "Spurs", "eng.1") is None

    def test_event_not_found_makes_no_odds_request(self, monkeypatch):
        """3. When event is not found, get_btts_odds returns None and makes NO odds request."""
        requested_urls: List[str] = []

        def tracking_get(url: str, *args, **kwargs):
            requested_urls.append(url)
            return default_router(url, *args, **kwargs)

        monkeypatch.setattr(requests, "get", tracking_get)

        odds = odds_api.get_btts_odds("Arsenal", "Aston Villa", "eng.1")
        assert odds is None
        # Only the /events endpoint should be called; zero /odds calls
        assert len(requested_urls) == 1
        assert "/events" in requested_urls[0]
        assert "/odds" not in requested_urls[0]


class TestEventOddsParsing:
    def test_valid_event_btts_payload(self, monkeypatch):
        """4. Valid event BTTS payload extracts Yes odds correctly."""
        monkeypatch.setattr(requests, "get", default_router)

        odds = odds_api.get_btts_odds("Wolverhampton Wanderers", "Tottenham Hotspur", "eng.1")
        assert odds == 1.95

    def test_deterministic_bookmaker_selection(self, monkeypatch):
        """5. Multiple bookmakers: selects first available bookmaker offering market."""
        payload_odds = {
            "id": "match_100",
            "bookmakers": [
                {
                    "key": "bm_1",
                    "markets": [
                        {
                            "key": "btts",
                            "outcomes": [
                                {"name": "Yes", "price": 1.90},
                                {"name": "No", "price": 1.90},
                            ],
                        }
                    ],
                },
                {
                    "key": "bm_2",
                    "markets": [
                        {
                            "key": "btts",
                            "outcomes": [
                                {"name": "Yes", "price": 2.20},
                                {"name": "No", "price": 1.70},
                            ],
                        }
                    ],
                },
            ],
        }

        def custom_router(url, *a, **k):
            if "/odds" in url:
                return FakeResponse(payload_odds)
            return FakeResponse([{"id": "match_100", "home_team": "Arsenal", "away_team": "Chelsea"}])

        monkeypatch.setattr(requests, "get", custom_router)
        odds = odds_api.get_btts_odds("Arsenal", "Chelsea", "eng.1")
        assert odds == 1.90

    def test_missing_btts_market_returns_none(self, monkeypatch):
        """6. Missing BTTS market returns None safely."""
        payload_odds = {
            "id": "match_100",
            "bookmakers": [
                {
                    "key": "bm_1",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [{"name": "Arsenal", "price": 1.50}],
                        }
                    ],
                }
            ],
        }

        def custom_router(url, *a, **k):
            if "/odds" in url:
                return FakeResponse(payload_odds)
            return FakeResponse([{"id": "match_100", "home_team": "Arsenal", "away_team": "Chelsea"}])

        monkeypatch.setattr(requests, "get", custom_router)
        odds = odds_api.get_btts_odds("Arsenal", "Chelsea", "eng.1")
        assert odds is None


class TestCachingBehavior:
    def test_events_cache_causes_single_discovery_call_per_league(self, monkeypatch):
        """7. Event discovery request happens at most once per league per run."""
        events_call_count = 0

        def counting_get(url: str, *args, **kwargs):
            nonlocal events_call_count
            if "/events" in url and "/odds" not in url:
                events_call_count += 1
            return default_router(url, *args, **kwargs)

        monkeypatch.setattr(requests, "get", counting_get)

        # Lookup 1
        odds_1 = odds_api.get_btts_odds("Wolves", "Spurs", "eng.1")
        # Lookup 2
        odds_2 = odds_api.get_btts_odds("Nottingham Forest", "Arsenal", "eng.1")
        # Lookup 3 (unmatched game)
        odds_3 = odds_api.get_btts_odds("Everton", "Liverpool", "eng.1")

        assert events_call_count == 1
        assert odds_1 == 1.95
        assert odds_2 == 1.75
        assert odds_3 is None

    def test_odds_cache_causes_single_request_per_event_id(self, monkeypatch):
        """8. Event odds request happens at most once per event_id per run."""
        odds_call_count = 0

        def counting_get(url: str, *args, **kwargs):
            nonlocal odds_call_count
            if "/odds" in url:
                odds_call_count += 1
            return default_router(url, *args, **kwargs)

        monkeypatch.setattr(requests, "get", counting_get)

        # First lookup fetches odds
        odds_1 = odds_api.get_btts_odds("Wolves", "Spurs", "eng.1")
        # Second lookup for same match uses cache
        odds_2 = odds_api.get_btts_odds("Wolverhampton Wanderers", "Tottenham Hotspur", "eng.1")

        assert odds_call_count == 1
        assert odds_1 == 1.95
        assert odds_2 == 1.95

    def test_cached_none_when_btts_unavailable(self, monkeypatch):
        """9. Unavailable BTTS result is cached as None; repeated lookup makes no API call."""
        odds_call_count = 0
        payload_no_btts = {"id": "epl_game_1", "bookmakers": []}

        def router(url: str, *args, **kwargs):
            nonlocal odds_call_count
            if "/odds" in url:
                odds_call_count += 1
                return FakeResponse(payload_no_btts)
            return FakeResponse(SAMPLE_EPL_EVENTS)

        monkeypatch.setattr(requests, "get", router)

        res_1 = odds_api.get_btts_odds("Wolves", "Spurs", "eng.1")
        res_2 = odds_api.get_btts_odds("Wolves", "Spurs", "eng.1")

        assert res_1 is None
        assert res_2 is None
        assert odds_call_count == 1


class TestErrorAndQuotaHandling:
    @pytest.mark.parametrize("status_code", [401, 404, 429, 500])
    def test_http_failures_on_events_return_none_safely(self, monkeypatch, status_code):
        """10. HTTP error status codes on /events return None safely."""
        monkeypatch.setattr(
            requests,
            "get",
            lambda *a, **k: FakeResponse({}, status_code=status_code),
        )
        assert odds_api.get_btts_odds("Wolves", "Spurs", "eng.1") is None

    def test_network_failure_on_events_returns_none_safely(self, monkeypatch):
        """10b. Network connection failure on /events returns None safely."""
        def raise_conn_err(*a, **k):
            raise requests.exceptions.ConnectionError("Network down")

        monkeypatch.setattr(requests, "get", raise_conn_err)
        assert odds_api.get_btts_odds("Wolves", "Spurs", "eng.1") is None

    @pytest.mark.parametrize("status_code", [401, 404, 429, 500])
    def test_http_failures_on_event_odds_return_none_safely(self, monkeypatch, status_code):
        """11. HTTP error status codes on /events/{id}/odds return None safely."""
        def router(url: str, *a, **k):
            if "/odds" in url:
                return FakeResponse({}, status_code=status_code)
            return FakeResponse(SAMPLE_EPL_EVENTS)

        monkeypatch.setattr(requests, "get", router)
        assert odds_api.get_btts_odds("Wolves", "Spurs", "eng.1") is None

    def test_network_failure_on_event_odds_returns_none_safely(self, monkeypatch):
        """11b. Network connection failure on /events/{id}/odds returns None safely."""
        def router(url: str, *a, **k):
            if "/odds" in url:
                raise requests.exceptions.ConnectionError("Socket timeout")
            return FakeResponse(SAMPLE_EPL_EVENTS)

        monkeypatch.setattr(requests, "get", router)
        assert odds_api.get_btts_odds("Wolves", "Spurs", "eng.1") is None

    def test_quota_headers_tracked_safely(self, monkeypatch):
        """12. Quota headers (x-requests-used, x-requests-remaining, x-requests-last) tracked."""
        def router(url: str, *a, **k):
            if "/odds" in url:
                return FakeResponse(
                    SAMPLE_EPL_GAME_1_ODDS,
                    status_code=200,
                    headers={
                        "x-requests-used": "15",
                        "x-requests-remaining": "485",
                        "x-requests-last": "1",
                    },
                )
            return FakeResponse(SAMPLE_EPL_EVENTS)

        monkeypatch.setattr(requests, "get", router)
        odds = odds_api.get_btts_odds("Wolves", "Spurs", "eng.1")
        assert odds == 1.95

        quota = odds_api.get_last_quota_info()
        assert quota["requests_used"] == 15
        assert quota["requests_remaining"] == 485
        assert quota["requests_last"] == 1


class TestCompatibilityAndInvariance:
    def test_public_api_compatibility(self):
        """13. get_btts_odds() signature unchanged."""
        sig = inspect.signature(odds_api.get_btts_odds)
        params = list(sig.parameters.keys())
        assert params == ["home_team", "away_team", "league_id"]

    def test_model_and_decision_invariance(self):
        """14. Odds never alter Poisson probability, filters, or decision thresholds."""
        # 1. Poisson calculation remains pure math
        base_prob = calculate_gg_probability(
            league_avg_goals=1.45,
            home_goals_scored_home=1.8,
            home_goals_conceded_home=1.1,
            away_goals_scored_away=1.4,
            away_goals_conceded_away=1.3,
        )
        assert base_prob is not None
        assert 0.0 < base_prob["gg_probability"] < 1.0

        # 2. Decision thresholds: edge >= 0.05, odds >= 1.60
        prob = 0.60

        # Qualifying edge and odds -> FLAG GG
        dec_ok = make_decision(gg_probability=prob, odds=2.10, passes_filters=True)
        assert dec_ok["decision"] == "FLAG GG"

        # Low odds < 1.60 -> NO BET
        dec_low_odds = make_decision(gg_probability=prob, odds=1.55, passes_filters=True)
        assert dec_low_odds["decision"] == "NO BET"

        # Missing odds -> NO BET
        dec_no_odds = make_decision(gg_probability=prob, odds=None, passes_filters=True)
        assert dec_no_odds["decision"] == "NO BET"
        assert "No odds available" in dec_no_odds["reasons"]
