"""OpenFEC client: pagination, keyset pagination, retries, key redaction (HTTP mocked)."""
import pytest
import responses

from election_data.clients.fec import FECClient
from election_data.http import HttpClient, redact_url

BASE = "https://api.open.fec.gov/v1"


def client():
    return FECClient(api_key="SECRET123", http=HttpClient(retry_wait_initial=0))


@responses.activate
def test_candidates_page_pagination_and_filters():
    for page, ids in ((1, ["H6AA00001", "H6AA00002"]), (2, ["H6AA00003"])):
        responses.get(
            f"{BASE}/candidates/",
            match=[responses.matchers.query_param_matcher(
                {"cycle": "2026", "election_year": "2026", "office": "H", "sort": "name",
                 "per_page": "100", "page": str(page), "api_key": "SECRET123"})],
            json={"results": [{"candidate_id": i} for i in ids], "pagination": {"pages": 2, "page": page}},
        )
    rows = list(client().get_candidates(cycle=2026, office="H", election_year=2026))
    assert [r["candidate_id"] for r, _ in rows] == ["H6AA00001", "H6AA00002", "H6AA00003"]
    # provenance URL never contains the key
    assert all("SECRET123" not in url and "api_key" not in url for _, url in rows)


@responses.activate
def test_schedule_a_keyset_pagination():
    responses.get(f"{BASE}/schedules/schedule_a/", json={
        "results": [{"sub_id": 1}, {"sub_id": 2}],
        "pagination": {"last_indexes": {"last_index": "2", "last_contribution_receipt_date": "2026-01-02"}}})
    responses.get(f"{BASE}/schedules/schedule_a/", json={"results": [{"sub_id": 3}], "pagination": {"last_indexes": None}})
    rows = list(client().get_schedule_a("C00000001", 2026))
    assert [r["sub_id"] for r, _ in rows] == [1, 2, 3]
    second = responses.calls[1].request.url
    assert "last_index=2" in second and "last_contribution_receipt_date=2026-01-02" in second


@responses.activate
def test_retries_on_429_then_succeeds():
    responses.get(f"{BASE}/candidates/", status=429)
    responses.get(f"{BASE}/candidates/", status=502)
    responses.get(f"{BASE}/candidates/", json={"results": [{"candidate_id": "S6GA00119"}], "pagination": {"pages": 1}})
    rows = list(client().get_candidates())
    assert len(rows) == 1 and len(responses.calls) == 3


@responses.activate
def test_gives_up_after_max_attempts():
    for _ in range(5):
        responses.get(f"{BASE}/candidates/", status=429)
    with pytest.raises(Exception):
        list(client().get_candidates())
    assert len(responses.calls) == 5


@responses.activate
def test_client_error_is_not_retried():
    responses.get(f"{BASE}/candidates/", status=403, json={"error": "bad key"})
    with pytest.raises(Exception):
        list(client().get_candidates())
    assert len(responses.calls) == 1


@responses.activate
def test_html_response_rejected():
    responses.get(f"{BASE}/candidates/", body="<html>maintenance</html>", content_type="text/html")
    with pytest.raises(ValueError):
        list(client().get_candidates())


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        FECClient(api_key=None)


def test_redact_url():
    assert redact_url("https://x.gov/a/?api_key=abc&cycle=2026") == "https://x.gov/a/?cycle=2026"
    assert "key=" not in redact_url("https://g.com/v2/elections?key=abc")


def test_redact_text_masks_keys_in_exception_messages():
    from election_data.http import redact_text
    msg = "Max retries exceeded with url: /v1/candidates/?cycle=2026&api_key=REALKEY123 (Caused by ...)"
    assert "REALKEY123" not in redact_text(msg)
    assert "REDACTED" in redact_text("Authorization: Bearer abc.def-123")
