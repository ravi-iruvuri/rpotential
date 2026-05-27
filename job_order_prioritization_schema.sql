-- ============================================================================
--  JOB ORDER PRIORITIZATION  —  PostgreSQL Schema
--  Source: Jobs_Data_05172026.xlsx  (42,422 orders · Jan 2023 – Dec 2025)
--  Tabs:   Jobs Report · Client Subs · Placements · Starts
--
--  Layered design:
--    1. Reference / enum tables  (seeded with values from the SLM spec)
--    2. Core entity tables       (cleaned + canonical from the 4 tabs)
--    3. Feature lookup tables    (versioned, retrained quarterly)
--    4. Scoring + routing tables (heuristic now, SLM later)
--    5. Indexes + views          (priority score computed on-the-fly)
--
--  Conventions:
--    - Fill rates stored as NUMERIC(5,4) → 0.0000 … 1.0000
--    - All timestamps TIMESTAMPTZ
--    - Status, job_type, client_type, category, hour_bin, openings_bin all
--      reference seeded enum tables (so canonical-name fixes — e.g.
--      'Contract to Hire' → 'Contract To Hire', 'Intellegence' →
--      'Intelligence', 'United States' → 'US' — happen at ingest).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS alerts;
SET search_path = jop, public;


-- ============================================================================
--  1. REFERENCE / ENUM TABLES
-- ============================================================================

-- 1.1  Status (target label, 10 values) ------------------------------------
CREATE TABLE job_status_ref (
    status_code      TEXT PRIMARY KEY,
    is_placed_label  BOOLEAN NOT NULL,           -- 1 = positive class
    is_terminal      BOOLEAN NOT NULL,           -- false → exclude from training
    description      TEXT,
    sort_order       SMALLINT
);

INSERT INTO job_status_ref VALUES
 ('Administrative Close',  FALSE, TRUE,  'Order closed by admin (largest class, 47%)',                1),
 ('Cancelled',              FALSE, TRUE,  'Client-initiated cancel',                                  2),
 ('Filled by Competition',  FALSE, TRUE,  'Lost to competitor (high-signal: order WAS fillable)',     3),
 ('Placed',                 TRUE,  TRUE,  'Confirmed Akkodis placement — POSITIVE CLASS',             4),
 ('Filled by Client',       FALSE, TRUE,  'Client filled internally (positive fillability signal)',   5),
 ('On Hold',                FALSE, FALSE, 'Ambiguous — exclude from training',                        6),
 ('Archive',                FALSE, TRUE,  'Removed from active queue',                                7),
 ('Declined',               FALSE, TRUE,  'Recruiter or client declined to proceed',                  8),
 ('Covered',                FALSE, TRUE,  'Order satisfied via alternate sourcing channel',           9),
 ('Accepting Candidates',   FALSE, FALSE, 'Active open order at extract — exclude from training',    10);


-- 1.2  Job Type (5 source values → 4 canonical) ---------------------------
CREATE TABLE job_type_ref (
    job_type          TEXT     PRIMARY KEY,                -- canonical
    encoded_value     SMALLINT NOT NULL UNIQUE,            -- 1..4
    fill_rate_weight  NUMERIC(5,4) NOT NULL,               -- used in priority score
    notes             TEXT
);

INSERT INTO job_type_ref VALUES
 ('Contract',         1, 0.0800, 'Baseline · 96.4% of orders'),
 ('Direct Hire',      2, 0.1800, '2.1× base rate'),
 ('Contract To Hire', 3, 0.2320, '2.9× base rate · canonical (lowercase variant maps here)'),
 ('Replacement',      4, 0.5000, 'Capped at 0.50 due to small N (12 records, raw 83.3%)');

-- ingest mapping for the 'Contract to Hire' duplicate
CREATE TABLE job_type_alias (
    raw_value         TEXT PRIMARY KEY,
    canonical_value   TEXT NOT NULL REFERENCES job_type_ref(job_type)
);
INSERT INTO job_type_alias VALUES
 ('Contract to Hire', 'Contract To Hire');


-- 1.3  Client Type (10 source values; small ones grouped) -----------------
CREATE TABLE client_type_ref (
    client_type        TEXT PRIMARY KEY,
    fill_rate_weight   NUMERIC(5,4),
    sample_size        INT,
    is_grouped_other   BOOLEAN NOT NULL DEFAULT FALSE,
    notes              TEXT
);

INSERT INTO client_type_ref VALUES
 ('Vendor',                          0.4710,    87, FALSE, 'Highest fill rate'),
 ('Enterprise - Technology',         0.1350,  3825, FALSE, '1.6× base rate'),
 ('Enterprise - Trans & Manu',       0.1350,  7477, FALSE, 'Auto / aerospace / industrial'),
 ('Retail',                          0.1070,  3435, FALSE, 'Slightly above base'),
 ('Enterprise - COE',                0.0700, 13985, FALSE, 'Largest segment, below base'),
 ('Enterprise - Financial Services', 0.0500, 13515, FALSE, 'Key drag — biggest AI opportunity'),
 ('Enterprise - T&M - Detroit',      0.0000,    17, FALSE, 'Zero fills in 3 yrs'),
 ('Other',                           NULL,      10, TRUE,  'Aggregates Regional / JW-Portfolio / National (n<5 each)');


-- 1.4  Category (24 source values → canonical) ----------------------------
CREATE TABLE category_ref (
    category            TEXT PRIMARY KEY,                -- canonical
    parent_category     TEXT,                            -- optional hierarchy
    fill_rate           NUMERIC(5,4),
    sample_size         INT,
    notes               TEXT
);

INSERT INTO category_ref (category, fill_rate, sample_size, notes) VALUES
 ('Service Desk',                  0.3340,  986, '3.9× base'),
 ('Technician',                    0.3180, 1082, '3.7× base'),
 ('Artificial Intelligence',       0.1980,   86, 'Source spelling Intellegence — standardize'),
 ('Business Professional',         0.1740, 2082, '2.0× base'),
 ('Emerging Technologies',         0.1710,  532, NULL),
 ('Manufacturing Engineering',     0.1680, 1066, NULL),
 ('Civil Engineering',             0.1680,  167, 'Low volume'),
 ('QA & Testing',                  0.1490,  830, NULL),
 ('Emerging Technologies & Data',  0.1480,  667, NULL),
 ('Data & Business Intelligence',  0.1430, 1844, NULL),
 ('Quality Engineering',           0.1400,  591, NULL),
 ('Business Analysis',             0.1390, 1946, NULL),
 ('Embedded Systems',              0.1280,  156, 'Low volume'),
 ('Project Management',            0.1280, 2565, NULL),
 ('Research & Development',        0.1260,  294, NULL),
 ('Infrastructure',                0.1140, 1664, NULL),
 ('Product Development Engineering',0.1010, 1539, NULL),
 ('Software Development',          0.0830, 7526, 'Largest, weakest signal');

-- ingest mapping for raw spellings → canonical
CREATE TABLE category_alias (
    raw_value         TEXT PRIMARY KEY,
    canonical_value   TEXT NOT NULL REFERENCES category_ref(category)
);
INSERT INTO category_alias VALUES
 ('Artificial Intellegence', 'Artificial Intelligence');


-- 1.5  Openings bin weights ----------------------------------------------
CREATE TABLE openings_bin_weights (
    openings_bin      TEXT     PRIMARY KEY,
    min_openings      INT      NOT NULL,
    max_openings      INT,                                 -- NULL = open-ended
    fill_rate_weight  NUMERIC(5,4) NOT NULL,
    sort_order        SMALLINT NOT NULL
);

INSERT INTO openings_bin_weights VALUES
 ('0',     0,    0, 0.0200, 1),
 ('1',     1,    1, 0.0700, 2),
 ('2',     2,    2, 0.1470, 3),
 ('3',     3,    3, 0.2110, 4),
 ('4-5',   4,    5, 0.2480, 5),
 ('6-10',  6,   10, 0.4120, 6),
 ('11+',  11, NULL, 0.5830, 7);


-- 1.6  Hour-of-day bin weights -------------------------------------------
CREATE TABLE hour_bin_weights (
    hour_bin          TEXT     PRIMARY KEY,
    min_hour          SMALLINT NOT NULL,
    max_hour          SMALLINT NOT NULL,
    fill_rate_weight  NUMERIC(5,4) NOT NULL
);

INSERT INTO hour_bin_weights VALUES
 ('overnight',  0,  7, 0.0480),
 ('morning',    8, 11, 0.0930),
 ('midday',    12, 15, 0.0850),
 ('afternoon', 16, 17, 0.0970),
 ('evening',   18, 23, 0.0630);


-- 1.7  Placement type ---------------------------------------------------
CREATE TABLE placement_type_ref (
    placement_type   TEXT PRIMARY KEY,
    notes            TEXT
);
INSERT INTO placement_type_ref VALUES
 ('Net New',              'Standard new hire — most common'),
 ('Redeploy',             'Candidate redeployed from expiring contract'),
 ('Backfill/Replacement', 'Replacing a prior worker — predictable demand'),
 ('Payroll',              'Payroll-only engagement — different GP economics');


-- 1.8  Routing tiers ----------------------------------------------------
CREATE TABLE priority_tier_ref (
    tier                CHAR(2) PRIMARY KEY,         -- T1 / T2 / T3
    min_score           NUMERIC(5,4) NOT NULL,
    max_score           NUMERIC(5,4) NOT NULL,
    sla_minutes         INT,                         -- target time-to-route
    action_description  TEXT                NOT NULL,
    CHECK (min_score < max_score)
);

INSERT INTO priority_tier_ref VALUES
 ('T1', 0.4000, 1.0000,   30, 'Auto-route to senior recruiter. Alert within 30 minutes of posting.'),
 ('T2', 0.1500, 0.3999,  480, 'Standard queue. Assign within 8 hours. Monitor sub velocity.'),
 ('T3', 0.0000, 0.1499, NULL, 'Deprioritize. Flag for repricing or client escalation after 48h with no submission.');


-- ============================================================================
--  2. CORE ENTITY TABLES  (cleaned + canonical from the 4 source tabs)
-- ============================================================================

-- 2.1  Companies --------------------------------------------------------
-- Jobs Report has 122 distinct Company IDs.
-- Placements has 1,514 (sub-entities/legacy). Starts has 102.
-- We model the 122 canonical companies and alias the rest.
CREATE TABLE companies (
    company_id        BIGINT PRIMARY KEY,                  -- 11,850 – 372,276
    company_name      TEXT   NOT NULL,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at     TIMESTAMPTZ,
    last_seen_at      TIMESTAMPTZ
);

CREATE TABLE company_alias (
    alias_company_id      BIGINT PRIMARY KEY,
    canonical_company_id  BIGINT NOT NULL REFERENCES companies(company_id),
    source_tab            TEXT   NOT NULL                  -- 'placements','starts','client_subs'
                          CHECK (source_tab IN ('placements','starts','client_subs','jobs_report'))
);


-- 2.1b  Job uploads (batch identity for each ingest event) -------------
-- One row per ingest batch. Every jobs.upload_id FKs here so the scorer
-- and downstream analytics can target a specific upload — useful for:
--   * Score only the rows from today's upload (not the whole table).
--   * Rollback / audit a bad upload by its batch id.
--   * Distinguish bulk-loaded historical data from per-API inserts.
CREATE TABLE job_uploads (
    upload_id       BIGSERIAL    PRIMARY KEY,
    uploaded_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source          TEXT,                                -- e.g. 'csv','api','manual'
    source_file     TEXT,                                -- file basename for CSV uploads
    source_hash     TEXT,                                -- sha256 of payload (idempotency)
    row_count       INT,                                 -- jobs successfully inserted
    uploaded_by     TEXT,                                -- user / system identifier
    notes           TEXT
);
CREATE INDEX ix_job_uploads_uploaded_at ON job_uploads (uploaded_at DESC);


-- 2.2  Jobs (primary table — 42,422 rows × 16 cols) --------------------
CREATE TABLE jobs (
    job_id                BIGINT      PRIMARY KEY,         -- 1,478,689 – 1,610,608
    vms_req_number        TEXT,                            -- nullable; null = non-VMS
    is_vms_order          BOOLEAN     GENERATED ALWAYS AS (vms_req_number IS NOT NULL) STORED,

    status                TEXT        NOT NULL REFERENCES job_status_ref(status_code),
    is_placed             BOOLEAN     GENERATED ALWAYS AS (status = 'Placed') STORED,

    num_openings          INT         NOT NULL CHECK (num_openings >= 0),

    job_title             TEXT,                            -- 2 nulls; near-unique
    category              TEXT        REFERENCES category_ref(category),  -- 39.6% null
    category_imputed      BOOLEAN     NOT NULL DEFAULT FALSE,             -- TRUE if filled by NLP
    required_skill        TEXT,                            -- 50.8% null

    publishing_status     TEXT,                            -- raw -1/0/1/2/'Expired/Canceled'/null
    is_published          BOOLEAN     GENERATED ALWAYS AS (
                              publishing_status IS NOT NULL
                              AND publishing_status <> 'Expired/Canceled'
                          ) STORED,

    job_type              TEXT        NOT NULL REFERENCES job_type_ref(job_type),

    date_added            TIMESTAMPTZ NOT NULL,            -- 2023-01-03 .. 2025-12-26

    client_type           TEXT        REFERENCES client_type_ref(client_type),  -- 71 nulls

    city                  TEXT,                            -- 6,579 nulls / 1,007 unique
    state_or_province     TEXT,                            -- 7,052 nulls / 72 unique
    country_of_placement  TEXT        CHECK (country_of_placement IN ('US') OR country_of_placement IS NULL),
                                                            -- standardize at ingest:
                                                            -- 'United States' → 'US', 'No' → NULL
    is_us_placement       BOOLEAN     GENERATED ALWAYS AS (country_of_placement = 'US') STORED,

    company_id            BIGINT      NOT NULL REFERENCES companies(company_id),

    -- ingest audit
    upload_id             BIGINT      REFERENCES job_uploads(upload_id),
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_row_hash       TEXT
);

COMMENT ON COLUMN jobs.status IS 'TARGET LABEL source. Do NOT include as a model feature (leakage risk).';
COMMENT ON COLUMN jobs.job_id IS 'Opaque identifier. Never use as a numeric feature (encodes ingest order).';
COMMENT ON COLUMN jobs.source_row_hash IS 'SHA-256 of the source data fields. Drives idempotent re-uploads and triggers history capture on real changes.';


-- 2.2b  jobs_history (SCD Type 4 audit — prior versions on UPDATE) -----
-- One row per UPDATE that mutates a data field. The current state lives
-- in jobs; everything that was overwritten lives here. Populated
-- automatically by trg_jobs_history_capture; do not INSERT manually.
CREATE TABLE jobs_history (
    history_id              BIGSERIAL    PRIMARY KEY,
    job_id                  BIGINT       NOT NULL,
    prior_upload_id         BIGINT       REFERENCES job_uploads(upload_id),
    superseded_by_upload_id BIGINT       REFERENCES job_uploads(upload_id),
    superseded_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- snapshot of OLD data columns at the moment of the update
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
CREATE INDEX ix_jobs_history_job_id        ON jobs_history (job_id, superseded_at DESC);
CREATE INDEX ix_jobs_history_superseded_by ON jobs_history (superseded_by_upload_id);

-- Trigger: copy OLD into jobs_history whenever source data changes.
-- The hash check means audit-column updates (e.g. flipping category_imputed
-- after agent enrichment) do NOT bloat history.
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

CREATE TRIGGER trg_jobs_history_capture
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION jobs_history_capture();


-- 2.3  Client Submissions  (78,146 rows × 5) ---------------------------
CREATE TABLE client_submissions (
    link_id            BIGINT PRIMARY KEY,                 -- one per submission event
    job_id             BIGINT NOT NULL REFERENCES jobs(job_id),
    company_id         BIGINT NOT NULL,                    -- no FK: 121 unique here
    submission_status  TEXT   NOT NULL DEFAULT 'Client Submission'
                            CHECK (submission_status = 'Client Submission'),
    date_added         TIMESTAMPTZ NOT NULL
);


-- 2.4  Placements  (22,281 rows × 5) -----------------------------------
CREATE TABLE placements (
    placement_id     BIGINT      PRIMARY KEY,              -- 481,934 – 526,993
    job_id           BIGINT      NOT NULL REFERENCES jobs(job_id),
    company_id       BIGINT      NOT NULL,                 -- 1,514 unique → no FK
    placement_type   TEXT        NOT NULL REFERENCES placement_type_ref(placement_type),
    date_added       TIMESTAMPTZ NOT NULL
);


-- 2.5  Starts  (4,776 rows × 5) ----------------------------------------
CREATE TABLE starts (
    placement_id    BIGINT      PRIMARY KEY REFERENCES placements(placement_id),
    job_id          BIGINT      NOT NULL    REFERENCES jobs(job_id),
    company_id      BIGINT      NOT NULL,
    placement_type  TEXT        NOT NULL    REFERENCES placement_type_ref(placement_type),
    start_date      TIMESTAMPTZ NOT NULL
);


-- ============================================================================
--  3. FEATURE LOOKUP TABLES  (versioned — retrained quarterly per spec)
-- ============================================================================

CREATE TABLE feature_lookup_version (
    version_id              SERIAL      PRIMARY KEY,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    training_period_start   DATE        NOT NULL,
    training_period_end     DATE        NOT NULL,
    global_base_fill_rate   NUMERIC(5,4) NOT NULL,         -- spec: 0.085
    smoothing_min_n         INT         NOT NULL DEFAULT 20,
    is_active               BOOLEAN     NOT NULL DEFAULT FALSE,
    notes                   TEXT,
    UNIQUE (training_period_start, training_period_end)
);

-- enforce single active version
CREATE UNIQUE INDEX ux_feature_lookup_version_active
   ON feature_lookup_version (is_active) WHERE is_active;


-- 3.1  Per-company fill rate (strongest signal, r = +0.458) ------------
CREATE TABLE company_fill_rate_lookup (
    version_id            INT          NOT NULL REFERENCES feature_lookup_version(version_id),
    company_id            BIGINT       NOT NULL REFERENCES companies(company_id),
    total_orders          INT          NOT NULL,
    placed_orders         INT          NOT NULL,
    fill_rate             NUMERIC(5,4) NOT NULL,
    smoothed_fill_rate    NUMERIC(5,4) NOT NULL,           -- shrinks toward global prior
    fill_rate_tier        CHAR(2)      NOT NULL REFERENCES priority_tier_ref(tier),
    PRIMARY KEY (version_id, company_id)
);

-- 3.2  Per-(company × category) fill rate (≥5 orders per cell) ---------
CREATE TABLE company_category_fill_rate_lookup (
    version_id      INT          NOT NULL REFERENCES feature_lookup_version(version_id),
    company_id      BIGINT       NOT NULL REFERENCES companies(company_id),
    category        TEXT         NOT NULL REFERENCES category_ref(category),
    total_orders    INT          NOT NULL CHECK (total_orders >= 5),
    placed_orders   INT          NOT NULL,
    fill_rate       NUMERIC(5,4) NOT NULL,
    PRIMARY KEY (version_id, company_id, category)
);

-- 3.3  Category fill rate ----------------------------------------------
CREATE TABLE category_fill_rate_lookup (
    version_id     INT          NOT NULL REFERENCES feature_lookup_version(version_id),
    category       TEXT         NOT NULL REFERENCES category_ref(category),
    total_orders   INT          NOT NULL,
    placed_orders  INT          NOT NULL,
    fill_rate      NUMERIC(5,4) NOT NULL,
    PRIMARY KEY (version_id, category)
);

-- 3.4  Required-skill fill rate (49.2% coverage) -----------------------
CREATE TABLE skill_fill_rate_lookup (
    version_id     INT          NOT NULL REFERENCES feature_lookup_version(version_id),
    required_skill TEXT         NOT NULL,
    total_orders   INT          NOT NULL,
    placed_orders  INT          NOT NULL,
    fill_rate      NUMERIC(5,4) NOT NULL,
    PRIMARY KEY (version_id, required_skill)
);

-- 3.5  Client-type fill rate -------------------------------------------
CREATE TABLE client_type_fill_rate_lookup (
    version_id     INT          NOT NULL REFERENCES feature_lookup_version(version_id),
    client_type    TEXT         NOT NULL REFERENCES client_type_ref(client_type),
    total_orders   INT          NOT NULL,
    placed_orders  INT          NOT NULL,
    fill_rate      NUMERIC(5,4) NOT NULL,
    PRIMARY KEY (version_id, client_type)
);


-- ============================================================================
--  4. SCORING + ROUTING
-- ============================================================================

-- 4.1  Score history — one row per scoring event (audit trail) -----------
CREATE TABLE job_priority_scores (
    score_id                     BIGSERIAL PRIMARY KEY,
    job_id                       BIGINT      NOT NULL REFERENCES jobs(job_id),

    priority_score               NUMERIC(5,4) NOT NULL CHECK (priority_score BETWEEN 0 AND 1),
    predicted_fill_probability   NUMERIC(5,4),                       -- from SLM once deployed
    tier                         CHAR(2)      NOT NULL REFERENCES priority_tier_ref(tier),

    -- explainability: heuristic score components (each = feature × weight)
    company_component            NUMERIC(6,4),                       -- ×0.45
    category_component           NUMERIC(6,4),                       -- ×0.20
    openings_component           NUMERIC(6,4),                       -- ×0.15
    job_type_component           NUMERIC(6,4),                       -- ×0.10
    client_type_component        NUMERIC(6,4),                       -- ×0.05
    hour_bin_component           NUMERIC(6,4),                       -- ×0.05

    scoring_method               TEXT NOT NULL CHECK (scoring_method IN
                                    ('heuristic_v1','slm_v1','slm_v2','manual_override')),
    feature_version_id           INT  REFERENCES feature_lookup_version(version_id),
    model_version                TEXT,

    computed_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- 4.2  Current routing state — one row per job ---------------------------
CREATE TABLE job_routing (
    job_id                  BIGINT PRIMARY KEY REFERENCES jobs(job_id),
    current_tier            CHAR(2) NOT NULL REFERENCES priority_tier_ref(tier),
    routed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    sla_deadline            TIMESTAMPTZ,                              -- routed_at + tier.sla_minutes
    assigned_recruiter_queue TEXT,

    is_stalled              BOOLEAN     NOT NULL DEFAULT FALSE,       -- no sub within 5h
    stalled_flagged_at      TIMESTAMPTZ,
    escalated_at            TIMESTAMPTZ,                              -- T3 + 48h no sub

    last_score_id           BIGINT REFERENCES job_priority_scores(score_id)
);


-- 4.3  Model registry (for SLM versions) ---------------------------------
CREATE TABLE model_versions (
    model_version       TEXT PRIMARY KEY,
    trained_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    feature_version_id  INT  NOT NULL REFERENCES feature_lookup_version(version_id),
    training_rows       INT,
    auc                 NUMERIC(5,4),
    pr_auc              NUMERIC(5,4),
    notes               TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX ux_model_versions_active
   ON model_versions (is_active) WHERE is_active;


-- ============================================================================
--  5. INDEXES
-- ============================================================================

-- jobs
CREATE INDEX ix_jobs_company_id           ON jobs (company_id);
CREATE INDEX ix_jobs_status               ON jobs (status);
CREATE INDEX ix_jobs_date_added           ON jobs (date_added);
CREATE INDEX ix_jobs_company_status       ON jobs (company_id, status);     -- fill-rate refresh
CREATE INDEX ix_jobs_category             ON jobs (category) WHERE category IS NOT NULL;
CREATE INDEX ix_jobs_client_type          ON jobs (client_type);
CREATE INDEX ix_jobs_job_type             ON jobs (job_type);
CREATE INDEX ix_jobs_upload_id            ON jobs (upload_id) WHERE upload_id IS NOT NULL;

-- submissions / placements / starts (all FK on job_id)
CREATE INDEX ix_subs_job_id               ON client_submissions (job_id);
CREATE INDEX ix_subs_job_date             ON client_submissions (job_id, date_added);
CREATE INDEX ix_placements_job_id         ON placements (job_id);
CREATE INDEX ix_placements_company_id     ON placements (company_id);
CREATE INDEX ix_starts_job_id             ON starts (job_id);

-- score history — most recent first per job
CREATE INDEX ix_scores_job_recent
   ON job_priority_scores (job_id, computed_at DESC);

-- routing SLAs
CREATE INDEX ix_routing_tier_sla
   ON job_routing (current_tier, sla_deadline)
   WHERE sla_deadline IS NOT NULL AND NOT is_stalled;
CREATE INDEX ix_routing_stalled
   ON job_routing (stalled_flagged_at) WHERE is_stalled;

-- feature lookups (read-heavy)
CREATE INDEX ix_company_flr_active
   ON company_fill_rate_lookup (company_id) INCLUDE (smoothed_fill_rate, fill_rate_tier);


-- ============================================================================
--  6. VIEWS
-- ============================================================================

-- 6.1  Active feature lookup snapshot (single-version convenience) -------
CREATE VIEW v_active_company_fill_rate AS
 SELECT c.* FROM company_fill_rate_lookup c
   JOIN feature_lookup_version v ON v.version_id = c.version_id
  WHERE v.is_active;

CREATE VIEW v_active_category_fill_rate AS
 SELECT c.* FROM category_fill_rate_lookup c
   JOIN feature_lookup_version v ON v.version_id = c.version_id
  WHERE v.is_active;

CREATE VIEW v_active_client_type_fill_rate AS
 SELECT c.* FROM client_type_fill_rate_lookup c
   JOIN feature_lookup_version v ON v.version_id = c.version_id
  WHERE v.is_active;


-- 6.2  Latest score per job ---------------------------------------------
CREATE VIEW v_latest_job_score AS
 SELECT DISTINCT ON (job_id)
        job_id, score_id, priority_score, predicted_fill_probability,
        tier, scoring_method, model_version, computed_at
   FROM job_priority_scores
  ORDER BY job_id, computed_at DESC;


-- 6.3  Heuristic priority score (matches §5 formula in the spec) --------
--      PRIORITY_SCORE =
--          0.45 · company_historical_fill_rate
--        + 0.20 · category_fill_rate
--        + 0.15 · openings_weight
--        + 0.10 · job_type_weight
--        + 0.05 · client_type_fill_rate
--        + 0.05 · hour_bin_weight
--
--      All inputs are read directly from the active feature lookup version.
--      `openings_bin` and `hour_bin` are derived inline from the jobs row.
CREATE VIEW v_priority_score_calc AS
WITH derived AS (
    SELECT
        j.job_id,
        j.company_id,
        j.category,
        j.client_type,
        j.job_type,
        j.date_added,
        CASE
            WHEN j.num_openings  = 0 THEN '0'
            WHEN j.num_openings  = 1 THEN '1'
            WHEN j.num_openings  = 2 THEN '2'
            WHEN j.num_openings  = 3 THEN '3'
            WHEN j.num_openings <= 5 THEN '4-5'
            WHEN j.num_openings <= 10 THEN '6-10'
            ELSE                          '11+'
        END AS openings_bin,
        CASE
            WHEN EXTRACT(HOUR FROM j.date_added) <  8 THEN 'overnight'
            WHEN EXTRACT(HOUR FROM j.date_added) < 12 THEN 'morning'
            WHEN EXTRACT(HOUR FROM j.date_added) < 16 THEN 'midday'
            WHEN EXTRACT(HOUR FROM j.date_added) < 18 THEN 'afternoon'
            ELSE                                            'evening'
        END AS hour_bin
    FROM jobs j
),
active_version AS (
    SELECT version_id FROM feature_lookup_version WHERE is_active LIMIT 1
)
SELECT
    d.job_id,
    av.version_id                                    AS feature_version_id,

    -- raw lookup values (handy for debugging / explainability)
    cfr.smoothed_fill_rate                           AS company_fill_rate,
    catfr.fill_rate                                  AS category_fill_rate,
    ow.fill_rate_weight                              AS openings_weight,
    jt.fill_rate_weight                              AS job_type_weight,
    ctfr.fill_rate                                   AS client_type_fill_rate,
    hb.fill_rate_weight                              AS hour_bin_weight,

    -- weighted components (NULL inputs treated as 0)
    COALESCE(cfr.smoothed_fill_rate, 0) * 0.45       AS company_component,
    COALESCE(catfr.fill_rate,        0) * 0.20       AS category_component,
    COALESCE(ow.fill_rate_weight,    0) * 0.15       AS openings_component,
    COALESCE(jt.fill_rate_weight,    0) * 0.10       AS job_type_component,
    COALESCE(ctfr.fill_rate,         0) * 0.05       AS client_type_component,
    COALESCE(hb.fill_rate_weight,    0) * 0.05       AS hour_bin_component,

    -- final priority score in [0, 1]
    ROUND(
        COALESCE(cfr.smoothed_fill_rate, 0) * 0.45 +
        COALESCE(catfr.fill_rate,        0) * 0.20 +
        COALESCE(ow.fill_rate_weight,    0) * 0.15 +
        COALESCE(jt.fill_rate_weight,    0) * 0.10 +
        COALESCE(ctfr.fill_rate,         0) * 0.05 +
        COALESCE(hb.fill_rate_weight,    0) * 0.05
    , 4)::NUMERIC(5,4)                               AS priority_score
FROM derived d
CROSS JOIN active_version av
LEFT JOIN company_fill_rate_lookup      cfr
       ON cfr.version_id  = av.version_id AND cfr.company_id  = d.company_id
LEFT JOIN category_fill_rate_lookup     catfr
       ON catfr.version_id = av.version_id AND catfr.category  = d.category
LEFT JOIN client_type_fill_rate_lookup  ctfr
       ON ctfr.version_id = av.version_id AND ctfr.client_type = d.client_type
LEFT JOIN openings_bin_weights          ow ON ow.openings_bin = d.openings_bin
LEFT JOIN job_type_ref                  jt ON jt.job_type     = d.job_type
LEFT JOIN hour_bin_weights              hb ON hb.hour_bin     = d.hour_bin;


-- 6.4  Routing tier mapped from a score ---------------------------------
CREATE VIEW v_priority_score_with_tier AS
 SELECT s.*, t.tier
   FROM v_priority_score_calc s
   JOIN priority_tier_ref     t
     ON s.priority_score BETWEEN t.min_score AND t.max_score;


-- 6.5  Open SLA breaches (stalled / past deadline) ----------------------
CREATE VIEW v_sla_breaches AS
 SELECT r.job_id, r.current_tier, r.routed_at, r.sla_deadline,
        EXTRACT(EPOCH FROM (now() - r.sla_deadline))/60 AS minutes_overdue,
        j.company_id, j.status
   FROM job_routing r
   JOIN jobs        j ON j.job_id = r.job_id
  WHERE r.sla_deadline < now()
    AND j.status NOT IN ('Placed','Filled by Client','Filled by Competition',
                         'Cancelled','Administrative Close','Archive',
                         'Declined','Covered');


-- 6.6  Temporal features derived from jobs.date_added --------------------
--      Used by:
--        - leadership dashboards (fill rate by month / quarter / year)
--        - SLM training (input feature matrix)
--      Note: heuristic scoring derives hour_bin inline in v_priority_score_calc;
--      this view is the canonical reference for downstream consumers.
--
--      Spec semantics: day_of_week uses 0=Mon..6=Sun (pandas convention).
--      Postgres EXTRACT(DOW) returns 0=Sun..6=Sat, so we remap.
CREATE VIEW v_job_temporal_features AS
SELECT
    j.job_id,
    j.date_added,

    EXTRACT(YEAR    FROM j.date_added)::SMALLINT     AS year,
    EXTRACT(MONTH   FROM j.date_added)::SMALLINT     AS month,
    EXTRACT(QUARTER FROM j.date_added)::SMALLINT     AS quarter,

    -- Postgres-native (0=Sun) and spec (0=Mon)
    EXTRACT(DOW FROM j.date_added)::SMALLINT         AS day_of_week_sun0,
    ((EXTRACT(DOW FROM j.date_added)::INT + 6) % 7)::SMALLINT
                                                     AS day_of_week,

    EXTRACT(HOUR FROM j.date_added)::SMALLINT        AS hour_of_day,

    CASE
        WHEN EXTRACT(HOUR FROM j.date_added) <  8 THEN 'overnight'
        WHEN EXTRACT(HOUR FROM j.date_added) < 12 THEN 'morning'
        WHEN EXTRACT(HOUR FROM j.date_added) < 16 THEN 'midday'
        WHEN EXTRACT(HOUR FROM j.date_added) < 18 THEN 'afternoon'
        ELSE                                              'evening'
    END                                              AS hour_bin,

    (EXTRACT(DOW FROM j.date_added) IN (0, 6))       AS is_weekend
FROM jobs j;
