"""Run cross-source verification and write the coverage report.

    python -m scripts.verify_data
"""
from __future__ import annotations

import json
import logging

from election_data.http import configure_logging
from election_data import config, database
from election_data.jobs.reports import write_report
from election_data.jobs.validation import run_verification


def main() -> None:
    configure_logging(config.LOG_LEVEL)
    with database.connect() as conn:
        summary = run_verification(conn)
        report, path = write_report(conn)
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nCoverage report written to {path}")
    keys = ["states_and_dc", "house_contests_2026", "senate_contests_2026", "candidates", "contributions",
            "polls", "result_rows_current", "certified_contests", "open_discrepancies", "failed_runs_24h"]
    for k in keys:
        print(f"  {k:28s} {report.get(k)}")


if __name__ == "__main__":
    main()
