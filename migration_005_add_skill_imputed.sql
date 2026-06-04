-- migration_005_add_skill_imputed.sql
-- Adds skill_imputed flag to jobs and jobs_history, and extends the
-- jobs_history_capture trigger to include it.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS skill_imputed BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE jobs_history
    ADD COLUMN IF NOT EXISTS skill_imputed BOOLEAN;

-- Rebuild trigger function to capture skill_imputed in history rows.
CREATE OR REPLACE FUNCTION jobs_history_capture() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.source_row_hash IS DISTINCT FROM NEW.source_row_hash THEN
        INSERT INTO jobs_history (
            job_id, prior_upload_id, superseded_by_upload_id,
            vms_req_number, status, num_openings, job_title, category,
            category_imputed, required_skill, skill_imputed, publishing_status, job_type,
            date_added, client_type, city, state_or_province,
            country_of_placement, company_id, source_row_hash
        ) VALUES (
            OLD.job_id, OLD.upload_id, NEW.upload_id,
            OLD.vms_req_number, OLD.status, OLD.num_openings, OLD.job_title, OLD.category,
            OLD.category_imputed, OLD.required_skill, OLD.skill_imputed, OLD.publishing_status, OLD.job_type,
            OLD.date_added, OLD.client_type, OLD.city, OLD.state_or_province,
            OLD.country_of_placement, OLD.company_id, OLD.source_row_hash
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
