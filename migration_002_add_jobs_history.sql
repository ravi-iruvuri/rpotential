-- ============================================================================
--  Migration 002 — Add jobs_history audit table and capture trigger
--
--  Adds:
--    1. jobs_history table (SCD Type 4 — prior versions of every UPDATE)
--    2. Indexes on (job_id, superseded_at) and (superseded_by_upload_id)
--    3. Trigger function jobs_history_capture()
--    4. BEFORE UPDATE trigger trg_jobs_history_capture on jobs
--
--  After this migration runs, the loader will switch from
--    ON CONFLICT (job_id) DO NOTHING
--  to
--    ON CONFLICT (job_id) DO UPDATE ... WHERE source_row_hash differs
--  so re-uploads with corrections will update jobs and the trigger will
--  preserve the prior row in jobs_history.
--
--  Idempotent: CREATE … IF NOT EXISTS, CREATE OR REPLACE FUNCTION, and a
--  DROP TRIGGER IF EXISTS guard. Safe to re-run.
--  No backfill needed — history begins now.
-- ============================================================================

SET search_path = jop, public;

-- 1. Audit table
CREATE TABLE IF NOT EXISTS jobs_history (
    history_id              BIGSERIAL    PRIMARY KEY,
    job_id                  BIGINT       NOT NULL,
    prior_upload_id         BIGINT       REFERENCES job_uploads(upload_id),
    superseded_by_upload_id BIGINT       REFERENCES job_uploads(upload_id),
    superseded_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    vms_req_number          TEXT,
    status                  TEXT,
    num_openings            INT,
    job_title               TEXT,
    category                TEXT,
    category_imputed        BOOLEAN,
    required_skill          TEXT,
    publishing_status       TEXT,
    job_type                TEXT,
    date_added              TIMESTAMPTZ,
    client_type             TEXT,
    city                    TEXT,
    state_or_province       TEXT,
    country_of_placement    TEXT,
    company_id              BIGINT,
    source_row_hash         TEXT
);

-- 2. Indexes
CREATE INDEX IF NOT EXISTS ix_jobs_history_job_id
    ON jobs_history (job_id, superseded_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_history_superseded_by
    ON jobs_history (superseded_by_upload_id);

-- 3. Trigger function — captures OLD when data fields change.
--    Hash check skips audit-only updates (e.g. category_imputed flip).
CREATE OR REPLACE FUNCTION jobs_history_capture() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.source_row_hash IS DISTINCT FROM NEW.source_row_hash THEN
        INSERT INTO jobs_history (
            job_id, prior_upload_id, superseded_by_upload_id,
            vms_req_number, status, num_openings, job_title, category,
            category_imputed, required_skill, publishing_status, job_type,
            date_added, client_type, city, state_or_province,
            country_of_placement, company_id, source_row_hash
        ) VALUES (
            OLD.job_id, OLD.upload_id, NEW.upload_id,
            OLD.vms_req_number, OLD.status, OLD.num_openings, OLD.job_title, OLD.category,
            OLD.category_imputed, OLD.required_skill, OLD.publishing_status, OLD.job_type,
            OLD.date_added, OLD.client_type, OLD.city, OLD.state_or_province,
            OLD.country_of_placement, OLD.company_id, OLD.source_row_hash
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 4. Trigger
DROP TRIGGER IF EXISTS trg_jobs_history_capture ON jobs;
CREATE TRIGGER trg_jobs_history_capture
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION jobs_history_capture();

-- ============================================================================
-- Verification queries:
--   SELECT COUNT(*) FROM jop.jobs_history;       -- expect 0 immediately after
--   \d+ jop.jobs                                 -- trigger should appear
--   SELECT tgname FROM pg_trigger
--    WHERE tgrelid = 'jop.jobs'::regclass;
-- ============================================================================
