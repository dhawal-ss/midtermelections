"""Create/upgrade the schema and seed reference data.

    python -m scripts.initialize_db
"""
from __future__ import annotations

import argparse
import logging

from election_data.http import configure_logging
from election_data import config, database
from election_data.ingestion.reference_data import seed_all


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--schema-only", action="store_true", help="apply schema without seeding")
    args = parser.parse_args()
    configure_logging(config.LOG_LEVEL)

    with database.connect(args.database_url) as conn:
        applied = database.apply_schema(conn)
        print(f"Schema applied: {', '.join(applied)}")
        if not args.schema_only:
            counts = seed_all(conn)
            print("Seeded:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
