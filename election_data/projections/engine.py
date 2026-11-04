"""Run the live model over every 2026 contest and store projections.

One run:
  * loads the latest statewide result lines per contest + its prior
  * draws one set of national shocks shared by every race (correlated errors)
  * simulates each race; stores a projection snapshot when its inputs changed
  * aggregates Senate and House control from the same draws

Projections never touch election_results, certification, or race_calls.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from election_data import config
from election_data.projections.model import (
    Candidate, ModelParams, RaceInput, normalize_party, prior_only_draws, simulate_chamber, simulate_race,
)
from election_data.provenance import content_hash

log = logging.getLogger(__name__)
DECIDED = {"projected", "final_count"}


@dataclass
class RunSummary:
    races: int
    snapshots_written: int
    with_results: int
    chambers: dict[str, dict[str, Any]]


def _pct_reporting(rows: list[dict[str, Any]]) -> float | None:
    pcts = [float(r["expected_vote_pct"]) for r in rows if r["expected_vote_pct"] is not None]
    if pcts:
        return max(pcts)
    rep = [(r["precincts_reporting"], r["precincts_total"]) for r in rows
           if r["precincts_reporting"] is not None and r["precincts_total"]]
    if rep:
        a, b = max(rep, key=lambda x: x[1])
        return 100.0 * a / b
    return None


def load_inputs(conn: psycopg.Connection) -> list[dict[str, Any]]:
    year = config.ELECTION_YEAR
    contests = conn.execute(
        """
        SELECT c.id, c.contest_key, c.state_code, c.seats, c.certification_status, o.office_code,
               rp.prior_margin, rp.prior_sd, rp.turnout_prior, rp.basis
        FROM contests c JOIN offices o ON o.id = c.office_id
        LEFT JOIN race_priors rp ON rp.contest_id = c.id
        WHERE c.contest_key LIKE %s
        ORDER BY c.contest_key
        """,
        (f"{year}:%",),
    ).fetchall()
    results = conn.execute(
        """
        SELECT r.contest_id, r.choice_label, r.votes, r.expected_vote_pct, r.precincts_reporting,
               r.precincts_total, COALESCE(r.party_label, cand.party) AS party, r.is_certified
        FROM election_results r
        JOIN reporting_units ru ON ru.id = r.reporting_unit_id AND ru.unit_type = 'state'
        LEFT JOIN candidates cand ON cand.id = r.candidate_id
        JOIN contests c ON c.id = r.contest_id
        WHERE c.contest_key LIKE %s
        """,
        (f"{year}:%",),
    ).fetchall()
    by_contest: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_contest[r["contest_id"]].append(r)
    for c in contests:
        c["results"] = by_contest.get(c["id"], [])
    return contests


def run_projections(conn: psycopg.Connection, params: ModelParams | None = None,
                    seed: int | None = None) -> RunSummary:
    model_cfg = config.load_sources()["model"]
    params = params or ModelParams.from_config(model_cfg)
    seed = seed if seed is not None else model_cfg.get("seed")
    rng = np.random.default_rng(seed)
    majority_states = set(model_cfg.get("majority_required", []))
    shocks = rng.normal(0.0, params.national_sd, params.simulations)

    contests = load_inputs(conn)
    latest_hash = {
        r["contest_id"]: r["inputs_hash"] for r in conn.execute(
            "SELECT DISTINCT ON (contest_id) contest_id, inputs_hash FROM projection_snapshots "
            "ORDER BY contest_id, computed_at DESC, id DESC")
    }
    chamber_draws: dict[str, list[np.ndarray]] = {"senate": [], "house": []}
    chamber_decided: dict[str, list[str]] = {"senate": [], "house": []}
    written = with_results = 0

    for c in contests:
        prior_margin = float(c["prior_margin"] or 0.0)
        prior_sd = float(c["prior_sd"] or model_cfg["prior_sd"]["governor"])
        basis = c["basis"] or "none"
        rows = [r for r in c["results"] if r["votes"] is not None]
        final = c["certification_status"] == "certified" and bool(rows) and all(r["is_certified"] for r in rows)
        inputs = {
            "candidates": sorted([[r["choice_label"], r["party"], int(r["votes"])] for r in rows]),
            "pct": _pct_reporting(rows),
            "prior": [prior_margin, prior_sd, c["turnout_prior"], basis],
            "final": final,
            "model": [params.version, params.national_shift, params.national_sd, params.observed_weight_k,
                      params.remaining_vote_sd, params.turnout_sd],
        }
        inputs_hash = content_hash(inputs)

        if rows:
            with_results += 1
            race = RaceInput(
                contest_key=c["contest_key"],
                candidates=[Candidate(r["choice_label"], r["party"] or "", int(r["votes"])) for r in rows],
                pct_reporting=inputs["pct"], prior_margin=prior_margin, prior_sd=prior_sd,
                turnout_prior=float(c["turnout_prior"]) if c["turnout_prior"] else None, prior_basis=basis,
                seats=c["seats"] or 1, majority_required=c["state_code"] in majority_states, final=final,
            )
            proj = simulate_race(race, params, rng, shocks)
            draws = proj.winner_party_draws
            leader = proj.leader
            snapshot = {
                "votes_counted": proj.votes_counted, "pct": proj.pct_reporting,
                "expected": proj.expected_total, "leader_label": leader["label"] if leader else None,
                "leader_party": leader["party"] if leader else None,
                "leader_prob": leader["win_prob"] if leader else None, "status": proj.status,
                "candidates": proj.candidates,
            }
        else:
            draws = prior_only_draws(prior_margin, prior_sd, params, rng, shocks)
            dem_prob = float((draws == 0).mean())
            fav = "DEM" if dem_prob >= 0.5 else "REP"
            snapshot = {
                "votes_counted": 0, "pct": None, "expected": (float(c["turnout_prior"] or 0),) * 3,
                "leader_label": None, "leader_party": fav, "leader_prob": max(dem_prob, 1 - dem_prob),
                "status": "prior_only" if basis != "none" else "no_results",
                "candidates": [{"party": "DEM", "win_prob": dem_prob}, {"party": "REP", "win_prob": 1 - dem_prob}],
            }

        office = c["office_code"]
        chamber = "senate" if office.startswith("US-SENATE") else "house" if office.startswith("US-HOUSE") else None
        if chamber and not (chamber == "house" and c["state_code"] == "DC"):  # DC delegate does not vote
            chamber_draws[chamber].append(draws)
            if snapshot["status"] in DECIDED and snapshot["leader_party"]:
                chamber_decided[chamber].append(snapshot["leader_party"])

        if latest_hash.get(c["id"]) != inputs_hash:
            exp = snapshot["expected"]
            conn.execute(
                """
                INSERT INTO projection_snapshots (contest_id, model_version, inputs_hash, votes_counted,
                    pct_reporting, expected_total_votes, expected_total_low, expected_total_high, leader_label,
                    leader_party, leader_win_prob, model_status, candidates, inputs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (c["id"], params.version, inputs_hash, snapshot["votes_counted"], snapshot["pct"],
                 int(exp[0]) if exp[0] else None, int(exp[1]) if exp[1] else None, int(exp[2]) if exp[2] else None,
                 snapshot["leader_label"], snapshot["leader_party"], snapshot["leader_prob"], snapshot["status"],
                 Jsonb(snapshot["candidates"]), Jsonb(json.loads(json.dumps(inputs, default=str)))),
            )
            written += 1

    chambers: dict[str, dict[str, Any]] = {}
    have_chamber = conn.execute("SELECT EXISTS (SELECT 1 FROM chamber_projections) AS e").fetchone()["e"]
    for chamber, draws in chamber_draws.items():
        if not draws or (written == 0 and have_chamber):
            continue  # nothing changed: keep the last chamber projection instead of adding noise
        cfg = config.load_sources()["chambers"][chamber]
        cp = simulate_chamber(chamber, draws, chamber_decided[chamber], cfg.get("holdovers") or {},
                              cfg["seats_needed"], cfg.get("tie_breaker"))
        conn.execute(
            """
            INSERT INTO chamber_projections (chamber, model_version, simulations, seats_needed, expected_seats,
                control_prob, seat_distribution, seats_decided, holdovers)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (chamber, params.version, cp.simulations, cp.seats_needed, Jsonb(cp.expected_seats),
             Jsonb(cp.control_prob), Jsonb(cp.seat_distribution), Jsonb(cp.seats_decided), Jsonb(cp.holdovers)),
        )
        chambers[chamber] = {"control_prob": cp.control_prob, "expected_seats": cp.expected_seats,
                             "races": len(draws)}
    conn.commit()
    log.info("Projections: %d races (%d with results), %d snapshots written, chambers=%s",
             len(contests), with_results, written, chambers)
    return RunSummary(len(contests), written, with_results, chambers)


__all__ = ["run_projections", "load_inputs", "normalize_party"]
