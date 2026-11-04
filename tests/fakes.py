"""In-memory stand-ins for API clients (no network)."""
from __future__ import annotations

FEC_CANDIDATES = [
    {"candidate_id": "S6GA00119", "name": "DOE, JANE A. MS.", "party": "DEM", "party_full": "DEMOCRATIC PARTY",
     "office": "S", "state": "GA", "district": "00", "incumbent_challenge": "I", "candidate_status": "C",
     "first_file_date": "2025-01-10", "election_years": [2026]},
    {"candidate_id": "S6GA00200", "name": "ROE, RICHARD", "party": "REP", "party_full": "REPUBLICAN PARTY",
     "office": "S", "state": "GA", "district": "00", "incumbent_challenge": "C", "candidate_status": "C",
     "first_file_date": "2025-03-02", "election_years": [2026]},
    {"candidate_id": "H6TX07111", "name": "SMITH, ALEX", "party": "IND", "party_full": "INDEPENDENT",
     "office": "H", "state": "TX", "district": "07", "incumbent_challenge": "O", "candidate_status": "N",
     "first_file_date": "2025-06-01", "election_years": [2026]},
    {"candidate_id": "S6OH00300", "name": "BUCKEYE, PAT", "party": "REP", "party_full": "REPUBLICAN PARTY",
     "office": "S", "state": "OH", "district": "00", "incumbent_challenge": "I", "candidate_status": "C",
     "first_file_date": "2025-02-01", "election_years": [2026]},
    {"candidate_id": "S6UT00999", "name": "PAPER, FILER", "party": "IND", "office": "S", "state": "UT",
     "district": "00", "candidate_status": "N", "election_years": [2026]},   # UT has no 2026 Senate race
    {"candidate_id": "H6SC86001", "name": "WRONG, DISTRICT", "party": "IND", "office": "H", "state": "SC",
     "district": "86", "candidate_status": "N", "election_years": [2026]},   # SC has 7 districts
    {"candidate_id": "BAD", "name": "", "office": "X"},  # malformed: must be rejected, not crash
]

FEC_COMMITTEES = [
    {"committee_id": "C00000001", "name": "DOE FOR SENATE", "committee_type": "S", "designation": "P",
     "party": "DEM", "state": "GA", "candidate_ids": ["S6GA00119"]},
    {"committee_id": "C00000002", "name": "ROE FOR GEORGIA", "committee_type": "S", "designation": "P",
     "party": "REP", "state": "GA", "candidate_ids": ["S6GA00200"]},
]

SCHEDULE_A = [
    {"sub_id": 4012345678901, "committee_id": "C00000001", "contributor_name": "PUBLIC, JOHN Q",
     "entity_type": "IND", "contributor_city": "ATLANTA", "contributor_state": "GA",
     "contribution_receipt_amount": 250.0, "contribution_receipt_date": "2026-09-15T00:00:00",
     "file_number": 1876543, "image_number": "202610159876543210", "report_type": "Q3", "report_year": 2026},
    {"sub_id": 4012345678902, "committee_id": "C00000001", "contributor_name": "EXAMPLE PAC",
     "entity_type": "PAC", "contribution_receipt_amount": 5000.0, "contribution_receipt_date": "2026-09-20",
     "file_number": 1876543, "image_number": "202610159876543211", "report_type": "Q3", "report_year": 2026},
]


class FakeFEC:
    def __init__(self):
        self.calls = []

    def _pages(self, rows, endpoint):
        for r in rows:
            yield r, f"https://api.open.fec.gov/v1/{endpoint}/"

    def get_candidates(self, **kw):
        self.calls.append(("candidates", kw))
        office = kw.get("office")
        rows = [r for r in FEC_CANDIDATES if r.get("office") == office]
        if office == "H":
            rows.append(FEC_CANDIDATES[-1])  # malformed record arrives with the House batch
        return self._pages(rows, "candidates")

    def get_committees(self, **kw):
        return self._pages([c for c in FEC_COMMITTEES if c["committee_type"] == kw.get("committee_type")],
                           "committees")

    def get_candidate_totals(self, **kw):
        if kw.get("office") != "S":
            return iter(())
        return self._pages([
            {"candidate_id": "S6GA00119", "receipts": 1000000.5, "disbursements": 400000,
             "cash_on_hand_end_period": 600000.5, "coverage_start_date": "2025-01-01",
             "coverage_end_date": "2026-09-30"},
            {"candidate_id": "S9ZZ99999", "receipts": 1},
        ], "candidates/totals")

    def get_schedule_a(self, committee_id, *a, **kw):
        return self._pages([r for r in SCHEDULE_A if r["committee_id"] == committee_id], "schedules/schedule_a")


class FakeCensus:
    def states(self, dataset, variables):
        return [{"NAME": "Georgia", "state": "13", "B01003_001E": "11029227", "B29001_001E": "7900000",
                 "B01002_001E": "37.9", "B19013_001E": "-666666666", "B15003_022E": "1500000"},
                {"NAME": "Puerto Rico", "state": "72", "B01003_001E": "3200000"}], "https://api.census.gov/x"

    def congressional_districts(self, dataset, variables):
        return [{"NAME": "Congressional District 7 (119th Congress), Texas", "state": "48",
                 "congressional district": "07", "B01003_001E": "780000"},
                {"NAME": "Delegate District (at Large) (119th Congress), District of Columbia", "state": "11",
                 "congressional district": "98", "B01003_001E": "700000"}], "https://api.census.gov/x"
