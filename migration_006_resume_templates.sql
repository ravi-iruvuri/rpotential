-- ============================================================================
--  MIGRATION 006 — RESUME TEMPLATES + CANDIDATE RESUMES + OUTPUTS
--
--  New objects (all under rpotential schema):
--    client_resume_templates  — per-client template: schema + stored DOCX
--    candidate_resumes        — structured extraction from raw resume files
--    resume_outputs           — rendered output files per candidate+template
-- ============================================================================

SET search_path = rpotential, public;


-- ============================================================================
--  A. CLIENT RESUME TEMPLATES
-- ============================================================================

CREATE TABLE client_resume_templates (
    template_id       BIGSERIAL    PRIMARY KEY,

    client_name       TEXT         NOT NULL,
    template_name     TEXT         NOT NULL,

    -- original file uploaded by user (PDF or DOCX)
    source_file       BYTEA,
    source_file_type  TEXT         CHECK (source_file_type IN ('pdf', 'docx')),
    source_filename   TEXT,

    -- extracted structure: section order, formatting rules, item formats
    -- see field_schema structure in ingest_template.py
    field_schema      JSONB        NOT NULL,

    -- generated docxtpl-compatible DOCX with {{ }} placeholders
    -- built from field_schema and used by transform_resume.py at render time
    docx_template     BYTEA,

    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_client_template UNIQUE (client_name, template_name)
);

COMMENT ON COLUMN client_resume_templates.field_schema IS
    'JSONB capturing section_order[], per-section label/style/content_type/item_format. '
    'Source of truth for the docx_template. Re-generate docx_template by running '
    'ingest_template.py --regen --template-id N.';

COMMENT ON COLUMN client_resume_templates.docx_template IS
    'docxtpl-compatible DOCX with Jinja2 placeholders. '
    'Stored here so render is a pure DB fetch + docxtpl fill — no file system required.';

CREATE INDEX ix_resume_templates_client ON client_resume_templates (client_name) WHERE is_active;


-- ============================================================================
--  B. CANDIDATE RESUMES  (structured extraction of raw resume files)
-- ============================================================================

CREATE TABLE candidate_resumes (
    resume_id         BIGSERIAL    PRIMARY KEY,

    candidate_id      BIGINT       NOT NULL REFERENCES candidates(candidate_id),

    -- original file
    source_file       BYTEA,
    source_file_type  TEXT         CHECK (source_file_type IN ('pdf', 'docx', 'txt')),
    source_filename   TEXT,

    -- LLM-extracted structured data (canonical schema):
    -- {
    --   "candidate_name": "...",
    --   "summary": "...",
    --   "education": [{"degree":"...","school":"...","city":"...","state":"...","year":"..."}],
    --   "skills": ["..."],
    --   "certifications": [{"cert_name":"...","provider":"..."}],
    --   "work_experience": [{"company":"...","city":"...","state":"...","job_title":"...",
    --                         "start_date":"...","end_date":"...","bullets":["..."]}]
    -- }
    structured_json   JSONB        NOT NULL,

    parsed_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    is_latest         BOOLEAN      NOT NULL DEFAULT TRUE
);

COMMENT ON TABLE candidate_resumes IS
    'One row per resume version per candidate. '
    'structured_json is the canonical intermediate used by the renderer. '
    'is_latest=TRUE marks the resume currently used for submissions.';

CREATE UNIQUE INDEX ux_one_latest_resume_per_candidate
    ON candidate_resumes (candidate_id)
    WHERE (is_latest = TRUE);

CREATE INDEX ix_candidate_resumes_candidate ON candidate_resumes (candidate_id, parsed_at DESC);
CREATE INDEX ix_candidate_resumes_json      ON candidate_resumes USING gin (structured_json);


-- ============================================================================
--  C. RESUME OUTPUTS  (rendered files, one per candidate+template+submission)
-- ============================================================================

CREATE TABLE resume_outputs (
    output_id         BIGSERIAL    PRIMARY KEY,

    candidate_id      BIGINT       NOT NULL REFERENCES candidates(candidate_id),
    resume_id         BIGINT       NOT NULL REFERENCES candidate_resumes(resume_id),
    template_id       BIGINT       NOT NULL REFERENCES client_resume_templates(template_id),

    -- optional: link to client_submissions.link_id when tied to a specific submission
    submission_id     BIGINT,

    output_file       BYTEA,
    output_file_type  TEXT         NOT NULL DEFAULT 'docx'
                          CHECK (output_file_type IN ('docx', 'pdf')),
    output_filename   TEXT,

    generated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- track which field mappings were applied (for audit / debugging)
    field_mapping     JSONB
);

COMMENT ON COLUMN resume_outputs.submission_id IS
    'Optional link to client_submissions.link_id when output is tied to a specific submission';

COMMENT ON COLUMN resume_outputs.field_mapping IS
    'Records how candidate JSON fields were mapped to template placeholders';

CREATE INDEX ix_resume_outputs_candidate  ON resume_outputs (candidate_id, generated_at DESC);
CREATE INDEX ix_resume_outputs_template   ON resume_outputs (template_id, generated_at DESC);
CREATE INDEX ix_resume_outputs_submission ON resume_outputs (submission_id) WHERE submission_id IS NOT NULL;


-- ============================================================================
--  D. HELPER VIEW
-- ============================================================================

CREATE VIEW v_resume_pipeline AS
SELECT
    c.candidate_id,
    c.full_name,
    cr.resume_id,
    cr.source_filename,
    cr.parsed_at,
    ro.output_id,
    ro.output_filename,
    ro.generated_at,
    crt.client_name,
    crt.template_name,
    a.application_status,
    j.job_title
FROM candidates              c
LEFT JOIN candidate_resumes  cr  ON cr.candidate_id = c.candidate_id AND cr.is_latest
LEFT JOIN resume_outputs     ro  ON ro.resume_id = cr.resume_id
LEFT JOIN client_resume_templates crt ON crt.template_id = ro.template_id
LEFT JOIN job_applications   a   ON a.application_id = ro.application_id
LEFT JOIN jobs               j   ON j.job_id = a.job_id
ORDER BY c.candidate_id, ro.generated_at DESC NULLS LAST;
