-- migration_003_add_score_hash.sql
-- Adds source_row_hash to job_priority_scores to support the idempotency
-- guard in score_and_route.py. Existing rows get NULL (scored before this
-- migration), which means they will be re-scored once on the next run.
ALTER TABLE rpotential.job_priority_scores
    ADD COLUMN IF NOT EXISTS source_row_hash TEXT;
