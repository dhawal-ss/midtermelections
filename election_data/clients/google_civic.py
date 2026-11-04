"""Google Civic Information API client (elections list, voter info, OCD divisions).

The Representatives endpoints were retired in 2025; only election-related
endpoints are used here. Requires a free Google API key.
"""
from __future__ import annotations

from typing import Any

from election_data import config
from election_data.http import HttpClient, build_url, redact_url


class GoogleCivicClient:
    def __init__(self, api_key: str | None = None, http: HttpClient | None = None):
        self.api_key = api_key or config.secret("GOOGLE_CIVIC_API_KEY")
        if not self.api_key:
            raise ValueError("Missing GOOGLE_CIVIC_API_KEY")
        self.base_url = config.GOOGLE_CIVIC_BASE_URL.rstrip("/")
        self.http = http or HttpClient(calls_per_hour=3600, timeout=30)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        url = f"{self.base_url}/{path}"
        params = dict(params or {})
        public = redact_url(build_url(url, params))
        params["key"] = self.api_key
        return self.http.get_json(url, params=params), public

    def elections(self):
        return self._get("elections")

    def voter_info(self, address: str, election_id: str | None = None):
        """Polling places, contests and election-official info for one address."""
        return self._get("voterinfo", {"address": address, "electionId": election_id})

    def divisions(self, query: str):
        return self._get("divisions", {"query": query})
