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


BLOCKED = {401, 403, 429}


def classify(status: int | None, error: str | None) -> str:
    if status is not None:
        if status < 400:
            return "ok"
        if status in BLOCKED:
            return "blocked_automated"
        if status in (404, 410):
            return "not_found"
        return "server_error" if status >= 500 else "client_error"
    err = (error or "").lower()
    if "ssl" in err or "certificate" in err:
        return "tls_error"
    if "timed out" in err or "timeout" in err:
        return "timeout"
    return "connection_error"


def check_url(session: requests.Session, url: str) -> dict[str, Any]:
    """HEAD first; fall back to GET on any error status (many servers mishandle HEAD)."""
    status, final, error = None, None, None
    for method in ("HEAD", "GET"):
        try:
            resp = session.request(method, url, allow_redirects=True, timeout=25, stream=(method == "GET"))
            status, final, error = resp.status_code, resp.url, None
            resp.close()
            if status < 400:
                break
        except requests.RequestException as exc:
            error = str(exc)[:500]
    outcome = classify(status, error)
    return {"http_status": status, "final_url": final, "ok": outcome == "ok", "error": error, "outcome": outcome}


def check_all(conn: psycopg.Connection) -> dict[str, Any]:
    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    summary: dict[str, Any] = {"checked": 0, "ok": 0, "outcomes": {}, "failed": [],
                               "states_verified": 0, "states_reachable": 0}
    for state in config.state_registry():
        all_ok, all_reachable, any_url = True, True, False
        for kind in URL_KINDS:
            url = state.get(kind)
            if not url:
                continue
            any_url = True
            result = check_url(session, url)
            summary["checked"] += 1
            summary["outcomes"][result["outcome"]] = summary["outcomes"].get(result["outcome"], 0) + 1
            conn.execute(
                """INSERT INTO source_link_checks (state_code, url_kind, url, http_status, final_url, ok, error, outcome)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (state["code"], kind, url, result["http_status"], result["final_url"], result["ok"],
                 result["error"], result["outcome"]),
            )
            if result["ok"]:
                summary["ok"] += 1
            else:
                all_ok = False
                if result["outcome"] != "blocked_automated":
                    all_reachable = False
                summary["failed"].append(f"{state['code']} {kind} [{result['outcome']}] {url} "
                                         f"-> {result['http_status'] or (result['error'] or '')[:100]}")
        if any_url and all_ok:
            conn.execute("UPDATE state_source_registry SET urls_verified_at = NOW() WHERE state_code = %s",
                         (state["code"],))
            summary["states_verified"] += 1
        if any_url and all_reachable:
            conn.execute("UPDATE state_source_registry SET urls_reachable_at = NOW() WHERE state_code = %s",
                         (state["code"],))
            summary["states_reachable"] += 1
        conn.commit()
    return summary


def main() -> None:
    configure_logging(config.LOG_LEVEL)
    with database.connect() as conn:
        summary = check_all(conn)
    print(f"Checked {summary['checked']} URLs: {summary['ok']} OK; outcomes {summary['outcomes']}")
    print(f"{summary['states_verified']}/51 jurisdictions: every URL returned OK")
    print(f"{summary['states_reachable']}/51 jurisdictions: every URL OK or up-but-blocking-scripts (403)")
    for line in summary["failed"]:
        print("  FAILED", line)


if __name__ == "__main__":
    main()
