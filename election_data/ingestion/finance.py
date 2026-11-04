"""Campaign finance ingestion from OpenFEC.

Keeps FEC filing identifiers (file_number, image_number, sub_id) and report
dates on every row so each figure can be traced to the original filing.

Volume: itemized Schedule A for every 2026 committee is tens of millions of
rows. By default contributions are pulled incrementally per principal
campaign committee of 2026 House/Senate candidates, starting from the last
stored date minus an overlap window. Use max_pages to cap a run.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Iterable

import psycopg

from election_data import config
from election_data.clients.fec import FECClient
from election_data.jobs.validation import validate_contribution
from election_data.provenance import IngestionRun

log = logging.getLogger(__name__)

OVERLAP_DAYS = 14  # late-filed / amended items land with older dates


def _candidate_id_for_fec(conn: psycopg.Connection, fec_candidate_id: str | None) -> int | None:
    if not fec_candidate_id:
        return None
    row = conn.execute(
        "SELECT candidate_id FROM candidate_source_ids WHERE id_type = 'fec_candidate_id' AND external_id = %s LIMIT 1",
        (fec_candidate_id,),
    ).fetchone()
    return row["candidate_id"] if row else None


def upsert_committee(conn: psycopg.Connection, record: dict[str, Any], raw_id: int | None) -> int:
    return conn.execute(
        """
        INSERT INTO campaign_committees (external_id, name, committee_type, designation, party, state_code,
                                         treasurer_name, first_file_date, last_file_date, source_record_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (external_id) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, campaign_committees.name),
            committee_type = COALESCE(EXCLUDED.committee_type, campaign_committees.committee_type),
            designation = COALESCE(EXCLUDED.designation, campaign_committees.designation),
            party = COALESCE(EXCLUDED.party, campaign_committees.party),
            state_code = COALESCE(EXCLUDED.state_code, campaign_committees.state_code),
            treasurer_name = COALESCE(EXCLUDED.treasurer_name, campaign_committees.treasurer_name),
            first_file_date = COALESCE(EXCLUDED.first_file_date, campaign_committees.first_file_date),
            last_file_date = COALESCE(EXCLUDED.last_file_date, campaign_committees.last_file_date),
            source_record_id = COALESCE(EXCLUDED.source_record_id, campaign_committees.source_record_id),
            updated_at = NOW()
        RETURNING id
        """,
        (
            record["committee_id"], record.get("name"), record.get("committee_type"), record.get("designation"),
            record.get("party"), (record.get("state") or None) and record["state"][:2], record.get("treasurer_name"),
            record.get("first_file_date"), record.get("last_file_date"), raw_id,
        ),
    ).fetchone()["id"]


def ingest_committees(conn: psycopg.Connection, cycle: int | None = None, client: FECClient | None = None,
                      committee_types: Iterable[str] = ("H", "S")) -> IngestionRun:
    """Candidate committees (House 'H', Senate 'S') and their candidate links."""
    cycle = cycle or config.ELECTION_YEAR
    client = client or FECClient()
    with IngestionRun(conn, "openfec", "fec_committees", {"cycle": cycle, "types": list(committee_types)}) as run:
        for ctype in committee_types:
            for record, url in client.get_committees(cycle=cycle, committee_type=ctype):
                raw = run.store_raw("fec_committee", record, record.get("committee_id"), url)
                try:
                    with conn.transaction():
                        committee_id = upsert_committee(conn, record, raw.id)
                        for fec_cand in record.get("candidate_ids") or []:
                            cand = _candidate_id_for_fec(conn, fec_cand)
                            if cand:
                                conn.execute(
                                    """INSERT INTO candidate_committees (candidate_id, committee_id, cycle, designation)
                                       VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                    (cand, committee_id, cycle, record.get("designation")),
                                )
                except Exception as exc:
                    run.record_failure(f"{record.get('committee_id')}: {exc}")
    return run


def ingest_candidate_totals(conn: psycopg.Connection, cycle: int | None = None,
                            client: FECClient | None = None) -> IngestionRun:
    cycle = cycle or config.ELECTION_YEAR
    client = client or FECClient()
    with IngestionRun(conn, "openfec", "fec_finance_totals", {"cycle": cycle}) as run:
        for office in ("H", "S"):
            for record, url in client.get_candidate_totals(cycle=cycle, office=office):
                raw = run.store_raw("fec_candidate_totals", record, record.get("candidate_id"), url)
                cand = _candidate_id_for_fec(conn, record.get("candidate_id"))
                if cand is None:
                    run.record_failure(f"totals for unknown candidate {record.get('candidate_id')}")
                    continue
                try:
                    with conn.transaction():
                        conn.execute(
                            """
                            INSERT INTO financial_summaries (candidate_id, cycle, coverage_start_date,
                                coverage_end_date, receipts, disbursements, cash_on_hand_end, debts_owed,
                                individual_contributions, pac_contributions, candidate_contributions,
                                source_record_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (candidate_id, cycle, coverage_end_date) DO UPDATE SET
                                receipts = EXCLUDED.receipts, disbursements = EXCLUDED.disbursements,
                                cash_on_hand_end = EXCLUDED.cash_on_hand_end, debts_owed = EXCLUDED.debts_owed,
                                individual_contributions = EXCLUDED.individual_contributions,
                                pac_contributions = EXCLUDED.pac_contributions,
                                candidate_contributions = EXCLUDED.candidate_contributions,
                                source_record_id = EXCLUDED.source_record_id, retrieved_at = NOW()
                            """,
                            (
                                cand, cycle, record.get("coverage_start_date"), record.get("coverage_end_date"),
                                record.get("receipts"), record.get("disbursements"),
                                record.get("cash_on_hand_end_period", record.get("last_cash_on_hand_end_period")),
                                record.get("debts_owed_by_committee", record.get("last_debts_owed_by_committee")),
                                record.get("individual_contributions", record.get("individual_itemized_contributions")),
                                record.get("other_political_committee_contributions"),
                                record.get("candidate_contribution"),
                                raw.id,
                            ),
                        )
                except Exception as exc:
                    run.record_failure(f"{record.get('candidate_id')}: {exc}")
    return run


def _principal_committees(conn: psycopg.Connection, cycle: int, limit: int | None) -> list[dict[str, Any]]:
    sql = """
        SELECT cm.id, cm.external_id, cc.candidate_id
        FROM campaign_committees cm
        JOIN candidate_committees cc ON cc.committee_id = cm.id AND cc.cycle = %s
        WHERE cm.designation = 'P'
        ORDER BY cm.external_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (cycle,)).fetchall()


def _watermark(conn: psycopg.Connection, table: str, date_col: str, committee_id: int,
               extra: str = "") -> str | None:
    row = conn.execute(
        f"SELECT MAX({date_col}) AS d FROM {table} WHERE committee_id = %s {extra}", (committee_id,)
    ).fetchone()
    if row and row["d"]:
        return (row["d"] - timedelta(days=OVERLAP_DAYS)).isoformat()
    return None


def ingest_contributions(conn: psycopg.Connection, cycle: int | None = None, client: FECClient | None = None,
                         committee_limit: int | None = None, max_pages: int | None = None,
                         min_amount: float | None = None) -> IngestionRun:
    cycle = cycle or config.ELECTION_YEAR
    client = client or FECClient()
    params = {"cycle": cycle, "committee_limit": committee_limit, "max_pages": max_pages, "min_amount": min_amount}
    with IngestionRun(conn, "openfec", "fec_contributions", params) as run:
        for committee in _principal_committees(conn, cycle, committee_limit):
            since = _watermark(conn, "contributions", "contribution_date", committee["id"])
            for record, url in client.get_schedule_a(committee["external_id"], cycle, min_date=since,
                                                     min_amount=min_amount, max_pages=max_pages):
                problems = validate_contribution(record)
                raw = run.store_raw("fec_schedule_a", record, str(record.get("sub_id")), url,
                                    record.get("load_date"))
                if problems:
                    run.record_failure(f"sub_id {record.get('sub_id')}: {problems}")
                    continue
                try:
                    with conn.transaction():
                        conn.execute(
                            """
                            INSERT INTO contributions (fec_sub_id, committee_id, candidate_id, amount,
                                contribution_date, contributor_name, contributor_type, contributor_city,
                                contributor_state, contributor_employer, contributor_occupation, receipt_type,
                                memo_code, file_number, image_number, report_type, report_year, source_record_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (fec_sub_id) DO UPDATE SET
                                amount = EXCLUDED.amount, contribution_date = EXCLUDED.contribution_date,
                                memo_code = EXCLUDED.memo_code, file_number = EXCLUDED.file_number,
                                image_number = EXCLUDED.image_number, source_record_id = EXCLUDED.source_record_id
                            """,
                            (
                                str(record["sub_id"]), committee["id"], committee["candidate_id"],
                                record.get("contribution_receipt_amount"),
                                (record.get("contribution_receipt_date") or "")[:10] or None,
                                record.get("contributor_name"), record.get("entity_type"),
                                record.get("contributor_city"), (record.get("contributor_state") or "")[:2] or None,
                                record.get("contributor_employer"), record.get("contributor_occupation"),
                                record.get("receipt_type"), record.get("memo_code"),
                                str(record.get("file_number") or "") or None, record.get("image_number"),
                                record.get("report_type"), record.get("report_year"), raw.id,
                            ),
                        )
                except Exception as exc:
                    run.record_failure(f"sub_id {record.get('sub_id')}: {exc}")
            conn.commit()  # checkpoint per committee so progress survives interruption
    return run


def ingest_operating_expenditures(conn: psycopg.Connection, cycle: int | None = None,
                                  client: FECClient | None = None, committee_limit: int | None = None,
                                  max_pages: int | None = None) -> IngestionRun:
    cycle = cycle or config.ELECTION_YEAR
    client = client or FECClient()
    with IngestionRun(conn, "openfec", "fec_expenditures",
                      {"cycle": cycle, "kind": "operating", "max_pages": max_pages}) as run:
        for committee in _principal_committees(conn, cycle, committee_limit):
            since = _watermark(conn, "expenditures", "expenditure_date", committee["id"],
                               "AND expenditure_kind = 'operating'")
            for record, url in client.get_schedule_b(committee["external_id"], cycle, min_date=since,
                                                     max_pages=max_pages):
                raw = run.store_raw("fec_schedule_b", record, str(record.get("sub_id")), url)
                if not record.get("sub_id"):
                    run.record_failure("schedule_b row without sub_id")
                    continue
                try:
                    with conn.transaction():
                        _upsert_expenditure(conn, record, "operating", committee["id"], committee["candidate_id"],
                                            None, record.get("disbursement_amount"),
                                            record.get("disbursement_date"), record.get("recipient_name"),
                                            record.get("disbursement_description"), raw.id)
                except Exception as exc:
                    run.record_failure(f"sub_id {record.get('sub_id')}: {exc}")
            conn.commit()
    return run


def ingest_independent_expenditures(conn: psycopg.Connection, cycle: int | None = None,
                                    client: FECClient | None = None, max_pages: int | None = None,
                                    min_date: str | None = None) -> IngestionRun:
    cycle = cycle or config.ELECTION_YEAR
    client = client or FECClient()
    with IngestionRun(conn, "openfec", "fec_independent_expenditures",
                      {"cycle": cycle, "max_pages": max_pages, "min_date": min_date}) as run:
        if min_date is None:
            row = conn.execute(
                "SELECT MAX(expenditure_date) AS d FROM expenditures WHERE expenditure_kind = 'independent'"
            ).fetchone()
            if row["d"]:
                min_date = (row["d"] - timedelta(days=OVERLAP_DAYS)).isoformat()
        for record, url in client.get_schedule_e(cycle=cycle, min_date=min_date, max_pages=max_pages):
            raw = run.store_raw("fec_schedule_e", record, str(record.get("sub_id")), url)
            if not record.get("sub_id") or not record.get("committee_id"):
                run.record_failure("schedule_e row without sub_id/committee_id")
                continue
            try:
                with conn.transaction():
                    committee = record.get("committee") or {}
                    committee_id = upsert_committee(
                        conn, {"committee_id": record["committee_id"], "name": committee.get("name"),
                               "committee_type": committee.get("committee_type")}, None)
                    _upsert_expenditure(
                        conn, record, "independent", committee_id, _candidate_id_for_fec(conn, record.get("candidate_id")),
                        record.get("support_oppose_indicator"), record.get("expenditure_amount"),
                        record.get("expenditure_date"), record.get("payee_name"),
                        record.get("expenditure_description"), raw.id)
            except Exception as exc:
                run.record_failure(f"sub_id {record.get('sub_id')}: {exc}")
    return run


def _upsert_expenditure(conn: psycopg.Connection, record: dict[str, Any], kind: str, committee_id: int,
                        candidate_id: int | None, support_oppose: str | None, amount: Any, when: Any,
                        payee: str | None, purpose: str | None, raw_id: int) -> None:
    so = (support_oppose or "").upper()[:1] or None
    conn.execute(
        """
        INSERT INTO expenditures (fec_sub_id, expenditure_kind, committee_id, candidate_id, support_oppose,
                                  amount, expenditure_date, payee, purpose, file_number, image_number,
                                  source_record_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fec_sub_id) DO UPDATE SET amount = EXCLUDED.amount,
            expenditure_date = EXCLUDED.expenditure_date, payee = EXCLUDED.payee, purpose = EXCLUDED.purpose,
            candidate_id = COALESCE(EXCLUDED.candidate_id, expenditures.candidate_id),
            source_record_id = EXCLUDED.source_record_id
        """,
        (
            str(record["sub_id"]), kind, committee_id, candidate_id, so if so in ("S", "O") else None,
            amount, (str(when or ""))[:10] or None, payee, purpose,
            str(record.get("file_number") or "") or None, record.get("image_number"), raw_id,
        ),
    )
