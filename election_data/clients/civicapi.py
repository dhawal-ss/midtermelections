"""civicAPI client (supplemental results, race calls and polling).

Endpoint paths and field mappings are read from config/sources.yaml
(sources.civicapi.endpoints / field_maps). They are deliberately NOT
hard-coded: copy them from the provider's current v2 reference before
enabling the source. A missing endpoint raises instead of guessing.
"""
from __future__ import annotations

from typing import Any

from election_data import config
from election_data.http import HttpClient, build_url, redact_url


class CivicAPINotConfigured(RuntimeError):
    pass


class CivicAPIClient:
    def __init__(self, http: HttpClient | None = None, base_url: str | None = None):
        self.cfg = config.source_config("civicapi")
        self.base_url = (base_url or config.CIVICAPI_BASE_URL).rstrip("/")
        # One request per second, short retries: the service is often overloaded on election night
        self.http = http or HttpClient(calls_per_hour=3600, timeout=45, max_attempts=3, retry_wait_initial=2.0)

    def endpoint(self, name: str) -> str:
        path = (self.cfg.get("endpoints") or {}).get(name)
        if not path:
            raise CivicAPINotConfigured(
                f"civicAPI endpoint '{name}' is not configured in config/sources.yaml "
                "(sources.civicapi.endpoints). Copy it from the provider's v2 documentation."
            )
        return path

    def field_map(self, name: str) -> dict[str, str]:
        return (self.cfg.get("field_maps") or {}).get(name) or {}

    def fetch(self, endpoint: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        data = self.http.get_json(url, params=params or {})
        return data, redact_url(build_url(url, params))

    def fetch_named(self, name: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        return self.fetch(self.endpoint(name), params)
