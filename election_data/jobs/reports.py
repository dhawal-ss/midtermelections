"""Daily monitoring / coverage report."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from election_data import config
from election_data.provenance import canonical_json

log = logging.getLogger(__name__)

COUNTS = {
    "states_and_dc": "SELECT COUNT(*) FROM states",
    "contests": "SELECT COUNT(*) FROM contests",
    "house_contests_2026": (
        "SELECT COUNT(*) FROM contests c JOIN offices o ON o.id = c.office_id "
        "WHERE o.office_code LIKE 'US-HOUSE-%%' AND c.state_code <> 'DC' AND c.contest_key LIKE '2026:%%'"
    ),
    "senate_contests_2026": (
        "SELECT COUNT(*) FROM contests c JOIN offices o ON o.id = c.office_id "
        "WHERE o.office_code LIKE 'US-SENATE-%%' AND c.contest_key LIKE '2026:%%'"
    ),
    "candidates": "SELECT COUNT(*) FROM candidates",
    "candidates_with_fec_id": "SELECT COUNT(DISTINCT candidate_id) FROM candidate_source_ids WHERE id_type = 'fec_candidate_id'",
    "ballot_qualified_candidates": (
        "SELECT COUNT(*) FROM contest_candidates WHERE ballot_status IN ('qualified', 'certified_on_ballot')"
    ),
    "committees": "SELECT COUNT(*) FROM campaign_committees",
    "contributions": "SELECT COUNT(*) FROM contributions",
    "expenditures": "SELECT COUNT(*) FROM expenditures",
    "financial_summaries": "SELECT COUNT(*) FROM financial_summaries",
    "ballot_measures": "SELECT COUNT(*) FROM ballot_measures",
    "polls": "SELECT COUNT(*) FROM polls",
    "result_rows_current": "SELECT COUNT(*) FROM election_results",
    "result_snapshots_24h": "SELECT COUNT(*) FROM election_result_snapshots WHERE observed_at > NOW() - INTERVAL '24 hours'",
    "certified_contests": "SELECT COUNT(DISTINCT contest_id) FROM result_certifications WHERE status <> 'rescinded'",
    "race_calls": "SELECT COUNT(*) FROM race_calls WHERE retracted_at IS NULL",
    "historical_result_rows": "SELECT COUNT(*) FROM historical_results",
    "raw_records": "SELECT COUNT(*) FROM raw_records",
    "failed_runs_24h": "SELECT COUNT(*) FROM ingestion_runs WHERE status = 'failed' AND started_at > NOW() - INTERVAL '24 hours'",
    "open_discrepancies": "SELECT COUNT(*) FROM data_discrepancies WHERE status = 'open'",
    "critical_discrepancies": "SELECT COUNT(*) FROM data_discrepancies WHERE status = 'open' AND severity = 'critical'",
    "candidate_match_queue_pending": "SELECT COUNT(*) FROM candidate_match_queue WHERE status = 'pending'",
    "state_urls_verified": "SELECT COUNT(*) FROM state_source_registry WHERE urls_verified_at IS NOT NULL",
    "state_adapters_implemented": (
        "SELECT COUNT(*) FROM state_source_registry WHERE adapter_status NOT IN ('not_implemented')"
    ),
}


def build_report(conn: psycopg.Connection) -> dict[str, Any]:
    metrics: dict[str, Any] = {name: list(conn.execute(sql).fetchone().values())[0] for name, sql in COUNTS.items()}
    metrics["states_updated_24h"] = conn.execute(
        """SELECT COUNT(DISTINCT ds.source_key) AS n FROM ingestion_runs r JOIN data_sources ds ON ds.id = r.source_id
           WHERE ds.source_key LIKE 'state:%%' AND r.status = 'succeeded' AND r.started_at > NOW() - INTERVAL '24 hours'"""
    ).fetchone()["n"]
    metrics["latest_runs"] = conn.execute(
        "SELECT job_name, source_key, status, started_at, records_received, records_failed, error_message "
        "FROM v_latest_ingestion ORDER BY job_name"
    ).fetchall()
    metrics["states_without_adapter"] = [
        r["state_code"] for r in conn.execute(
            "SELECT state_code FROM state_source_registry WHERE adapter_status = 'not_implemented' ORDER BY 1")
    ]
    metrics["generated_at"] = datetime.now(timezone.utc).isoformat()
    return json.loads(canonical_json(metrics))


def write_report(conn: psycopg.Connection) -> tuple[dict[str, Any], str]:
    report = build_report(conn)
    conn.execute("INSERT INTO data_quality_reports (metrics) VALUES (%s)", (Jsonb(report),))
    conn.commit()
    config.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.EXPORTS_DIR / f"coverage_report_{date.today().isoformat()}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report, str(path)
