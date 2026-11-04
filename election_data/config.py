"""Runtime configuration.

Values come from environment variables (optionally a .env file) and from
config/sources.yaml. Nothing here raises at import time, so tests and tools
can import the package without a database configured.
"""
from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SOURCES_YAML = Path(os.getenv("SOURCES_YAML", PROJECT_ROOT / "config" / "sources.yaml"))
SCHEMA_SQL = PROJECT_ROOT / "sql" / "schema.sql"
MIGRATIONS_DIR = PROJECT_ROOT / "sql" / "migrations"
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
EXPORTS_DIR = DATA_DIR / "exports"

ELECTION_YEAR = int(os.getenv("ELECTION_YEAR", "2026"))
GENERAL_ELECTION_DATE = date(2026, 11, 3)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
USER_AGENT = os.getenv("HTTP_USER_AGENT", "USMidtermElectionResearch/0.1 (+research database)")

FEC_BASE_URL = os.getenv("FEC_BASE_URL", "https://api.open.fec.gov/v1")
CENSUS_BASE_URL = os.getenv("CENSUS_BASE_URL", "https://api.census.gov/data")
ELECTION_ORG_BASE_URL = os.getenv("ELECTION_ORG_BASE_URL", "https://election.org/api/v1")
CIVICAPI_BASE_URL = os.getenv("CIVICAPI_BASE_URL", "https://civicapi.org/api/v2")
GOOGLE_CIVIC_BASE_URL = os.getenv("GOOGLE_CIVIC_BASE_URL", "https://www.googleapis.com/civicinfo/v2")
DATAVERSE_BASE_URL = os.getenv("DATAVERSE_BASE_URL", "https://dataverse.harvard.edu")


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return url


def secret(name: str) -> str | None:
    """Return an API key from the environment, treating blanks as missing."""
    value = os.getenv(name, "").strip()
    return value or None


@lru_cache(maxsize=1)
def load_sources() -> dict[str, Any]:
    with open(SOURCES_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def source_config(key: str) -> dict[str, Any]:
    try:
        return load_sources()["sources"][key]
    except KeyError as exc:
        raise KeyError(f"Unknown source '{key}' in {SOURCES_YAML}") from exc


def state_registry() -> list[dict[str, Any]]:
    return load_sources()["states"]
