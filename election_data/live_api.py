"""Live results + projections API and the dashboard's static files.

Mounted into election_data.api:app. Endpoints:
  GET /live/summary            chamber control, status counts, races to watch, feed health
  GET /live/races              latest projection for every race (filter by office/state)
  GET /live/races/{key}        one race: candidates, projection, vote + probability history
  GET /live/stream             Server-Sent Events: 'update' whenever new projections land
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from election_data import config, database

router = APIRouter()
WEB_DIR = Path(__file__).resolve().parent / "web"

OFFICE_PREFIX = {"senate": "US-SENATE-%", "house": "US-HOUSE-%", "governor": "GOV-%"}
LIVE_JOBS = ("live_results", "state_results", "projections")


def _q(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with database.connect() as conn:
        return conn.execute(sql, params).fetchall()


def _office_kind(office_code: str) -> str:
    return "senate" if office_code.startswith("US-SENATE") else "house" if office_code.startswith("US-HOUSE") \
        else "governor" if office_code.startswith("GOV-") else "other"


def data_mode() -> str:
    rows = _q("""SELECT EXISTS (SELECT 1 FROM election_results r JOIN data_sources ds ON ds.id = r.source_id
                                WHERE ds.source_key = 'simulation') AS sim,
                        EXISTS (SELECT 1 FROM election_results) AS any_results""")
    if rows[0]["sim"]:
        return "simulation"
    return "live" if rows[0]["any_results"] else "awaiting_results"


def _race_row(r: dict[str, Any], top: int = 3) -> dict[str, Any]:
    cands = sorted(r["candidates"] or [], key=lambda c: -(c.get("votes") or 0) - (c.get("win_prob") or 0))
    return {
        "contest_key": r["contest_key"],
        "state": r["state_code"],
        "office": _office_kind(r["office_code"]),
        "office_name": r["office_name"],
        "status": r["model_status"],
        "leader": r["leader_label"],
        "leader_party": r["leader_party"],
        "leader_prob": float(r["leader_win_prob"]) if r["leader_win_prob"] is not None else None,
        "votes_counted": r["votes_counted"],
        "pct_reporting": float(r["pct_reporting"]) if r["pct_reporting"] is not None else None,
        "expected_total": r["expected_total_votes"],
        "certification_status": r["certification_status"],
        "computed_at": r["computed_at"].isoformat(),
        "candidates": cands[:top] if top else cands,
    }


@router.get("/live/summary")
def summary() -> dict[str, Any]:
    chambers = {}
    for chamber in ("senate", "house"):
        rows = _q("SELECT * FROM chamber_projections WHERE chamber = %s ORDER BY computed_at DESC LIMIT 1",
                  (chamber,))
        if rows:
            r = rows[0]
            chambers[chamber] = {
                "computed_at": r["computed_at"].isoformat(), "seats_needed": r["seats_needed"],
                "control_prob": r["control_prob"], "expected_seats": r["expected_seats"],
                "seat_distribution": r["seat_distribution"], "seats_decided": r["seats_decided"],
                "holdovers": r["holdovers"], "simulations": r["simulations"],
                "total_seats": config.load_sources()["chambers"][chamber]["total_seats"],
            }
    races = [_race_row(r) for r in _q("SELECT * FROM v_latest_projections")]
    counts: dict[str, dict[str, int]] = {}
    for r in races:
        counts.setdefault(r["office"], {}).setdefault(r["status"], 0)
        counts[r["office"]][r["status"]] += 1
    reporting = [r for r in races if r["votes_counted"]]
    watch_pool = reporting if reporting else races
    watch = sorted((r for r in watch_pool if r["office"] in ("senate", "governor", "house")
                    and r["status"] not in ("projected", "final_count")),
                   key=lambda r: (abs((r["leader_prob"] or 0.5) - 0.5), r["office"] != "senate"))[:8]
    feeds = _q(
        """SELECT DISTINCT ON (job_name) job_name, status, started_at, finished_at, records_received, error_message
           FROM ingestion_runs WHERE job_name = ANY(%s) ORDER BY job_name, started_at DESC""",
        (list(LIVE_JOBS),),
    )
    last = _q("SELECT MAX(computed_at) AS t FROM projection_snapshots")[0]["t"]
    return {
        "data_mode": data_mode(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_projection_at": last.isoformat() if last else None,
        "model_version": config.load_sources()["model"]["version"],
        "chambers": chambers,
        "status_counts": counts,
        "races_reporting": len(reporting),
        "watch": watch,
        "feeds": [{**f, "started_at": f["started_at"].isoformat(),
                   "finished_at": f["finished_at"].isoformat() if f["finished_at"] else None} for f in feeds],
    }


@router.get("/live/races")
def races(office: str | None = Query(None, pattern="^(senate|house|governor)$"),
          state: str | None = None) -> dict[str, Any]:
    sql = "SELECT * FROM v_latest_projections WHERE TRUE"
    params: list[Any] = []
    if office:
        sql += " AND office_code LIKE %s"
        params.append(OFFICE_PREFIX[office])
    if state:
        sql += " AND state_code = %s"
        params.append(state.upper())
    rows = [_race_row(r) for r in _q(sql + " ORDER BY contest_key", tuple(params))]
    return {"data_mode": data_mode(), "races": rows}


@router.get("/live/races/{contest_key}")
def race_detail(contest_key: str) -> dict[str, Any]:
    latest = _q("SELECT * FROM v_latest_projections WHERE contest_key = %s", (contest_key,))
    if not latest:  # also accept the URL slug form used by the dashboard (#race-2026-general-us-senate-ga-ii)
        latest = _q("SELECT * FROM v_latest_projections "
                    "WHERE lower(regexp_replace(contest_key, '[^A-Za-z0-9]+', '-', 'g')) = lower(%s)", (contest_key,))
    if not latest:
        raise HTTPException(404, "Unknown contest or no projection yet")
    race = _race_row(latest[0], top=0)
    cid = latest[0]["contest_id"]
    history = _q(
        """SELECT computed_at, votes_counted, pct_reporting, leader_party, leader_win_prob, model_status, candidates
           FROM projection_snapshots WHERE contest_id = %s ORDER BY computed_at, id""", (cid,))
    prob_series = []
    for h in history:
        by_party: dict[str, float] = {}
        for c in h["candidates"] or []:
            by_party[c["party"]] = by_party.get(c["party"], 0.0) + float(c.get("win_prob") or 0)
        prob_series.append({"t": h["computed_at"].isoformat(), "pct": float(h["pct_reporting"] or 0),
                            "votes": h["votes_counted"], "status": h["model_status"], "prob": by_party,
                            "share": {c["label"]: c.get("share") for c in h["candidates"] or [] if c.get("label")}})
    prior = _q("SELECT prior_margin, prior_sd, turnout_prior, basis FROM race_priors WHERE contest_id = %s", (cid,))
    calls = _q("SELECT caller, winner_label, called_at, retracted_at FROM race_calls WHERE contest_id = %s "
               "ORDER BY called_at", (cid,))
    official = _q(
        """SELECT r.choice_label, r.votes, r.result_stage, r.is_certified, ds.name AS source, ds.authority_level,
                  r.source_reported_at, r.updated_at
           FROM election_results r JOIN reporting_units ru ON ru.id = r.reporting_unit_id AND ru.unit_type = 'state'
           JOIN data_sources ds ON ds.id = r.source_id WHERE r.contest_id = %s ORDER BY r.votes DESC NULLS LAST""",
        (cid,))
    counties = _q(
        """SELECT ru.name AS unit, r.choice_label, r.votes, r.party_label FROM election_results r
           JOIN reporting_units ru ON ru.id = r.reporting_unit_id AND ru.unit_type = 'county'
           WHERE r.contest_id = %s ORDER BY ru.name""", (cid,))
    return {
        "data_mode": data_mode(),
        "race": race,
        "prior": {k: (float(v) if k in ("prior_margin", "prior_sd") and v is not None else v)
                  for k, v in prior[0].items()} if prior else None,
        "history": prob_series,
        "race_calls": [{**c, "called_at": c["called_at"].isoformat(),
                        "retracted_at": c["retracted_at"].isoformat() if c["retracted_at"] else None} for c in calls],
        "results_source_rows": [{**o, "source_reported_at": o["source_reported_at"].isoformat()
                                 if o["source_reported_at"] else None, "updated_at": o["updated_at"].isoformat()}
                                for o in official],
        "county_rows": counties,
        "notes": [
            "Probabilities and 'projected' statuses are model estimates, not official results or media race calls.",
            "Results are preliminary until certified by the state election authority.",
        ],
    }


@router.get("/live/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events. Emits 'update' whenever a new projection or chamber run is stored."""

    def marker() -> tuple:
        r = _q("""SELECT (SELECT MAX(id) FROM projection_snapshots) AS p,
                         (SELECT MAX(id) FROM chamber_projections) AS c,
                         (SELECT MAX(id) FROM election_result_snapshots) AS s""")[0]
        return r["p"], r["c"], r["s"]

    async def events():
        last = None
        yield "retry: 5000\n\n"
        while True:
            try:
                current = await asyncio.to_thread(marker)
            except Exception:  # database briefly unavailable: keep the stream open
                current = last
            if current != last:
                last = current
                yield f"event: update\ndata: {json.dumps({'marker': list(current or ()), 'at': datetime.now(timezone.utc).isoformat()})}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
