"""Supplemental third-party data: civicAPI results/race calls, polls, EAC/state voter statistics.

All third-party records are stored with verification_status 'unverified'.
They can never certify a result (database trigger) and never overwrite an
official-authority value (results.record_results).

civicAPI field mappings live in config/sources.yaml so they can be filled in
from the provider's current documentation without code changes. Mapping keys:

  results:  records_path, state, office, district, choice, votes, percent,
            reporting_unit, precincts_reporting, precincts_total, reported_at,
            winner_flag (optional, for race calls), caller (optional label)
  polls:    records_path, external_id, pollster, sponsor, population, sample_size,
            field_start, field_end, margin_of_error, methodology, state, office,
            district, url, answers_path, answer_choice, answer_pct
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from election_data import reference
from election_data.clients.civicapi import CivicAPIClient
from election_data.clients.csv_source import iter_records, read_table
from election_data.ingestion.results import ResultObservation, contest_key_for, record_race_call, record_results
from election_data.jobs.validation import validate_poll
from election_data.provenance import IngestionRun, content_hash

log = logging.getLogger(__name__)


def dig(obj: Any, path: str | None) -> Any:
    """Follow a dotted path ('data.races') through nested dicts."""
    if not path:
        return obj
    for part in path.split("."):
        if obj is None:
            return None
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def _state(value: Any) -> str | None:
    code = (str(value or "")).strip().upper()
    if code in reference.STATES:
        return code
    by_name = {name.upper(): c for c, (name, _, _) in reference.STATES.items()}
    return by_name.get(code)


# ---------------------------------------------------------------------------
# civicAPI results and race calls
# ---------------------------------------------------------------------------
def ingest_civicapi_results(conn: psycopg.Connection, client: CivicAPIClient | None = None,
                            params: dict[str, Any] | None = None, max_pages: int | None = None) -> IngestionRun:
    """Page through civicAPI race/search and store every candidate line as a preliminary observation.

    civicAPI returns one record per race with a nested candidate list. Race-type labels are
    mapped to project offices through sources.civicapi.office_types; unmapped types (local
    races, ballot measures, offices not yet modeled) are counted and skipped.
    """
    client = client or CivicAPIClient()
    fmap = client.field_map("results")
    office_types = {k.lower(): v for k, v in (client.cfg.get("office_types") or {}).items()}
    query_cfg = (client.cfg.get("query") or {}).get("results", {})
    # A query is either one parameter set or a list of {params, per_state} entries; per_state
    # entries are repeated with province=<code> for all 50 states + DC (one request each).
    queries: list[dict[str, Any]] = []
    for q in (query_cfg if isinstance(query_cfg, list) else [{"params": query_cfg}]):
        base = {**q.get("params", {}), **(params or {})}
        if q.get("per_state"):
            queries += [{**base, "province": code} for code in reference.STATES]
        else:
            queries.append(base)
    page_size = int(client.cfg.get("page_size", 100))
    unmapped: dict[str, int] = {}
    with IngestionRun(conn, "civicapi", "live_results", {"queries": len(queries)}) as run:
        for query in queries:
            offset, pages = 0, 0
            while True:
                try:
                    data, url = client.fetch_named("results", {**query, "limit": page_size, "offset": offset})
                except Exception as exc:  # one failed request (e.g. a 502) must not lose the rest of the cycle
                    run.record_failure(f"civicAPI {query.get('province') or query.get('election_type')}: {exc}")
                    break
                races = dig(data, fmap.get("records_path")) or []
                for race in races:
                    raw = run.store_raw("civicapi_race", race, str(dig(race, fmap.get("race_id")) or ""), url)
                    office = office_types.get(str(dig(race, fmap["office"]) or "").lower())
                    state = _state(dig(race, fmap["state"]))
                    if office is None or state is None:
                        label = str(dig(race, fmap["office"]))
                        unmapped[label] = unmapped.get(label, 0) + 1
                        continue
                    try:
                        observations = list(_civicapi_race_observations(race, fmap, office, state, url))
                    except (KeyError, ValueError) as exc:
                        run.record_failure(f"civicAPI race {dig(race, fmap.get('race_id'))}: {exc}")
                        continue
                    record_results(conn, run, observations, raw.id)
                    _corroborate_contest(conn, observations)
                    for obs in observations:
                        if obs.extra.get("winner"):
                            # civicAPI exposes no call timestamp; the first time we observe the call is used.
                            record_race_call(conn, run, obs.contest_key, fmap.get("caller", "civicAPI"),
                                             datetime.now(timezone.utc).replace(microsecond=0, second=0),
                                             obs.choice_label, raw_record_id=raw.id)
                conn.commit()
                pages += 1
                if len(races) < page_size or (max_pages and pages >= max_pages):
                    break
                offset += page_size
        if unmapped:
            top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:15]
            log.info("civicAPI race types not mapped to modeled offices: %s", top)
    return run


def _civicapi_race_observations(race: dict[str, Any], fmap: dict[str, str], office: str, state: str,
                                url: str) -> Iterable[ResultObservation]:
    for required in ("state", "office", "candidates_path", "choice", "votes"):
        if not fmap.get(required):
            raise KeyError(f"field_maps.results.{required} is not configured")
    key = contest_key_for(office, state, _district_number(dig(race, fmap.get("district"))))
    pct_reporting = dig(race, fmap.get("percent_reporting"))
    for cand in dig(race, fmap["candidates_path"]) or []:
        votes = dig(cand, fmap["votes"])
        yield ResultObservation(
            state_code=state, contest_key=key, choice_label=str(dig(cand, fmap["choice"])).strip(),
            votes=int(votes) if votes not in (None, "") else None, source_url=url,
            result_stage="preliminary",  # third-party feeds are never treated as certified
            vote_percentage=dig(cand, fmap.get("percent")),
            expected_vote_pct=pct_reporting,
            source_reported_at=dig(race, fmap.get("reported_at")) if fmap.get("reported_at") else None,
            party=dig(cand, fmap.get("party")) if fmap.get("party") else None,
            extra={"winner": bool(dig(cand, fmap.get("winner_flag"))) if fmap.get("winner_flag") else False},
        )


def _district_number(value: Any) -> str | None:
    """'GA-04' -> '04'; 'WY-AL' / at-large -> '00'; None stays None (statewide races)."""
    if value in (None, ""):
        return None
    text = str(value).upper()
    if text.endswith("-AL") or "AT-LARGE" in text or text == "AL":
        return "00"
    digits = re.sub(r"\D", "", text)
    return digits.zfill(2) if digits else None


def _corroborate_contest(conn: psycopg.Connection, observations: list[ResultObservation]) -> None:
    """A race listed by civicAPI corroborates that the seeded contest exists (not its results)."""
    if observations:
        conn.execute("UPDATE contests SET verification_status = 'corroborated' "
                     "WHERE contest_key = %s AND verification_status = 'unverified'",
                     (observations[0].contest_key,))


# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------
def _poll_from_record(record: dict[str, Any], fmap: dict[str, str]) -> dict[str, Any]:
    answers = dig(record, fmap.get("answers_path")) or []
    results = {}
    for a in answers if isinstance(answers, list) else []:
        choice, pct = dig(a, fmap.get("answer_choice", "choice")), dig(a, fmap.get("answer_pct", "pct"))
        if choice is not None and pct not in (None, ""):
            results[str(choice)] = float(pct)
    population = (dig(record, fmap.get("population")) or "")
    return {
        "external_id": dig(record, fmap.get("external_id")),
        "pollster": dig(record, fmap.get("pollster")),
        "sponsor": dig(record, fmap.get("sponsor")),
        "population": str(population).lower() or None,
        "sample_size": dig(record, fmap.get("sample_size")),
        "field_start": dig(record, fmap.get("field_start")),
        "field_end": dig(record, fmap.get("field_end")),
        "margin_of_error": dig(record, fmap.get("margin_of_error")),
        "methodology": dig(record, fmap.get("methodology")),
        "state": _state(dig(record, fmap.get("state"))),
        "office": dig(record, fmap.get("office")),
        "district": dig(record, fmap.get("district")),
        "url": dig(record, fmap.get("url")),
        "results": results,
    }


def store_polls(conn: psycopg.Connection, run: IngestionRun, records: Iterable[dict[str, Any]],
                fmap: dict[str, str], source_url: str) -> None:
    for record in records:
        raw = run.store_raw("poll", record, str(dig(record, fmap.get("external_id")) or ""), source_url)
        poll = _poll_from_record(record, fmap)
        problems = validate_poll(poll)
        if problems:
            run.record_failure(f"poll {poll.get('external_id')}: {problems}")
            continue
        contest_id, label = None, " ".join(str(x) for x in (poll["state"], poll["office"], poll["district"]) if x)
        if poll["state"] and poll["office"]:
            try:
                key = contest_key_for(str(poll["office"]), poll["state"], poll["district"])
                row = conn.execute("SELECT id FROM contests WHERE contest_key = %s", (key,)).fetchone()
                contest_id = row["id"] if row else None
            except ValueError:
                pass
        poll_key = f"{run.source_key}:{poll['external_id'] or content_hash(record)}"
        with conn.transaction():
            poll_id = conn.execute(
                """
                INSERT INTO polls (poll_key, contest_id, contest_label, state_code, pollster, sponsor, population,
                                   sample_size, field_start, field_end, margin_of_error, methodology, results,
                                   source_url, source_record_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (poll_key) DO UPDATE SET results = EXCLUDED.results,
                    contest_id = COALESCE(EXCLUDED.contest_id, polls.contest_id),
                    source_record_id = EXCLUDED.source_record_id
                RETURNING id
                """,
                (poll_key, contest_id, label or None, poll["state"], poll["pollster"], poll["sponsor"],
                 poll["population"], int(poll["sample_size"]), str(poll["field_start"])[:10],
                 str(poll["field_end"])[:10], poll["margin_of_error"], poll["methodology"], Jsonb(poll["results"]),
                 poll["url"] or source_url, raw.id),
            ).fetchone()["id"]
            for choice, pct in poll["results"].items():
                conn.execute(
                    """INSERT INTO poll_answers (poll_id, choice, pct) VALUES (%s, %s, %s)
                       ON CONFLICT (poll_id, choice) DO UPDATE SET pct = EXCLUDED.pct""",
                    (poll_id, choice, pct),
                )


def ingest_civicapi_polls(conn: psycopg.Connection, client: CivicAPIClient | None = None,
                          params: dict[str, Any] | None = None) -> IngestionRun:
    client = client or CivicAPIClient()
    fmap = client.field_map("polls")
    with IngestionRun(conn, "civicapi", "polls", {"params": params or {}}) as run:
        data, url = client.fetch_named("polls", params)
        store_polls(conn, run, dig(data, fmap.get("records_path")) or [], fmap, url)
    return run


def ingest_polls_csv(conn: psycopg.Connection, path: str, fmap: dict[str, str], source_key: str,
                     source_url: str) -> IngestionRun:
    """Load a pollster's published CSV. One row per poll; answers in columns listed in fmap['answer_columns']."""
    frame = read_table(Path(path))
    answer_cols = fmap.get("answer_columns") or []
    with IngestionRun(conn, source_key, "polls", {"file": path}) as run:
        rows = []
        for row in iter_records(frame):
            row["_answers"] = [{"choice": c, "pct": row.get(c)} for c in answer_cols]
            rows.append(row)
        store_polls(conn, run, rows, {**fmap, "answers_path": "_answers"}, source_url)
    return run


# ---------------------------------------------------------------------------
# Voter statistics (EAC EAVS / state registration reports): aggregate only
# ---------------------------------------------------------------------------
def ingest_voter_statistics_csv(conn: psycopg.Connection, path: str, source_key: str, source_url: str,
                                metric_columns: dict[str, str], state_column: str,
                                reporting_period: str, election_id: int | None = None) -> IngestionRun:
    """Load aggregate statistics; metric_columns maps file column -> metric name."""
    frame = read_table(Path(path))
    with IngestionRun(conn, source_key, "voter_statistics", {"file": path, "period": reporting_period}) as run:
        for row in iter_records(frame):
            state = _state(row.get(state_column))
            if state is None:
                continue
            raw = run.store_raw("voter_statistics_row", row, None, source_url)
            for column, metric in metric_columns.items():
                value = row.get(column)
                if value in (None, "", "-", "N/A"):
                    continue
                try:
                    number = float(str(value).replace(",", ""))
                except ValueError:
                    run.record_failure(f"{state} {column}: not numeric {value!r}")
                    continue
                if number < 0:  # EAVS uses negative codes for 'data not available'
                    continue
                conn.execute(
                    """
                    INSERT INTO voter_statistics (election_id, state_code, metric, metric_value, reporting_period,
                                                  source_id, source_record_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (election_id, state_code, jurisdiction_id, metric, reporting_period, source_id)
                    DO UPDATE SET metric_value = EXCLUDED.metric_value, source_record_id = EXCLUDED.source_record_id
                    """,
                    (election_id, state, metric, number, reporting_period, run.source_id, raw.id),
                )
    return run
