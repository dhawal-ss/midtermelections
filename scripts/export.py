"""Export key views/tables to CSV in data/exports/.

    python -m scripts.export                 # default set
    python -m scripts.export v_results_labeled contributions
"""
from __future__ import annotations

import sys
from datetime import date

from election_data import config, database

DEFAULT = ["v_results_labeled", "v_contest_summary", "v_state_coverage", "data_discrepancies"]
ALLOWED = set(DEFAULT) | {"candidates", "contests", "contest_candidates", "campaign_committees",
                          "financial_summaries", "contributions", "expenditures", "polls",
                          "historical_results", "district_demographics", "race_calls",
                          "result_certifications", "election_result_snapshots", "elections"}


def main() -> None:
    names = sys.argv[1:] or DEFAULT
    config.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with database.connect() as conn:
        for name in names:
            if name not in ALLOWED:
                sys.exit(f"Not exportable: {name}")
            path = config.EXPORTS_DIR / f"{name}_{date.today().isoformat()}.csv"
            with conn.cursor() as cur, open(path, "wb") as fh:
                with cur.copy(f"COPY (SELECT * FROM {name}) TO STDOUT WITH CSV HEADER") as copy:
                    for chunk in copy:
                        fh.write(chunk)
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
