"""Census ACS demographics for states and congressional districts."""
from __future__ import annotations

import logging

import psycopg

from election_data import config, reference
from election_data.clients.census import CensusClient
from election_data.ingestion.reference_data import ensure_district
from election_data.provenance import IngestionRun

log = logging.getLogger(__name__)


def ingest_census_demographics(conn: psycopg.Connection, client: CensusClient | None = None,
                               dataset: str | None = None) -> IngestionRun:
    cfg = config.source_config("census")
    dataset = dataset or cfg["acs_dataset"]
    variables: dict[str, str] = cfg["variables"]
    client = client or CensusClient()
    with IngestionRun(conn, "census", "census_demographics", {"dataset": dataset}) as run:
        for level, fetch in (("state", client.states), ("congressional_district", client.congressional_districts)):
            rows, url = fetch(dataset, list(variables))
            for row in rows:
                state = reference.FIPS_TO_STATE.get(row.get("state"))
                if state is None:
                    continue  # Puerto Rico and other territories
                if level == "state":
                    geo_id, district_id = row["state"], None
                else:
                    cd = row.get("congressional district")
                    if cd in ("ZZ", None):
                        continue
                    # ACS uses '00' for at-large and '98' for DC's delegate district
                    number = "00" if cd in ("00", "98") else cd
                    geo_id = f"{row['state']}{number}"
                    district_id = ensure_district(conn, state, "congressional", number)
                raw = run.store_raw(f"census:{level}", row, geo_id, url)
                for var, label in variables.items():
                    value = row.get(var)
                    if value in (None, "", "null") or str(value).startswith("-666"):
                        continue  # Census sentinel for unavailable estimates
                    conn.execute(
                        """
                        INSERT INTO district_demographics (state_code, district_id, geography_level, geo_id,
                                                           dataset, variable, variable_label, value, source_record_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (geo_id, dataset, variable) DO UPDATE SET value = EXCLUDED.value,
                            source_record_id = EXCLUDED.source_record_id
                        """,
                        (state, district_id, level, geo_id, dataset, var, label, value, raw.id),
                    )
    return run
