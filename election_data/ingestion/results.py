"""Election results: snapshots, current values, certification, race calls, recounts.

Guarantees (enforced here AND by database constraints/triggers):
  * Every observation is appended to election_result_snapshots.
  * A row is certified only when a result_certifications record exists for the
    contest, from an official election authority, with a document URL.
  * A certified current value is never overwritten by a non-certified one.
  * A third-party value never overwrites an official-authority value; a
    disagreement is preserved as a data_discrepancy.
  * Race calls are stored separately and never change certification status.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable

import psycopg

from election_data import config, reference
from election_data.jobs.validation import validate_result
from election_data.provenance import IngestionRun, ensure_source, record_discrepancy

log = logging.getLogger(__name__)

STAGE_RANK = {"preliminary": 0, "unofficial_complete": 1, "canvassed": 2, "certified": 3, "amended_certified": 4}
CERTIFIED_STAGES = {"certified", "amended_certified"}


@dataclass
class ResultObservation:
    state_code: str
    contest_key: str
    choice_label: str
    votes: int | None
    source_url: str
    result_stage: str = "preliminary"
    vote_percentage: float | None = None
    candidate_fec_id: str | None = None
    reporting_unit_type: str = "state"
    reporting_unit_name: str | None = None
    reporting_unit_code: str = ""
    precincts_reporting: int | None = None
    precincts_total: int | None = None
    expected_vote_pct: float | None = None
    source_reported_at: datetime | str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def contest_key_for(office: str, state: str, district: str | None = None, year: int | None = None,
                    stage: str = "general") -> str:
    """Build the contest_key used in `contests` from human office names."""
    year = year or config.ELECTION_YEAR
    office = office.upper().replace(".", "").strip()
    if office in ("US HOUSE", "HOUSE", "US REPRESENTATIVE", "H"):
        return reference.contest_key(year, stage, reference.house_office_code(state, (district or "00").zfill(2)))
    if office in ("US SENATE", "SENATE", "S"):
        special = state in reference.SENATE_2026_SPECIAL
        stage = "special_general" if special and stage == "general" else stage
        return reference.contest_key(year, stage, reference.senate_office_code(state))
    if office in ("GOVERNOR", "GOV"):
        return reference.contest_key(year, stage, reference.governor_office_code(state))
    raise ValueError(f"Unsupported office for automatic contest mapping: {office}")


def ensure_reporting_unit(conn: psycopg.Connection, state: str, unit_type: str, name: str | None,
                          code: str = "") -> int:
    name = name or reference.STATES[state][0]
    row = conn.execute(
        """
        INSERT INTO reporting_units (state_code, unit_type, name, external_code)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (state_code, unit_type, name, external_code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (state, unit_type, name, code or ""),
    ).fetchone()
    return row["id"]


def active_certification(conn: psycopg.Connection, contest_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT * FROM result_certifications WHERE contest_id = %s AND status <> 'rescinded'
           ORDER BY certified_at DESC, id DESC LIMIT 1""",
        (contest_id,),
    ).fetchone()


def _contest_id(conn: psycopg.Connection, key: str) -> int | None:
    row = conn.execute("SELECT id FROM contests WHERE contest_key = %s", (key,)).fetchone()
    return row["id"] if row else None


def _candidate_id(conn: psycopg.Connection, contest_id: int, obs: ResultObservation) -> int | None:
    if obs.candidate_fec_id:
        row = conn.execute(
            "SELECT candidate_id FROM candidate_source_ids WHERE id_type = 'fec_candidate_id' AND external_id = %s",
            (obs.candidate_fec_id,),
        ).fetchone()
        if row:
            return row["candidate_id"]
    # Exact canonical-name match restricted to candidates already linked to this contest
    rows = conn.execute(
        """SELECT c.id FROM candidates c JOIN contest_candidates cc ON cc.candidate_id = c.id
           WHERE cc.contest_id = %s AND lower(c.canonical_name) = lower(%s)""",
        (contest_id, obs.choice_label),
    ).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def record_results(conn: psycopg.Connection, run: IngestionRun, observations: Iterable[ResultObservation],
                   raw_record_id: int | None = None) -> dict[str, int]:
    """Validate, snapshot and (where allowed) update current results."""
    stats = {"snapshots": 0, "current_updated": 0, "kept_existing": 0, "rejected": 0}
    authority = conn.execute("SELECT authority_level FROM data_sources WHERE id = %s",
                             (run.source_id,)).fetchone()["authority_level"]
    is_official_source = authority == "official_election_authority"

    for obs in observations:
        contest_id = _contest_id(conn, obs.contest_key)
        if contest_id is None:
            run.record_failure(f"unknown contest {obs.contest_key}")
            stats["rejected"] += 1
            continue

        cert = active_certification(conn, contest_id) if obs.result_stage in CERTIFIED_STAGES else None
        check = validate_result({**asdict(obs), "certification_evidence": cert and cert["document_url"]})
        if obs.result_stage in CERTIFIED_STAGES and not is_official_source:
            check["errors"].append("Certified stage reported by a non-official source")
        if check["errors"]:
            run.record_failure(f"{obs.contest_key}/{obs.choice_label}: {check['errors']}")
            stats["rejected"] += 1
            continue

        with conn.transaction():
            unit_id = ensure_reporting_unit(conn, obs.state_code, obs.reporting_unit_type,
                                            obs.reporting_unit_name, obs.reporting_unit_code)
            candidate_id = _candidate_id(conn, contest_id, obs)
            conn.execute(
                """
                INSERT INTO election_result_snapshots (contest_id, candidate_id, choice_label, reporting_unit_id,
                    votes, vote_percentage, precincts_reporting, precincts_total, expected_vote_pct, result_stage,
                    source_id, source_record_id, source_reported_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (contest_id, candidate_id, obs.choice_label, unit_id, obs.votes, obs.vote_percentage,
                 obs.precincts_reporting, obs.precincts_total, obs.expected_vote_pct, obs.result_stage,
                 run.source_id, raw_record_id, obs.source_reported_at),
            )
            stats["snapshots"] += 1

            existing = conn.execute(
                """SELECT r.*, ds.authority_level, ds.source_key FROM election_results r
                   JOIN data_sources ds ON ds.id = r.source_id
                   WHERE r.contest_id = %s AND r.choice_label = %s AND r.reporting_unit_id = %s""",
                (contest_id, obs.choice_label, unit_id),
            ).fetchone()

            if existing and not _may_replace(existing, obs, is_official_source, run.source_id):
                if existing["votes"] != obs.votes:
                    record_discrepancy(
                        conn, "election_results", existing["id"], "votes", "competing_result_value",
                        {existing["source_key"]: {"votes": existing["votes"], "stage": existing["result_stage"]},
                         run.source_key: {"votes": obs.votes, "stage": obs.result_stage}},
                        severity="warning" if existing["is_certified"] else "info",
                    )
                stats["kept_existing"] += 1
                continue

            certified = obs.result_stage in CERTIFIED_STAGES
            verification = "certified" if certified else ("official" if is_official_source else "unverified")
            conn.execute(
                """
                INSERT INTO election_results (contest_id, candidate_id, choice_label, reporting_unit_id, votes,
                    vote_percentage, precincts_reporting, precincts_total, expected_vote_pct, result_stage,
                    is_certified, certification_id, source_id, source_record_id, source_reported_at,
                    verification_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (contest_id, choice_label, reporting_unit_id) DO UPDATE SET
                    candidate_id = COALESCE(EXCLUDED.candidate_id, election_results.candidate_id),
                    votes = EXCLUDED.votes, vote_percentage = EXCLUDED.vote_percentage,
                    precincts_reporting = EXCLUDED.precincts_reporting, precincts_total = EXCLUDED.precincts_total,
                    expected_vote_pct = EXCLUDED.expected_vote_pct, result_stage = EXCLUDED.result_stage,
                    is_certified = EXCLUDED.is_certified, certification_id = EXCLUDED.certification_id,
                    source_id = EXCLUDED.source_id, source_record_id = EXCLUDED.source_record_id,
                    source_reported_at = EXCLUDED.source_reported_at,
                    verification_status = EXCLUDED.verification_status
                """,
                (contest_id, candidate_id, obs.choice_label, unit_id, obs.votes, obs.vote_percentage,
                 obs.precincts_reporting, obs.precincts_total, obs.expected_vote_pct, obs.result_stage,
                 certified, cert["id"] if certified else None, run.source_id, raw_record_id,
                 obs.source_reported_at, verification),
            )
            stats["current_updated"] += 1
    return stats


def _may_replace(existing: dict[str, Any], obs: ResultObservation, incoming_official: bool,
                 incoming_source_id: int) -> bool:
    incoming_certified = obs.result_stage in CERTIFIED_STAGES
    if existing["is_certified"] and not incoming_certified:
        return False  # never replace a certified value with an uncertified one
    existing_official = existing["authority_level"] == "official_election_authority"
    if existing_official and not incoming_official:
        return False  # third-party data never overrides the election authority
    if STAGE_RANK.get(obs.result_stage, 0) < STAGE_RANK.get(existing["result_stage"], 0):
        return False  # no stage downgrades (e.g. canvassed -> preliminary)
    if obs.source_reported_at and existing["source_reported_at"] and existing["source_id"] == incoming_source_id:
        incoming = obs.source_reported_at
        if isinstance(incoming, str):
            incoming = datetime.fromisoformat(incoming)
        if incoming.tzinfo is None:
            incoming = incoming.replace(tzinfo=existing["source_reported_at"].tzinfo)
        if incoming < existing["source_reported_at"]:
            return False  # stale snapshot from the same source arriving out of order
    return True


def certify_contest(conn: psycopg.Connection, contest_key: str, state_code: str, certifying_authority: str,
                    certified_at: datetime | str, document_url: str, document_sha256: str | None = None,
                    notes: str | None = None, status: str = "certified") -> int:
    """Record an official certification. Source must be the state's election authority."""
    contest_id = _contest_id(conn, contest_key)
    if contest_id is None:
        raise ValueError(f"Unknown contest {contest_key}")
    if not document_url:
        raise ValueError("Certification requires an official document URL")
    source_id = ensure_source(conn, f"state:{state_code}")
    cert_id = conn.execute(
        """
        INSERT INTO result_certifications (contest_id, certifying_authority, certified_at, document_url,
                                           document_sha256, source_id, status, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (contest_id, certifying_authority, certified_at, document_url, document_sha256, source_id, status, notes),
    ).fetchone()["id"]
    conn.execute("UPDATE contests SET certification_status = 'certified' WHERE id = %s", (contest_id,))
    conn.execute("UPDATE elections SET status = 'canvassing' WHERE id = (SELECT election_id FROM contests "
                 "WHERE id = %s) AND status IN ('scheduled', 'in_progress', 'counting')", (contest_id,))
    return cert_id


def record_race_call(conn: psycopg.Connection, run: IngestionRun, contest_key: str, caller: str,
                     called_at: datetime | str, winner_label: str, winner_fec_id: str | None = None,
                     raw_record_id: int | None = None) -> int | None:
    """A projection by a media/data organization. Does not affect certification."""
    contest_id = _contest_id(conn, contest_key)
    if contest_id is None:
        run.record_failure(f"race call for unknown contest {contest_key}")
        return None
    obs = ResultObservation("", contest_key, winner_label, None, "", candidate_fec_id=winner_fec_id)
    winner = _candidate_id(conn, contest_id, obs)
    row = conn.execute(
        """
        INSERT INTO race_calls (contest_id, caller, winner_candidate_id, winner_label, called_at, source_id,
                                source_record_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (contest_id, caller, called_at) DO NOTHING RETURNING id
        """,
        (contest_id, caller, winner, winner_label, called_at, run.source_id, raw_record_id),
    ).fetchone()
    return row["id"] if row else None


def record_recount(conn: psycopg.Connection, contest_key: str, state_code: str, recount_type: str, status: str,
                   document_url: str | None = None, **fields: Any) -> int:
    contest_id = _contest_id(conn, contest_key)
    if contest_id is None:
        raise ValueError(f"Unknown contest {contest_key}")
    source_id = ensure_source(conn, f"state:{state_code}")
    rid = conn.execute(
        """
        INSERT INTO recounts (contest_id, recount_type, status, requested_by, started_on, completed_on,
                              margin_before, margin_after, document_url, source_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (contest_id, recount_type, status, fields.get("requested_by"), fields.get("started_on"),
         fields.get("completed_on"), fields.get("margin_before"), fields.get("margin_after"), document_url, source_id),
    ).fetchone()["id"]
    if status in ("announced", "in_progress"):
        conn.execute("UPDATE contests SET certification_status = 'recount' WHERE id = %s", (contest_id,))
    return rid
