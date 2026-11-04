"""Check every URL in config/sources.yaml and record the outcome.

    python -m scripts.verify_sources

A state's urls_verified_at is set only when every non-null URL for it responded
successfully. Run this from a network that can reach state election sites.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
import requests

from election_data.http import configure_logging
from election_data import config, database

log = logging.getLogger(__name__)
URL_KINDS = ("official_election_url", "results_url", "candidates_url", "voter_lookup_url")


def check_url(session: requests.Session, url: str) -> dict[str, Any]:
    try:
        resp = session.head(url, allow_redirects=True, timeout=20)
        if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
            resp = session.get(url, allow_redirects=True, timeout=30, stream=True)
            resp.close()
        return {"http_status": resp.status_code, "final_url": resp.url, "ok": resp.status_code < 400, "error": None}
    except requests.RequestException as exc:
        return {"http_status": None, "final_url": None, "ok": False, "error": str(exc)[:500]}


def check_all(conn: psycopg.Connection) -> dict[str, Any]:
    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    summary = {"checked": 0, "ok": 0, "failed": [], "states_verified": 0}
    for state in config.state_registry():
        all_ok, any_url = True, False
        for kind in URL_KINDS:
            url = state.get(kind)
            if not url:
                continue
            any_url = True
            result = check_url(session, url)
            summary["checked"] += 1
            conn.execute(
                """INSERT INTO source_link_checks (state_code, url_kind, url, http_status, final_url, ok, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (state["code"], kind, url, result["http_status"], result["final_url"], result["ok"], result["error"]),
            )
            if result["ok"]:
                summary["ok"] += 1
            else:
                all_ok = False
                summary["failed"].append(f"{state['code']} {kind} {url} -> {result['http_status'] or result['error']}")
        if any_url and all_ok:
            conn.execute("UPDATE state_source_registry SET urls_verified_at = NOW() WHERE state_code = %s",
                         (state["code"],))
            summary["states_verified"] += 1
        conn.commit()
    return summary


def main() -> None:
    configure_logging(config.LOG_LEVEL)
    with database.connect() as conn:
        summary = check_all(conn)
    print(f"Checked {summary['checked']} URLs: {summary['ok']} OK; "
          f"{summary['states_verified']}/51 jurisdictions fully verified")
    for line in summary["failed"]:
        print("  FAILED", line)


if __name__ == "__main__":
    main()
