"""US Census Bureau API client (ACS demographics for states and congressional districts)."""
from __future__ import annotations

from typing import Any, Iterator

from election_data import config
from election_data.http import HttpClient, build_url, redact_url


class CensusKeyError(RuntimeError):
    pass


class CensusClient:
    def __init__(self, api_key: str | None = None, http: HttpClient | None = None, base_url: str | None = None):
        self.api_key = api_key or config.secret("CENSUS_API_KEY")
        self.base_url = (base_url or config.CENSUS_BASE_URL).rstrip("/")
        self.http = http or HttpClient(calls_per_hour=1800, timeout=90)

    def query(self, dataset: str, variables: list[str], for_geo: str, in_geo: str | None = None
              ) -> tuple[list[dict[str, Any]], str]:
        """Run a Census data query and return (rows as dicts, provenance URL)."""
        url = f"{self.base_url}/{dataset.strip('/')}"
        params: dict[str, Any] = {"get": ",".join(["NAME", *variables]), "for": for_geo}
        if in_geo:
            params["in"] = in_geo
        public = redact_url(build_url(url, params))
        if self.api_key:
            params["key"] = self.api_key
        resp = self.http.get(url, params=params, allow_redirects=False)
        location = resp.headers.get("Location", "")
        if resp.is_redirect and ("invalid_key" in location or "missing_key" in location):
            raise CensusKeyError(
                "Census API rejected the key (" + location.rsplit("/", 1)[-1] + "). New keys must be activated "
                "from the signup email and can take several minutes to work on every Census server."
            )
        table = resp.json()
        if not table or not isinstance(table, list):
            raise ValueError(f"Unexpected Census response for {public}")
        header, *rows = table
        return [dict(zip(header, r)) for r in rows], public

    def congressional_districts(self, dataset: str, variables: list[str]):
        return self.query(dataset, variables, "congressional district:*", "state:*")

    def states(self, dataset: str, variables: list[str]):
        return self.query(dataset, variables, "state:*")
