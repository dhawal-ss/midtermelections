"""OpenFEC API client (https://api.open.fec.gov/developers/).

Covers candidates, committees, candidate financial totals, Schedule A
(itemized receipts), Schedule B (disbursements), Schedule E (independent
expenditures) and the election-dates calendar.

Notes
  * Registered keys: 1,000 calls/hour by default; DEMO_KEY is heavily limited.
  * FEC data is refreshed nightly; this is NOT a live-results source.
  * FEC registration is not ballot qualification. Never infer ballot status here.
  * Schedules A/B/E use keyset pagination (pagination.last_indexes), not pages.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from election_data import config
from election_data.http import HttpClient, build_url, redact_url

log = logging.getLogger(__name__)


class FECClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        calls_per_hour: float | None = None,
        http: HttpClient | None = None,
    ):
        self.api_key = api_key or config.secret("FEC_API_KEY")
        if not self.api_key:
            raise ValueError("Missing FEC_API_KEY (register free at https://api.open.fec.gov/developers/)")
        self.base_url = (base_url or config.FEC_BASE_URL).rstrip("/")
        if calls_per_hour is None:
            calls_per_hour = config.source_config("openfec").get("rate_limit_per_hour", 1000)
            # Leave headroom so concurrent jobs do not trip the limit.
            calls_per_hour = calls_per_hour * 0.9
        self.http = http or HttpClient(calls_per_hour=calls_per_hour, timeout=60)
        self.calls = 0

    # -- low level ---------------------------------------------------------
    def url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint.strip('/')}/"

    def public_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        """URL suitable for storing as provenance (API key removed)."""
        return redact_url(build_url(self.url(endpoint), params))

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query.setdefault("per_page", 100)
        query["api_key"] = self.api_key
        self.calls += 1
        return self.http.get_json(self.url(endpoint), params=query)

    def paginate(self, endpoint: str, params: dict[str, Any] | None = None, max_pages: int | None = None
                 ) -> Iterator[tuple[dict[str, Any], str]]:
        """Page-number pagination. Yields (record, provenance_url)."""
        params = dict(params or {})
        page = 1
        while True:
            params["page"] = page
            data = self.get(endpoint, params)
            records = data.get("results", [])
            source_url = self.public_url(endpoint, params)
            for record in records:
                yield record, source_url
            pages = (data.get("pagination") or {}).get("pages") or 1
            if not records or page >= pages or (max_pages and page >= max_pages):
                break
            page += 1

    def paginate_keyset(self, endpoint: str, params: dict[str, Any] | None = None,
                        max_pages: int | None = None) -> Iterator[tuple[dict[str, Any], str]]:
        """Keyset pagination used by Schedule A/B/E endpoints."""
        params = dict(params or {})
        pages = 0
        while True:
            data = self.get(endpoint, params)
            records = data.get("results", [])
            source_url = self.public_url(endpoint, params)
            for record in records:
                yield record, source_url
            pages += 1
            last_indexes = (data.get("pagination") or {}).get("last_indexes")
            if not records or not last_indexes or (max_pages and pages >= max_pages):
                break
            params.update(last_indexes)

    # -- endpoints -----------------------------------------------------------
    def get_candidates(self, cycle: int = 2026, office: str | None = None, state: str | None = None,
                       election_year: int | None = None, **extra: Any):
        """Candidates registered with the FEC for a two-year cycle (office: H, S, P)."""
        params = {"cycle": cycle, "office": office, "state": state, "election_year": election_year,
                  "sort": "name", **extra}
        return self.paginate("candidates", params)

    def get_candidate_committees(self, candidate_id: str, cycle: int = 2026):
        return self.paginate(f"candidate/{candidate_id}/committees", {"cycle": cycle})

    def get_committees(self, cycle: int = 2026, committee_type: str | None = None,
                       designation: str | None = None, **extra: Any):
        params = {"cycle": cycle, "committee_type": committee_type, "designation": designation,
                  "sort": "name", **extra}
        return self.paginate("committees", params)

    def get_candidate_totals(self, cycle: int = 2026, office: str | None = None, **extra: Any):
        """Per-candidate financial totals for the cycle."""
        params = {"cycle": cycle, "office": office, "election_full": "true",
                  "election_year": cycle, "sort": "-receipts", **extra}
        return self.paginate("candidates/totals", params)

    def get_schedule_a(self, committee_id: str, two_year_transaction_period: int = 2026,
                       min_date: str | None = None, min_amount: float | None = None,
                       max_pages: int | None = None):
        """Itemized receipts (contributions) to a committee."""
        params = {
            "committee_id": committee_id,
            "two_year_transaction_period": two_year_transaction_period,
            "min_date": min_date,
            "min_amount": min_amount,
            "sort": "contribution_receipt_date",
        }
        return self.paginate_keyset("schedules/schedule_a", params, max_pages=max_pages)

    def get_schedule_b(self, committee_id: str, two_year_transaction_period: int = 2026,
                       min_date: str | None = None, max_pages: int | None = None):
        """Itemized disbursements (operating expenditures) by a committee."""
        params = {
            "committee_id": committee_id,
            "two_year_transaction_period": two_year_transaction_period,
            "min_date": min_date,
            "sort": "disbursement_date",
        }
        return self.paginate_keyset("schedules/schedule_b", params, max_pages=max_pages)

    def get_schedule_e(self, cycle: int = 2026, candidate_id: str | None = None,
                       min_date: str | None = None, max_pages: int | None = None):
        """Independent expenditures supporting/opposing federal candidates."""
        params = {
            "cycle": cycle,
            "candidate_id": candidate_id,
            "min_date": min_date,
            "sort": "expenditure_date",
            "is_notice": None,
        }
        return self.paginate_keyset("schedules/schedule_e", params, max_pages=max_pages)

    def get_election_dates(self, election_year: int = 2026, **extra: Any):
        """Federal election calendar, including state primaries and runoffs."""
        params = {"election_year": election_year, "sort": "election_date", **extra}
        return self.paginate("election-dates", params)
