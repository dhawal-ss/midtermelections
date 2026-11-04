"""Election.org public-beta downloads (supplemental source).

Paths come from config/sources.yaml. Responses are verified before loading:
non-empty, not an HTML error page, required columns present (if configured),
and the column fingerprint is compared with the last run to detect format changes.
Election.org warns that candidate filings are not proof of ballot qualification.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from election_data import config
from election_data.clients.csv_source import check_required_columns, looks_like_html, read_table
from election_data.http import HttpClient

log = logging.getLogger(__name__)


@dataclass
class Download:
    name: str
    url: str
    content: bytes
    frame: pd.DataFrame


class ElectionOrgClient:
    def __init__(self, http: HttpClient | None = None, base_url: str | None = None):
        self.cfg = config.source_config("election_org")
        self.base_url = (base_url or config.ELECTION_ORG_BASE_URL).rstrip("/")
        headers = {}
        key = config.secret("ELECTION_ORG_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.http = http or HttpClient(calls_per_hour=600, timeout=120, headers=headers)

    @property
    def datasets(self) -> dict[str, str]:
        return self.cfg.get("datasets", {})

    def dataset_url(self, name: str) -> str:
        return f"{self.base_url}/{self.datasets[name].lstrip('/')}"

    def download(self, name: str) -> Download:
        url = self.dataset_url(name)
        resp = self.http.get(url)
        content = resp.content
        if not content.strip():
            raise ValueError(f"{name}: empty response from {url}")
        if looks_like_html(content):
            raise ValueError(f"{name}: received HTML instead of CSV from {url}")
        frame = read_table(content)
        if frame.empty:
            raise ValueError(f"{name}: CSV has a header but no rows")
        required = (self.cfg.get("required_columns") or {}).get(name, [])
        missing = check_required_columns(frame.columns, required)
        if missing:
            raise ValueError(f"{name}: missing required columns {missing}")
        log.info("Downloaded %s: %d rows, %d columns", name, len(frame), len(frame.columns))
        return Download(name, url, content, frame)
