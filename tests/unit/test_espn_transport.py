"""
ESPN transport contract - GG-003 / GG-012 (Epic 1B.2).

The units under test are `espn._fetch` and its `Optional[dict]` wrapper
`espn._make_request`. Exactly ONE seam is faked - `espn.requests.get`. Status
handling, error classification and the retry policy are the real production
code.

Two properties are defended here:

  1. HTTP 200 is not evidence of data. ESPN's old standings path answered 200
     with the 2-byte body `{}`; nothing raised, so the pipeline treated it as a
     successful fetch and silently fell through to a hardcoded league average.
     That is the GG-003 signature: success by status code, nothing by content.
  2. A transport failure must never become football statistics. Every failure
     mode is asserted to yield None - never a dict - because a dict here would
     be fed straight to the model.

Offline and deterministic: no sockets are opened, and `espn.time.sleep` is
replaced with a recording no-op so the bounded backoff is exercised without
spending real wall-clock time.
"""

from typing import Any, Dict, List, Optional

import pytest
import requests

import config
import espn
from espn import ESPNError

URL = "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings"

# A body that looks like real data: non-empty dict, so it is allowed through.
PAYLOAD: Dict[str, Any] = {"children": [{"standings": {"entries": [{"team": "Arsenal"}]}}]}

# The policy under test, derived from config rather than hardcoded so these
# tests keep asserting "bounded by the configured limit" if the limit changes.
EXPECTED_ATTEMPTS = config.ESPN_MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeResponse:
    """
    Minimal stand-in for `requests.Response`: a status code and a `.json()`.

    `json_error` models the real failure where a 200 carries a body that will
    not parse - `requests` raises a ValueError subclass from `.json()`.
    """

    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        json_error: Optional[Exception] = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class RecordingTransport:
    """
    Scripted replacement for `requests.get` that records every call.

    Each scripted step is either a FakeResponse to return or an exception to
    raise. The last step repeats, so a single step models a persistent failure
    while a two-step script models "fails once, then recovers". Recording the
    calls is what makes "was this retried, and how many times" assertable.
    """

    def __init__(self, *scripted: Any) -> None:
        self._scripted: List[Any] = list(scripted)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, "kwargs": kwargs})
        step = self._scripted[min(len(self.calls) - 1, len(self._scripted) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_kwargs(self) -> Dict[str, Any]:
        return self.calls[-1]["kwargs"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """
    Make the retry backoff free.

    Autouse, so no test in this module can sleep for real: the live policy waits
    0.5s then 1.0s per failing call, which would make a bounded-retry suite
    slower than the behaviour it verifies. The recorded durations are handed
    back so a test can still assert retries were spaced, not busy-looped.
    """
    slept: List[float] = []
    monkeypatch.setattr(espn.time, "sleep", slept.append)
    return slept


@pytest.fixture
def fake_get(monkeypatch):
    """Install a scripted stand-in for `espn.requests.get` and return it."""

    def _install(*scripted: Any) -> RecordingTransport:
        transport = RecordingTransport(*scripted)
        monkeypatch.setattr(espn.requests, "get", transport)
        return transport

    return _install


# ---------------------------------------------------------------------------
# (1) Success
# ---------------------------------------------------------------------------
class TestSuccessfulFetch:
    """A 200 carrying a real body is the ONLY case permitted to produce data."""

    def test_ok_is_true_and_data_is_returned(self, fake_get):
        """The happy path: 200 + non-empty dict -> ok, with the body intact."""
        fake_get(FakeResponse(200, payload=PAYLOAD))
        result = espn._fetch(URL)
        assert result.ok is True
        assert result.error is None
        assert result.data == PAYLOAD

    def test_success_costs_exactly_one_request(self, fake_get):
        """A working call must not retry - retries are for failures only."""
        transport = fake_get(FakeResponse(200, payload=PAYLOAD))
        espn._fetch(URL)
        assert transport.call_count == 1

    def test_make_request_passes_real_data_through_unchanged(self, fake_get):
        """The Optional[dict] wrapper must not reshape a genuine payload."""
        fake_get(FakeResponse(200, payload=PAYLOAD))
        assert espn._make_request(URL) == PAYLOAD


# ---------------------------------------------------------------------------
# (2) HTTP 200 with `{}` - the GG-003 signature
# ---------------------------------------------------------------------------
class TestEmptyResponseIsNotSuccess:
    """
    GG-003 exactly: success by status code, nothing by content.

    ESPN answered the old standings URL with HTTP 200 and the body `{}`. Nothing
    raised, so the caller believed the fetch worked and quietly substituted a
    hardcoded 1.35 league average. An empty body must be NAMED as an error so it
    cannot pass silently.
    """

    def test_empty_object_is_classified_empty_response(self, fake_get):
        """`{}` gets its own error name, distinct from "the request failed"."""
        fake_get(FakeResponse(200, payload={}))
        result = espn._fetch(URL)
        assert result.error is ESPNError.EMPTY_RESPONSE

    def test_empty_object_is_not_ok_and_carries_no_data(self, fake_get):
        """`ok` must be False despite the 200 - status is not evidence of data."""
        fake_get(FakeResponse(200, payload={}))
        result = espn._fetch(URL)
        assert result.ok is False
        assert result.data is None

    def test_empty_object_is_not_retried(self, fake_get):
        """A 200 is a final answer; re-asking cannot make `{}` non-empty."""
        transport = fake_get(FakeResponse(200, payload={}))
        espn._fetch(URL)
        assert transport.call_count == 1

    def test_make_request_returns_none_for_empty_object(self, fake_get):
        """The assertion GG-003 exists for: `{}` must never reach the model."""
        fake_get(FakeResponse(200, payload={}))
        assert espn._make_request(URL) is None


# ---------------------------------------------------------------------------
# (3) HTTP 200 with an unparseable body
# ---------------------------------------------------------------------------
class TestMalformedJson:
    """A 200 whose body will not parse is permanent - the bytes are already here."""

    def test_value_error_from_json_is_classified(self, fake_get):
        """`.json()` raising ValueError is data corruption, not a dead endpoint."""
        fake_get(FakeResponse(200, json_error=ValueError("Expecting value: line 1 column 1")))
        result = espn._fetch(URL)
        assert result.error is ESPNError.MALFORMED_JSON
        assert result.data is None

    def test_malformed_json_is_not_retried(self, fake_get):
        """Re-requesting an unparseable body only burns a free endpoint's quota."""
        transport = fake_get(FakeResponse(200, json_error=ValueError("bad body")))
        espn._fetch(URL)
        assert transport.call_count == 1

    def test_make_request_returns_none(self, fake_get):
        """Half-parsed JSON must not become a partial statistics dict."""
        fake_get(FakeResponse(200, json_error=ValueError("bad body")))
        assert espn._make_request(URL) is None


# ---------------------------------------------------------------------------
# (4) HTTP 4xx - permanent
# ---------------------------------------------------------------------------
class TestClientErrorIsPermanent:
    """A 404 never becomes a 200, so retrying one is pure latency."""

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_client_status_is_http_error(self, fake_get, status):
        """4xx means the request itself was wrong - a distinct, permanent fact."""
        fake_get(FakeResponse(status))
        result = espn._fetch(URL)
        assert result.error is ESPNError.HTTP_ERROR
        assert result.data is None

    def test_404_is_not_retried(self, fake_get):
        """One attempt only: the resource is absent, not temporarily unwell."""
        transport = fake_get(FakeResponse(404))
        espn._fetch(URL)
        assert transport.call_count == 1

    def test_404_costs_no_wall_clock_time(self, fake_get, no_real_sleep):
        """No backoff for a permanent failure - nothing is being waited for."""
        fake_get(FakeResponse(404))
        espn._fetch(URL)
        assert no_real_sleep == []

    def test_make_request_returns_none(self, fake_get):
        """A missing endpoint must not be mistaken for an empty league."""
        fake_get(FakeResponse(404))
        assert espn._make_request(URL) is None


# ---------------------------------------------------------------------------
# (5) HTTP 5xx - transient, retried, bounded
# ---------------------------------------------------------------------------
class TestServerErrorIsRetriedButBounded:
    """5xx is worth another attempt - but a bounded number of them."""

    def test_500_is_classified_server_error(self, fake_get):
        """Distinct from 4xx: ESPN is unwell, the request was fine."""
        fake_get(FakeResponse(500))
        result = espn._fetch(URL)
        assert result.error is ESPNError.SERVER_ERROR
        assert result.data is None

    def test_retries_are_bounded_by_config(self, fake_get):
        """Unbounded retry turns an outage into a hang and hammers a free API."""
        transport = fake_get(FakeResponse(500))
        espn._fetch(URL)
        assert transport.call_count == EXPECTED_ATTEMPTS

    def test_backoff_is_exponential_and_stops_before_the_last_attempt(
        self, fake_get, no_real_sleep
    ):
        """Retries are spaced, and no pointless sleep follows the final attempt."""
        fake_get(FakeResponse(500))
        espn._fetch(URL)
        expected = [config.ESPN_BACKOFF_SECONDS * 2**i for i in range(config.ESPN_MAX_RETRIES)]
        assert no_real_sleep == expected

    def test_make_request_returns_none_after_retries_are_exhausted(self, fake_get):
        """A sustained outage yields no data, not a plausible-looking default."""
        fake_get(FakeResponse(500))
        assert espn._make_request(URL) is None


# ---------------------------------------------------------------------------
# (6) Timeout - transient, retried, bounded
# ---------------------------------------------------------------------------
class TestTimeoutIsRetriedButBounded:
    """A slow endpoint is transient, but must not stall the run forever."""

    def test_timeout_is_classified(self, fake_get):
        """`requests.Timeout` is recorded as TIMEOUT, not a generic failure."""
        fake_get(requests.Timeout("read timed out"))
        result = espn._fetch(URL)
        assert result.error is ESPNError.TIMEOUT
        assert result.data is None

    def test_timeout_retries_are_bounded(self, fake_get):
        """Bounded so a permanently slow endpoint cannot hang the whole run."""
        transport = fake_get(requests.Timeout("read timed out"))
        espn._fetch(URL)
        assert transport.call_count == EXPECTED_ATTEMPTS

    def test_make_request_returns_none(self, fake_get):
        """A timeout is an absence of data, never an empty statistics record."""
        fake_get(requests.Timeout("read timed out"))
        assert espn._make_request(URL) is None


# ---------------------------------------------------------------------------
# (7) Connection failure - transient, retried, bounded
# ---------------------------------------------------------------------------
class TestConnectionErrorIsRetriedButBounded:
    """DNS/socket failures are transient; the retry budget is still finite."""

    def test_connection_error_is_classified(self, fake_get):
        """Recorded as CONNECTION so "ESPN unreachable" stays distinguishable."""
        fake_get(requests.ConnectionError("name resolution failed"))
        result = espn._fetch(URL)
        assert result.error is ESPNError.CONNECTION
        assert result.data is None

    def test_connection_retries_are_bounded(self, fake_get):
        """An offline machine must fail fast and finitely, not loop."""
        transport = fake_get(requests.ConnectionError("name resolution failed"))
        espn._fetch(URL)
        assert transport.call_count == EXPECTED_ATTEMPTS

    def test_make_request_returns_none(self, fake_get):
        """No network means no statistics - and no invented substitute."""
        fake_get(requests.ConnectionError("name resolution failed"))
        assert espn._make_request(URL) is None


# ---------------------------------------------------------------------------
# (8) Recovery - the point of retrying at all
# ---------------------------------------------------------------------------
class TestTransientFailureThenSuccess:
    """Retry has to actually recover, otherwise it is only added latency."""

    def test_500_then_200_succeeds_on_the_retry(self, fake_get):
        """One bad gateway response must not discard an otherwise fine call."""
        transport = fake_get(FakeResponse(500), FakeResponse(200, payload=PAYLOAD))
        result = espn._fetch(URL)
        assert result.ok is True
        assert result.error is None
        assert result.data == PAYLOAD
        assert transport.call_count == 2

    def test_timeout_then_200_succeeds_on_the_retry(self, fake_get):
        """The same recovery must hold for an exception, not just a status code."""
        transport = fake_get(requests.Timeout("read timed out"), FakeResponse(200, payload=PAYLOAD))
        result = espn._fetch(URL)
        assert result.ok is True
        assert result.data == PAYLOAD
        assert transport.call_count == 2

    def test_make_request_returns_the_data_recovered_on_retry(self, fake_get):
        """Through the wrapper too: a recovered call yields the real payload."""
        fake_get(FakeResponse(500), FakeResponse(200, payload=PAYLOAD))
        assert espn._make_request(URL) == PAYLOAD


# ---------------------------------------------------------------------------
# (9) CRITICAL - no failure path may ever produce a dict
# ---------------------------------------------------------------------------
FAILURE_SCENARIOS = [
    pytest.param((FakeResponse(200, payload={}),), ESPNError.EMPTY_RESPONSE, 1, id="200-empty-object"),
    pytest.param(
        (FakeResponse(200, json_error=ValueError("bad body")),),
        ESPNError.MALFORMED_JSON,
        1,
        id="200-malformed-json",
    ),
    pytest.param((FakeResponse(404),), ESPNError.HTTP_ERROR, 1, id="404-not-found"),
    pytest.param((FakeResponse(500),), ESPNError.SERVER_ERROR, EXPECTED_ATTEMPTS, id="500-server-error"),
    pytest.param((requests.Timeout("read timed out"),), ESPNError.TIMEOUT, EXPECTED_ATTEMPTS, id="timeout"),
    pytest.param(
        (requests.ConnectionError("name resolution failed"),),
        ESPNError.CONNECTION,
        EXPECTED_ATTEMPTS,
        id="connection-error",
    ),
]


class TestNoFailurePathEverReturnsData:
    """
    The single invariant this module exists to protect.

    An HTTP failure must never become football statistics. Every failure mode is
    re-checked here through `_make_request`, because that is the function the
    pipeline actually calls - and a dict escaping from it would be indistinguish-
    able from real data by the time it reaches the model.
    """

    @pytest.mark.parametrize("script, expected_error, expected_attempts", FAILURE_SCENARIOS)
    def test_fetch_reports_the_error_and_never_data(
        self, fake_get, script, expected_error, expected_attempts
    ):
        """Each failure is named, carries no data, and stays within its budget."""
        transport = fake_get(*script)
        result = espn._fetch(URL)
        assert result.error is expected_error
        assert result.data is None
        assert result.ok is False
        assert transport.call_count == expected_attempts

    @pytest.mark.parametrize("script, expected_error, expected_attempts", FAILURE_SCENARIOS)
    def test_make_request_returns_none_never_a_dict(
        self, fake_get, script, expected_error, expected_attempts
    ):
        """No failure - transient or permanent - may hand back a mapping."""
        transport = fake_get(*script)
        data = espn._make_request(URL)
        assert data is None, f"{expected_error.value} produced a dict instead of None"
        assert not isinstance(data, dict)
        assert transport.call_count <= EXPECTED_ATTEMPTS


# ---------------------------------------------------------------------------
# (10) An explicit timeout is always passed
# ---------------------------------------------------------------------------
class TestExplicitTimeoutIsAlwaysPassed:
    """
    `requests.get` without `timeout=` can block indefinitely (GG-012).

    A single unresponsive endpoint would then hang the entire run, which is
    worse than a clean failure: nothing is reported and nothing terminates.
    """

    def test_timeout_kwarg_is_the_configured_value(self, fake_get):
        """The timeout is explicit and non-None, not left to library defaults."""
        transport = fake_get(FakeResponse(200, payload=PAYLOAD))
        espn._fetch(URL)
        assert transport.last_kwargs["timeout"] is not None
        assert transport.last_kwargs["timeout"] == config.ESPN_TIMEOUT_SECONDS

    def test_configured_timeout_is_a_positive_bound(self):
        """A zero/negative timeout would be a bound in name only."""
        assert config.ESPN_TIMEOUT_SECONDS > 0

    def test_every_retry_attempt_also_carries_the_timeout(self, fake_get):
        """A retried request must not silently lose its deadline."""
        transport = fake_get(FakeResponse(500))
        espn._fetch(URL)
        assert transport.call_count == EXPECTED_ATTEMPTS
        timeouts = [call["kwargs"].get("timeout") for call in transport.calls]
        assert timeouts == [config.ESPN_TIMEOUT_SECONDS] * EXPECTED_ATTEMPTS
        assert all(value is not None for value in timeouts)

    def test_url_and_params_are_forwarded_unchanged(self, fake_get):
        """Guards against a transport rewrite quietly dropping query params."""
        transport = fake_get(FakeResponse(200, payload=PAYLOAD))
        espn._fetch(URL, {"dates": "20260118"})
        assert transport.calls[0]["url"] == URL
        assert transport.calls[0]["kwargs"]["params"] == {"dates": "20260118"}
