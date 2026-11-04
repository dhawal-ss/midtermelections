# US Midterm Election Intelligence Database

A PostgreSQL database and Python ingestion platform for the **November 3, 2026 US midterm elections**, covering all 50 states and DC, with historical cycles (2014–2024) for comparison.

The platform keeps five kinds of information separate, and the database enforces the separation:

| Kind | Where it lives | Can it be shown as certified? |
|---|---|---|
| Official election information | `contests`, `elections`, `state_source_registry` | n/a |
| Candidate and campaign filings | `candidates`, `candidate_filings`, `contributions`, `expenditures` | n/a |
| Third-party polling and research | `polls`, `poll_answers` (always `unverified`) | never |
| Preliminary results | `election_results` with `result_stage` = `preliminary`/`unofficial_complete`/`canvassed` | **no** |
| Certified results | `election_results` with `is_certified = TRUE` + a `result_certifications` row | only with an official document |

A preliminary result **cannot** be stored as certified. CHECK constraints and triggers reject it, even when the application code is bypassed.

---

## Implementation status

This section lists what works and what doesn't, so nobody mistakes the coverage targets for coverage.

| Component | Status |
|---|---|
| PostgreSQL schema (43 tables, 4 views, integrity triggers) | ✅ implemented, tested |
| Reference seed: 51 jurisdictions, 435 House seats + DC delegate, 35 Senate contests (33 Class II + FL/OH specials), 36 governor races | ✅ implemented, tested |
| Raw-record provenance (immutable, content-hashed, API keys redacted) | ✅ implemented, tested |
| OpenFEC: candidates, committees, totals, Schedule A/B/E, election calendar | ✅ implemented, tested with mocked HTTP |
| Census ACS demographics (states + congressional districts) | ✅ implemented, tested with fake client |
| MIT Election Lab historical returns (Harvard Dataverse) | ✅ implemented, loader tested |
| Election.org CSV downloads (schema-change detection, FEC-ID cross-reference) | ✅ implemented, **not tested against live files** |
| Results pipeline: snapshots, source precedence, certification, race calls, recounts | ✅ implemented, tested |
| Generic CSV adapter for official state/county results exports | ✅ implemented, tested |
| Polls (CSV + civicAPI), EAC/state aggregate voter statistics (CSV) | ✅ implemented, mapping-driven |
| Verification jobs, discrepancy tracking, daily coverage report | ✅ implemented, tested |
| APScheduler runner, REST API, CSV export, Docker | ✅ implemented |
| civicAPI endpoints | ⚠️ client ready, **endpoint paths/field maps deliberately blank**: fill them in from the provider's v2 docs, then set `enabled: true` |
| State-specific live results adapters | ❌ **0 of 51 implemented**. Every state is `adapter_status: not_implemented`. Load official exports with the generic CSV adapter in the meantime |
| State ballot-qualification feeds | ❌ not implemented. `ballot_status` stays `unknown` until a state source provides evidence |
| State legislative / local contests | ❌ schema supports them; no data source wired up |
| AP Elections API | ❌ commercial; not integrated |
| Live verification of the state URLs in `config/sources.yaml` | ⚠️ written from public knowledge and **unverified**. Run `python -m scripts.verify_sources` |

---

## Quick start

```bash
cp .env.example .env          # add FEC_API_KEY (free) and CENSUS_API_KEY (free)
docker compose up -d postgres
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m scripts.initialize_db          # schema + 51 jurisdictions + 2026 contest map
python -m scripts.verify_sources         # check every state URL, record results
python -m scripts.ingest_all             # FEC, calendar, Election.org, Census, MIT history
python -m scripts.verify_data            # cross-source checks + coverage report
```

Or run everything in containers: `docker compose up -d`. This starts PostgreSQL, a one-shot `init`, the `scheduler`, and the `api` on port 8000.

Free keys: [OpenFEC](https://api.open.fec.gov/developers/) (1,000 calls/hour) · [Census](https://api.census.gov/data/key_signup.html) · Google Civic (optional).

Requires **PostgreSQL 15+** (the schema uses `UNIQUE NULLS NOT DISTINCT`) and Python 3.11+.

### Common commands

```bash
python -m scripts.ingest_all --only fec_candidates --state GA
python -m scripts.ingest_all --only fec_committees,fec_finance_totals
python -m scripts.ingest_all --only fec_contributions --committee-limit 50 --max-pages 10
python -m scripts.ingest_all --only state_results --state NC --file nc_results.csv --mapping examples/state_results_mapping.yaml
python -m scripts.ingest_all --only polls_csv --file polls.csv --mapping examples/polls_mapping.yaml
python -m scripts.ingest_all --only voter_stats --file eavs.csv --mapping examples/eavs_mapping.yaml
python -m scripts.certify --contest 2026:general:US-SENATE-GA-II --state GA --authority "Georgia Secretary of State" \
    --certified-at 2026-11-21T17:00:00-05:00 --document-url https://... --document-file cert.pdf
python -m scripts.export v_results_labeled v_state_coverage
python -m scripts.run_scheduler
uvicorn election_data.api:app --port 8000
```

---

## Architecture

```
Data sources ─► clients/ (HTTP, retries, rate limits, key redaction)
                   │
                   ▼
            provenance.IngestionRun ─► raw_records (immutable JSONB, sha256) / raw_files (archived downloads)
                   │
                   ▼
            validation ─► normalization ─► domain tables (upserts, idempotent)
                   │                          │
                   └─► source_verifications   └─► election_result_snapshots (append-only)
                       data_discrepancies          election_results (latest value, guarded)
                   │
                   ▼
            views (v_results_labeled, v_contest_summary, v_state_coverage) ─► REST API / CSV / SQL
```

```
config/sources.yaml        source registry, 51 state entries, schedules
sql/schema.sql             full schema (idempotent); sql/migrations/NNN_*.sql for later changes
election_data/
  clients/                 fec, election_org, civicapi, census, google_civic, mit_election_lab, csv_source
  ingestion/               reference_data, candidates, finance, calendar, demographics, historical,
                           results, state_results, supplemental (civicAPI, polls, voter stats)
  adapters/                state results adapter interface + generic CSV adapter
  jobs/                    validation, reports, registry, scheduler, daily, hourly
  api.py                   read-only FastAPI
scripts/                   initialize_db, ingest_all, verify_data, verify_sources, certify, export, run_scheduler
tests/                     unit (mocked HTTP) + integration (real PostgreSQL)
```

---

## Data sources

| Source | Data | Access | Authority level |
|---|---|---|---|
| OpenFEC | Federal candidates, committees, receipts, disbursements, IEs, election calendar | Free key | `official_federal` (primary for filings) |
| State/DC election offices | Candidates, ballot status, results, certification | Public | `official_election_authority` (**only source that can certify**) |
| Census API | ACS demographics by state and congressional district | Free key | `official_federal` |
| EAC EAVS | Registration, turnout, administration (latest published: 2024) | Free download | `official_federal` |
| MIT Election Lab | Historical House/Senate/President returns | Free download | `academic` |
| Election.org | 2026 contests/candidates, historical results | Free public beta | `third_party` |
| civicAPI | Results, race calls, polls | Free, no key | `third_party` |
| Google Civic | Elections list, voter info, OCD divisions | Free key | `third_party` |
| AP | Live results, race calls | Commercial | `commercial` (disabled) |

No single free API provides every state's live results, ballot-qualified candidates, local races and turnout. The FEC refreshes nightly and is not a results source. Election.org warns that candidate filings are not proof of ballot qualification.

---

## Verification model

Every normalized record carries a `verification_status`:

| Status | Meaning |
|---|---|
| `official` | Directly from the responsible authority (FEC for filings, state office for results) |
| `corroborated` | Matched against an independent source on an official ID |
| `unverified` | Received but not independently checked (all third-party data starts here) |
| `disputed` | Sources conflict (see `data_discrepancies`) |
| `superseded` | Replaced by a newer record |
| `certified` | Explicitly certified by the responsible authority, with a document |

Rules enforced in code and schema:

- **Candidate identity** matches on FEC candidate ID. Name-only matches go to `candidate_match_queue` for review and are never auto-merged.
- **FEC registration ≠ ballot qualification.** `ballot_status` of `qualified`/`certified_on_ballot` requires an evidence URL and source (CHECK constraint).
- **Campaign finance** keeps `fec_sub_id`, `file_number`, `image_number`, report type and year.
- **Polls** require pollster, population, sample size and field dates. They are never official.
- **Results.** Every observation is snapshotted. Third-party values never overwrite official ones. Certified values are never overwritten by uncertified ones. Stage never moves backwards. Disagreements become `data_discrepancies`.
- **Certification** requires a `result_certifications` row from an `official_election_authority` source with a document URL (trigger enforced). Race calls live in `race_calls` and never change certification status.
- **Vote percentages are not required to sum to 100.** Write-ins, under/overvotes and multi-seat contests break that assumption.
- **Raw evidence** (`raw_records`, `election_result_snapshots`) is append-only (UPDATE/DELETE raise).
- **Secrets.** API keys are stripped from stored URLs, error messages and logs.

`python -m scripts.verify_data` runs these checks: reference coverage vs FEC filings, certification integrity, county-sum vs statewide totals, cross-source vote mismatches, missing provenance, and stale jobs. It writes `data_quality_reports` and `data/exports/coverage_report_<date>.json`.

---

## Update schedule

Defined in `config/sources.yaml → schedules`. These are proposed polling intervals, not provider refresh guarantees.

| Job | Interval |
|---|---|
| FEC candidates, committees, election dates, Election.org | 24 h |
| FEC totals, contributions, expenditures; polls | 12 h |
| Validation | 6 h |
| Daily coverage report | 06:15 UTC |
| Source URL verification | 7 days |
| Census demographics, historical results | 30 days |
| Live results (civicAPI + implemented state adapters) | 5 min, 2026-11-03 18:00 ET → 2026-11-10 |
| Certification watch | 60 min, through 2027-01-06 |

### Post-election runbook (we are here: counting and canvass)

1. Download official results exports from state sites. Load them with the generic CSV adapter (`result_stage: preliminary` or `unofficial_complete`). Every load is snapshotted.
2. Re-load as counts update. History is kept in `election_result_snapshots`.
3. Track recounts with `record_recount` (sets `certification_status = 'recount'`).
4. When a state certifies, run `scripts/certify.py` with the official document. Then load the certified totals with `result_stage: certified`. The database rejects certified rows for contests without a certification record.
5. Display results through `v_results_labeled`. Every row carries a `display_label` such as `PRELIMINARY — NOT CERTIFIED`.

---

## Adding a state results adapter

1. Subclass `StateResultsAdapter` in `election_data/adapters/<state>.py` and decorate it with `@register("<name>")`. Yield `(ResultObservation, original_row)` pairs.
2. Import it in `election_data/adapters/__init__.py`.
3. In `config/sources.yaml`, set the state's `results_adapter: <name>`, `adapter_status: configured`, and any `adapter_options`.
4. Add a test with a recorded official export. Move to `tested` only after it has run against a live count.

Good first candidates: states with bulk machine-readable exports (for example, North Carolina publishes bulk results files).

---

## Caveats

- **Redistricting.** Several states adopted new congressional maps for 2026. `districts` is versioned (`plan_name`, `valid_from`). Census ACS 2024 uses 119th Congress boundaries and won't match redrawn districts. Historical rows keep the district number as published that year.
- **Special electoral systems** need extra modeling not yet implemented: Alaska top-four and ranked-choice, Maine federal RCV, Georgia runoffs, Louisiana's new closed congressional primaries, California/Washington top-two, and New York fusion.
- **FEC contributor data** may not be used to solicit contributions or for commercial purposes (52 U.S.C. 30111(a)(4)).
- **Individual voter files** are out of scope. Only aggregate registration and turnout statistics are collected.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest                                   # unit tests (no network, no DB needed)
TEST_DATABASE_URL=postgresql://election:change_me@localhost:5432/elections_test pytest   # + integration tests
```

The integration tests **drop and recreate** the `public` schema of `TEST_DATABASE_URL`. Point it only at a disposable database.
