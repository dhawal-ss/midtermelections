"""Candidate ingestion: OpenFEC (primary for federal filings) and Election.org (supplement).

Identity rules
  * Candidates are matched on official IDs (FEC candidate_id), never on name alone.
  * A name-only match goes to candidate_match_queue for human review.
  * FEC registration sets filing_status = 'filed_with_fec'. ballot_status stays
    'unknown' until a state election authority provides evidence.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from election_data import config, reference
from election_data.clients.election_org import ElectionOrgClient
from election_data.clients.fec import FECClient
from election_data.clients.csv_source import iter_records
from election_data.ingestion.reference_data import (
    ensure_contest, ensure_election, house_office, senate_office,
)
from election_data.jobs.validation import validate_fec_candidate
from election_data.provenance import (
    IngestionRun, ensure_source, record_discrepancy, record_schema_fingerprint, record_verification,
)

log = logging.getLogger(__name__)

_HONORIFICS = {"MR", "MRS", "MS", "DR", "JR", "SR", "II", "III", "IV", "HON", "MISS", "REV"}
FEC_ID_COLUMNS = ("fec_candidate_id", "fec_id", "candidate_fec_id", "fec_cand_id")


def parse_fec_name(raw: str) -> tuple[str, str | None, str | None]:
    """'SMITH, JOHN A. MR.' -> ('John A. Smith', 'John', 'Smith')."""
    raw = (raw or "").strip()
    if "," not in raw:
        return raw.title(), None, None
    last, rest = [p.strip() for p in raw.split(",", 1)]
    tokens = [t for t in re.split(r"\s+", rest) if t.strip(".").upper() not in _HONORIFICS]
    first = tokens[0].title() if tokens else None
    canonical = " ".join([t.title() for t in tokens] + [last.title()]).strip()
    return canonical, first, last.title()


def _candidate_by_external_id(conn: psycopg.Connection, id_type: str, external_id: str) -> int | None:
    row = conn.execute(
        "SELECT candidate_id FROM candidate_source_ids WHERE id_type = %s AND external_id = %s LIMIT 1",
        (id_type, external_id),
    ).fetchone()
    return row["candidate_id"] if row else None


def upsert_fec_candidate(conn: psycopg.Connection, run: IngestionRun, record: dict[str, Any],
                         raw_id: int, general_election_id: int) -> int | None:
    fec_id = record["candidate_id"]
    canonical, first, last = parse_fec_name(record.get("name", ""))
    party = record.get("party") or "UNK"
    incumbent = record.get("incumbent_challenge") == "I"
    conn.execute("INSERT INTO parties (party_code, party_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (party, record.get("party_full") or party))

    candidate_id = _candidate_by_external_id(conn, "fec_candidate_id", fec_id)
    if candidate_id is None:
        candidate_id = conn.execute(
            """INSERT INTO candidates (canonical_name, first_name, last_name, party, incumbent, verification_status)
               VALUES (%s, %s, %s, %s, %s, 'official') RETURNING id""",
            (canonical, first, last, party, incumbent),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO candidate_source_ids (candidate_id, source_id, id_type, external_id)
               VALUES (%s, %s, 'fec_candidate_id', %s) ON CONFLICT DO NOTHING""",
            (candidate_id, run.source_id, fec_id),
        )
    else:
        conn.execute(
            """UPDATE candidates SET canonical_name = %s, first_name = %s, last_name = %s, party = %s,
                      incumbent = %s, updated_at = NOW() WHERE id = %s""",
            (canonical, first, last, party, incumbent, candidate_id),
        )

    conn.execute(
        """
        INSERT INTO candidate_filings (candidate_id, filing_authority, filing_type, filing_id, filed_on,
                                       status, source_record_id)
        VALUES (%s, 'FEC', 'statement_of_candidacy', %s, %s, %s, %s)
        ON CONFLICT (candidate_id, filing_authority, filing_type, filing_id)
            DO UPDATE SET status = EXCLUDED.status, source_record_id = EXCLUDED.source_record_id
        """,
        (candidate_id, fec_id, record.get("first_file_date"), record.get("candidate_status"), raw_id),
    )

    # Link to the 2026 general-election contest for the seat
    office, state = record.get("office"), record.get("state")
    if state not in reference.STATES or office not in ("H", "S"):
        return candidate_id  # presidential or territory: identity kept, no 2026 contest
    year = config.ELECTION_YEAR
    if office == "H":
        district = (record.get("district") or "00").zfill(2)
        if district not in reference.house_district_numbers(state):
            record_discrepancy(conn, "candidates", candidate_id, "district", "district_not_in_apportionment",
                               {"openfec": f"{state}-{district}",
                                "project_reference": reference.house_district_numbers(state)})
        office_id = house_office(conn, state, district)
        key = reference.contest_key(year, "general", reference.house_office_code(state, district))
        stage, special = "general", False
    else:
        special = state in reference.SENATE_2026_SPECIAL
        if reference.senate_seat_class(state) is None:
            record_discrepancy(conn, "candidates", candidate_id, "state", "senate_seat_not_up_in_2026",
                               {"openfec": state, "project_reference": "no 2026 Senate contest"})
        office_id = senate_office(conn, state)
        stage = "special_general" if special else "general"
        key = reference.contest_key(year, stage, reference.senate_office_code(state))
    contest_id = ensure_contest(conn, key, general_election_id, office_id, state, stage=stage,
                                is_special=special, source_id=run.source_id)

    filing_status = "filed_with_fec" if record.get("candidate_status") in ("C", "F", "N", "P") else "unknown"
    conn.execute(
        """
        INSERT INTO contest_candidates (contest_id, candidate_id, filing_status, party_label,
                                        incumbent_challenge, verification_status)
        VALUES (%s, %s, %s, %s, %s, 'official')
        ON CONFLICT (contest_id, candidate_id) DO UPDATE SET
            filing_status = CASE WHEN contest_candidates.filing_status IN ('withdrawn', 'terminated')
                                 THEN contest_candidates.filing_status ELSE EXCLUDED.filing_status END,
            party_label = EXCLUDED.party_label,
            incumbent_challenge = EXCLUDED.incumbent_challenge,
            updated_at = NOW()
        """,
        (contest_id, candidate_id, filing_status, party, record.get("incumbent_challenge")),
    )
    return candidate_id


def ingest_fec_candidates(conn: psycopg.Connection, cycle: int | None = None,
                          offices: tuple[str, ...] = ("H", "S"), states: list[str] | None = None,
                          client: FECClient | None = None) -> IngestionRun:
    cycle = cycle or config.ELECTION_YEAR
    client = client or FECClient()
    with IngestionRun(conn, "openfec", "fec_candidates",
                      {"cycle": cycle, "offices": list(offices), "states": states}) as run:
        reference_source = ensure_source(conn, "project_reference")
        general_id = ensure_election(conn, "2026 United States General Election",
                                     config.GENERAL_ELECTION_DATE, "general", None, reference_source)
        for office in offices:
            for state in (states or [None]):
                # election_year filters out candidates whose race is 2028/2030 but who
                # raised money during the 2026 cycle.
                for record, url in client.get_candidates(cycle=cycle, office=office, state=state,
                                                         election_year=cycle):
                    problems = validate_fec_candidate(record)
                    raw = run.store_raw("fec_candidate", record, record.get("candidate_id"), url,
                                        record.get("load_date"))
                    if problems:
                        run.record_failure(f"{record.get('candidate_id')}: {problems}")
                        continue
                    try:
                        with conn.transaction():
                            upsert_fec_candidate(conn, run, record, raw.id, general_id)
                    except Exception as exc:  # keep going; one bad record must not stop the run
                        run.record_failure(f"{record.get('candidate_id')}: {exc}")
    return run


def ingest_election_org(conn: psycopg.Connection, client: ElectionOrgClient | None = None,
                        datasets: list[str] | None = None) -> IngestionRun:
    """Store Election.org datasets as raw evidence and cross-reference candidates by FEC ID."""
    client = client or ElectionOrgClient()
    names = datasets or list(client.datasets)
    with IngestionRun(conn, "election_org", "election_org", {"datasets": names}) as run:
        for name in names:
            try:
                download = client.download(name)
            except Exception as exc:
                run.record_failure(f"{name}: {exc}")
                continue
            run.archive_file(download.url, download.content, f"{name}.csv", "text/csv")
            change = record_schema_fingerprint(conn, run.source_id, name, download.frame.columns)
            if change["changed"]:
                run.errors.append(f"{name}: column set changed {change}")
            fec_col = next((c for c in FEC_ID_COLUMNS if c in download.frame.columns), None)
            for i, row in enumerate(iter_records(download.frame)):
                raw = run.store_raw(f"election_org:{name}", row, row.get(fec_col) if fec_col else str(i),
                                    download.url)
                if "candidates" in name:
                    with conn.transaction():
                        _crossref_election_org_candidate(conn, run, row, fec_col, raw.id)
            conn.commit()
    return run


def _crossref_election_org_candidate(conn: psycopg.Connection, run: IngestionRun, row: dict[str, Any],
                                     fec_col: str | None, raw_id: int) -> None:
    fec_id = (row.get(fec_col) or "").strip() if fec_col else ""
    candidate_id = _candidate_by_external_id(conn, "fec_candidate_id", fec_id) if fec_id else None
    if candidate_id is None:
        # No official-ID match: queue for review, never auto-merge on name.
        conn.execute(
            """INSERT INTO candidate_match_queue (source_record_id, match_method, match_score)
               VALUES (%s, %s, NULL)""",
            (raw_id, "no_fec_id" if not fec_id else "fec_id_not_found"),
        )
        return
    conn.execute(
        """INSERT INTO candidate_source_ids (candidate_id, source_id, id_type, external_id)
           VALUES (%s, %s, 'fec_candidate_id', %s) ON CONFLICT DO NOTHING""",
        (candidate_id, run.source_id, fec_id),
    )
    party = (row.get("party") or row.get("party_code") or "").strip().upper()
    stored = conn.execute("SELECT party FROM candidates WHERE id = %s", (candidate_id,)).fetchone()["party"]
    if party and stored and party[:3] != stored[:3]:
        record_discrepancy(conn, "candidates", candidate_id, "party", "party_mismatch",
                           {"openfec": stored, "election_org": party}, severity="info")
    else:
        # 'official' (FEC is the filing authority) outranks 'corroborated'; only upgrade unverified.
        conn.execute(
            "UPDATE candidates SET verification_status = 'corroborated' WHERE id = %s "
            "AND verification_status = 'unverified'",
            (candidate_id,),
        )
        record_verification(conn, "candidate_identity_crosscheck", "passed",
                            {"matched_on": "fec_candidate_id", "sources": ["openfec", "election_org"]},
                            source_record_id=raw_id, entity_table="candidates", entity_id=candidate_id)
