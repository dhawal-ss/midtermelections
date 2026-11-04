"""Named jobs, used by the scheduler and the ingest_all CLI.

Each job opens its own connection, so a failure in one does not affect others.
"""
from __future__ import annotations

import logging
from typing import Callable

from election_data import config, database

log = logging.getLogger(__name__)


def _with_conn(fn: Callable, **kwargs):
    conn = database.connect()
    try:
        return fn(conn, **kwargs)
    finally:
        conn.close()


def fec_candidates(**kw):
    from election_data.ingestion.candidates import ingest_fec_candidates
    return _with_conn(ingest_fec_candidates, **kw)


def fec_committees(**kw):
    from election_data.ingestion.finance import ingest_committees
    return _with_conn(ingest_committees, **kw)


def fec_finance_totals(**kw):
    from election_data.ingestion.finance import ingest_candidate_totals
    return _with_conn(ingest_candidate_totals, **kw)


def fec_contributions(**kw):
    from election_data.ingestion.finance import ingest_contributions
    return _with_conn(ingest_contributions, **kw)


def fec_expenditures(**kw):
    from election_data.ingestion.finance import ingest_independent_expenditures, ingest_operating_expenditures
    _with_conn(ingest_operating_expenditures, **kw)
    return _with_conn(ingest_independent_expenditures, max_pages=kw.get("max_pages"))


def fec_election_dates(**kw):
    from election_data.ingestion.calendar import ingest_election_dates
    return _with_conn(ingest_election_dates, **kw)


def election_org(**kw):
    from election_data.ingestion.candidates import ingest_election_org
    return _with_conn(ingest_election_org, **kw)


def census_demographics(**kw):
    from election_data.ingestion.demographics import ingest_census_demographics
    return _with_conn(ingest_census_demographics, **kw)


def historical_results(**kw):
    from election_data.ingestion.historical import ingest_mit_election_lab
    return _with_conn(ingest_mit_election_lab, **kw)


def live_results(**kw):
    """Poll every configured live source: civicAPI (if enabled) and implemented state adapters."""
    from election_data.adapters.base import AdapterNotImplemented
    from election_data.ingestion.state_results import ingest_state_results
    from election_data.ingestion.supplemental import ingest_civicapi_results

    if config.source_config("civicapi").get("enabled"):
        try:
            _with_conn(ingest_civicapi_results)
        except Exception:
            log.exception("civicAPI results failed")
    for state in config.state_registry():
        if state.get("adapter_status") in ("configured", "tested", "production"):
            try:
                _with_conn(ingest_state_results, state_code=state["code"])
            except AdapterNotImplemented as exc:
                log.warning("%s", exc)
            except Exception:
                log.exception("State results failed for %s", state["code"])


def polls(**kw):
    from election_data.ingestion.supplemental import ingest_civicapi_polls
    if not config.source_config("civicapi").get("enabled"):
        log.info("civicAPI disabled; load pollster CSVs with: python -m scripts.ingest_all --only polls_csv")
        return None
    return _with_conn(ingest_civicapi_polls, **kw)


def projections(**kw):
    from election_data.projections.engine import run_projections
    from election_data.projections.priors import build_priors

    def _run(conn):
        if not conn.execute("SELECT EXISTS (SELECT 1 FROM race_priors) AS e").fetchone()["e"]:
            build_priors(conn)
        return run_projections(conn)
    return _with_conn(_run)


def validation(**kw):
    from election_data.jobs.validation import run_verification
    return _with_conn(run_verification)


def daily_report(**kw):
    from election_data.jobs.reports import write_report
    return _with_conn(write_report)


def verify_sources(**kw):
    from scripts.verify_sources import check_all
    return _with_conn(check_all)


def certification_watch(**kw):
    """Certification is recorded from official documents (scripts/certify.py); this job reports pending ones."""
    def _pending(conn):
        rows = conn.execute(
            """SELECT state_code, COUNT(*) AS n FROM contests c
               WHERE c.contest_key LIKE %s AND c.certification_status <> 'certified'
               GROUP BY state_code ORDER BY state_code""",
            (f"{config.ELECTION_YEAR}:%",),
        ).fetchall()
        log.info("Contests awaiting certification by state: %s", {r["state_code"]: r["n"] for r in rows})
        return rows
    return _with_conn(_pending)


JOBS: dict[str, Callable] = {
    "fec_candidates": fec_candidates,
    "fec_committees": fec_committees,
    "fec_finance_totals": fec_finance_totals,
    "fec_contributions": fec_contributions,
    "fec_expenditures": fec_expenditures,
    "fec_election_dates": fec_election_dates,
    "election_org": election_org,
    "census_demographics": census_demographics,
    "historical_results": historical_results,
    "live_results": live_results,
    "polls": polls,
    "projections": projections,
    "validation": validation,
    "daily_report": daily_report,
    "verify_sources": verify_sources,
    "certification_watch": certification_watch,
}
