"""MIT Election Data and Science Lab returns via the Harvard Dataverse API.

File IDs change with each dataset version, so files are discovered through the
dataset metadata endpoint rather than hard-coded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from election_data import config
from election_data.http import HttpClient

log = logging.getLogger(__name__)


@dataclass
class DataverseFile:
    file_id: int
    filename: str
    original_filename: str | None
    content_type: str | None
    dataset_version: str


class MITElectionLabClient:
    def __init__(self, http: HttpClient | None = None, base_url: str | None = None):
        self.base_url = (base_url or config.DATAVERSE_BASE_URL).rstrip("/")
        self.http = http or HttpClient(calls_per_hour=600, timeout=300)

    def dataset_files(self, doi: str) -> list[DataverseFile]:
        url = f"{self.base_url}/api/datasets/:persistentId/"
        data = self.http.get_json(url, params={"persistentId": doi})
        latest = data["data"]["latestVersion"]
        version = f"{latest.get('versionNumber')}.{latest.get('versionMinorNumber')}"
        files = []
        for item in latest.get("files", []):
            df: dict[str, Any] = item.get("dataFile", {})
            files.append(DataverseFile(
                file_id=df["id"],
                filename=df.get("filename", ""),
                original_filename=df.get("originalFileName"),
                content_type=df.get("originalFileFormat") or df.get("contentType"),
                dataset_version=version,
            ))
        return files

    def find_data_file(self, doi: str, pattern: str) -> DataverseFile:
        candidates = [
            f for f in self.dataset_files(doi)
            if pattern.lower() in (f.original_filename or f.filename).lower()
            and (f.original_filename or f.filename).lower().endswith((".csv", ".tab"))
        ]
        if not candidates:
            raise FileNotFoundError(f"No data file matching '{pattern}' in {doi}")
        return candidates[0]

    def file_url(self, file_id: int) -> str:
        return f"{self.base_url}/api/access/datafile/{file_id}?format=original"

    def download(self, file: DataverseFile) -> bytes:
        return self.http.get(self.file_url(file.file_id)).content
