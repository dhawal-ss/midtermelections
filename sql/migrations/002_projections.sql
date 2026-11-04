-- Live projection model: priors, per-race projection snapshots, chamber control simulations.
-- Projections are MODEL ESTIMATES. They are stored apart from results and never touch
-- election_results, certification, or race_calls.

CREATE TABLE IF NOT EXISTS race_priors (
    contest_id      BIGINT PRIMARY KEY REFERENCES contests(id),
    prior_margin    NUMERIC(7,4) NOT NULL,      -- expected DEM minus REP two-party share, -1..1
    prior_sd        NUMERIC(7,4) NOT NULL,      -- uncertainty of prior_margin
    turnout_prior   BIGINT,                     -- expected total votes, if known
    basis           TEXT NOT NULL,              -- e.g. '2024 House result', 'none'
    details         JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS projection_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    contest_id            BIGINT NOT NULL REFERENCES contests(id),
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version         TEXT NOT NULL,
    inputs_hash           TEXT NOT NULL,
    votes_counted         BIGINT NOT NULL DEFAULT 0,
    pct_reporting         NUMERIC(6,3),
    expected_total_votes  BIGINT,
    expected_total_low    BIGINT,
    expected_total_high   BIGINT,
    leader_label          TEXT,
    leader_party          TEXT,
    leader_win_prob       NUMERIC(7,6),
    model_status          TEXT NOT NULL
        CHECK (model_status IN ('no_results', 'prior_only', 'tossup', 'lean', 'likely', 'projected', 'final_count')),
    candidates            JSONB NOT NULL,       -- per candidate: votes, share, proj_share, low, high, win_prob
    inputs                JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_projection_contest_time ON projection_snapshots(contest_id, computed_at DESC);

DROP TRIGGER IF EXISTS trg_projection_snapshots_immutable ON projection_snapshots;
CREATE TRIGGER trg_projection_snapshots_immutable
    BEFORE UPDATE OR DELETE ON projection_snapshots
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE IF NOT EXISTS chamber_projections (
    id               BIGSERIAL PRIMARY KEY,
    chamber          TEXT NOT NULL CHECK (chamber IN ('senate', 'house')),
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version    TEXT NOT NULL,
    simulations      INTEGER NOT NULL,
    seats_needed     INTEGER NOT NULL,
    expected_seats   JSONB NOT NULL,           -- {"DEM": 48.2, "REP": 51.1, "OTHER": 0.7}
    control_prob     JSONB NOT NULL,           -- {"DEM": 0.31, "REP": 0.69}
    seat_distribution JSONB NOT NULL,          -- {"DEM": {"45": 0.01, ...}}
    seats_decided    JSONB NOT NULL,           -- seats where the model status is projected/final_count
    holdovers        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_chamber_proj_time ON chamber_projections(chamber, computed_at DESC);

-- Latest projection per contest, for the live API
CREATE OR REPLACE VIEW v_latest_projections AS
SELECT DISTINCT ON (p.contest_id) p.*, c.contest_key, c.state_code, o.office_code, o.name AS office_name,
       c.certification_status
FROM projection_snapshots p
JOIN contests c ON c.id = p.contest_id
JOIN offices o ON o.id = c.office_id
ORDER BY p.contest_id, p.computed_at DESC, p.id DESC;

-- Party label as published by the results source (needed when a result line is not
-- linked to an FEC candidate record).
ALTER TABLE election_results ADD COLUMN IF NOT EXISTS party_label TEXT;
ALTER TABLE election_result_snapshots ADD COLUMN IF NOT EXISTS party_label TEXT;
