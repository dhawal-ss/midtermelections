"""Pre-election baselines (priors) for the live model, built from stored evidence.

  House     -> 2024 result in the same district (wider where maps were redrawn for 2026)
  Senate    -> last election for the same seat: 2020 for Class II, 2022 for the FL/OH Class III specials
  Governor  -> no baseline loaded yet: margin 0 with a deliberately wide uncertainty

Turnout prior: 2022 (midterm) House votes in the district, or summed statewide.
Every prior records its basis so the UI can say what a pre-results probability rests on.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from election_data import config, reference
from election_data.projections.model import normalize_party

log = logging.getLogger(__name__)

UNCONTESTED_CAP = 0.40


def _votes(payload: dict[str, Any]) -> float:
    for key in ("final_votes", "general_votes"):
        value = payload.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def _load(conn: psycopg.Connection, year: int, office_code: str) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """SELECT DISTINCT ON (payload->>'contest_id', payload->>'candidate_name') payload
           FROM raw_records WHERE record_type = %s AND payload->>'office_code' = %s
           ORDER BY payload->>'contest_id', payload->>'candidate_name', retrieved_at DESC""",
        (f"election_org:federal_results_{year}", office_code),
    ).fetchall()
    by_contest: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_contest[r["payload"]["contest_id"]].append(r["payload"])
    return by_contest


def party_of(parties: str | None) -> str:
    """First party listed: 'D | W' -> DEM, 'Democrat; Working Families' -> DEM."""
    import re
    first = re.split(r"[;,|/]", parties or "")[0]
    return normalize_party(first)


def two_party_margin(rows: list[dict[str, Any]]) -> tuple[float | None, float, bool]:
    """(DEM-minus-REP two-party margin, total votes, contested?) for one past contest."""
    dem = rep = total = 0.0
    unopposed_party = None
    for row in rows:
        v = _votes(row)
        total += v
        party = party_of(row.get("parties"))
        if party == "DEM":
            dem += v
        elif party == "REP":
            rep += v
        if str(row.get("general_unopposed")).lower() == "true" and party in ("DEM", "REP"):
            unopposed_party = party
    if dem + rep == 0:
        if unopposed_party:  # unopposed and not printed on the ballot: no votes recorded
            return (1.0 if unopposed_party == "DEM" else -1.0), total, False
        return None, total, False
    contested = dem > 0 and rep > 0
    return (dem - rep) / (dem + rep), total, contested


def _district_of(row: dict[str, Any]) -> str:
    d = str(row.get("district") or "00")
    return d.zfill(2) if d.isdigit() else "00"


def build_priors(conn: psycopg.Connection) -> dict[str, int]:
    model = config.load_sources()["model"]
    sd = model["prior_sd"]
    redrawn = set(model.get("redistricted_for_2026", []))
    year = config.ELECTION_YEAR

    house24 = _load(conn, 2024, "H")
    house22 = _load(conn, 2022, "H")
    senate20 = _load(conn, 2020, "S")
    senate22 = _load(conn, 2022, "S")

    def index_house(data):
        out = {}
        for rows in data.values():
            out[(rows[0]["state"], _district_of(rows[0]))] = rows
        return out

    h24, h22 = index_house(house24), index_house(house22)
    state_turnout_22: dict[str, float] = defaultdict(float)
    for (state, _), rows in h22.items():
        state_turnout_22[state] += sum(_votes(r) for r in rows)

    contests = conn.execute(
        """SELECT c.id, c.contest_key, c.state_code, o.office_code FROM contests c
           JOIN offices o ON o.id = c.office_id WHERE c.contest_key LIKE %s""",
        (f"{year}:%",),
    ).fetchall()
    stats = {"house": 0, "senate": 0, "governor": 0, "no_baseline": 0}
    for c in contests:
        state, code = c["state_code"], c["office_code"]
        margin, prior_sd, basis, turnout, details = 0.0, sd["governor"], "none", None, {}
        if code.startswith("US-HOUSE-"):
            district = code.rsplit("-", 1)[-1]
            rows = h24.get((state, district))
            t22 = h22.get((state, district))
            turnout = sum(_votes(r) for r in t22) if t22 else None
            if rows:
                m, total, contested = two_party_margin(rows)
                if m is not None:
                    margin = max(-UNCONTESTED_CAP, min(UNCONTESTED_CAP, m)) if not contested else m
                    prior_sd = sd["house_uncontested_baseline"] if not contested else (
                        sd["house_redistricted"] if state in redrawn else sd["house"])
                    basis = "2024 House result" + (" (pre-redistricting lines)" if state in redrawn else "") + \
                        ("" if contested else " (uncontested; capped)")
                    details = {"baseline_margin": m, "baseline_votes": total, "contested": contested}
                    stats["house"] += 1
            if basis == "none":
                prior_sd = sd["house_redistricted"]
        elif code.startswith("US-SENATE-"):
            special = state in reference.SENATE_2026_SPECIAL
            source, year_used = (senate22, 2022) if special else (senate20, 2020)
            candidates = [cid for cid in source if cid.split("-")[2] == state]
            if state == "GA" and not special:
                candidates = [cid for cid in candidates if "FULL" in cid.upper()] or candidates
            rows = source.get(candidates[0]) if candidates else None
            turnout = state_turnout_22.get(state) or None
            if rows:
                m, total, contested = two_party_margin(rows)
                if m is not None:
                    margin = m if contested else max(-UNCONTESTED_CAP, min(UNCONTESTED_CAP, m))
                    prior_sd = sd["senate"]
                    basis = f"{year_used} Senate result, same seat"
                    details = {"baseline_contest": candidates[0], "baseline_margin": m, "baseline_votes": total}
                    stats["senate"] += 1
        elif code.startswith("GOV-"):
            turnout = state_turnout_22.get(state) or None
            stats["governor"] += 1
        if basis == "none":
            stats["no_baseline"] += 1
        conn.execute(
            """
            INSERT INTO race_priors (contest_id, prior_margin, prior_sd, turnout_prior, basis, details, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (contest_id) DO UPDATE SET prior_margin = EXCLUDED.prior_margin,
                prior_sd = EXCLUDED.prior_sd, turnout_prior = EXCLUDED.turnout_prior, basis = EXCLUDED.basis,
                details = EXCLUDED.details, updated_at = NOW()
            """,
            (c["id"], round(margin, 4), round(prior_sd, 4), int(turnout) if turnout else None, basis, Jsonb(details)),
        )
    conn.commit()
    log.info("Priors built: %s", stats)
    return stats
