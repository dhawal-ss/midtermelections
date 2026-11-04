-- =====================================================================
-- US Midterm Election Intelligence Database — PostgreSQL schema
-- Target: 2026 US midterm elections (general: 2026-11-03), 50 states + DC,
-- with historical cycles (2014–2024) for comparison.
--
-- Design principles
--   1. raw_records is immutable evidence. Normalized tables point back to it.
--   2. Every normalized record carries a verification_status.
--   3. A preliminary result can NEVER be stored as certified: enforced by
--      CHECK constraints + a trigger that requires a certification record
--      backed by an official document.
--   4. Every result observation is appended to election_result_snapshots;
--      election_results holds only the latest value per reporting unit.
--   5. Conflicting sources create data_discrepancies; nothing is silently
--      overwritten.
--
-- This file is idempotent: it is safe to run repeatedly.
-- Requires PostgreSQL 15+ (UNIQUE NULLS NOT DISTINCT).
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- Shared domain for verification status
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'verification_status') THEN
        CREATE DOMAIN verification_status AS TEXT
            CHECK (VALUE IN ('official', 'corroborated', 'unverified',
                             'disputed', 'superseded', 'certified'));
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- Migration bookkeeping
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    TEXT
);

-- =====================================================================
-- PROVENANCE
-- =====================================================================
CREATE TABLE IF NOT EXISTS data_sources (
    id              BIGSERIAL PRIMARY KEY,
    source_key      TEXT NOT NULL UNIQUE,          -- e.g. 'openfec', 'state:GA'
    name            TEXT NOT NULL UNIQUE,
    base_url        TEXT,
    source_type     TEXT NOT NULL
        CHECK (source_type IN ('api', 'csv_download', 'web', 'manual', 'reference')),
    authority_level TEXT NOT NULL
        CHECK (authority_level IN (
            'official_election_authority',   -- state/local election office
            'official_federal',              -- FEC, EAC, Census
            'academic',                      -- MIT Election Lab
            'third_party',                   -- election.org, civicAPI, pollsters
            'commercial',                    -- AP
            'project_reference'              -- seeded by this project
        )),
    access_type     TEXT,                         -- free_key, free_no_key, commercial
    terms_url       TEXT,
    attribution     TEXT,
    license_notes   TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id         BIGINT REFERENCES data_sources(id),
    job_name          TEXT NOT NULL,
    parameters        JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    records_received  INTEGER NOT NULL DEFAULT 0,
    records_inserted  INTEGER NOT NULL DEFAULT 0,
    records_unchanged INTEGER NOT NULL DEFAULT 0,
    records_failed    INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source_started ON ingestion_runs(source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_job_started ON ingestion_runs(job_name, started_at DESC);

CREATE TABLE IF NOT EXISTS raw_records (
    id                BIGSERIAL PRIMARY KEY,
    source_id         BIGINT NOT NULL REFERENCES data_sources(id),
    run_id            UUID REFERENCES ingestion_runs(id),
    external_id       TEXT,
    record_type       TEXT NOT NULL,
    payload           JSONB NOT NULL,
    content_hash      TEXT NOT NULL,               -- sha256 of canonical JSON
    source_url        TEXT,                        -- never contains API keys
    source_updated_at TIMESTAMPTZ,
    retrieved_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, record_type, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_records_type ON raw_records(record_type);
CREATE INDEX IF NOT EXISTS idx_raw_records_external ON raw_records(source_id, record_type, external_id);

-- raw_records is append-only evidence
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_raw_records_immutable ON raw_records;
CREATE TRIGGER trg_raw_records_immutable
    BEFORE UPDATE OR DELETE ON raw_records
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Downloaded files (CSV, PDF canvass reports...) archived on disk
CREATE TABLE IF NOT EXISTS raw_files (
    id            BIGSERIAL PRIMARY KEY,
    source_id     BIGINT NOT NULL REFERENCES data_sources(id),
    run_id        UUID REFERENCES ingestion_runs(id),
    source_url    TEXT NOT NULL,
    local_path    TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    byte_size     BIGINT NOT NULL,
    content_type  TEXT,
    retrieved_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, sha256)
);

-- Column fingerprint of each tabular/JSON feed, for schema-change detection
CREATE TABLE IF NOT EXISTS schema_fingerprints (
    id            BIGSERIAL PRIMARY KEY,
    source_id     BIGINT NOT NULL REFERENCES data_sources(id),
    dataset       TEXT NOT NULL,
    columns       TEXT[] NOT NULL,
    fingerprint   TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, dataset, fingerprint)
);

-- Liveness checks of every URL in config/sources.yaml
CREATE TABLE IF NOT EXISTS source_link_checks (
    id            BIGSERIAL PRIMARY KEY,
    state_code    CHAR(2),
    url_kind      TEXT NOT NULL,
    url           TEXT NOT NULL,
    http_status   INTEGER,
    final_url     TEXT,
    ok            BOOLEAN NOT NULL,
    error         TEXT,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_link_checks_url ON source_link_checks(url, checked_at DESC);

-- =====================================================================
-- GEOGRAPHY
-- =====================================================================
CREATE TABLE IF NOT EXISTS states (
    state_code      CHAR(2) PRIMARY KEY,
    state_name      TEXT NOT NULL,
    fips_code       CHAR(2) UNIQUE,
    is_state        BOOLEAN NOT NULL DEFAULT TRUE,     -- FALSE for DC
    house_seats     INTEGER,                           -- 2020 apportionment
    has_voting_house_rep BOOLEAN NOT NULL DEFAULT TRUE
);

-- Integration status of each state's official sources (mirrors sources.yaml)
CREATE TABLE IF NOT EXISTS state_source_registry (
    state_code            CHAR(2) PRIMARY KEY REFERENCES states(state_code),
    election_office_name  TEXT,
    official_election_url TEXT,
    results_url           TEXT,
    candidates_url        TEXT,
    voter_lookup_url      TEXT,
    results_adapter       TEXT,
    adapter_status        TEXT NOT NULL DEFAULT 'not_implemented'
        CHECK (adapter_status IN ('not_implemented', 'manual_csv', 'configured', 'tested', 'production')),
    urls_verified_at      TIMESTAMPTZ,
    notes                 TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jurisdictions (
    id                BIGSERIAL PRIMARY KEY,
    state_code        CHAR(2) NOT NULL REFERENCES states(state_code),
    name              TEXT NOT NULL,
    jurisdiction_type TEXT NOT NULL
        CHECK (jurisdiction_type IN ('state', 'county', 'county_equivalent', 'municipality',
                                     'township', 'school_district', 'judicial_district', 'other')),
    fips_code         TEXT,
    ocd_id            TEXT,                          -- Open Civic Data division id
    UNIQUE (state_code, name, jurisdiction_type)
);
CREATE INDEX IF NOT EXISTS idx_jurisdictions_fips ON jurisdictions(fips_code);

-- Districts are versioned by plan: a 2022 CD may differ from its 2026 successor
CREATE TABLE IF NOT EXISTS districts (
    id              BIGSERIAL PRIMARY KEY,
    state_code      CHAR(2) NOT NULL REFERENCES states(state_code),
    district_type   TEXT NOT NULL
        CHECK (district_type IN ('congressional', 'state_senate', 'state_house', 'statewide', 'other')),
    district_number TEXT NOT NULL,                  -- '00' = at-large
    plan_name       TEXT NOT NULL DEFAULT 'current',
    geo_id          TEXT,                           -- Census GEOID
    ocd_id          TEXT,
    valid_from      DATE NOT NULL DEFAULT DATE '2023-01-03',
    valid_to        DATE,
    UNIQUE (state_code, district_type, district_number, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_districts_geoid ON districts(geo_id);

CREATE TABLE IF NOT EXISTS reporting_units (
    id              BIGSERIAL PRIMARY KEY,
    state_code      CHAR(2) NOT NULL REFERENCES states(state_code),
    unit_type       TEXT NOT NULL
        CHECK (unit_type IN ('state', 'county', 'municipality', 'precinct',
                             'congressional_district', 'legislative_district', 'other')),
    name            TEXT NOT NULL,
    external_code   TEXT NOT NULL DEFAULT '',
    jurisdiction_id BIGINT REFERENCES jurisdictions(id),
    UNIQUE (state_code, unit_type, name, external_code)
);

-- =====================================================================
-- ELECTIONS, OFFICES, CONTESTS
-- =====================================================================
CREATE TABLE IF NOT EXISTS elections (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    election_year   INTEGER NOT NULL,
    election_date   DATE NOT NULL,
    election_type   TEXT NOT NULL
        CHECK (election_type IN ('general', 'primary', 'runoff', 'special_general',
                                 'special_primary', 'special_runoff', 'convention', 'other')),
    state_code      CHAR(2) REFERENCES states(state_code),   -- NULL = nationwide
    status          TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'in_progress', 'counting', 'canvassing', 'certified', 'cancelled')),
    source_id       BIGINT REFERENCES data_sources(id),
    verification_status verification_status NOT NULL DEFAULT 'unverified',
    UNIQUE NULLS NOT DISTINCT (election_date, election_type, state_code)
);

CREATE TABLE IF NOT EXISTS parties (
    party_code  TEXT PRIMARY KEY,                  -- FEC 3-letter code where possible
    party_name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offices (
    id              BIGSERIAL PRIMARY KEY,
    office_code     TEXT NOT NULL UNIQUE,          -- e.g. 'US-HOUSE-TX-07', 'GOV-GA'
    name            TEXT NOT NULL,
    office_level    TEXT NOT NULL
        CHECK (office_level IN ('federal', 'state', 'county', 'municipal', 'judicial', 'special_district')),
    branch          TEXT CHECK (branch IN ('legislative', 'executive', 'judicial') OR branch IS NULL),
    state_code      CHAR(2) REFERENCES states(state_code),
    district_id     BIGINT REFERENCES districts(id),
    jurisdiction_id BIGINT REFERENCES jurisdictions(id),
    seat_class      TEXT                           -- Senate class I/II/III
);

CREATE TABLE IF NOT EXISTS contests (
    id                   BIGSERIAL PRIMARY KEY,
    contest_key          TEXT NOT NULL UNIQUE,     -- deterministic natural key
    election_id          BIGINT NOT NULL REFERENCES elections(id),
    office_id            BIGINT NOT NULL REFERENCES offices(id),
    state_code           CHAR(2) NOT NULL REFERENCES states(state_code),
    stage                TEXT NOT NULL DEFAULT 'general'
        CHECK (stage IN ('general', 'primary', 'runoff', 'special_general', 'special_primary', 'special_runoff')),
    party                TEXT,                     -- for partisan primaries
    is_special           BOOLEAN NOT NULL DEFAULT FALSE,
    seats                INTEGER NOT NULL DEFAULT 1,
    ballot_status        TEXT NOT NULL DEFAULT 'expected'
        CHECK (ballot_status IN ('expected', 'on_ballot', 'uncontested_not_on_ballot', 'cancelled')),
    certification_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (certification_status IN ('unknown', 'not_certified', 'canvass_in_progress',
                                        'certified', 'recount', 'contested')),
    source_id            BIGINT REFERENCES data_sources(id),
    verification_status  verification_status NOT NULL DEFAULT 'unverified',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contests_state ON contests(state_code);
CREATE INDEX IF NOT EXISTS idx_contests_election ON contests(election_id);

-- =====================================================================
-- CANDIDATES
-- =====================================================================
CREATE TABLE IF NOT EXISTS candidates (
    id                  BIGSERIAL PRIMARY KEY,
    canonical_name      TEXT NOT NULL,
    first_name          TEXT,
    last_name           TEXT,
    party               TEXT,
    incumbent           BOOLEAN,
    website             TEXT,
    biography           TEXT,
    verification_status verification_status NOT NULL DEFAULT 'unverified',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_candidates_name ON candidates(canonical_name);
CREATE INDEX IF NOT EXISTS idx_candidates_lastname ON candidates(lower(last_name));

-- Identity is matched on official IDs, never on name alone
CREATE TABLE IF NOT EXISTS candidate_source_ids (
    id            BIGSERIAL PRIMARY KEY,
    candidate_id  BIGINT NOT NULL REFERENCES candidates(id),
    source_id     BIGINT NOT NULL REFERENCES data_sources(id),
    id_type       TEXT NOT NULL DEFAULT 'source_native',   -- fec_candidate_id, state_filing_id...
    external_id   TEXT NOT NULL,
    UNIQUE (source_id, id_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_source_ids_candidate ON candidate_source_ids(candidate_id);
CREATE INDEX IF NOT EXISTS idx_candidate_source_ids_ext ON candidate_source_ids(id_type, external_id);

-- Name-based candidate matches that need human review before merging
CREATE TABLE IF NOT EXISTS candidate_match_queue (
    id                  BIGSERIAL PRIMARY KEY,
    source_record_id    BIGINT REFERENCES raw_records(id),
    proposed_candidate_id BIGINT REFERENCES candidates(id),
    match_method        TEXT NOT NULL,
    match_score         NUMERIC(5,4),
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at         TIMESTAMPTZ
);

-- Every filing event (FEC Form 2, state declaration of candidacy...)
CREATE TABLE IF NOT EXISTS candidate_filings (
    id                BIGSERIAL PRIMARY KEY,
    candidate_id      BIGINT NOT NULL REFERENCES candidates(id),
    contest_id        BIGINT REFERENCES contests(id),
    filing_authority  TEXT NOT NULL,              -- 'FEC', 'GA Secretary of State'...
    filing_type       TEXT NOT NULL,              -- 'statement_of_candidacy', 'ballot_petition'...
    filing_id         TEXT,
    filed_on          DATE,
    status            TEXT,
    source_record_id  BIGINT REFERENCES raw_records(id),
    UNIQUE NULLS NOT DISTINCT (candidate_id, filing_authority, filing_type, filing_id)
);

CREATE TABLE IF NOT EXISTS contest_candidates (
    contest_id               BIGINT NOT NULL REFERENCES contests(id),
    candidate_id             BIGINT NOT NULL REFERENCES candidates(id),
    filing_status            TEXT NOT NULL DEFAULT 'unknown'
        CHECK (filing_status IN ('unknown', 'filed_with_fec', 'filed_with_state', 'withdrawn', 'terminated')),
    ballot_status            TEXT NOT NULL DEFAULT 'unknown'
        CHECK (ballot_status IN ('unknown', 'pending', 'qualified', 'certified_on_ballot',
                                 'write_in', 'withdrawn', 'disqualified', 'lost_primary')),
    ballot_status_evidence_url TEXT,
    ballot_status_source_id  BIGINT REFERENCES data_sources(id),
    party_label              TEXT,
    incumbent_challenge      TEXT,               -- I / C / O (open seat)
    verification_status      verification_status NOT NULL DEFAULT 'unverified',
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contest_id, candidate_id),
    -- FEC registration is NOT ballot qualification: qualification needs evidence
    CONSTRAINT ballot_qualification_needs_evidence CHECK (
        ballot_status NOT IN ('qualified', 'certified_on_ballot')
        OR (ballot_status_evidence_url IS NOT NULL AND ballot_status_source_id IS NOT NULL)
    )
);

-- =====================================================================
-- CAMPAIGN FINANCE (OpenFEC)
-- =====================================================================
CREATE TABLE IF NOT EXISTS campaign_committees (
    id              BIGSERIAL PRIMARY KEY,
    external_id     TEXT NOT NULL UNIQUE,          -- FEC committee_id (C00...)
    name            TEXT,
    committee_type  TEXT,
    designation     TEXT,                          -- P = principal campaign committee
    party           TEXT,
    state_code      CHAR(2),
    treasurer_name  TEXT,
    first_file_date DATE,
    last_file_date  DATE,
    source_record_id BIGINT REFERENCES raw_records(id),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candidate_committees (
    candidate_id  BIGINT NOT NULL REFERENCES candidates(id),
    committee_id  BIGINT NOT NULL REFERENCES campaign_committees(id),
    cycle         INTEGER NOT NULL,
    designation   TEXT,
    PRIMARY KEY (candidate_id, committee_id, cycle)
);

CREATE TABLE IF NOT EXISTS financial_summaries (
    id                    BIGSERIAL PRIMARY KEY,
    candidate_id          BIGINT REFERENCES candidates(id),
    committee_id          BIGINT REFERENCES campaign_committees(id),
    cycle                 INTEGER NOT NULL,
    coverage_start_date   DATE,
    coverage_end_date     DATE,
    receipts              NUMERIC(16,2),
    disbursements         NUMERIC(16,2),
    cash_on_hand_end      NUMERIC(16,2),
    debts_owed            NUMERIC(16,2),
    individual_contributions NUMERIC(16,2),
    pac_contributions     NUMERIC(16,2),
    candidate_contributions NUMERIC(16,2),
    source_record_id      BIGINT REFERENCES raw_records(id),
    retrieved_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE NULLS NOT DISTINCT (candidate_id, cycle, coverage_end_date)
);

CREATE TABLE IF NOT EXISTS contributions (
    id                BIGSERIAL PRIMARY KEY,
    fec_sub_id        TEXT UNIQUE,                 -- FEC unique row id
    committee_id      BIGINT REFERENCES campaign_committees(id),
    candidate_id      BIGINT REFERENCES candidates(id),
    amount            NUMERIC(16,2),
    contribution_date DATE,
    contributor_name  TEXT,
    contributor_type  TEXT,                        -- entity_type: IND, PAC, ORG...
    contributor_city  TEXT,
    contributor_state CHAR(2),
    contributor_employer   TEXT,
    contributor_occupation TEXT,
    receipt_type      TEXT,
    memo_code         TEXT,
    file_number       TEXT,                        -- FEC filing identifier
    image_number      TEXT,
    report_type       TEXT,
    report_year       INTEGER,
    source_record_id  BIGINT REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_contributions_date ON contributions(contribution_date);
CREATE INDEX IF NOT EXISTS idx_contributions_committee ON contributions(committee_id);
CREATE INDEX IF NOT EXISTS idx_contributions_candidate ON contributions(candidate_id);

CREATE TABLE IF NOT EXISTS expenditures (
    id                BIGSERIAL PRIMARY KEY,
    fec_sub_id        TEXT UNIQUE,
    expenditure_kind  TEXT NOT NULL DEFAULT 'operating'
        CHECK (expenditure_kind IN ('operating', 'independent', 'coordinated', 'electioneering')),
    committee_id      BIGINT REFERENCES campaign_committees(id),
    candidate_id      BIGINT REFERENCES candidates(id),   -- for IEs: candidate supported/opposed
    support_oppose    CHAR(1) CHECK (support_oppose IN ('S', 'O') OR support_oppose IS NULL),
    amount            NUMERIC(16,2),
    expenditure_date  DATE,
    payee             TEXT,
    purpose           TEXT,
    file_number       TEXT,
    image_number      TEXT,
    source_record_id  BIGINT REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_expenditures_committee ON expenditures(committee_id);
CREATE INDEX IF NOT EXISTS idx_expenditures_candidate ON expenditures(candidate_id);
CREATE INDEX IF NOT EXISTS idx_expenditures_date ON expenditures(expenditure_date);

-- =====================================================================
-- POLLING (third-party research — never official)
-- =====================================================================
CREATE TABLE IF NOT EXISTS polls (
    id                BIGSERIAL PRIMARY KEY,
    poll_key          TEXT NOT NULL UNIQUE,        -- source + external id
    contest_id        BIGINT REFERENCES contests(id),
    contest_label     TEXT,                        -- as published, when unmatched
    state_code        CHAR(2) REFERENCES states(state_code),
    pollster          TEXT NOT NULL,
    sponsor           TEXT,
    sponsor_partisan  TEXT,                        -- D / R / I / NULL
    population        TEXT CHECK (population IN ('lv', 'rv', 'a', 'v') OR population IS NULL),
    sample_size       INTEGER CHECK (sample_size > 0 OR sample_size IS NULL),
    field_start       DATE,
    field_end         DATE,
    margin_of_error   NUMERIC(6,3),
    methodology       TEXT,
    results           JSONB NOT NULL,
    source_url        TEXT,
    source_record_id  BIGINT REFERENCES raw_records(id),
    verification_status verification_status NOT NULL DEFAULT 'unverified',
    CHECK (field_end IS NULL OR field_start IS NULL OR field_end >= field_start)
);
CREATE INDEX IF NOT EXISTS idx_polls_contest ON polls(contest_id);
CREATE INDEX IF NOT EXISTS idx_polls_field_end ON polls(field_end);

CREATE TABLE IF NOT EXISTS poll_answers (
    poll_id       BIGINT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    choice        TEXT NOT NULL,
    candidate_id  BIGINT REFERENCES candidates(id),
    pct           NUMERIC(6,3),
    PRIMARY KEY (poll_id, choice)
);

-- =====================================================================
-- BALLOT MEASURES, POLLING PLACES
-- =====================================================================
CREATE TABLE IF NOT EXISTS ballot_measures (
    id                BIGSERIAL PRIMARY KEY,
    measure_key       TEXT NOT NULL UNIQUE,
    election_id       BIGINT REFERENCES elections(id),
    state_code        CHAR(2) REFERENCES states(state_code),
    jurisdiction_id   BIGINT REFERENCES jurisdictions(id),
    measure_number    TEXT,
    title             TEXT,
    ballot_text       TEXT,
    measure_type      TEXT,                        -- initiative, referendum, legislative...
    status            TEXT,
    outcome           TEXT CHECK (outcome IN ('passed', 'failed', 'pending', 'unknown') OR outcome IS NULL),
    outcome_certified BOOLEAN NOT NULL DEFAULT FALSE,
    yes_votes         BIGINT,
    no_votes          BIGINT,
    source_record_id  BIGINT REFERENCES raw_records(id),
    verification_status verification_status NOT NULL DEFAULT 'unverified'
);

CREATE TABLE IF NOT EXISTS polling_locations (
    id                BIGSERIAL PRIMARY KEY,
    election_id       BIGINT REFERENCES elections(id),
    jurisdiction_id   BIGINT REFERENCES jurisdictions(id),
    state_code        CHAR(2) REFERENCES states(state_code),
    location_name     TEXT,
    address           TEXT,
    location_type     TEXT CHECK (location_type IN ('election_day', 'early_voting', 'drop_box', 'other')
                                  OR location_type IS NULL),
    opens_on          DATE,
    closes_on         DATE,
    hours             TEXT,
    source_record_id  BIGINT REFERENCES raw_records(id),
    verification_status verification_status NOT NULL DEFAULT 'unverified'
);

-- =====================================================================
-- RESULTS — preliminary vs certified is enforced in the database
-- =====================================================================

-- Official certification of a contest by the responsible authority.
CREATE TABLE IF NOT EXISTS result_certifications (
    id                    BIGSERIAL PRIMARY KEY,
    contest_id            BIGINT NOT NULL REFERENCES contests(id),
    certifying_authority  TEXT NOT NULL,         -- e.g. 'Georgia Secretary of State'
    certified_at          TIMESTAMPTZ NOT NULL,
    document_url          TEXT NOT NULL,         -- official canvass / certificate
    document_sha256       TEXT,                  -- hash of archived document
    source_id             BIGINT NOT NULL REFERENCES data_sources(id),
    status                TEXT NOT NULL DEFAULT 'certified'
        CHECK (status IN ('certified', 'amended', 'rescinded')),
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_certifications_contest ON result_certifications(contest_id);

-- Certifications may only come from an official election authority
CREATE OR REPLACE FUNCTION check_certification_source() RETURNS trigger AS $$
DECLARE
    lvl TEXT;
BEGIN
    SELECT authority_level INTO lvl FROM data_sources WHERE id = NEW.source_id;
    IF lvl IS DISTINCT FROM 'official_election_authority' THEN
        RAISE EXCEPTION 'Certification must come from an official election authority source (got %)', lvl;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_certification_source ON result_certifications;
CREATE TRIGGER trg_certification_source
    BEFORE INSERT OR UPDATE ON result_certifications
    FOR EACH ROW EXECUTE FUNCTION check_certification_source();

-- Latest known value per (contest, candidate, reporting unit)
CREATE TABLE IF NOT EXISTS election_results (
    id                   BIGSERIAL PRIMARY KEY,
    contest_id           BIGINT NOT NULL REFERENCES contests(id),
    candidate_id         BIGINT REFERENCES candidates(id),
    choice_label         TEXT NOT NULL,          -- candidate name / 'Write-ins' / 'Yes'
    reporting_unit_id    BIGINT NOT NULL REFERENCES reporting_units(id),
    votes                BIGINT CHECK (votes IS NULL OR votes >= 0),
    vote_percentage      NUMERIC(9,5) CHECK (vote_percentage IS NULL OR vote_percentage BETWEEN 0 AND 100),
    precincts_reporting  INTEGER CHECK (precincts_reporting IS NULL OR precincts_reporting >= 0),
    precincts_total      INTEGER CHECK (precincts_total IS NULL OR precincts_total >= 0),
    expected_vote_pct    NUMERIC(6,3),
    result_stage         TEXT NOT NULL
        CHECK (result_stage IN ('preliminary', 'unofficial_complete', 'canvassed', 'certified', 'amended_certified')),
    is_certified         BOOLEAN NOT NULL DEFAULT FALSE,
    certification_id     BIGINT REFERENCES result_certifications(id),
    source_id            BIGINT NOT NULL REFERENCES data_sources(id),
    source_record_id     BIGINT REFERENCES raw_records(id),
    source_reported_at   TIMESTAMPTZ,            -- timestamp published by the source
    verification_status  verification_status NOT NULL DEFAULT 'unverified',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (contest_id, choice_label, reporting_unit_id),
    -- is_certified and the stage must agree, and certification needs a record
    CONSTRAINT certified_flag_matches_stage CHECK (
        is_certified = (result_stage IN ('certified', 'amended_certified'))
    ),
    CONSTRAINT certified_requires_certification CHECK (
        NOT is_certified OR certification_id IS NOT NULL
    ),
    CONSTRAINT certified_verification_consistent CHECK (
        (verification_status = 'certified') = is_certified
    ),
    CHECK (precincts_total IS NULL OR precincts_reporting IS NULL OR precincts_reporting <= precincts_total)
);
CREATE INDEX IF NOT EXISTS idx_results_contest ON election_results(contest_id);
CREATE INDEX IF NOT EXISTS idx_results_unit ON election_results(reporting_unit_id);

-- A certification row must belong to the same contest and be in force
CREATE OR REPLACE FUNCTION check_result_certification() RETURNS trigger AS $$
DECLARE
    cert RECORD;
BEGIN
    IF NEW.is_certified THEN
        IF NEW.certification_id IS NULL THEN
            RAISE EXCEPTION 'Certified result requires a result_certifications record (contest %)', NEW.contest_id;
        END IF;
        SELECT * INTO cert FROM result_certifications WHERE id = NEW.certification_id;
        IF cert.contest_id IS DISTINCT FROM NEW.contest_id THEN
            RAISE EXCEPTION 'Certification % does not belong to contest %', NEW.certification_id, NEW.contest_id;
        END IF;
        IF cert.status = 'rescinded' THEN
            RAISE EXCEPTION 'Certification % has been rescinded', NEW.certification_id;
        END IF;
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_result_certification ON election_results;
CREATE TRIGGER trg_result_certification
    BEFORE INSERT OR UPDATE ON election_results
    FOR EACH ROW EXECUTE FUNCTION check_result_certification();

-- Append-only history: every observation from every source
CREATE TABLE IF NOT EXISTS election_result_snapshots (
    id                   BIGSERIAL PRIMARY KEY,
    contest_id           BIGINT NOT NULL REFERENCES contests(id),
    candidate_id         BIGINT REFERENCES candidates(id),
    choice_label         TEXT NOT NULL,
    reporting_unit_id    BIGINT NOT NULL REFERENCES reporting_units(id),
    votes                BIGINT,
    vote_percentage      NUMERIC(9,5),
    precincts_reporting  INTEGER,
    precincts_total      INTEGER,
    expected_vote_pct    NUMERIC(6,3),
    result_stage         TEXT NOT NULL,
    source_id            BIGINT NOT NULL REFERENCES data_sources(id),
    source_record_id     BIGINT REFERENCES raw_records(id),
    source_reported_at   TIMESTAMPTZ,
    observed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_snapshots_contest_time ON election_result_snapshots(contest_id, observed_at);

DROP TRIGGER IF EXISTS trg_snapshots_immutable ON election_result_snapshots;
CREATE TRIGGER trg_snapshots_immutable
    BEFORE UPDATE OR DELETE ON election_result_snapshots
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Media/organization race calls are projections, NOT certification
CREATE TABLE IF NOT EXISTS race_calls (
    id                BIGSERIAL PRIMARY KEY,
    contest_id        BIGINT NOT NULL REFERENCES contests(id),
    caller            TEXT NOT NULL,             -- 'AP', 'civicAPI', ...
    winner_candidate_id BIGINT REFERENCES candidates(id),
    winner_label      TEXT,
    called_at         TIMESTAMPTZ NOT NULL,
    retracted_at      TIMESTAMPTZ,
    source_id         BIGINT NOT NULL REFERENCES data_sources(id),
    source_record_id  BIGINT REFERENCES raw_records(id),
    UNIQUE (contest_id, caller, called_at)
);

CREATE TABLE IF NOT EXISTS recounts (
    id                BIGSERIAL PRIMARY KEY,
    contest_id        BIGINT NOT NULL REFERENCES contests(id),
    recount_type      TEXT NOT NULL CHECK (recount_type IN ('automatic', 'requested', 'court_ordered', 'audit')),
    status            TEXT NOT NULL CHECK (status IN ('announced', 'in_progress', 'completed', 'cancelled')),
    requested_by      TEXT,
    started_on        DATE,
    completed_on      DATE,
    margin_before     BIGINT,
    margin_after      BIGINT,
    document_url      TEXT,
    source_id         BIGINT REFERENCES data_sources(id),
    source_record_id  BIGINT REFERENCES raw_records(id),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Historical returns (MIT Election Lab and official archives), 1976+
CREATE TABLE IF NOT EXISTS historical_results (
    id                BIGSERIAL PRIMARY KEY,
    year              INTEGER NOT NULL,
    state_code        CHAR(2) NOT NULL REFERENCES states(state_code),
    office            TEXT NOT NULL,             -- US HOUSE, US SENATE, US PRESIDENT
    district          TEXT,                      -- as published (district plan of that year)
    stage             TEXT,                      -- GEN, PRI
    is_special        BOOLEAN NOT NULL DEFAULT FALSE,
    is_runoff         BOOLEAN NOT NULL DEFAULT FALSE,
    candidate         TEXT,
    party             TEXT,
    is_writein        BOOLEAN NOT NULL DEFAULT FALSE,
    mode              TEXT,
    candidate_votes   BIGINT,
    total_votes       BIGINT,
    is_unofficial     BOOLEAN NOT NULL DEFAULT FALSE,
    dataset_version   TEXT,
    source_id         BIGINT NOT NULL REFERENCES data_sources(id),
    row_hash          TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_hist_year_state_office ON historical_results(year, state_code, office, district);

-- =====================================================================
-- VOTER STATISTICS (aggregate only — no individual voter records)
-- =====================================================================
CREATE TABLE IF NOT EXISTS voter_statistics (
    id                BIGSERIAL PRIMARY KEY,
    election_id       BIGINT REFERENCES elections(id),
    state_code        CHAR(2) REFERENCES states(state_code),
    jurisdiction_id   BIGINT REFERENCES jurisdictions(id),
    metric            TEXT NOT NULL,              -- 'ballots_cast', 'turnout_vep_pct', ...
    metric_value      NUMERIC(18,4),
    population        TEXT,
    reporting_period  TEXT,
    as_of             TIMESTAMPTZ,
    source_id         BIGINT REFERENCES data_sources(id),
    source_record_id  BIGINT REFERENCES raw_records(id),
    verification_status verification_status NOT NULL DEFAULT 'unverified',
    UNIQUE NULLS NOT DISTINCT (election_id, state_code, jurisdiction_id, metric, reporting_period, source_id)
);

CREATE TABLE IF NOT EXISTS voter_registration_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    state_code        CHAR(2) NOT NULL REFERENCES states(state_code),
    jurisdiction_id   BIGINT REFERENCES jurisdictions(id),
    as_of_date        DATE NOT NULL,
    party             TEXT NOT NULL DEFAULT 'ALL',
    status            TEXT NOT NULL DEFAULT 'active',  -- active / inactive / total
    registered_voters BIGINT NOT NULL CHECK (registered_voters >= 0),
    source_id         BIGINT REFERENCES data_sources(id),
    source_record_id  BIGINT REFERENCES raw_records(id),
    UNIQUE NULLS NOT DISTINCT (state_code, jurisdiction_id, as_of_date, party, status)
);

CREATE TABLE IF NOT EXISTS advance_voting_statistics (
    id                BIGSERIAL PRIMARY KEY,
    election_id       BIGINT REFERENCES elections(id),
    state_code        CHAR(2) NOT NULL REFERENCES states(state_code),
    jurisdiction_id   BIGINT REFERENCES jurisdictions(id),
    voting_mode       TEXT NOT NULL CHECK (voting_mode IN ('early_in_person', 'mail', 'all_advance')),
    metric            TEXT NOT NULL CHECK (metric IN ('requested', 'sent', 'returned', 'accepted',
                                                       'rejected', 'cast')),
    value             BIGINT NOT NULL CHECK (value >= 0),
    as_of             TIMESTAMPTZ NOT NULL,
    source_id         BIGINT REFERENCES data_sources(id),
    source_record_id  BIGINT REFERENCES raw_records(id),
    UNIQUE NULLS NOT DISTINCT (election_id, state_code, jurisdiction_id, voting_mode, metric, as_of)
);

-- Census ACS context for districts / states
CREATE TABLE IF NOT EXISTS district_demographics (
    id                BIGSERIAL PRIMARY KEY,
    state_code        CHAR(2) NOT NULL REFERENCES states(state_code),
    district_id       BIGINT REFERENCES districts(id),
    geography_level   TEXT NOT NULL CHECK (geography_level IN ('state', 'congressional_district', 'county')),
    geo_id            TEXT NOT NULL,
    dataset           TEXT NOT NULL,              -- e.g. 'acs/acs1 2024'
    variable          TEXT NOT NULL,              -- e.g. 'B01003_001E'
    variable_label    TEXT,
    value             NUMERIC(20,4),
    source_record_id  BIGINT REFERENCES raw_records(id),
    UNIQUE (geo_id, dataset, variable)
);

-- =====================================================================
-- VERIFICATION & QUALITY
-- =====================================================================
CREATE TABLE IF NOT EXISTS source_verifications (
    id                  BIGSERIAL PRIMARY KEY,
    source_record_id    BIGINT REFERENCES raw_records(id),
    entity_table        TEXT,
    entity_id           BIGINT,
    verification_type   TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('passed', 'failed', 'warning')),
    details             JSONB,
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_verifications_entity ON source_verifications(entity_table, entity_id);

-- Conflicting sources are preserved, never silently resolved
CREATE TABLE IF NOT EXISTS data_discrepancies (
    id                BIGSERIAL PRIMARY KEY,
    discrepancy_key   TEXT NOT NULL UNIQUE,      -- deterministic, prevents duplicates
    entity_table      TEXT NOT NULL,
    entity_id         BIGINT,
    field_name        TEXT,
    discrepancy_type  TEXT NOT NULL,
    values_by_source  JSONB NOT NULL,             -- {"openfec": ..., "election_org": ...}
    severity          TEXT NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'accepted')),
    resolution_note   TEXT,
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_detected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS data_quality_reports (
    id            BIGSERIAL PRIMARY KEY,
    report_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics       JSONB NOT NULL
);

-- =====================================================================
-- VIEWS
-- =====================================================================

-- Results with an unambiguous public label. Use this view for display.
CREATE OR REPLACE VIEW v_results_labeled AS
SELECT
    r.id,
    c.contest_key,
    c.state_code,
    o.name                        AS office,
    r.choice_label,
    ru.unit_type                  AS reporting_unit_type,
    ru.name                       AS reporting_unit,
    r.votes,
    r.vote_percentage,
    r.precincts_reporting,
    r.precincts_total,
    r.result_stage,
    r.is_certified,
    CASE
        WHEN r.is_certified THEN 'CERTIFIED'
        WHEN r.result_stage = 'canvassed' THEN 'CANVASSED — NOT YET CERTIFIED'
        WHEN r.result_stage = 'unofficial_complete' THEN 'UNOFFICIAL — NOT CERTIFIED'
        ELSE 'PRELIMINARY — NOT CERTIFIED'
    END                           AS display_label,
    rc.certifying_authority,
    rc.certified_at,
    rc.document_url               AS certification_document,
    ds.name                       AS source_name,
    ds.authority_level            AS source_authority,
    r.source_reported_at,
    r.updated_at
FROM election_results r
JOIN contests c          ON c.id = r.contest_id
JOIN offices o           ON o.id = c.office_id
JOIN reporting_units ru  ON ru.id = r.reporting_unit_id
JOIN data_sources ds     ON ds.id = r.source_id
LEFT JOIN result_certifications rc ON rc.id = r.certification_id;

CREATE OR REPLACE VIEW v_contest_summary AS
SELECT
    c.id AS contest_id,
    c.contest_key,
    c.state_code,
    o.name AS office,
    o.office_level,
    e.election_date,
    c.stage,
    c.is_special,
    c.certification_status,
    COUNT(DISTINCT cc.candidate_id)                                         AS candidates,
    COUNT(DISTINCT cc.candidate_id) FILTER (WHERE cc.ballot_status IN ('qualified', 'certified_on_ballot'))
                                                                            AS ballot_qualified_candidates,
    COUNT(DISTINCT p.id)                                                    AS polls,
    EXISTS (SELECT 1 FROM election_results er WHERE er.contest_id = c.id)   AS has_results,
    EXISTS (SELECT 1 FROM result_certifications rc
            WHERE rc.contest_id = c.id AND rc.status <> 'rescinded')        AS has_certification
FROM contests c
JOIN offices o   ON o.id = c.office_id
JOIN elections e ON e.id = c.election_id
LEFT JOIN contest_candidates cc ON cc.contest_id = c.id
LEFT JOIN polls p ON p.contest_id = c.id
GROUP BY c.id, o.name, o.office_level, e.election_date;

CREATE OR REPLACE VIEW v_state_coverage AS
SELECT
    s.state_code,
    s.state_name,
    s.house_seats,
    ssr.adapter_status,
    ssr.urls_verified_at,
    (SELECT COUNT(*) FROM contests c WHERE c.state_code = s.state_code)                AS contests,
    (SELECT COUNT(DISTINCT cc.candidate_id) FROM contest_candidates cc
       JOIN contests c ON c.id = cc.contest_id WHERE c.state_code = s.state_code)      AS candidates,
    (SELECT COUNT(*) FROM election_results r JOIN contests c ON c.id = r.contest_id
       WHERE c.state_code = s.state_code)                                              AS result_rows,
    (SELECT COUNT(*) FROM result_certifications rc JOIN contests c ON c.id = rc.contest_id
       WHERE c.state_code = s.state_code AND rc.status <> 'rescinded')                 AS certified_contests,
    (SELECT COUNT(*) FROM polls p WHERE p.state_code = s.state_code)                   AS polls
FROM states s
LEFT JOIN state_source_registry ssr ON ssr.state_code = s.state_code;

CREATE OR REPLACE VIEW v_latest_ingestion AS
SELECT DISTINCT ON (r.job_name)
    r.job_name, ds.source_key, r.status, r.started_at, r.finished_at,
    r.records_received, r.records_inserted, r.records_unchanged, r.records_failed, r.error_message
FROM ingestion_runs r
LEFT JOIN data_sources ds ON ds.id = r.source_id
ORDER BY r.job_name, r.started_at DESC;
