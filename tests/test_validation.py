"""Pure validators: results, polls, FEC records."""
from datetime import date, timedelta

from election_data.jobs.validation import (
    validate_contribution, validate_fec_candidate, validate_poll, validate_result,
)

GOOD = {"votes": 1200, "vote_percentage": 51.2, "source_url": "https://sos.example.gov/r.csv",
        "result_stage": "preliminary", "precincts_reporting": 10, "precincts_total": 20}


def test_valid_preliminary_result():
    assert validate_result(GOOD) == {"valid": True, "errors": []}


def test_negative_votes_and_bad_percentage():
    r = validate_result({**GOOD, "votes": -1, "vote_percentage": 101})
    assert not r["valid"]
    assert "Negative vote count" in r["errors"] and "Invalid vote percentage" in r["errors"]


def test_missing_source_and_stage():
    r = validate_result({"votes": 1})
    assert "Missing source URL" in r["errors"] and "Missing result stage" in r["errors"]


def test_certified_requires_evidence():
    r = validate_result({**GOOD, "result_stage": "certified"})
    assert "Missing certification evidence" in r["errors"]
    r = validate_result({**GOOD, "is_certified": True, "result_stage": "preliminary"})
    assert "Missing certification evidence" in r["errors"]
    ok = validate_result({**GOOD, "result_stage": "certified", "certification_evidence": "https://sos/cert.pdf"})
    assert ok["valid"]


def test_precincts_cannot_exceed_total():
    assert "Precincts reporting exceeds precincts total" in validate_result(
        {**GOOD, "precincts_reporting": 21})["errors"]


def test_percentages_need_not_sum_to_100():
    # Each row is validated alone; nothing enforces a 100% total (write-ins, multi-seat...)
    assert validate_result({**GOOD, "vote_percentage": 48.0})["valid"]


def test_poll_requires_methodology_fields():
    poll = {"pollster": "Example Research", "field_start": "2026-10-01", "field_end": "2026-10-04",
            "sample_size": 800, "population": "lv", "results": {"A": 48, "B": 46}}
    assert validate_poll(poll) == []
    assert "missing pollster" in validate_poll({**poll, "pollster": None})
    assert "field_end before field_start" in validate_poll({**poll, "field_end": "2026-09-01"})
    assert any("unknown population" in p for p in validate_poll({**poll, "population": "xx"}))
    future = (date.today() + timedelta(days=10)).isoformat()
    assert "field_end in the future" in validate_poll({**poll, "field_end": future})


def test_fec_candidate_validation():
    assert validate_fec_candidate({"candidate_id": "H6TX07111", "name": "X", "office": "H", "state": "TX"}) == []
    assert validate_fec_candidate({"candidate_id": "BAD", "name": "", "office": "X"})


def test_contribution_needs_filing_identifier():
    rec = {"sub_id": 1, "contribution_receipt_amount": 5, "committee_id": "C1"}
    assert any("filing identifier" in p for p in validate_contribution(rec))
    assert validate_contribution({**rec, "file_number": 123}) == []
