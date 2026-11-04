"""Election-night loop: pull live results, recompute projections, repeat.

    python -m scripts.run_live                 # every 60 s
    python -m scripts.run_live --interval 30 --once

Each cycle: civicAPI results (if enabled in config/sources.yaml) and any state adapter
marked configured/tested/production, then the projection model. Start the dashboard
separately with: uvicorn election_data.api:app --port 8000
"""
from __future__ import annotations

import argparse
import logging
import time

from election_data import config, database
from election_data.http import configure_logging
from election_data.jobs.registry import live_results
from election_data.projections.engine import run_projections
from election_data.projections.priors import build_priors

log = logging.getLogger("run_live")


def cycle() -> None:
    try:
        live_results()
    except Exception:
        log.exception("Live results pull failed; projecting from data already stored")
    with database.connect() as conn:
        if not conn.execute("SELECT EXISTS (SELECT 1 FROM race_priors) AS e").fetchone()["e"]:
            build_priors(conn)
        s = run_projections(conn)
    sen = s.chambers.get("senate", {}).get("control_prob", {})
    hou = s.chambers.get("house", {}).get("control_prob", {})
    log.info("races reporting=%d snapshots=%d | Senate D %.0f%% R %.0f%% | House D %.0f%% R %.0f%%",
             s.with_results, s.snapshots_written, 100 * sen.get("DEM", 0), 100 * sen.get("REP", 0),
             100 * hou.get("DEM", 0), 100 * hou.get("REP", 0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    configure_logging(config.LOG_LEVEL)
    while True:
        started = time.monotonic()
        cycle()
        if args.once:
            break
        time.sleep(max(1.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
