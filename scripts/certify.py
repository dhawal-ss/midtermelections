"""Record an official certification from a state canvass document.

    python -m scripts.certify --contest 2026:general:US-SENATE-GA-II --state GA \
        --authority "Georgia Secretary of State" --certified-at 2026-11-21T17:00:00-05:00 \
        --document-url https://.../certification.pdf --document-file ./certification.pdf

The document file (if given) is hashed so the certification can be re-verified later.
Certified vote totals are then loaded with the generic CSV adapter using result_stage: certified.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from election_data import database
from election_data.ingestion.results import certify_contest
from election_data.provenance import file_sha256


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--contest", required=True, help="contest_key")
    p.add_argument("--state", required=True)
    p.add_argument("--authority", required=True)
    p.add_argument("--certified-at", required=True)
    p.add_argument("--document-url", required=True)
    p.add_argument("--document-file")
    p.add_argument("--notes")
    a = p.parse_args()
    digest = file_sha256(Path(a.document_file)) if a.document_file else None
    with database.transaction() as conn:
        cert_id = certify_contest(conn, a.contest, a.state.upper(), a.authority, a.certified_at,
                                  a.document_url, digest, a.notes)
    print(f"Certification {cert_id} recorded for {a.contest}")


if __name__ == "__main__":
    main()
