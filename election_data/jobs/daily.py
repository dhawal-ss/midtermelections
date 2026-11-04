"""Daily batch: FEC filings, calendar, supplemental datasets, verification and report."""
from __future__ import annotations

import logging

from election_data import config
from election_data.http import configure_logging

from election_data.jobs.registry import JOBS

log = logging.getLogger(__name__)

DAILY_SEQUENCE = [
    "fec_election_dates", "fec_candidates", "fec_committees", "fec_finance_totals",
    "election_org", "validation", "daily_report",
]


def run_daily() -> dict[str, str]:
    outcome = {}
    for name in DAILY_SEQUENCE:
        try:
            JOBS[name]()
            outcome[name] = "ok"
        except Exception as exc:  # one source failing must not block the rest
            log.exception("Daily job %s failed", name)
            outcome[name] = f"failed: {exc}"
    return outcome


if __name__ == "__main__":
    configure_logging(config.LOG_LEVEL)
    print(run_daily())
