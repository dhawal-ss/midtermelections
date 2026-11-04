"""Election calendar (2026 primaries, runoffs, specials, general) from OpenFEC election-dates."""
from __future__ import annotations

import logging
from datetime import date

import psycopg

from election_data import config, reference
from election_data.clients.fec import FECClient
from election_data.ingestion.reference_data import ensure_election
from election_data.provenance import IngestionRun

log = logging.getLogger(__name__)

FEC_ELECTION_TYPES = {
    "G": "general", "P": "primary", "R": "runoff", "PR": "runoff", "GR": "runoff",
    "SG": "special_general", "SP": "special_primary", "SR": "special_runoff", "SGR": "special_runoff",
    "SPR": "special_runoff", "C": "convention", "SC": "convention",
}


def ingest_election_dates(conn: psycopg.Connection, year: int | None = None,
                          client: FECClient | None = None) -> IngestionRun:
    year = year or config.ELECTION_YEAR
    client = client or FECClient()
    with IngestionRun(conn, "openfec", "fec_election_dates", {"election_year": year}) as run:
        for record, url in client.get_election_dates(election_year=year):
            run.store_raw("fec_election_date", record, None, url)
            state = record.get("election_state")
            if state not in reference.STATES or not record.get("election_date"):
                continue
            etype = FEC_ELECTION_TYPES.get((record.get("election_type_id") or "").upper(), "other")
            name = f"{year} {reference.STATES[state][0]} {record.get('election_type_full') or etype}"
            try:
                with conn.transaction():
                    ensure_election(conn, name, date.fromisoformat(record["election_date"][:10]), etype, state,
                                    run.source_id, verification_status="official")
            except Exception as exc:
                run.record_failure(f"{state} {record.get('election_date')}: {exc}")
    return run
