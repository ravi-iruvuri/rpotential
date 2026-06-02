-- migration_004_model_weights.sql
-- Add per-feature weight columns to model_versions so v_priority_score_calc
-- can read blended/learned weights from the active model instead of
-- hardcoded heuristic constants.
-- Defaults match the original heuristic weights so existing rows remain valid.
ALTER TABLE rpotential.model_versions
    ADD COLUMN IF NOT EXISTS w_company      NUMERIC(6,4) NOT NULL DEFAULT 0.4500,
    ADD COLUMN IF NOT EXISTS w_category     NUMERIC(6,4) NOT NULL DEFAULT 0.2000,
    ADD COLUMN IF NOT EXISTS w_openings     NUMERIC(6,4) NOT NULL DEFAULT 0.1500,
    ADD COLUMN IF NOT EXISTS w_job_type     NUMERIC(6,4) NOT NULL DEFAULT 0.1000,
    ADD COLUMN IF NOT EXISTS w_client_type  NUMERIC(6,4) NOT NULL DEFAULT 0.0500,
    ADD COLUMN IF NOT EXISTS w_hour_bin     NUMERIC(6,4) NOT NULL DEFAULT 0.0500;
