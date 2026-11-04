"""Config-driven adapter for official results files (CSV/TSV) downloaded from a state or county.

Mapping (YAML or dict) example:

    source_url: https://example-state.gov/results/2026-general.csv   # where the file came from
    result_stage: preliminary          # preliminary | unofficial_complete | canvassed | certified
    reporting_unit_type: county        # or state / precinct
    columns:
      office: Office
      district: District
      choice: Candidate
      votes: Votes
      reporting_unit: County
      precincts_reporting: PrecinctsReporting
      precincts_total: PrecinctsTotal
      fec_candidate_id: null
      party: Party                     # optional; used by the live projection model
    office_values:                     # map the file's office labels to supported offices
      "U.S. Senate": US SENATE
      "U.S. House": US HOUSE
      "Governor": GOVERNOR
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from election_data.adapters.base import StateResultsAdapter, register
from election_data.clients.csv_source import iter_records, normalize_column, read_table
from election_data.ingestion.results import ResultObservation, contest_key_for


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(float(str(value).replace(",", "")))


@register("generic_csv")
class GenericCSVResultsAdapter(StateResultsAdapter):
    def observations(self) -> Iterator[tuple[ResultObservation, dict[str, Any]]]:
        path = Path(self.options["file"])
        mapping = self.options["mapping"]
        cols = {k: normalize_column(v) for k, v in mapping["columns"].items() if v}
        office_values = {k.strip().lower(): v for k, v in (mapping.get("office_values") or {}).items()}
        stage = mapping.get("result_stage", "preliminary")
        unit_type = mapping.get("reporting_unit_type", "county")
        source_url = mapping.get("source_url") or path.resolve().as_uri()
        frame = read_table(path)
        missing = [c for c in cols.values() if c not in frame.columns]
        if missing:
            raise ValueError(f"{path.name}: mapped columns not found: {missing}")

        for row in iter_records(frame):
            label = (row.get(cols["office"]) or "").strip()
            office = office_values.get(label.lower())
            if office is None:
                continue  # contests this project does not model yet (local, judicial...)
            district = row.get(cols["district"]) if "district" in cols else None
            if district:
                digits = re.sub(r"\D", "", str(district))
                district = digits.zfill(2) if digits else None
            yield ResultObservation(
                state_code=self.state_code,
                contest_key=contest_key_for(office, self.state_code, district),
                choice_label=(row.get(cols["choice"]) or "").strip(),
                votes=_int(row.get(cols["votes"])),
                source_url=source_url,
                result_stage=stage,
                candidate_fec_id=row.get(cols["fec_candidate_id"]) if "fec_candidate_id" in cols else None,
                reporting_unit_type=unit_type,
                reporting_unit_name=row.get(cols["reporting_unit"]) if "reporting_unit" in cols else None,
                precincts_reporting=_int(row.get(cols["precincts_reporting"])) if "precincts_reporting" in cols else None,
                precincts_total=_int(row.get(cols["precincts_total"])) if "precincts_total" in cols else None,
                source_reported_at=mapping.get("source_reported_at"),
                party=row.get(cols["party"]) if "party" in cols else None,
            ), row
