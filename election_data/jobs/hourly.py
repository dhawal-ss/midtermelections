"""Hourly / election-window jobs: finance deltas, polls, live results."""
from __future__ import annotations

import logging

from election_data import config
from election_data.http import configure_logging

from election_data.jobs.registry import JOBS

log = logging.getLogger(__name__)

HOURLY_SEQUENCE = ["live_results", "polls", "certification_watch"]


def run_hourly() -> dict[str, str]:
    outcome = {}
    for name in HOURLY_SEQUENCE:
        try:
            JOBS[name]()
            outcome[name] = "ok"
        except Exception as exc:
            log.exception("Hourly job %s failed", name)
            outcome[name] = f"failed: {exc}"
    return outcome


if __name__ == "__main__":
    configure_logging(config.LOG_LEVEL)
    print(run_hourly())
