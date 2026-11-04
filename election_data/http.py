"""Shared HTTP client: retries, client-side rate limiting, key redaction."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from election_data import config

log = logging.getLogger(__name__)

SENSITIVE_PARAMS = {"api_key", "apikey", "key", "token", "access_token"}
_SECRET_IN_TEXT = re.compile(r"(?i)\b(api_key|apikey|key|token|access_token)=([^&\s'\"<>)]+)")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_text(text: str | None) -> str | None:
    """Mask credentials inside free text (exception messages, log lines)."""
    if not text:
        return text
    return _BEARER.sub(r"\1REDACTED", _SECRET_IN_TEXT.sub(r"\1=REDACTED", text))


class RedactingFormatter(logging.Formatter):
    """Log formatter that masks API keys, including inside tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record)) or ""


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def redact_url(url: str) -> str:
    """Strip credentials from a URL before it is logged or stored."""
    parts = urlsplit(url)
    query = [(k, "REDACTED" if k.lower() in SENSITIVE_PARAMS else v) for k, v in parse_qsl(parts.query)]
    query = [(k, v) for k, v in query if v != "REDACTED"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def build_url(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{url}?{urlencode(clean, doseq=True)}"


class RateLimiter:
    """Minimum interval between calls, derived from a per-hour budget."""

    def __init__(self, calls_per_hour: float | None):
        self.interval = 3600.0 / calls_per_hour if calls_per_hour else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class RetryableHTTPError(requests.HTTPError):
    pass


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableHTTPError, requests.ConnectionError, requests.Timeout))


class HttpClient:
    def __init__(
        self,
        calls_per_hour: float | None = None,
        timeout: float = 60,
        headers: dict[str, str] | None = None,
        max_attempts: int = 5,
        session: requests.Session | None = None,
        retry_wait_initial: float = 1.0,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT, "Accept-Encoding": "gzip"})
        if headers:
            self.session.headers.update(headers)
        self.timeout = timeout
        self.limiter = RateLimiter(calls_per_hour)
        self.max_attempts = max_attempts
        self.retry_wait_initial = retry_wait_initial

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.retry_wait_initial, max=60) + wait_random(0, self.retry_wait_initial),
            reraise=True,
        )
        def _do() -> requests.Response:
            self.limiter.wait()
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and self.retry_wait_initial:
                    time.sleep(min(int(retry_after), 120))
                log.warning("HTTP %s from %s; retrying", resp.status_code, redact_url(resp.url))
                raise RetryableHTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp

        return _do()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        resp = self.get(url, params=params, **kwargs)
        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype and not resp.text.lstrip().startswith(("{", "[")):
            raise ValueError(f"Expected JSON from {redact_url(resp.url)}, got {ctype or 'unknown type'}")
        return resp.json()
