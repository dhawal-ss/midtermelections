"""Historical returns (MIT Election Lab via Harvard Dataverse) for comparison baselines.

District numbers are kept exactly as published for that year: a 2018 district
is not assumed to match the 2026 district with the same number.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from election_data import config, reference
from election_data.clients.csv_source import iter_records, read_table
from election_data.clients.mit_election_lab import MITElectionLabClient
from election_data.provenance import IngestionRun, content_hash, record_schema_fingerprint

log = logging.getLogger(__name__)

REQUIRED = {"year", "state_po", "office", "candidate", "candidatevotes", "totalvotes"}


def _truthy(value: Any) -> bool:
    return str(value).strip().upper() in ("TRUE", "T", "1", "YES")


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_mit_frame(conn: psycopg.Connection, run: IngestionRun, frame, office_default: str,
                   dataset_version: str, years: set[int]) -> int:
    missing = REQUIRED - set(frame.columns)
    if missing:
        raise ValueError(f"MIT dataset missing columns {sorted(missing)}")
    inserted = 0
    for row in iter_records(frame):
        year = _int(row.get("year"))
        state = (row.get("state_po") or "").upper()
        if year not in years or state not in reference.STATES:
            continue
        district = row.get("district")
        if district is not None and str(district).isdigit():
            district = str(int(district)).zfill(2)
        values = (
            year, state, (row.get("office") or office_default).upper(), district, row.get("stage"),
            _truthy(row.get("special")), _truthy(row.get("runoff")), row.get("candidate"),
            row.get("party") or row.get("party_detailed") or row.get("party_simplified"),
            _truthy(row.get("writein")), row.get("mode"), _int(row.get("candidatevotes")),
            _int(row.get("totalvotes")), _truthy(row.get("unofficial")), dataset_version,
        )
        row_hash = content_hash(list(values[:13]))
        result = conn.execute(
            """
            INSERT INTO historical_results (year, state_code, office, district, stage, is_special, is_runoff,
                candidate, party, is_writein, mode, candidate_votes, total_votes, is_unofficial, dataset_version,
                source_id, row_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (row_hash) DO NOTHING
            """,
            (*values, run.source_id, row_hash),
        )
        inserted += result.rowcount
    run.received += len(frame)
    run.inserted += inserted
    run.unchanged += len(frame) - inserted
    return inserted


def ingest_mit_election_lab(conn: psycopg.Connection, client: MITElectionLabClient | None = None,
                            datasets: list[str] | None = None, years: list[int] | None = None) -> IngestionRun:
    cfg = config.source_config("mit_election_lab")["datasets"]
    names = datasets or list(cfg)
    year_set = set(years or config.load_sources()["election"]["historical_cycles"])
    client = client or MITElectionLabClient()
    with IngestionRun(conn, "mit_election_lab", "historical_results",
                      {"datasets": names, "years": sorted(year_set)}) as run:
        for name in names:
            spec = cfg[name]
            try:
                data_file = client.find_data_file(spec["doi"], spec["file_pattern"])
                content = client.download(data_file)
                url = client.file_url(data_file.file_id)
                fname = data_file.original_filename or data_file.filename
                run.archive_file(url, content, fname)
                frame = read_table(content, sep="\t" if fname.endswith(".tab") else ",")
                record_schema_fingerprint(conn, run.source_id, name, frame.columns)
                n = load_mit_frame(conn, run, frame, spec["office"], data_file.dataset_version, year_set)
                conn.commit()
                log.info("%s: %d new historical rows", name, n)
            except Exception as exc:
                conn.rollback()
                run.record_failure(f"{name}: {exc}")
    return run
