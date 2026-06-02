-- ============================================================================
--  MIGRATION 004 — CANDIDATES + JOB APPLICATIONS
--  Introduces the candidate entity and the candidate↔job application mapping.
--
--  New objects (all under rpotential schema):
--    application_status_ref  — enum: lifecycle states for an application
--    candidates              — core candidate entity
--    job_applications        — many-to-many mapping with status + audit trail
--    ix_*                    — supporting indexes
--    v_candidate_pipeline    — per-candidate active/latest application
--    v_job_application_detail — per-job application list with candidate info
--
--  Future rule (commented index at bottom):
--    A candidate may hold only ONE non-terminal application at a time.
--    Uncomment ux_one_active_application_per_candidate to enforce it.
-- ============================================================================

SET search_path = rpotential, public;


-- ============================================================================
--  A. APPLICATION STATUS ENUM
-- ============================================================================

CREATE TABLE application_status_ref (
    status_code   TEXT    PRIMARY KEY,
    is_terminal   BOOLEAN NOT NULL,   -- TRUE = no further transitions expected
    description   TEXT,
    sort_order    SMALLINT
);

INSERT INTO application_status_ref VALUES
 ('Applied',    FALSE, 'Candidate submitted application — pending recruiter review',     1),
 ('Screening',  FALSE, 'Recruiter is actively reviewing / phone-screening the candidate', 2),
 ('Submitted',  FALSE, 'Candidate submitted to client for consideration',                3),
 ('Interviewing', FALSE, 'Interview(s) scheduled or in progress',                        4),
 ('Offered',    FALSE, 'Offer extended — awaiting candidate acceptance',                 5),
 ('Placed',     TRUE,  'Candidate accepted offer and is placed — POSITIVE OUTCOME',      6),
 ('Withdrawn',  TRUE,  'Candidate withdrew from consideration',                          7),
 ('Rejected',   TRUE,  'Candidate not selected by recruiter or client',                  8);


-- ============================================================================
--  B. CANDIDATES
-- ============================================================================

CREATE TABLE candidates (
    candidate_id      BIGSERIAL    PRIMARY KEY,

    -- identity
    full_name         TEXT         NOT NULL,
    email             TEXT         NOT NULL,
    phone             TEXT,

    -- location (optional — mirrors jobs.city / state_or_province)
    city              TEXT,
    state_or_province TEXT,

    -- profile
    current_title     TEXT,
    skills_summary    TEXT,         -- free-text until structured skills are modelled

    -- sourcing
    source            TEXT,         -- e.g. 'LinkedIn', 'Referral', 'Indeed', 'Internal'

    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,

    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_candidates_email UNIQUE (email)
);

COMMENT ON COLUMN candidates.email IS 'Natural key. Enforced unique — use as idempotency key on ingest.';
COMMENT ON COLUMN candidates.skills_summary IS 'Free-text initially. Will migrate to a normalised skills table when volume justifies it.';

CREATE INDEX ix_candidates_email      ON candidates (email);
CREATE INDEX ix_candidates_is_active  ON candidates (is_active) WHERE is_active;


-- ============================================================================
--  C. JOB APPLICATIONS  (candidate ↔ job mapping)
-- ============================================================================

CREATE TABLE job_applications (
    application_id    BIGSERIAL    PRIMARY KEY,

    candidate_id      BIGINT       NOT NULL REFERENCES candidates(candidate_id),
    job_id            BIGINT       NOT NULL REFERENCES jobs(job_id),

    application_status TEXT        NOT NULL DEFAULT 'Applied'
                            REFERENCES application_status_ref(status_code),

    applied_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    status_updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- optional recruiter notes / rejection reason
    notes             TEXT,

    -- a candidate should not apply to the same job twice
    CONSTRAINT uq_candidate_job UNIQUE (candidate_id, job_id)
);

COMMENT ON TABLE job_applications IS
    'Maps candidates to jobs. One row per (candidate, job) pair. '
    'Status flows: Applied → Screening → Submitted → Interviewing → Offered → Placed '
    '             or any non-terminal → Withdrawn / Rejected.';

-- Keep status_updated_at current automatically
CREATE OR REPLACE FUNCTION job_applications_set_status_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.application_status IS DISTINCT FROM NEW.application_status THEN
        NEW.status_updated_at := now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_job_applications_status_ts
    BEFORE UPDATE ON job_applications
    FOR EACH ROW
    EXECUTE FUNCTION job_applications_set_status_updated_at();


-- Core access patterns
CREATE INDEX ix_job_applications_candidate   ON job_applications (candidate_id, applied_at DESC);
CREATE INDEX ix_job_applications_job         ON job_applications (job_id, applied_at DESC);
CREATE INDEX ix_job_applications_status      ON job_applications (application_status)
    WHERE application_status NOT IN ('Placed', 'Withdrawn', 'Rejected');  -- active only


-- ============================================================================
--  D. ONE-ACTIVE-APPLICATION CONSTRAINT  (future rule — currently disabled)
--
--  Business rule: a candidate may have at most ONE non-terminal application
--  at any point in time, and must wait for the job status to change to a
--  terminal state before applying elsewhere.
--
--  HOW TO ENABLE:
--    1. Decide on the trigger: is the unlock driven by jobs.status reaching a
--       terminal code, or by job_applications.application_status turning
--       terminal (Placed / Withdrawn / Rejected)?  The index below gates on
--       job_applications.application_status, which is the recommended approach
--       because it gives recruiters explicit control independent of the job.
--    2. Resolve any existing rows that would violate the constraint, then run:
--
--         CREATE UNIQUE INDEX ux_one_active_application_per_candidate
--             ON job_applications (candidate_id)
--          WHERE application_status NOT IN ('Placed', 'Withdrawn', 'Rejected');
--
--  To relax the rule back: DROP INDEX ux_one_active_application_per_candidate;
-- ============================================================================


-- ============================================================================
--  E. VIEWS
-- ============================================================================

-- E.1  Latest / active application per candidate
CREATE VIEW v_candidate_pipeline AS
SELECT DISTINCT ON (a.candidate_id)
    c.candidate_id,
    c.full_name,
    c.email,
    a.application_id,
    a.job_id,
    j.job_title,
    j.status           AS job_status,
    a.application_status,
    a.applied_at,
    a.status_updated_at,
    r.current_tier,
    r.sla_deadline,
    asr.is_terminal    AS is_application_terminal
FROM candidates          c
JOIN job_applications    a   ON a.candidate_id = c.candidate_id
JOIN jobs                j   ON j.job_id       = a.job_id
LEFT JOIN job_routing    r   ON r.job_id       = a.job_id
JOIN application_status_ref asr ON asr.status_code = a.application_status
ORDER BY a.candidate_id, a.applied_at DESC;

COMMENT ON VIEW v_candidate_pipeline IS
    'Most-recent application per candidate. '
    'Filter WHERE NOT is_application_terminal to see active pipeline only.';


-- E.2  Full application detail per job (useful for recruiter dashboards)
CREATE VIEW v_job_application_detail AS
SELECT
    j.job_id,
    j.job_title,
    j.status           AS job_status,
    r.current_tier,
    a.application_id,
    c.candidate_id,
    c.full_name,
    c.email,
    c.current_title,
    a.application_status,
    asr.is_terminal    AS is_application_terminal,
    a.applied_at,
    a.status_updated_at,
    a.notes
FROM job_applications       a
JOIN jobs                   j   ON j.job_id       = a.job_id
JOIN candidates             c   ON c.candidate_id = a.candidate_id
JOIN application_status_ref asr ON asr.status_code = a.application_status
LEFT JOIN job_routing        r   ON r.job_id       = a.job_id
ORDER BY j.job_id, a.applied_at DESC;
