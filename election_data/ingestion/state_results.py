"""Run a state results adapter and persist observations with provenance."""
from __future__ import annotations

import logging
from typing import Any

import psycopg
import yaml

from election_data import config
from election_data.adapters import get_adapter
from election_data.adapters.base import ADAPTERS
from election_data.ingestion.results import record_results
from election_data.provenance import IngestionRun

log = logging.getLogger(__name__)


def ingest_state_results(conn: psycopg.Connection, state_code: str, file: str | None = None,
                         mapping_path: str | None = None) -> IngestionRun:
    state = next(s for s in config.state_registry() if s["code"] == state_code)
    options: dict[str, Any] = {}
    if file:
        # Manual load of an official export through the generic CSV adapter
        with open(mapping_path or "", encoding="utf-8") as fh:
            options = {"file": file, "mapping": yaml.safe_load(fh)}
        adapter = ADAPTERS["generic_csv"](state_code, options)
    else:
        adapter = get_adapter(state)
    with IngestionRun(conn, f"state:{state_code}", "state_results",
                      {"state": state_code, "adapter": adapter.name, "file": file}) as run:
        for obs, row in adapter.observations():
            raw = run.store_raw("state_result_row", row, None, obs.source_url)
            record_results(conn, run, [obs], raw.id)
    return run
