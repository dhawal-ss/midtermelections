"""Seed states, the state source registry, districts, offices, parties and 2026 contests."""
from __future__ import annotations

import logging
from datetime import date

import psycopg

from election_data import config, reference
from election_data.provenance import ensure_source, register_all_sources

log = logging.getLogger(__name__)


def seed_states(conn: psycopg.Connection) -> int:
    for code, (name, fips, seats) in reference.STATES.items():
        conn.execute(
            """
            INSERT INTO states (state_code, state_name, fips_code, is_state, house_seats, has_voting_house_rep)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (state_code) DO UPDATE SET state_name = EXCLUDED.state_name,
                fips_code = EXCLUDED.fips_code, house_seats = EXCLUDED.house_seats,
                is_state = EXCLUDED.is_state, has_voting_house_rep = EXCLUDED.has_voting_house_rep
            """,
            (code, name, fips, code != "DC", seats, code != "DC"),
        )
        conn.execute(
            """INSERT INTO jurisdictions (state_code, name, jurisdiction_type, fips_code, ocd_id)
               VALUES (%s, %s, 'state', %s, %s) ON CONFLICT DO NOTHING""",
            (code, name, fips,
             "ocd-division/country:us/district:dc" if code == "DC" else f"ocd-division/country:us/state:{code.lower()}"),
        )
    return len(reference.STATES)


def seed_state_registry(conn: psycopg.Connection) -> int:
    rows = config.state_registry()
    for s in rows:
        conn.execute(
            """
            INSERT INTO state_source_registry (state_code, election_office_name, official_election_url,
                results_url, candidates_url, voter_lookup_url, results_adapter, adapter_status, notes, updated_at)
            VALUES (%(code)s, %(office)s, %(official_election_url)s, %(results_url)s, %(candidates_url)s,
                    %(voter_lookup_url)s, %(results_adapter)s, %(adapter_status)s, %(notes)s, NOW())
            ON CONFLICT (state_code) DO UPDATE SET
                election_office_name = EXCLUDED.election_office_name,
                official_election_url = EXCLUDED.official_election_url,
                results_url = EXCLUDED.results_url, candidates_url = EXCLUDED.candidates_url,
                voter_lookup_url = EXCLUDED.voter_lookup_url, results_adapter = EXCLUDED.results_adapter,
                adapter_status = EXCLUDED.adapter_status, notes = EXCLUDED.notes, updated_at = NOW()
            """,
            {**{k: None for k in ("results_url", "candidates_url", "voter_lookup_url", "results_adapter", "notes")},
             **s},
        )
    return len(rows)


def seed_parties(conn: psycopg.Connection) -> None:
    for code, name in reference.PARTIES.items():
        conn.execute("INSERT INTO parties (party_code, party_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (code, name))


def ensure_election(conn: psycopg.Connection, name: str, election_date: date, election_type: str,
                    state_code: str | None = None, source_id: int | None = None,
                    verification_status: str = "unverified") -> int:
    row = conn.execute(
        """
        INSERT INTO elections (name, election_year, election_date, election_type, state_code, source_id,
                               verification_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (election_date, election_type, state_code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (name, election_date.year, election_date, election_type, state_code, source_id, verification_status),
    ).fetchone()
    return row["id"]


def ensure_district(conn: psycopg.Connection, state: str, district_type: str, number: str,
                    valid_from: date = date(2023, 1, 3), plan_name: str = "current") -> int:
    fips = reference.STATES[state][1]
    geo_id = f"{fips}{number}" if district_type == "congressional" else None
    row = conn.execute(
        """
        INSERT INTO districts (state_code, district_type, district_number, plan_name, geo_id, valid_from)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (state_code, district_type, district_number, valid_from)
            DO UPDATE SET plan_name = EXCLUDED.plan_name
        RETURNING id
        """,
        (state, district_type, number, plan_name, geo_id, valid_from),
    ).fetchone()
    return row["id"]


def ensure_office(conn: psycopg.Connection, office_code: str, name: str, level: str, branch: str | None,
                  state: str | None, district_id: int | None = None, seat_class: str | None = None) -> int:
    row = conn.execute(
        """
        INSERT INTO offices (office_code, name, office_level, branch, state_code, district_id, seat_class)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (office_code) DO UPDATE SET name = EXCLUDED.name,
            district_id = COALESCE(EXCLUDED.district_id, offices.district_id),
            seat_class = COALESCE(EXCLUDED.seat_class, offices.seat_class)
        RETURNING id
        """,
        (office_code, name, level, branch, state, district_id, seat_class),
    ).fetchone()
    return row["id"]


def ensure_contest(conn: psycopg.Connection, key: str, election_id: int, office_id: int, state: str,
                   stage: str = "general", is_special: bool = False, party: str | None = None,
                   source_id: int | None = None) -> int:
    row = conn.execute(
        """
        INSERT INTO contests (contest_key, election_id, office_id, state_code, stage, is_special, party, source_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (contest_key) DO UPDATE SET election_id = EXCLUDED.election_id
        RETURNING id
        """,
        (key, election_id, office_id, state, stage, is_special, party, source_id),
    ).fetchone()
    return row["id"]


def house_office(conn: psycopg.Connection, state: str, district: str) -> int:
    district_id = ensure_district(conn, state, "congressional", district)
    label = "Delegate" if state == "DC" else "Representative"
    suffix = "At-Large" if district == "00" else f"District {int(district)}"
    return ensure_office(conn, reference.house_office_code(state, district),
                         f"U.S. House — {state} {suffix} ({label})", "federal", "legislative", state, district_id)


def senate_office(conn: psycopg.Connection, state: str) -> int:
    seat_class = reference.senate_seat_class(state)
    return ensure_office(conn, reference.senate_office_code(state), f"U.S. Senate — {state}"
                         + (f" (Class {seat_class})" if seat_class else ""),
                         "federal", "legislative", state, seat_class=seat_class)


def seed_2026_general_contests(conn: psycopg.Connection) -> dict[str, int]:
    """Create the expected 2026 general-election federal + gubernatorial contests."""
    source_id = ensure_source(conn, "project_reference")
    general = ensure_election(conn, "2026 United States General Election", config.GENERAL_ELECTION_DATE,
                              "general", None, source_id)
    counts = {"house": 0, "senate": 0, "governor": 0}
    year = config.ELECTION_YEAR

    for state in reference.STATES:
        ensure_district(conn, state, "statewide", "00")
        for number in reference.house_district_numbers(state):
            office_id = house_office(conn, state, number)
            ensure_contest(conn, reference.contest_key(year, "general", reference.house_office_code(state, number)),
                           general, office_id, state, source_id=source_id)
            if state != "DC":
                counts["house"] += 1

    for state in reference.SENATE_2026_REGULAR + reference.SENATE_2026_SPECIAL:
        special = state in reference.SENATE_2026_SPECIAL
        office_id = senate_office(conn, state)
        stage = "special_general" if special else "general"
        ensure_contest(conn, reference.contest_key(year, stage, reference.senate_office_code(state)),
                       general, office_id, state, stage=stage, is_special=special, source_id=source_id)
        counts["senate"] += 1

    for state in reference.GOVERNOR_2026:
        office_id = ensure_office(conn, reference.governor_office_code(state), f"Governor — {state}",
                                  "state", "executive", state)
        ensure_contest(conn, reference.contest_key(year, "general", reference.governor_office_code(state)),
                       general, office_id, state, source_id=source_id)
        counts["governor"] += 1

    log.info("Seeded 2026 general contests: %s", counts)
    return counts


def seed_all(conn: psycopg.Connection) -> dict[str, int]:
    result = {
        "sources": register_all_sources(conn),
        "states": seed_states(conn),
        "state_registry": seed_state_registry(conn),
    }
    seed_parties(conn)
    result.update(seed_2026_general_contests(conn))
    conn.commit()
    return result
