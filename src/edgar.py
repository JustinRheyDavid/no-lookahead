"""HTTP access to SEC EDGAR: polite, rate limited, and cached forever.

This module owns the network and nothing else. Callers get bytes and parsed
JSON back; deciding what a filing *means* belongs to ``src/extract.py``, and
deciding when a fact became knowable belongs to ``src/pit.py``.

Two rules from the SEC's access policy shape everything here, and neither is
advisory:

* A request whose ``User-Agent`` does not name a real person and a reachable
  email is refused. There is no default that works, so this module declines to
  build a client at all rather than send one that will be turned away — a
  failure at construction is legible, and a 403 forty minutes into a backfill
  is not.
* Sustained traffic above ten requests a second gets the source IP blocked.
  A block is measured in days and there is no appeal form, so the default rate
  here is nine. The headroom costs eleven percent of throughput on a job that
  runs overnight anyway.

The cache is the other half of the same concern. This pipeline is re-run
dozens of times over the course of the build, and a re-run that re-fetches is
both slower and thousands of needless requests at a public agency. Every 200
response is written to disk and never requested again.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

__all__ = [
    "EdgarClient",
    "EdgarConfigError",
    "EdgarError",
    "EdgarHTTPError",
    "ResponseCache",
    "TokenBucket",
    "normalize_accession",
    "normalize_cik",
]

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# Archives paths take the CIK with leading zeros stripped, while the two JSON
# APIs above take it zero-padded to ten digits. Same identifier, two spellings,
# and mixing them up returns 404 rather than anything that hints at the cause.
ARCHIVE_DIR_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"

USER_AGENT_ENV = "EDGAR_USER_AGENT"

#: Nine, not ten. See the module docstring.
DEFAULT_RATE_PER_SECOND = 9.0

#: 429 and the 5xx family are worth another attempt; a 404 is an answer.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT = 30.0


class EdgarError(RuntimeError):
    """Base class for everything this module raises."""


class EdgarConfigError(EdgarError):
    """The client cannot be built as configured."""


class EdgarHTTPError(EdgarError):
    """A request failed, and retrying it is not going to help."""

    def __init__(self, status: int, url: str, body: bytes = b"") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}")


class TokenBucket:
    """Paces callers to a fixed rate.

    ``capacity`` defaults to a single token, which means no burst allowance:
    the very first request is free and every one after it waits its full
    interval. A larger capacity would let a fresh process fire ``capacity``
    requests instantly, which is exactly the shape of traffic that reads as
    abuse from the far end.

    ``clock`` and ``sleep`` are injectable so the pacing can be tested against
    a fake clock in milliseconds instead of by actually waiting. A rate limiter
    verified only by a real ``time.sleep`` is a rate limiter with one slow test
    that everybody eventually marks skip.
    """

    def __init__(
        self,
        rate: float = DEFAULT_RATE_PER_SECOND,
        capacity: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity < 1:
            raise ValueError("capacity must admit at least one token")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available. Returns seconds spent waiting.

        The slot is reserved under the lock and paid for by sleeping outside
        it, so a second caller queues behind this one's *reservation* rather
        than behind its nap. One pass, no retry loop.

        The retry-loop version of this — sleep, recompute, check again — spins
        forever on a rate that does not divide cleanly. Refilling after a
        ``1/9`` second sleep computes ``0 + (1/9) * 9``, which floating point
        makes ``0.9999999999999999``; the check fails, the residual deficit is
        ``1e-16``, and the next sleep is far below the clock's resolution, so
        the clock stops advancing and the loop never exits. It hung the suite
        at ``test_a_hundred_requests_take_at_least_ten_seconds``. Reserving
        instead of re-checking removes the class of bug rather than the
        instance: there is no convergence to fail.
        """
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._updated)
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            delay = (tokens - self._tokens) / self.rate
            # Spend the shortfall now and push the clock forward to when it
            # will have been earned. The sleep below is settling that debt.
            self._tokens = 0.0
            self._updated = now + delay
        self._sleep(delay)
        return delay


class ResponseCache:
    """An append-only, on-disk cache of successful responses, keyed by URL.

    Each entry is two files: ``<key>.body`` holding the bytes, and
    ``<key>.json`` holding the URL, status, content type and fetch time. **A
    hit requires both**, and the metadata is written second.

    That ordering is the whole design. Writes are atomic (temp file, then
    rename), so a process killed mid-write leaves at worst a stranded ``.tmp``
    or a body with no metadata — both of which read as a miss. The failure mode
    it rules out is the expensive one: a truncated body that later looks like a
    complete answer and quietly poisons every downstream number.

    The metadata also keeps the cache auditable. A directory of bare SHA-256
    filenames is unreadable by a human trying to work out what was fetched and
    when, which matters in a project whose central claim is about knowing when
    things were knowable.
    """

    def __init__(self, directory: Path | str = DEFAULT_CACHE_DIR) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = self.key(url)
        return self.directory / f"{key}.body", self.directory / f"{key}.json"

    def get(self, url: str) -> bytes | None:
        body_path, meta_path = self._paths(url)
        if not (body_path.exists() and meta_path.exists()):
            return None
        return body_path.read_bytes()

    def metadata(self, url: str) -> dict[str, Any] | None:
        _, meta_path = self._paths(url)
        if not meta_path.exists():
            return None
        return json.loads(meta_path.read_text())

    def put(self, url: str, body: bytes, *, status: int, content_type: str) -> None:
        body_path, meta_path = self._paths(url)
        meta = {
            "url": url,
            "status": status,
            "content_type": content_type,
            "bytes": len(body),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._atomic_write(body_path, body)
        self._atomic_write(meta_path, json.dumps(meta, indent=2).encode("utf-8"))

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        scratch = path.with_suffix(path.suffix + ".tmp")
        with open(scratch, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        scratch.replace(path)


@dataclass
class Stats:
    """Counters, mostly so the tests can assert the cache is doing its job."""

    network_calls: int = 0
    cache_hits: int = 0
    retries: int = 0

    @property
    def total_requests(self) -> int:
        return self.network_calls + self.cache_hits


class EdgarClient:
    """Fetches from EDGAR, at most once per URL, at most nine times a second."""

    def __init__(
        self,
        user_agent: str | None = None,
        cache: ResponseCache | None = None,
        bucket: TokenBucket | None = None,
        session: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent or os.environ.get(USER_AGENT_ENV, "").strip()
        if not self.user_agent:
            raise EdgarConfigError(
                f"{USER_AGENT_ENV} is not set. The SEC refuses requests that do "
                "not identify the sender, so there is no working default. Set "
                'it to something like "Jane Doe jane@example.com" — a real name '
                "and an address that reaches you."
            )
        self.cache = cache if cache is not None else ResponseCache()
        self.bucket = bucket if bucket is not None else TokenBucket()
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self._sleep = sleep
        self.stats = Stats()

    # -- low level ---------------------------------------------------------

    def get(self, url: str) -> bytes:
        """Return the body for ``url``, from cache when it has been seen before."""
        cached = self.cache.get(url)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached

        body, status, content_type = self._fetch(url)
        self.cache.put(url, body, status=status, content_type=content_type)
        return body

    def get_json(self, url: str) -> Any:
        return json.loads(self.get(url))

    def _fetch(self, url: str) -> tuple[bytes, int, str]:
        headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        last_status = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.bucket.acquire()
            self.stats.network_calls += 1
            response = self.session.get(url, headers=headers, timeout=self.timeout)
            status = int(response.status_code)
            if status == 200:
                content_type = response.headers.get("Content-Type", "")
                return response.content, status, content_type
            if status not in RETRY_STATUSES:
                raise EdgarHTTPError(status, url, getattr(response, "content", b""))
            last_status = status
            if attempt < MAX_ATTEMPTS:
                self.stats.retries += 1
                self._sleep(self._backoff(attempt, response))
        raise EdgarHTTPError(last_status, url)

    @staticmethod
    def _backoff(attempt: int, response: Any) -> float:
        """Honour ``Retry-After`` when the server sends one, else back off."""
        header = getattr(response, "headers", {}).get("Retry-After")
        if header:
            try:
                return max(0.0, float(header))
            except (TypeError, ValueError):
                pass
        return float(2 ** (attempt - 1))

    # -- EDGAR endpoints ---------------------------------------------------

    def submissions(self, cik: str | int) -> Any:
        """The filing index for one company: every form, with acceptance times."""
        return self.get_json(SUBMISSIONS_URL.format(cik=normalize_cik(cik)))

    def company_facts(self, cik: str | int) -> Any:
        """Every XBRL fact the company has ever tagged, each with a filed date.

        The ``filed`` date on each fact is what makes Step 7's automatic ground
        truth possible, and what ``pit.py`` turns into ``available_at``.
        """
        return self.get_json(COMPANY_FACTS_URL.format(cik=normalize_cik(cik)))

    def filing_index(self, cik: str | int, accession: str) -> Any:
        """The file listing for one filing, used to find the EX-99.1 exhibit."""
        return self.get_json(f"{self._archive_dir(cik, accession)}/index.json")

    def document(self, cik: str | int, accession: str, filename: str) -> bytes:
        """One document out of one filing, by its name in the filing index."""
        return self.get(f"{self._archive_dir(cik, accession)}/{filename}")

    @staticmethod
    def _archive_dir(cik: str | int, accession: str) -> str:
        return ARCHIVE_DIR_URL.format(
            cik_int=int(normalize_cik(cik)),
            accession=normalize_accession(accession),
        )


def normalize_cik(cik: str | int) -> str:
    """Zero-pad a CIK to the ten digits the JSON APIs expect."""
    digits = str(cik).strip().upper().removeprefix("CIK").lstrip("-")
    if not digits.isdigit():
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def normalize_accession(accession: str) -> str:
    """Strip the dashes an accession number carries in every other context."""
    digits = str(accession).strip().replace("-", "")
    if not digits.isdigit() or len(digits) != 18:
        raise ValueError(f"not an accession number: {accession!r}")
    return digits
