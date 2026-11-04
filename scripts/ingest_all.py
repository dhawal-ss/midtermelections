"""Run ingestion modules.

    python -m scripts.ingest_all                         # default daily set
    python -m scripts.ingest_all --only fec_candidates,fec_committees
    python -m scripts.ingest_all --only fec_contributions --committee-limit 25 --max-pages 5
    python -m scripts.ingest_all --only state_results --state NC --file nc.csv --mapping nc_mapping.yaml
    python -m scripts.ingest_all --only polls_csv --file polls.csv --mapping polls_mapping.yaml
    python -m scripts.ingest_all --only voter_stats --file eavs_2024.csv --mapping eavs_mapping.yaml
    python -m scripts.ingest_all --only historical_file --dataset us_house --file 1976-2024-house.tab
"""
from __future__ import annotations

import argparse
import logging
import sys

import yaml

from election_data.http import configure_logging, redact_text
from election_data import config, database
from election_data.jobs.registry import JOBS

DEFAULT = ["fec_election_dates", "fec_candidates", "fec_committees", "fec_finance_totals",
           "election_org", "census_demographics", "historical_results", "validation", "daily_report"]
FILE_JOBS = {"state_results", "polls_csv", "voter_stats", "historical_file"}


def _load_mapping(path: str | None) -> dict:
    if not path:
        sys.exit("--mapping is required for file-based jobs")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run_file_job(name: str, args: argparse.Namespace) -> None:
    from election_data.ingestion.state_results import ingest_state_results
    from election_data.ingestion.supplemental import ingest_polls_csv, ingest_voter_statistics_csv

    with database.connect() as conn:
        if name == "historical_file":
            from election_data.ingestion.historical import ingest_mit_file
            if not args.dataset:
                sys.exit("--dataset is required for historical_file (us_house, us_senate, us_president)")
            run = ingest_mit_file(conn, args.dataset, args.file)
        elif name == "state_results":
            if not args.state:
                sys.exit("--state is required for state_results")
            run = ingest_state_results(conn, args.state.upper(), args.file, args.mapping)
        elif name == "polls_csv":
            m = _load_mapping(args.mapping)
            run = ingest_polls_csv(conn, args.file, m["fields"], m.get("source_key", "civicapi"), m["source_url"])
        else:
            m = _load_mapping(args.mapping)
            run = ingest_voter_statistics_csv(conn, args.file, m.get("source_key", "eac_eavs"), m["source_url"],
                                              m["metric_columns"], m["state_column"], m["reporting_period"])
        print(f"{name}: received={run.received} inserted={run.inserted} failed={run.failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help=f"comma-separated jobs: {', '.join(sorted(set(JOBS) | FILE_JOBS))}")
    parser.add_argument("--state", help="two-letter state code (filters FEC candidates; required for state_results)")
    parser.add_argument("--file", help="input file for file-based jobs")
    parser.add_argument("--mapping", help="YAML column mapping for file-based jobs")
    parser.add_argument("--dataset", help="MIT dataset key for historical_file (us_house, us_senate, us_president)")
    parser.add_argument("--committee-limit", type=int, help="cap committees for contributions/expenditures")
    parser.add_argument("--max-pages", type=int, help="cap API pages per committee for schedules")
    parser.add_argument("--continue-on-error", action="store_true", default=True)
    args = parser.parse_args()
    configure_logging(config.LOG_LEVEL)

    names = [n.strip() for n in args.only.split(",")] if args.only else DEFAULT
    failures = []
    for name in names:
        try:
            if name in FILE_JOBS:
                if not args.file:
                    sys.exit(f"--file is required for {name}")
                run_file_job(name, args)
                continue
            if name not in JOBS:
                sys.exit(f"Unknown job '{name}'")
            kwargs = {}
            if name == "fec_candidates" and args.state:
                kwargs["states"] = [args.state.upper()]
            if name in ("fec_contributions", "fec_expenditures"):
                kwargs.update(committee_limit=args.committee_limit, max_pages=args.max_pages)
            result = JOBS[name](**kwargs)
            if hasattr(result, "received"):
                print(f"{name}: received={result.received} inserted={result.inserted} "
                      f"unchanged={result.unchanged} failed={result.failed}")
            else:
                print(f"{name}: done")
        except SystemExit:
            raise
        except Exception as exc:
            logging.exception("Job %s failed", name)
            failures.append(redact_text(f"{name}: {exc}"))
    if failures:
        print("\nFAILED JOBS:\n  " + "\n  ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
