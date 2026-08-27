"""Step 1's acceptance criteria, plus the failure modes that cost real time.

Two criteria come straight from the plan: one URL is fetched at most once, and
a hundred requests take at least ten seconds. Both are asserted below without
the suite ever waiting ten seconds, by injecting a clock into the limiter — a
rate limiter whose only test is a real sleep is a rate limiter that eventually
gets marked skip.
"""

from __future__ import annotations

import json

import pytest

from src.edgar import (
    DEFAULT_RATE_PER_SECOND,
    EdgarClient,
    EdgarConfigError,
    EdgarHTTPError,
    ResponseCache,
    TokenBucket,
    normalize_accession,
    normalize_cik,
)

AGENT = "Test Runner test@example.com"


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class FakeResponse:
    def __init__(self, status_code=200, content=b"{}", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}


class FakeSession:
    """Records every call, and replays a scripted sequence of responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [FakeResponse()])
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def make_client(tmp_path, session=None, sleep=lambda _: None, **kwargs):
    clock = FakeClock()
    return EdgarClient(
        user_agent=AGENT,
        cache=ResponseCache(tmp_path / "cache"),
        bucket=TokenBucket(clock=clock, sleep=clock.sleep),
        session=session if session is not None else FakeSession(),
        sleep=sleep,
        **kwargs,
    )


# -- identifiers -----------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [("320193", "0000320193"), (320193, "0000320193"), ("CIK0000320193", "0000320193")],
)
def test_normalize_cik_pads_to_ten_digits(given, expected):
    assert normalize_cik(given) == expected


def test_normalize_cik_rejects_a_ticker():
    with pytest.raises(ValueError):
        normalize_cik("AAPL")


def test_normalize_accession_strips_dashes():
    assert normalize_accession("0000320193-24-000006") == "000032019324000006"


def test_normalize_accession_rejects_the_wrong_length():
    with pytest.raises(ValueError):
        normalize_accession("0000320193-24-6")


# -- the cache -------------------------------------------------------------


def test_one_url_is_fetched_exactly_once(tmp_path):
    """Plan Step 1, first acceptance criterion."""
    session = FakeSession([FakeResponse(content=b'{"cik": 320193}')])
    client = make_client(tmp_path, session)

    first = client.filing_index("320193", "0000320193-24-000006")
    second = client.filing_index("320193", "0000320193-24-000006")

    assert first == second
    assert len(session.calls) == 1
    assert client.stats.network_calls == 1
    assert client.stats.cache_hits == 1


def test_cache_outlives_the_client(tmp_path):
    """A re-run of the pipeline must not re-fetch anything."""
    session = FakeSession()
    make_client(tmp_path, session).submissions("320193")

    second_session = FakeSession()
    second_run = EdgarClient(
        user_agent=AGENT,
        cache=ResponseCache(tmp_path / "cache"),
        session=second_session,
    )
    second_run.submissions("320193")

    assert second_session.calls == []
    assert second_run.stats.cache_hits == 1


def test_a_body_without_metadata_reads_as_a_miss(tmp_path):
    """The atomicity contract: a half-written entry must never look complete.

    Metadata is written after the body, so this is the exact state a process
    killed mid-write leaves behind. Treating it as a hit would serve a possibly
    truncated document forever, and nothing downstream could tell.
    """
    cache = ResponseCache(tmp_path / "cache")
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    body_path, meta_path = cache._paths(url)
    body_path.write_bytes(b'{"truncated": ')

    assert cache.get(url) is None
    assert not meta_path.exists()


def test_a_404_is_not_cached(tmp_path):
    session = FakeSession([FakeResponse(status_code=404, content=b"nope")])
    client = make_client(tmp_path, session)

    with pytest.raises(EdgarHTTPError):
        client.submissions("320193")

    assert client.cache.get(
        "https://data.sec.gov/submissions/CIK0000320193.json"
    ) is None


# -- pacing ----------------------------------------------------------------


def test_a_hundred_requests_take_at_least_ten_seconds():
    """Plan Step 1, second acceptance criterion.

    Ninety-nine intervals at nine per second is 11.0 seconds. The first token
    is free because the bucket starts full, and the bucket holds exactly one —
    a larger capacity would let a fresh process fire a burst, which is the
    traffic shape that reads as abuse from the far end.
    """
    clock = FakeClock()
    bucket = TokenBucket(clock=clock, sleep=clock.sleep)

    for _ in range(100):
        bucket.acquire()

    assert clock.slept >= 10.0
    assert clock.slept == pytest.approx(99 / DEFAULT_RATE_PER_SECOND)


def test_the_default_rate_leaves_headroom_under_the_sec_limit():
    """The published ceiling is ten a second. Nine is a deliberate margin."""
    assert DEFAULT_RATE_PER_SECOND < 10.0


def test_the_bucket_does_not_sleep_when_it_does_not_need_to():
    clock = FakeClock()
    bucket = TokenBucket(rate=1000.0, clock=clock, sleep=clock.sleep)
    assert bucket.acquire() == 0.0
    assert clock.slept == 0.0


# -- retries ---------------------------------------------------------------


def test_transient_failures_are_retried(tmp_path):
    session = FakeSession(
        [
            FakeResponse(status_code=503, content=b""),
            FakeResponse(status_code=503, content=b""),
            FakeResponse(content=b'{"ok": true}'),
        ]
    )
    client = make_client(tmp_path, session)

    assert client.get_json("https://data.sec.gov/x.json") == {"ok": True}
    assert len(session.calls) == 3
    assert client.stats.retries == 2


def test_retry_after_is_honoured(tmp_path):
    naps = []
    session = FakeSession(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "7"}),
            FakeResponse(content=b"{}"),
        ]
    )
    client = make_client(tmp_path, session, sleep=naps.append)

    client.get("https://data.sec.gov/x.json")

    assert naps == [7.0]


def test_a_404_is_not_retried(tmp_path):
    session = FakeSession([FakeResponse(status_code=404)])
    client = make_client(tmp_path, session)

    with pytest.raises(EdgarHTTPError) as caught:
        client.get("https://data.sec.gov/missing.json")

    assert caught.value.status == 404
    assert len(session.calls) == 1


def test_persistent_failure_gives_up_and_says_so(tmp_path):
    session = FakeSession([FakeResponse(status_code=500)])
    client = make_client(tmp_path, session)

    with pytest.raises(EdgarHTTPError) as caught:
        client.get("https://data.sec.gov/x.json")

    assert caught.value.status == 500
    assert len(session.calls) == 5


# -- identification --------------------------------------------------------


def test_a_client_without_a_user_agent_refuses_to_exist(monkeypatch):
    """Fail at construction, not forty minutes into a backfill."""
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    with pytest.raises(EdgarConfigError) as caught:
        EdgarClient()
    assert "EDGAR_USER_AGENT" in str(caught.value)


def test_the_user_agent_is_actually_sent(tmp_path):
    session = FakeSession()
    make_client(tmp_path, session).submissions("320193")
    assert session.calls[0]["headers"]["User-Agent"] == AGENT


def test_the_environment_supplies_the_user_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "Env Person env@example.com")
    client = EdgarClient(cache=ResponseCache(tmp_path / "cache"), session=FakeSession())
    assert client.user_agent == "Env Person env@example.com"


# -- URLs ------------------------------------------------------------------


def test_archive_urls_strip_the_leading_zeros_the_json_apis_require(tmp_path):
    """The two spellings of a CIK, and the 404 that follows from confusing them."""
    session = FakeSession()
    client = make_client(tmp_path, session)

    client.document("0000320193", "0000320193-24-000006", "ex-991.htm")

    assert session.calls[0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019324000006/ex-991.htm"
    )


def test_company_facts_url_uses_the_padded_form(tmp_path):
    session = FakeSession()
    client = make_client(tmp_path, session)

    client.company_facts(320193)

    assert session.calls[0]["url"] == (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    )


# -- against the real SEC --------------------------------------------------


@pytest.mark.network
def test_a_real_filing_index_comes_back(tmp_path):
    """Excluded from CI. The one test that proves the headers are acceptable."""
    client = EdgarClient(cache=ResponseCache(tmp_path / "cache"))
    payload = client.submissions("320193")
    assert payload["cik"] == "320193"
    assert "filings" in payload


@pytest.mark.parametrize("rate", [3.0, 7.0, 9.0, 11.0])
def test_pacing_terminates_on_rates_that_do_not_divide_cleanly(rate):
    """Regression: the limiter used to spin forever on 1/9 of a second.

    The original ``acquire`` slept, refilled, and re-checked. Refilling after a
    ``1/9`` second sleep computes ``0 + (1/9) * 9`` — which is
    ``0.9999999999999999``, not ``1.0`` — so the check failed, the residual
    deficit fell below the clock's resolution, and the clock stopped moving.
    The suite hung here rather than failing, which is the worse outcome: a
    hang looks like a slow machine.
    """
    clock = FakeClock()
    bucket = TokenBucket(rate=rate, clock=clock, sleep=clock.sleep)

    for _ in range(50):
        bucket.acquire()

    assert clock.slept == pytest.approx(49 / rate)
