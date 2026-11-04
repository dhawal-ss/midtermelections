"""Play back a SYNTHETIC election night into a separate demo database, for testing the live dashboard.

    python -m scripts.simulate_night --database-url postgresql://election:change_me@localhost:5432/elections_demo

Safety
  * Refuses to run unless the target database name contains 'demo' or 'sim'.
  * Results are written under the 'simulation' source, which makes the API report
    data_mode = "simulation" and the dashboard show a SIMULATED DATA banner.
  * Candidates are generic party placeholders, never real names.

How it plays out
  * True outcome per race = baseline (copied from the main database's race_priors)
    + a national swing + race noise.
  * Each state starts counting at its (approximate) poll-closing time and counts
    along a saturating curve; slow-counting states stop short of 100% on the night.
  * Some states count mail ballots first (early batches lean D), others
    election-day votes first (lean R): the first returns are biased, the
    final count is not. This exercises the model's resistance to early leads.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from urllib.parse import urlsplit

import numpy as np

from election_data import config, database, reference
from election_data.http import configure_logging
from election_data.ingestion.reference_data import seed_all
from election_data.ingestion.results import ResultObservation, record_results
from election_data.projections.engine import run_projections
from election_data.provenance import IngestionRun

# Approximate final poll-closing hour (Eastern, 24h) by state
CLOSE_ET = {
    **dict.fromkeys(["GA", "IN", "KY", "SC", "VA", "VT"], 19.0),
    **dict.fromkeys(["NC", "OH", "WV"], 19.5),
    **dict.fromkeys(["AL", "CT", "DE", "DC", "FL", "IL", "ME", "MD", "MA", "MI", "MS", "MO", "NH", "NJ", "OK",
                     "PA", "RI", "TN", "TX", "KS", "IA"], 20.0),
    **dict.fromkeys(["AR"], 20.5),
    **dict.fromkeys(["AZ", "CO", "LA", "MN", "NE", "NM", "NY", "SD", "WI", "WY", "ND"], 21.0),
    **dict.fromkeys(["MT", "NV", "UT", "ID"], 22.0),
    **dict.fromkeys(["CA", "OR", "WA", "HI"], 23.0),
    "AK": 25.0,
}
SLOW_COUNT = {"CA": 0.62, "WA": 0.70, "OR": 0.72, "NV": 0.80, "AZ": 0.78, "AK": 0.55, "UT": 0.85}
MAIL_FIRST = {"GA", "NC", "FL", "OH", "VA", "TX", "IA", "NH", "AZ", "NV", "CO", "WA", "OR", "CA", "UT", "MN"}


def guard(url: str) -> None:
    name = urlsplit(url).path.lstrip("/")
    if "demo" not in name and "sim" not in name:
        sys.exit(f"Refusing to write simulated results into database '{name}'. Use a database whose name "
                 "contains 'demo' or 'sim' (e.g. elections_demo).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", required=True, help="demo database (name must contain 'demo' or 'sim')")
    ap.add_argument("--priors-from", default=None, help="database to copy race_priors from (default: DATABASE_URL)")
    ap.add_argument("--start", type=float, default=18.75, help="start clock, Eastern hours (18.75 = 6:45pm)")
    ap.add_argument("--end", type=float, default=27.0, help="end clock, Eastern hours (27 = 3am)")
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--interval", type=float, default=4.0, help="seconds between steps")
    ap.add_argument("--swing", type=float, default=None, help="national swing (DEM +); random if omitted")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--simulations", type=int, default=4000, help="model simulations per step (speed)")
    args = ap.parse_args()
    guard(args.database_url)
    configure_logging("WARNING")
    rng = np.random.default_rng(args.seed)

    # -- fresh demo database ------------------------------------------------
    admin = database.connect(args.database_url, autocommit=True)
    # Demo database only (guarded above): close other sessions, e.g. a running dashboard, before resetting
    admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE datname = current_database() AND pid <> pg_backend_pid()")
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    admin.execute("DROP DOMAIN IF EXISTS verification_status CASCADE")
    admin.close()
    conn = database.connect(args.database_url)
    database.apply_schema(conn)
    seed_all(conn)

    # -- copy baselines ------------------------------------------------------
    priors = {}
    try:
        with database.connect(args.priors_from) as src:
            for r in src.execute("""SELECT c.contest_key, rp.prior_margin, rp.prior_sd, rp.turnout_prior, rp.basis,
                                           rp.details FROM race_priors rp JOIN contests c ON c.id = rp.contest_id"""):
                priors[r["contest_key"]] = r
    except Exception as exc:  # no main DB: run with neutral baselines
        print(f"No priors copied ({exc}); using neutral baselines")
    from psycopg.types.json import Jsonb
    contests = conn.execute("""SELECT c.id, c.contest_key, c.state_code, o.office_code FROM contests c
                               JOIN offices o ON o.id = c.office_id WHERE c.contest_key LIKE '2026:%'""").fetchall()
    for c in contests:
        p = priors.get(c["contest_key"])
        conn.execute(
            """INSERT INTO race_priors (contest_id, prior_margin, prior_sd, turnout_prior, basis, details)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (contest_id) DO NOTHING""",
            (c["id"], p["prior_margin"] if p else 0, p["prior_sd"] if p else 0.15,
             p["turnout_prior"] if p else None, p["basis"] if p else "none", Jsonb(p["details"] if p else {})))
    conn.commit()

    # -- hidden truth --------------------------------------------------------
    swing = args.swing if args.swing is not None else float(rng.normal(0, 0.03))
    races = []
    for c in contests:
        if c["state_code"] == "DC" and c["office_code"].startswith("US-HOUSE"):
            continue
        p = priors.get(c["contest_key"])
        base = float(p["prior_margin"]) if p else 0.0
        sd = float(p["prior_sd"]) if p else 0.15
        turnout = float(p["turnout_prior"]) if p and p["turnout_prior"] else (280_000 if "HOUSE" in c["office_code"] else 2_000_000)
        races.append({
            "key": c["contest_key"], "state": c["state_code"],
            "margin": float(np.clip(base + swing + rng.normal(0, sd * 0.6), -0.95, 0.95)),
            "other": float(rng.uniform(0.01, 0.04)),
            "turnout": turnout * float(rng.lognormal(0, 0.08)),
            "bias": (0.10 if c["state_code"] in MAIL_FIRST else -0.08) * float(rng.uniform(0.5, 1.2)),
            "close": CLOSE_ET.get(c["state_code"], 20.0) + float(rng.uniform(0, 0.25)),
            "cap": SLOW_COUNT.get(c["state_code"], 0.985) * float(rng.uniform(0.97, 1.0)),
            "tau": float(rng.uniform(0.8, 2.0)),
        })
    print(f"Simulating {len(races)} races; hidden national swing {swing:+.3f}. Dashboard shows SIMULATED DATA.")
    truth = {r["key"]: ("DEM" if r["margin"] > 0 else "REP") for r in races}
    wrong_projections: set[str] = set()
    projected_ever: set[str] = set()

    params = None
    from election_data.projections.model import ModelParams
    params = ModelParams.from_config(config.load_sources()["model"])
    params.simulations = args.simulations

    for step in range(args.steps + 1):
        clock = args.start + (args.end - args.start) * step / args.steps
        obs = []
        for r in races:
            dt = clock - r["close"]
            if dt <= 0:
                continue
            f = r["cap"] * (1 - math.exp(-dt / r["tau"]))
            if f < 0.002:
                continue
            cum_margin = r["margin"] + r["bias"] * (1 - f)   # average of batch margins counted so far
            total = r["turnout"] * f
            major = 1 - r["other"]
            dem = total * major * (1 + cum_margin) / 2
            rep = total * major * (1 - cum_margin) / 2
            other = total * r["other"]
            url = f"simulation://night/{r['key']}"
            for label, party, votes in (("Democratic candidate (sim)", "Democratic", dem),
                                        ("Republican candidate (sim)", "Republican", rep),
                                        ("Other candidates (sim)", "Independent", other)):
                obs.append(ResultObservation(state_code=r["state"], contest_key=r["key"], choice_label=label,
                                             votes=int(votes), source_url=url, result_stage="preliminary",
                                             expected_vote_pct=round(100 * f, 1), party=party))
        if obs:
            with IngestionRun(conn, "simulation", "live_results", {"clock_et": round(clock, 2), "step": step}) as run:
                record_results(conn, run, obs)
        summary = run_projections(conn, params=params, seed=args.seed + step)
        for row in conn.execute("""SELECT contest_key, leader_party, model_status FROM v_latest_projections
                                   WHERE model_status = 'projected'"""):
            if row["leader_party"] != truth[row["contest_key"]]:
                wrong_projections.add(row["contest_key"])
            projected_ever.add(row["contest_key"])
        sen = summary.chambers.get("senate", {}).get("control_prob", {})
        hou = summary.chambers.get("house", {}).get("control_prob", {})
        hh, mm = int(clock) % 24, int((clock % 1) * 60)
        print(f"{hh:02d}:{mm:02d} ET  races reporting {summary.with_results:3d}  "
              f"Senate D {sen.get('DEM', 0):.0%} / R {sen.get('REP', 0):.0%}   "
              f"House D {hou.get('DEM', 0):.0%} / R {hou.get('REP', 0):.0%}", flush=True)
        if step < args.steps:
            time.sleep(args.interval)
    final = conn.execute("SELECT contest_key, leader_party FROM v_latest_projections").fetchall()
    correct = sum(1 for r in final if r["contest_key"] in truth and r["leader_party"] == truth[r["contest_key"]])
    print(f"\nModel check against the hidden outcome: final leader correct in {correct}/{len(truth)} races; "
          f"{len(wrong_projections)} wrong 'projected' statuses out of {len(projected_ever)} races ever projected"
          + (f": {sorted(wrong_projections)}" if wrong_projections else ""))
    conn.close()


if __name__ == "__main__":
    main()
