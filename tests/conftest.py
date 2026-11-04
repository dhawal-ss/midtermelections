"""Shared fixtures.

Integration tests need TEST_DATABASE_URL pointing at a disposable database;
its public schema is dropped and recreated. They are skipped when unset.
"""
from __future__ import annotations

import os

import pytest

from election_data import database
from election_data.ingestion.reference_data import seed_all

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _no_real_api_keys(monkeypatch):
    monkeypatch.setenv("FEC_API_KEY", "TEST_KEY")


@pytest.fixture(scope="session")
def db_url():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping database integration tests")
    return TEST_DATABASE_URL


@pytest.fixture()
def conn(db_url, tmp_path, monkeypatch):
    monkeypatch.setattr("election_data.config.RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr("election_data.config.EXPORTS_DIR", tmp_path / "exports")
    admin = database.connect(db_url, autocommit=True)
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    admin.execute("DROP DOMAIN IF EXISTS verification_status CASCADE")
    admin.close()
    c = database.connect(db_url)
    database.apply_schema(c)
    seed_all(c)
    yield c
    c.rollback()
    c.close()
