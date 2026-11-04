"""Integration tests against a real PostgreSQL (TEST_DATABASE_URL)."""
from __future__ import annotations

import psycopg
import pytest

from election_data.adapters.base import AdapterNotImplemented, get_adapter
from election_data.ingestion.candidates import ingest_fec_candidates
from election_data.ingestion.demographics import ingest_census_demographics
from election_data.ingestion.finance import ingest_candidate_totals, ingest_committees, ingest_contributions
from election_data.ingestion.historical import load_mit_frame
from election_data.ingestion.results import (
    ResultObservation, certify_contest, contest_key_for, record_race_call, record_results,
)
from election_data.ingestion.state_results import ingest_state_results
from election_data.jobs.reports import write_report
from election_data.jobs.validation import run_verification
from election_data.provenance import IngestionRun
from election_data.config import PROJECT_ROOT
from tests.fakes import FakeCensus, FakeFEC

GA_SENATE = "2026:general:US-SENATE-GA-II"


def one(conn, sql, params=()):
    return list(conn.execute(sql, params).fetchone().values())[0]


# -- seeding ----------------------------------------------------------------
def test_seed_creates_full_2026_map(conn):
    assert one(conn, "SELECT COUNT(*) FROM states") == 51
    assert one(conn, "SELECT COUNT(*) FROM state_source_registry") == 51
    assert one(conn, "SELECT COUNT(*) FROM contests c JOIN offices o ON o.id=c.office_id "
                     "WHERE o.office_code LIKE 'US-HOUSE-%%' AND c.state_code <> 'DC'") == 435
    assert one(conn, "SELECT COUNT(*) FROM contests c JOIN offices o ON o.id=c.office_id "
                     "WHERE o.office_code LIKE 'US-SENATE-%%'") == 35
    assert one(conn, "SELECT COUNT(*) FROM contests WHERE is_special") == 2
    assert one(conn, "SELECT COUNT(*) FROM contests c JOIN offices o ON o.id=c.office_id "
                     "WHERE o.office_code LIKE 'GOV-%%'") == 36


def test_seed_is_idempotent(conn):
    from election_data.ingestion.reference_data import seed_all
    before = one(conn, "SELECT COUNT(*) FROM contests")
    seed_all(conn)
    assert one(conn, "SELECT COUNT(*) FROM contests") == before


# -- provenance ---------------------------------------------------------------
def test_raw_records_are_immutable_and_deduplicated(conn):
    with IngestionRun(conn, "openfec", "test") as run:
        a = run.store_raw("t", {"x": 1, "y": [1, 2]}, "e1", "https://api.open.fec.gov/v1/x/?api_key=SECRET")
        b = run.store_raw("t", {"y": [1, 2], "x": 1}, "e1")  # same content, different key order
    assert a.id == b.id and a.is_new and not b.is_new
    assert "SECRET" not in one(conn, "SELECT source_url FROM raw_records WHERE id=%s", (a.id,))
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE raw_records SET payload='{}' WHERE id=%s", (a.id,))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM raw_records WHERE id=%s", (a.id,))
    conn.rollback()


def test_failed_run_is_recorded(conn):
    with pytest.raises(RuntimeError):
        with IngestionRun(conn, "openfec", "boom"):
            raise RuntimeError("provider down")
    row = conn.execute("SELECT status, error_message FROM ingestion_runs WHERE job_name='boom'").fetchone()
    assert row["status"] == "failed" and "provider down" in row["error_message"]


def test_failed_run_message_never_stores_api_key(conn):
    with pytest.raises(RuntimeError):
        with IngestionRun(conn, "openfec", "leak"):
            raise RuntimeError("GET https://api.open.fec.gov/v1/candidates/?api_key=REALKEY123 failed")
    msg = one(conn, "SELECT error_message FROM ingestion_runs WHERE job_name='leak'")
    assert "REALKEY123" not in msg and "api_key=REDACTED" in msg


# -- candidates & finance -------------------------------------------------------
def test_fec_candidate_ingestion_and_idempotency(conn):
    fake = FakeFEC()
    run = ingest_fec_candidates(conn, client=fake)
    assert run.failed == 1  # the malformed record
    assert one(conn, "SELECT COUNT(*) FROM candidates") == 6
    # paper filings for seats that do not exist keep identity + a discrepancy, but no phantom contest
    assert one(conn, "SELECT COUNT(*) FROM contests WHERE contest_key LIKE '%%UNKNOWN%%' "
                     "OR contest_key LIKE '%%SC-86%%'") == 0
    assert one(conn, "SELECT COUNT(*) FROM data_discrepancies WHERE discrepancy_type IN "
                     "('senate_seat_not_up_in_2026', 'district_not_in_apportionment')") == 2
    # election_year filter is always requested so 2028/2030 filers are excluded
    assert all(kw["election_year"] == 2026 for _, kw in fake.calls)

    link = conn.execute(
        """SELECT cc.filing_status, cc.ballot_status, c.canonical_name, c.incumbent
           FROM contest_candidates cc JOIN contests k ON k.id=cc.contest_id JOIN candidates c ON c.id=cc.candidate_id
           WHERE k.contest_key=%s AND c.last_name='Doe'""", (GA_SENATE,)).fetchone()
    # FEC registration is not ballot qualification
    assert link == {"filing_status": "filed_with_fec", "ballot_status": "unknown",
                    "canonical_name": "Jane A. Doe", "incumbent": True}
    assert one(conn, "SELECT COUNT(*) FROM contest_candidates cc JOIN contests k ON k.id=cc.contest_id "
                     "WHERE k.contest_key='2026:special_general:US-SENATE-OH-III'") == 1

    ingest_fec_candidates(conn, client=FakeFEC())
    assert one(conn, "SELECT COUNT(*) FROM candidates") == 6
    assert one(conn, "SELECT COUNT(*) FROM contest_candidates") == 4
    assert one(conn, "SELECT COUNT(*) FROM raw_records WHERE record_type='fec_candidate'") == 7


def test_ballot_qualification_requires_evidence(conn):
    ingest_fec_candidates(conn, client=FakeFEC())
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE contest_candidates SET ballot_status='qualified'")
    conn.rollback()


def test_finance_pipeline(conn):
    fake = FakeFEC()
    ingest_fec_candidates(conn, client=fake)
    ingest_committees(conn, client=fake)
    totals = ingest_candidate_totals(conn, client=fake)
    assert totals.failed == 0  # out-of-scope candidates are kept as raw evidence, not failures
    assert totals.received == 2
    run = ingest_contributions(conn, client=fake)
    assert run.failed == 0
    row = conn.execute("SELECT amount, contribution_date::text AS d, file_number, image_number, candidate_id "
                       "FROM contributions WHERE fec_sub_id='4012345678901'").fetchone()
    assert float(row["amount"]) == 250.0 and row["d"] == "2026-09-15"
    assert row["file_number"] == "1876543" and row["candidate_id"] is not None
    ingest_contributions(conn, client=fake)
    assert one(conn, "SELECT COUNT(*) FROM contributions") == 2
    assert float(one(conn, "SELECT receipts FROM financial_summaries")) == 1000000.5


# -- results & certification --------------------------------------------------
def obs(**kw):  # noqa: D401 - test helper
    base = dict(state_code="GA", contest_key=GA_SENATE, choice_label="Jane A. Doe", votes=1000,
                source_url="https://results.sos.ga.gov/x.json", result_stage="preliminary")
    base.update(kw)
    return ResultObservation(**base)


def test_preliminary_results_snapshot_and_label(conn):
    ingest_fec_candidates(conn, client=FakeFEC())
    with IngestionRun(conn, "state:GA", "results_test") as run:
        record_results(conn, run, [obs(votes=1000), obs(choice_label="Richard Roe", votes=900)])
        record_results(conn, run, [obs(votes=1500)])
    assert one(conn, "SELECT COUNT(*) FROM election_result_snapshots") == 3
    row = conn.execute("SELECT * FROM v_results_labeled WHERE choice_label='Jane A. Doe'").fetchone()
    assert row["votes"] == 1500 and not row["is_certified"]
    assert row["display_label"] == "PRELIMINARY — NOT CERTIFIED"
    # candidate linked by exact name within the contest
    assert one(conn, "SELECT candidate_id FROM election_results WHERE choice_label='Jane A. Doe'") is not None


def test_cannot_store_certified_without_certification(conn):
    with IngestionRun(conn, "state:GA", "results_test") as run:
        stats = record_results(conn, run, [obs(result_stage="certified")])
    assert stats["rejected"] == 1 and one(conn, "SELECT COUNT(*) FROM election_results") == 0
    # database refuses even if application checks are bypassed (trigger and CHECK constraint)
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.RaiseException), match="certif"):
        conn.execute(
            """INSERT INTO election_results (contest_id, choice_label, reporting_unit_id, votes, result_stage,
                   is_certified, source_id, verification_status)
               SELECT c.id, 'X', (SELECT MIN(id) FROM reporting_units), 1, 'certified', TRUE,
                      (SELECT id FROM data_sources WHERE source_key='state:GA'), 'certified'
               FROM contests c WHERE contest_key=%s""", (GA_SENATE,))
    conn.rollback()


def test_third_party_cannot_certify(conn):
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute(
            """INSERT INTO result_certifications (contest_id, certifying_authority, certified_at, document_url, source_id)
               SELECT c.id, 'Some aggregator', NOW(), 'https://x', (SELECT id FROM data_sources WHERE source_key='civicapi')
               FROM contests c WHERE contest_key=%s""", (GA_SENATE,))
    conn.rollback()
    with IngestionRun(conn, "civicapi", "results_test") as run:
        stats = record_results(conn, run, [obs(result_stage="certified")])
    assert stats["rejected"] == 1


def test_certification_flow_and_precedence(conn):
    with IngestionRun(conn, "state:GA", "r") as run:
        record_results(conn, run, [obs(votes=1000)])
    certify_contest(conn, GA_SENATE, "GA", "Georgia Secretary of State", "2026-11-21T17:00:00-05:00",
                    "https://sos.ga.gov/certification.pdf", "abc123")
    with IngestionRun(conn, "state:GA", "r") as run:
        record_results(conn, run, [obs(votes=1012, result_stage="certified")])
    row = conn.execute("SELECT * FROM v_results_labeled").fetchone()
    assert row["is_certified"] and row["votes"] == 1012 and row["display_label"] == "CERTIFIED"
    assert row["certification_document"] == "https://sos.ga.gov/certification.pdf"
    assert one(conn, "SELECT certification_status FROM contests WHERE contest_key=%s", (GA_SENATE,)) == "certified"

    # A later preliminary or third-party value cannot replace the certified one
    with IngestionRun(conn, "civicapi", "r") as run:
        stats = record_results(conn, run, [obs(votes=999)])
    assert stats["kept_existing"] == 1
    assert one(conn, "SELECT votes FROM election_results") == 1012
    assert one(conn, "SELECT COUNT(*) FROM data_discrepancies WHERE discrepancy_type='competing_result_value'") == 1


def test_third_party_does_not_override_official(conn):
    with IngestionRun(conn, "state:GA", "r") as run:
        record_results(conn, run, [obs(votes=1000)])
    with IngestionRun(conn, "civicapi", "r") as run:
        record_results(conn, run, [obs(votes=1100)])
    assert one(conn, "SELECT votes FROM election_results") == 1000
    assert one(conn, "SELECT COUNT(*) FROM election_result_snapshots") == 2
    summary = run_verification(conn)
    assert summary["cross_source_mismatches"] == 1


def test_race_call_is_not_certification(conn):
    with IngestionRun(conn, "civicapi", "calls") as run:
        record_race_call(conn, run, GA_SENATE, "civicAPI", "2026-11-04T01:30:00Z", "Jane A. Doe")
    assert one(conn, "SELECT COUNT(*) FROM race_calls") == 1
    assert one(conn, "SELECT certification_status FROM contests WHERE contest_key=%s", (GA_SENATE,)) == "unknown"


def test_generic_csv_state_adapter(conn, tmp_path):
    csv = tmp_path / "ga.csv"
    csv.write_text(
        "Contest Name,District,Candidate Name,Total Votes,County,Precincts Reported,Total Precincts\n"
        "US Senate,,Jane A. Doe,\"1,200\",Fulton,10,20\n"
        "US Senate,,Richard Roe,800,Fulton,10,20\n"
        "Representative in Congress,District 5,Someone,500,Fulton,3,9\n"
        "County Commission,,Local Person,50,Fulton,1,1\n")
    mapping = tmp_path / "m.yaml"
    mapping.write_text((PROJECT_ROOT / "examples" / "state_results_mapping.yaml").read_text())
    run = ingest_state_results(conn, "GA", str(csv), str(mapping))
    assert run.failed == 0
    rows = conn.execute("SELECT contest_key, choice_label, votes, reporting_unit, display_label "
                        "FROM v_results_labeled ORDER BY votes DESC").fetchall()
    assert rows[0]["votes"] == 1200 and rows[0]["reporting_unit"] == "Fulton"
    assert {r["contest_key"] for r in rows} == {GA_SENATE, "2026:general:US-HOUSE-GA-05"}


def test_unimplemented_adapter_is_explicit():
    with pytest.raises(AdapterNotImplemented):
        get_adapter({"code": "AL", "results_adapter": None, "adapter_status": "not_implemented"})


def test_contest_key_mapping():
    assert contest_key_for("US Senate", "OH") == "2026:special_general:US-SENATE-OH-III"
    assert contest_key_for("U.S. House", "WY", "1") == "2026:general:US-HOUSE-WY-01"
    assert contest_key_for("Governor", "GA") == "2026:general:GOV-GA"


# -- context data -------------------------------------------------------------
def test_census_ingestion(conn):
    ingest_census_demographics(conn, client=FakeCensus())
    assert one(conn, "SELECT value FROM district_demographics WHERE geo_id='13' AND variable='B01003_001E'") == 11029227
    # sentinel -666666666 skipped; Puerto Rico skipped; DC delegate '98' -> '00'
    assert one(conn, "SELECT COUNT(*) FROM district_demographics WHERE variable='B19013_001E'") == 0
    assert one(conn, "SELECT COUNT(*) FROM district_demographics WHERE geo_id='1100'") == 1


def test_historical_load(conn):
    import pandas as pd
    frame = pd.DataFrame([
        {"year": "2022", "state_po": "GA", "office": "US HOUSE", "district": "5", "stage": "GEN",
         "special": "FALSE", "runoff": "FALSE", "candidate": "A", "party": "DEMOCRAT", "writein": "FALSE",
         "mode": "TOTAL", "candidatevotes": "100", "totalvotes": "180", "unofficial": "FALSE"},
        {"year": "1990", "state_po": "GA", "office": "US HOUSE", "district": "5", "candidate": "Old",
         "candidatevotes": "1", "totalvotes": "1"},
    ])
    with IngestionRun(conn, "mit_election_lab", "historical_results") as run:
        load_mit_frame(conn, run, frame, "US HOUSE", "13.0", {2022})
        load_mit_frame(conn, run, frame, "US HOUSE", "13.0", {2022})
    assert one(conn, "SELECT COUNT(*) FROM historical_results") == 1
    assert one(conn, "SELECT district FROM historical_results") == "05"


def test_report_and_verification(conn):
    ingest_fec_candidates(conn, client=FakeFEC())
    summary = run_verification(conn)
    assert summary["certification_violations"] == 0
    assert summary["reference_coverage"]["house_contests"] == 435
    report, path = write_report(conn)
    assert report["states_and_dc"] == 51 and report["candidates"] == 6
    assert report["state_adapters_implemented"] == 0
    assert len(report["states_without_adapter"]) == 51


# -- civicAPI (recorded real response, 2026-11-04) ----------------------------
def test_civicapi_results_from_recorded_response(conn):
    import copy
    import json

    from election_data.clients.civicapi import CivicAPIClient
    from election_data.ingestion.supplemental import ingest_civicapi_results

    data = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "civicapi_race_search.json").read_text())
    called = copy.deepcopy(data)
    called["races"][1]["candidates"][0]["winner"] = True   # pretend NE governor was called

    class FakeCivic(CivicAPIClient):
        def __init__(self, payload):
            super().__init__(http=object())
            self.payload = payload

        def fetch(self, endpoint, params=None):
            return self.payload, f"https://civicapi.org/api/v2/{endpoint}"

    # official state data first, then civicAPI: civicAPI must not override it
    with IngestionRun(conn, "state:GA", "r") as run:
        record_results(conn, run, [obs(contest_key="2026:general:GOV-GA", choice_label="Keisha Lance Bottoms",
                                       votes=1)])
    ingest_civicapi_results(conn, client=FakeCivic(called))

    rows = conn.execute("SELECT contest_key, choice_label, votes, display_label, source_name FROM v_results_labeled "
                        "ORDER BY contest_key, votes DESC").fetchall()
    assert {r["contest_key"] for r in rows} == {"2026:general:GOV-GA", "2026:general:GOV-NE"}  # referendum skipped
    assert all(r["display_label"] == "PRELIMINARY — NOT CERTIFIED" for r in rows)
    ga = [r for r in rows if r["choice_label"] == "Keisha Lance Bottoms"][0]
    assert ga["votes"] == 1 and ga["source_name"].startswith("Georgia")   # official value kept
    assert one(conn, "SELECT COUNT(*) FROM election_result_snapshots WHERE source_id = "
                     "(SELECT id FROM data_sources WHERE source_key='civicapi')") == 6
    assert one(conn, "SELECT COUNT(*) FROM race_calls") == 1
    assert one(conn, "SELECT certification_status FROM contests WHERE contest_key='2026:general:GOV-NE'") == "unknown"
    assert one(conn, "SELECT verification_status FROM contests WHERE contest_key='2026:general:GOV-NE'") == "corroborated"


# -- live projections + API ----------------------------------------------------
def _seed_ga_results(conn, d_votes, r_votes, pct):
    with IngestionRun(conn, "state:GA", "r") as run:
        record_results(conn, run, [
            obs(choice_label="Dem Candidate", votes=d_votes, party="Democratic", expected_vote_pct=pct),
            obs(choice_label="Rep Candidate", votes=r_votes, party="Republican", expected_vote_pct=pct),
        ])


def test_projection_engine_snapshots_and_chambers(conn):
    from election_data.projections.engine import run_projections
    from election_data.projections.model import ModelParams
    from election_data.projections.priors import build_priors

    stats = build_priors(conn)
    assert stats["no_baseline"] == one(conn, "SELECT COUNT(*) FROM contests WHERE contest_key LIKE '2026:%%'")
    params = ModelParams(simulations=2000)
    s1 = run_projections(conn, params=params, seed=1)
    assert s1.races == 507 and s1.with_results == 0 and s1.snapshots_written == 507
    assert set(s1.chambers) == {"senate", "house"}
    assert s1.chambers["house"]["races"] == 435          # DC delegate excluded from control
    assert s1.chambers["senate"]["races"] == 35

    s2 = run_projections(conn, params=params, seed=2)     # nothing changed
    assert s2.snapshots_written == 0
    assert one(conn, "SELECT COUNT(*) FROM chamber_projections") == 2   # no noise rows added

    _seed_ga_results(conn, 1_200_000, 900_000, 60)
    s3 = run_projections(conn, params=params, seed=3)
    assert s3.with_results == 1 and s3.snapshots_written == 1
    row = conn.execute("SELECT * FROM v_latest_projections WHERE contest_key = %s", (GA_SENATE,)).fetchone()
    assert row["leader_party"] == "DEM" and row["votes_counted"] == 2_100_000
    assert {c["label"] for c in row["candidates"]} == {"Dem Candidate", "Rep Candidate"}
    assert all("majority_prob" in c for c in row["candidates"])   # GA runoff rule
    # projections never write to results or certification
    assert one(conn, "SELECT certification_status FROM contests WHERE contest_key=%s", (GA_SENATE,)) == "unknown"
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE projection_snapshots SET leader_party = 'REP'")
    conn.rollback()


def test_live_api_endpoints(conn, db_url, monkeypatch):
    from fastapi.testclient import TestClient

    from election_data.api import app
    from election_data.projections.engine import run_projections
    from election_data.projections.model import ModelParams

    monkeypatch.setenv("DATABASE_URL", db_url)
    _seed_ga_results(conn, 1_200_000, 900_000, 60)
    run_projections(conn, params=ModelParams(simulations=1000), seed=4)
    client = TestClient(app)

    summary = client.get("/live/summary").json()
    assert summary["data_mode"] == "live" and summary["races_reporting"] == 1
    assert set(summary["chambers"]) == {"senate", "house"}
    assert summary["chambers"]["senate"]["total_seats"] == 100

    races = client.get("/live/races", params={"office": "senate"}).json()["races"]
    assert len(races) == 35 and all(r["office"] == "senate" for r in races)
    assert client.get("/live/races", params={"office": "mayor"}).status_code == 422

    detail = client.get(f"/live/races/{GA_SENATE}").json()
    assert detail["race"]["leader_party"] == "DEM" and len(detail["history"]) == 1
    assert detail["results_source_rows"][0]["result_stage"] == "preliminary"
    assert any("not official" in n for n in detail["notes"])
    assert client.get("/live/races/2026:general:NOPE").status_code == 404

    page = client.get("/")
    assert page.status_code == 200 and "Midterms 2026" in page.text
    assert client.get("/static/app.js").status_code == 200
