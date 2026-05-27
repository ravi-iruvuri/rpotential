-- ============================================================================
--  Migration 001 — Add job_uploads batch tracking
--
--  Adds:
--    1. job_uploads table (one row per ingest batch)
--    2. jobs.upload_id column (nullable BIGINT FK)
--    3. ix_jobs_upload_id index
--    4. ix_job_uploads_uploaded_at index
--    5. Backfill: one synthetic 'historical_initial_load' upload row,
--       all existing jobs tagged with it.
--
--  Idempotent: re-running is safe (uses IF NOT EXISTS / WHERE upload_id IS NULL).
-- ============================================================================

SET search_path = jop, public;

-- 1. New table
CREATE TABLE IF NOT EXISTS job_uploads (
    upload_id       BIGSERIAL    PRIMARY KEY,
    uploaded_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source          TEXT,
    source_file     TEXT,
    source_hash     TEXT,
    row_count       INT,
    uploaded_by     TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS ix_job_uploads_uploaded_at
    ON job_uploads (uploaded_at DESC);

-- 2. New column on jobs (nullable so existing rows survive)
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS upload_id BIGINT REFERENCES job_uploads(upload_id);

-- 3. Index
CREATE INDEX IF NOT EXISTS ix_jobs_upload_id
    ON jobs (upload_id) WHERE upload_id IS NOT NULL;

-- 4. Backfill: bucket every pre-existing job into one synthetic batch
--    keyed off the earliest ingested_at. Safe to re-run — only fires
--    if jobs with NULL upload_id still exist.
DO $$
DECLARE
    v_backfill_id BIGINT;
    v_pending     INT;
BEGIN
    SELECT COUNT(*) INTO v_pending FROM jobs WHERE upload_id IS NULL;
    IF v_pending = 0 THEN
        RAISE NOTICE 'no jobs require backfill; skipping';
        RETURN;
    END IF;

    INSERT INTO job_uploads
        (uploaded_at, source, source_file, row_count, uploaded_by, notes)
    SELECT MIN(ingested_at),
           'csv',
           'historical_initial_load',
           v_pending,
           'migration_001',
           'Synthetic batch — wraps every job loaded before upload tracking '
           'was added. Created by migration_001_add_job_uploads.sql.'
      FROM jobs
     WHERE upload_id IS NULL
    RETURNING upload_id INTO v_backfill_id;

    UPDATE jobs SET upload_id = v_backfill_id WHERE upload_id IS NULL;

    RAISE NOTICE 'backfilled % jobs into job_uploads.upload_id = %',
                 v_pending, v_backfill_id;
END$$;

-- ============================================================================
-- Verification queries (run after migration):
--
--   SELECT * FROM jop.job_uploads ORDER BY upload_id;
--   SELECT upload_id, COUNT(*) FROM jop.jobs GROUP BY upload_id;
--   SELECT COUNT(*) FROM jop.jobs WHERE upload_id IS NULL;  -- expect 0
-- ============================================================================
