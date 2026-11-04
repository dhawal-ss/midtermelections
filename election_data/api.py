"""Read-only REST API.

    uvicorn election_data.api:app --host 0.0.0.0 --port 8000

Every result returned carries display_label and is_certified so a client
cannot mistake preliminary numbers for certified ones.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from election_data import __version__, database

app = FastAPI(title="US Midterm Election Intelligence Database", version=__version__)


def _query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with database.connect() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/health")
def health() -> dict[str, Any]:
    rows = _query("SELECT COUNT(*) AS contests FROM contests")
    return {"status": "ok", **rows[0]}


@app.get("/states")
def states() -> list[dict[str, Any]]:
    return _query("SELECT * FROM v_state_coverage ORDER BY state_code")


@app.get("/contests")
def contests(state: str | None = None, office_level: str | None = None,
             limit: int = Query(200, le=1000), offset: int = 0) -> list[dict[str, Any]]:
    return _query(
        """SELECT * FROM v_contest_summary
           WHERE (%(state)s::text IS NULL OR state_code = %(state)s)
             AND (%(level)s::text IS NULL OR office_level = %(level)s)
           ORDER BY contest_key LIMIT %(limit)s OFFSET %(offset)s""",
        {"state": state.upper() if state else None, "level": office_level, "limit": limit, "offset": offset},
    )


@app.get("/contests/{contest_key}/candidates")
def contest_candidates(contest_key: str) -> list[dict[str, Any]]:
    return _query(
        """SELECT c.id, c.canonical_name, c.party, cc.filing_status, cc.ballot_status,
                  cc.ballot_status_evidence_url, cc.incumbent_challenge, c.verification_status,
                  (SELECT array_agg(csi.external_id) FROM candidate_source_ids csi
                    WHERE csi.candidate_id = c.id AND csi.id_type = 'fec_candidate_id') AS fec_ids
           FROM contest_candidates cc JOIN candidates c ON c.id = cc.candidate_id
           JOIN contests k ON k.id = cc.contest_id WHERE k.contest_key = %s ORDER BY c.canonical_name""",
        (contest_key,),
    )


@app.get("/contests/{contest_key}/results")
def contest_results(contest_key: str, reporting_unit_type: str = "state") -> dict[str, Any]:
    rows = _query(
        "SELECT * FROM v_results_labeled WHERE contest_key = %s AND reporting_unit_type = %s "
        "ORDER BY reporting_unit, votes DESC NULLS LAST",
        (contest_key, reporting_unit_type),
    )
    calls = _query(
        """SELECT rc.caller, rc.winner_label, rc.called_at, rc.retracted_at FROM race_calls rc
           JOIN contests c ON c.id = rc.contest_id WHERE c.contest_key = %s ORDER BY rc.called_at""",
        (contest_key,),
    )
    if not rows and not calls:
        raise HTTPException(404, "No results for this contest")
    return {
        "contest_key": contest_key,
        "all_certified": bool(rows) and all(r["is_certified"] for r in rows),
        "results": rows,
        "race_calls_note": "Race calls are projections by the named organization, not certification.",
        "race_calls": calls,
    }


@app.get("/candidates")
def candidates(q: str = Query(..., min_length=2), limit: int = Query(50, le=200)) -> list[dict[str, Any]]:
    return _query(
        "SELECT id, canonical_name, party, incumbent, verification_status FROM candidates "
        "WHERE canonical_name ILIKE %s ORDER BY canonical_name LIMIT %s",
        (f"%{q}%", limit),
    )


@app.get("/candidates/{candidate_id}/finance")
def candidate_finance(candidate_id: int) -> list[dict[str, Any]]:
    return _query("SELECT * FROM financial_summaries WHERE candidate_id = %s ORDER BY coverage_end_date DESC",
                  (candidate_id,))


@app.get("/discrepancies")
def discrepancies(status: str = "open", limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    return _query("SELECT * FROM data_discrepancies WHERE status = %s ORDER BY last_detected_at DESC LIMIT %s",
                  (status, limit))


@app.get("/coverage")
def coverage() -> dict[str, Any]:
    rows = _query("SELECT metrics FROM data_quality_reports ORDER BY generated_at DESC LIMIT 1")
    if not rows:
        raise HTTPException(404, "No report yet; run python -m scripts.verify_data")
    return rows[0]["metrics"]


# Live dashboard: projections API, SSE stream and static front end
from fastapi.staticfiles import StaticFiles  # noqa: E402

from election_data.live_api import WEB_DIR, router as live_router  # noqa: E402

app.include_router(live_router)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
