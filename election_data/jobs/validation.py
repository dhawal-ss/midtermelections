"""Record validators and database-wide verification checks.

Pure validators (validate_*) return a list of problems and are used during
ingestion. run_verification() runs cross-source checks against the database
and writes source_verifications / data_discrepancies.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from election_data import config, reference
from election_data.provenance import record_discrepancy, record_verification

log = logging.getLogger(__name__)

RESULT_STAGES = {"preliminary", "unofficial_complete", "canvassed", "certified", "amended_certified"}
POLL_POPULATIONS = {"lv", "rv", "a", "v"}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}")


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


# ---------------------------------------------------------------------------
# Record validators
# ---------------------------------------------------------------------------
def validate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate one result row before it is written.

    Candidate percentages are NOT required to sum to 100: write-ins,
    undervotes/overvotes, multi-seat contests and reporting conventions
    legitimately break that assumption.
    """
    errors: list[str] = []
    votes = result.get("votes")
    if votes is not None:
        try:
            if int(votes) < 0:
                errors.append("Negative vote count")
        except (TypeError, ValueError):
            errors.append(f"Non-integer vote count: {votes!r}")

    try:
        pct = _decimal(result.get("vote_percentage"))
        if pct is not None and (pct < 0 or pct > 100):
            errors.append("Invalid vote percentage")
    except ValueError as exc:
        errors.append(f"Invalid vote percentage: {exc}")

    reporting, total = result.get("precincts_reporting"), result.get("precincts_total")
    if reporting is not None and total is not None and int(reporting) > int(total):
        errors.append("Precincts reporting exceeds precincts total")

    if not result.get("source_url"):
        errors.append("Missing source URL")

    stage = result.get("result_stage")
    if not stage:
        errors.append("Missing result stage")
    elif stage not in RESULT_STAGES:
        errors.append(f"Unknown result stage: {stage}")

    certified = bool(result.get("is_certified")) or stage in ("certified", "amended_certified")
    if certified and not result.get("certification_evidence"):
        errors.append("Missing certification evidence")

    return {"valid": not errors, "errors": errors}


def validate_fec_candidate(record: dict[str, Any]) -> list[str]:
    problems = []
    cid = record.get("candidate_id") or ""
    if not cid or cid[0] not in "HSP" or len(cid) != 9:
        problems.append(f"malformed candidate_id {cid!r}")
    if not record.get("name"):
        problems.append("missing name")
    if record.get("office") not in ("H", "S", "P"):
        problems.append(f"unknown office {record.get('office')!r}")
    if record.get("office") in ("H", "S") and not record.get("state"):
        problems.append("missing state")
    return problems


def validate_contribution(record: dict[str, Any]) -> list[str]:
    problems = []
    if not record.get("sub_id"):
        problems.append("missing sub_id")
    if record.get("contribution_receipt_amount") is None:
        problems.append("missing amount")
    if not record.get("committee_id"):
        problems.append("missing committee_id")
    if not (record.get("file_number") or record.get("image_number")):
        problems.append("missing FEC filing identifier (file_number/image_number)")
    return problems


def validate_poll(poll: dict[str, Any]) -> list[str]:
    """Polls must keep pollster, population, sample size, method and field dates."""
    problems = []
    for field in ("pollster", "field_start", "field_end", "sample_size", "population"):
        if poll.get(field) in (None, ""):
            problems.append(f"missing {field}")
    pop = (poll.get("population") or "").lower()
    if pop and pop not in POLL_POPULATIONS:
        problems.append(f"unknown population {pop!r}")
    try:
        start, end = _parse_date(poll.get("field_start")), _parse_date(poll.get("field_end"))
        if start and end and end < start:
            problems.append("field_end before field_start")
        if end and end > date.today() + timedelta(days=1):
            problems.append("field_end in the future")
    except ValueError as exc:
        problems.append(f"bad field date: {exc}")
    size = poll.get("sample_size")
    if size not in (None, "") and int(size) <= 0:
        problems.append("non-positive sample size")
    if not poll.get("results"):
        problems.append("missing results")
    return problems


# ---------------------------------------------------------------------------
# Database-wide checks
# ---------------------------------------------------------------------------
def check_reference_coverage(conn: psycopg.Connection) -> dict[str, Any]:
    """Compare seeded 2026 seat map with what FEC filings show."""
    rows = conn.execute(
        """
        SELECT c.state_code, o.office_code, COUNT(cc.candidate_id) AS fec_candidates
        FROM contests c JOIN offices o ON o.id = c.office_id
        JOIN elections e ON e.id = c.election_id
        LEFT JOIN contest_candidates cc ON cc.contest_id = c.id AND cc.filing_status = 'filed_with_fec'
        WHERE e.election_date = %s AND o.office_code LIKE 'US-%%'
        GROUP BY c.state_code, o.office_code
        """,
        (config.GENERAL_ELECTION_DATE,),
    ).fetchall()
    empty = [r["office_code"] for r in rows if r["fec_candidates"] == 0]
    house = sum(1 for r in rows if r["office_code"].startswith("US-HOUSE") and r["state_code"] != "DC")
    senate = sum(1 for r in rows if r["office_code"].startswith("US-SENATE"))
    expected_senate = len(reference.SENATE_2026_REGULAR) + len(reference.SENATE_2026_SPECIAL)
    result = {
        "house_contests": house, "house_expected": 435,
        "senate_contests": senate, "senate_expected": expected_senate,
        "federal_contests_without_fec_candidates": len(empty),
        "examples_without_candidates": empty[:20],
    }
    status = "passed" if house == 435 and senate == expected_senate and not empty else "warning"
    record_verification(conn, "reference_coverage", status, result)
    return result


def check_certification_integrity(conn: psycopg.Connection) -> int:
    """Belt-and-braces: constraints should make this zero."""
    rows = conn.execute(
        """
        SELECT r.id FROM election_results r
        LEFT JOIN result_certifications rc ON rc.id = r.certification_id
        WHERE r.is_certified AND (rc.id IS NULL OR rc.status = 'rescinded' OR rc.contest_id <> r.contest_id)
        """
    ).fetchall()
    for r in rows:
        record_discrepancy(conn, "election_results", r["id"], "is_certified", "certified_without_evidence",
                           {"election_results": True, "result_certifications": None}, severity="critical")
    record_verification(conn, "certification_integrity", "passed" if not rows else "failed", {"violations": len(rows)})
    return len(rows)


def check_result_totals(conn: psycopg.Connection) -> int:
    """Flag reporting units where county sums disagree with the statewide total from the same source."""
    rows = conn.execute(
        """
        WITH state_level AS (
            SELECT r.contest_id, r.choice_label, r.source_id, r.votes
            FROM election_results r JOIN reporting_units ru ON ru.id = r.reporting_unit_id
            WHERE ru.unit_type = 'state'
        ), county_sum AS (
            SELECT r.contest_id, r.choice_label, r.source_id, SUM(r.votes) AS votes, COUNT(*) AS units
            FROM election_results r JOIN reporting_units ru ON ru.id = r.reporting_unit_id
            WHERE ru.unit_type = 'county'
            GROUP BY r.contest_id, r.choice_label, r.source_id
        )
        SELECT s.contest_id, s.choice_label, s.source_id, s.votes AS state_votes, c.votes AS county_votes
        FROM state_level s JOIN county_sum c USING (contest_id, choice_label, source_id)
        WHERE s.votes IS DISTINCT FROM c.votes
        """
    ).fetchall()
    for r in rows:
        record_discrepancy(
            conn, "contests", r["contest_id"], f"votes:{r['choice_label']}", "county_sum_mismatch",
            {"statewide": r["state_votes"], "sum_of_counties": r["county_votes"], "source_id": r["source_id"]},
            # Partial county reporting during counting is normal; it is informational until canvass.
            severity="info",
        )
    return len(rows)


def check_cross_source_results(conn: psycopg.Connection) -> int:
    """Statewide vote totals for the same contest/choice from different sources."""
    rows = conn.execute(
        """
        SELECT s.contest_id, s.choice_label, jsonb_object_agg(ds.source_key, s.votes) AS values,
               MAX(s.votes) - MIN(s.votes) AS spread
        FROM (
            SELECT DISTINCT ON (contest_id, choice_label, source_id, reporting_unit_id)
                   contest_id, choice_label, source_id, reporting_unit_id, votes
            FROM election_result_snapshots
            ORDER BY contest_id, choice_label, source_id, reporting_unit_id, observed_at DESC
        ) s
        JOIN reporting_units ru ON ru.id = s.reporting_unit_id AND ru.unit_type = 'state'
        JOIN data_sources ds ON ds.id = s.source_id
        GROUP BY s.contest_id, s.choice_label
        HAVING COUNT(DISTINCT s.source_id) > 1 AND MAX(s.votes) <> MIN(s.votes)
        """
    ).fetchall()
    for r in rows:
        record_discrepancy(conn, "contests", r["contest_id"], f"votes:{r['choice_label']}",
                           "cross_source_vote_mismatch", r["values"], severity="warning")
    return len(rows)


def check_stale_sources(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Jobs whose last successful run is older than twice their schedule."""
    schedules = config.load_sources().get("schedules", {})
    not_ingestion = {"validation", "daily_report", "verify_sources", "certification_watch"}
    stale = []
    now = datetime.now(timezone.utc)
    for job, spec in schedules.items():
        if job in not_ingestion:
            continue
        if spec.get("active_to") and datetime.fromisoformat(spec["active_to"]) < now:
            continue  # election-window job that has finished
        if spec.get("active_from") and datetime.fromisoformat(spec["active_from"]) > now:
            continue
        if "every_hours" in spec:
            window = timedelta(hours=spec["every_hours"] * 2)
        elif "every_days" in spec:
            window = timedelta(days=spec["every_days"] * 2)
        elif "every_minutes" in spec:
            window = timedelta(minutes=spec["every_minutes"] * 3)
        else:
            continue
        row = conn.execute(
            "SELECT MAX(finished_at) AS last_ok FROM ingestion_runs WHERE job_name = %s "
            "AND status IN ('succeeded', 'partial')",
            (job,),
        ).fetchone()
        last_ok = row["last_ok"] if row else None
        if last_ok is None or now - last_ok > window:
            stale.append({"job": job, "last_success": last_ok.isoformat() if last_ok else None})
    return stale


def check_missing_provenance(conn: psycopg.Connection) -> dict[str, int]:
    queries = {
        "results_without_source_record": "SELECT COUNT(*) AS n FROM election_results WHERE source_record_id IS NULL",
        "contributions_without_source_record": "SELECT COUNT(*) AS n FROM contributions WHERE source_record_id IS NULL",
        "polls_without_source_record": "SELECT COUNT(*) AS n FROM polls WHERE source_record_id IS NULL",
        "qualified_without_evidence": (
            "SELECT COUNT(*) AS n FROM contest_candidates WHERE ballot_status IN ('qualified','certified_on_ballot') "
            "AND ballot_status_evidence_url IS NULL"
        ),
    }
    return {name: conn.execute(sql).fetchone()["n"] for name, sql in queries.items()}


def close_orphaned_runs(conn: psycopg.Connection, older_than_hours: int = 6) -> int:
    """Mark runs left 'running' by a killed process as failed (data committed so far is kept)."""
    rows = conn.execute(
        """UPDATE ingestion_runs SET status = 'failed', finished_at = NOW(),
                  error_message = COALESCE(error_message, 'orphaned: process ended without closing the run')
           WHERE status = 'running' AND started_at < NOW() - make_interval(hours => %s)
           RETURNING id""",
        (older_than_hours,),
    ).fetchall()
    return len(rows)


def run_verification(conn: psycopg.Connection) -> dict[str, Any]:
    summary = {
        "orphaned_runs_closed": close_orphaned_runs(conn),
        "reference_coverage": check_reference_coverage(conn),
        "certification_violations": check_certification_integrity(conn),
        "county_total_mismatches": check_result_totals(conn),
        "cross_source_mismatches": check_cross_source_results(conn),
        "missing_provenance": check_missing_provenance(conn),
        "stale_jobs": check_stale_sources(conn),
    }
    conn.commit()
    log.info("Verification summary: %s", summary)
    return summary
