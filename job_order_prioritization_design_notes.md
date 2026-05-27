# Job Order Prioritization — Design Notes

Running record of the schema and UI design decisions for the Noida India MSP
Job Order Prioritization Agent. Companion to
`job_order_prioritization_schema.sql`.

Source spec: `SLM_DataSpec_JobOrderPrioritization_05202026_latest.docx.pdf`
plus `JobOrderPrioritization_Executive_Brief_05202026_latest.docx.pdf`.

---

## 1. Business case (one paragraph)

The MSP receives hundreds of new job orders per week from 122 enterprise
clients. Today there is no systematic prioritization, so 91.5% of orders go
unfilled (8.5% baseline fill rate across 42,422 orders, 2023–2025). The agent
must score every new order at ingest using a 0–1 fill-probability score and
route it to a tier:

| Tier | Score | Action | SLA |
|------|-------|--------|-----|
| T1 | ≥ 0.40 | Auto-route to senior recruiter | 30 min |
| T2 | 0.15–0.39 | Standard queue | 8 hours |
| T3 | < 0.15 | Deprioritize / repricing conversation | escalate after 48h with no sub |

The largest single opportunity is Bank of America: 9,506 orders at 3.3% fill
rate. Moving that to 15% generates ~$4.84M annual gross profit.

---

## 2. Source data (4 tabs in Jobs_Data_05172026.xlsx)

| Tab | Rows | Role |
|---|---:|---|
| Jobs Report | 42,422 | Primary; one row per job order |
| Client Subs | 78,146 | Candidate submissions per job |
| Placements | 22,281 | Actual placements made |
| Starts | 4,776 | Candidates who actually started |

Single join key: `Job ID`. Period: Jan 2023 – Dec 2025.

---

## 3. Schema design principles

These were arrived at iteratively (see §4 for the decisions that produced
them).

1. **Cleaned canonical tables only** — no raw "as-loaded" tables retained.
   Aliases handle dirty source values at ingest (`Contract to Hire` →
   `Contract To Hire`, `Artificial Intellegence` → `Artificial Intelligence`,
   `United States` → `US`).
2. **Derive when cheap, store only what can't be derived.** Generated columns
   (`is_placed`, `is_vms_order`, `is_us_placement`, `is_published`) and views
   (`v_priority_score_calc`, `v_job_temporal_features`) replace materialized
   feature tables.
3. **Versioned lookups, atomic switchover.** Every fill-rate lookup row is
   keyed by `(version_id, dimension_value)`. Quarterly retrain inserts a new
   `feature_lookup_version`, populates new lookup rows, then flips
   `is_active` in a single transaction. Old scores keep their
   `feature_version_id` reference for reproducibility.
4. **Append-only audit, mutable current state separate.** Every scoring
   event lands in `job_priority_scores`. Live state lives in views derived
   from the audit log + a thin `job_alerts` table for notification idempotency.
5. **No status-as-feature.** `jobs.status` is the target label source. Only
   the generated `is_placed` column may flow into model features.
6. **Heuristic and SLM coexist.** `job_priority_scores.scoring_method`
   distinguishes them; both can write rows for the same job, enabling A/B.

---

## 4. Key design decisions (with rationale)

### 4.1 Drop `job_features` (the wide materialized features table)

**Reasoning.** It carried two jobs and did neither well:

- Audit ("what did the model see?") — already covered by the six per-component
  values stored in `job_priority_scores` plus `feature_version_id`.
- Performance ("fast scoring") — the volumes (hundreds/week) don't justify
  materialization; a join-based view runs in ms.

**Additionally**, bulk-filling features for the 42,422 historical jobs from
the *current* lookup tables would create a leakage trap — a Jan-2023 job's
`company_historical_fill_rate` would silently include 2024–2025 placements
that didn't exist when the order was posted. The spec calls this out:
*"Must be computed on training data only … leave-one-out or prior-period
fold."* Training features therefore belong in a separate per-training-run
artifact (parquet / `training_features_v<n>` table), produced and discarded
by the training pipeline — not stored permanently in this OLTP schema.

**Resolution.** Removed the table; rewrote `v_priority_score_calc` to be
self-contained (derives `openings_bin` and `hour_bin` inline; reads lookups
directly). A standalone `v_job_temporal_features` view covers analytics and
SLM-training reads.

### 4.2 Drop `job_routing` (live state table) — *agreed, not yet edited*

Of its eight columns, six are pure derivations:

| Column | Derivable from |
|---|---|
| `current_tier` | latest `job_priority_scores` row per job |
| `last_score_id` | same |
| `routed_at` | `MIN(computed_at)` per job in `job_priority_scores` |
| `sla_deadline` | `routed_at + priority_tier_ref.sla_minutes` |
| `assigned_recruiter_queue` | function of tier (T1 → senior, T2 → standard, T3 → repricing) |
| `is_stalled` | active status + no submission in 5h since `routed_at` |

Only `stalled_flagged_at` and `escalated_at` are real state — and only
because they exist for *notification idempotency* ("don't page twice").

**Plan (pending implementation):**

- Drop `job_routing`.
- Add a view `v_job_routing` deriving live state from `jobs`,
  `job_priority_scores`, `client_submissions`, `priority_tier_ref`.
- Add a thin append-only `job_alerts` table:
  ```sql
  CREATE TABLE job_alerts (
      job_id      BIGINT      NOT NULL REFERENCES jobs(job_id),
      alert_type  TEXT        NOT NULL CHECK (alert_type IN
                      ('t1_route','stalled','t3_escalation','tier_changed')),
      sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (job_id, alert_type)
  );
  ```
- Watchdog cron: `SELECT … FROM v_job_routing WHERE is_stalled AND NOT EXISTS
  (… in job_alerts)` → page → `INSERT INTO job_alerts`.
- Rewrite `v_sla_breaches` to read from `v_job_routing` instead of the
  dropped table.

### 4.3 No tier column on `jobs`

Tier lives in three places with distinct semantics:

| Location | Mutability | Purpose |
|---|---|---|
| `priority_tier_ref` | static reference | Definitions: thresholds, SLA, action |
| `job_priority_scores.tier` | append-only | Tier at the moment of each scoring event |
| `v_job_routing.current_tier` (after §4.2) | derived | Tier right now (latest score) |

`jobs` is the cleaned source record — derived, time-varying decisions belong
elsewhere.

### 4.4 Smoothed fill rate, not raw

Raw `placed / total` is noisy for low-N companies. With α=20 and
prior=0.085 (the global base rate, stored on `feature_lookup_version`):

```
smoothed = (placed_count + α × global_rate) / (total_count + α)
```

Effect at the extremes:

| Sample | Raw | Smoothed |
|---|---:|---:|
| Bank of America (9,506 orders) | 3.3% | 3.3% (large N — barely moves) |
| Tiny client, 1 of 3 placed | 33.3% | 12.0% (pulled toward 8.5%) |
| Detroit T&M, 0 of 17 | 0.0% | 4.6% (phantom prior lifts it) |

The view uses `smoothed_fill_rate`; the raw rate is also stored for audit.

### 4.5 Brand-new companies (not in training data)

A company never seen during training has no row in
`company_fill_rate_lookup`, so the `LEFT JOIN` in `v_priority_score_calc`
returns NULL and `COALESCE(..., 0)` gives it a zero contribution. **This is
incorrect** — they should get the global prior. Fix is a one-line change:
replace `COALESCE(cfr.smoothed_fill_rate, 0)` with
`COALESCE(cfr.smoothed_fill_rate, av.global_base_fill_rate_for_active)`.
Deferred until new client onboarding actually happens.

---

## 5. End-to-end lifecycle

### Phase 0 — Initial load (one-time)

1. INSERT 122 canonical companies into `companies`; aliases (1,392
   sub-entity Company IDs from Placements) into `company_alias`.
2. INSERT 42,422 jobs (with their final status) into `jobs`. Aliases run at
   ingest to canonicalize dirty values.
3. INSERT submissions, placements, starts.
4. Create the first `feature_lookup_version` row, populate the five lookup
   tables from `jobs` aggregations, set `is_active = TRUE`.

Historical orders are **training data, not scoring targets** — they get no
`job_priority_scores` row.

### Phase 1 — A new order arrives

```sql
INSERT INTO jobs (job_id, company_id, job_type, num_openings, category,
                  client_type, status, date_added, ...)
VALUES (..., 'Accepting Candidates', now(), ...);
```

### Phase 2 — Score and route

Single SQL call:

```sql
WITH scored AS (SELECT * FROM v_priority_score_calc WHERE job_id = $1)
INSERT INTO job_priority_scores (
    job_id, priority_score, tier,
    company_component, category_component, openings_component,
    job_type_component, client_type_component, hour_bin_component,
    scoring_method, feature_version_id)
SELECT s.job_id, s.priority_score, t.tier,
       s.company_component, s.category_component, s.openings_component,
       s.job_type_component, s.client_type_component, s.hour_bin_component,
       'heuristic_v1', s.feature_version_id
FROM scored s
JOIN priority_tier_ref t
  ON s.priority_score BETWEEN t.min_score AND t.max_score;
```

Once §4.2 is applied, this single insert is the entire scoring + routing
operation — `v_job_routing` derives current state from it.

### Phase 3 — Recruiter activity

- Recruiter pulls from queue ordered by tier, then `sla_deadline`.
- Submissions land in `client_submissions`.
- Watchdog cron (every few min) inserts `job_alerts` rows for newly
  stalled / escalation-due orders.

### Phase 4 — Order closes

`jobs.status` flips to a terminal value. `is_placed` flips automatically
(generated column). If `Placed`, a row goes into `placements` (and later
`starts`). The order joins the historical training set for the next
quarterly retrain.

### Phase 5 — Quarterly retrain

```sql
-- 1. Stage v2
INSERT INTO feature_lookup_version (training_period_start, training_period_end,
                                    global_base_fill_rate, is_active)
VALUES ('2023-01-01', '2026-03-31', 0.087, FALSE)
RETURNING version_id;  -- 2

INSERT INTO company_fill_rate_lookup (version_id=2, ...) ...   -- and the others

-- 2. Atomic switchover (partial unique index enforces single active version)
BEGIN;
UPDATE feature_lookup_version SET is_active = FALSE WHERE version_id = 1;
UPDATE feature_lookup_version SET is_active = TRUE  WHERE version_id = 2;
COMMIT;
```

`v_priority_score_calc` reads `WHERE is_active`, so new scores
automatically use v2. Old scores keep `feature_version_id = 1` —
historical decisions remain reproducible.

---

## 6. Priority score formula

From §5 of the spec, encoded directly in `v_priority_score_calc`:

```
priority_score
  = 0.45 · company_historical_fill_rate    (from company_fill_rate_lookup, smoothed)
  + 0.20 · category_fill_rate              (from category_fill_rate_lookup)
  + 0.15 · openings_weight                 (from openings_bin_weights)
  + 0.10 · job_type_weight                 (from job_type_ref)
  + 0.05 · client_type_fill_rate           (from client_type_fill_rate_lookup)
  + 0.05 · hour_bin_weight                 (from hour_bin_weights)
```

Two of the six (`openings_bin`, `hour_bin`) are derived inline from
`jobs.num_openings` and `EXTRACT(HOUR FROM jobs.date_added)` via CASE
expressions. The other four are pure lookups against the active version.

### Worked examples

**Bank of America, Software Dev Contract, 2 openings, 2:30 PM:**

```
  company[BoA]                       0.033 × 0.45 = 0.0149
  category[Software Development]     0.083 × 0.20 = 0.0166
  openings_bin['2']                  0.147 × 0.15 = 0.0221
  job_type[Contract]                 0.080 × 0.10 = 0.0080
  client_type[Financial Services]    0.050 × 0.05 = 0.0025
  hour_bin[midday]                   0.085 × 0.05 = 0.0043
  ──────────────────────────────────────────────
  priority_score                                 = 0.0684  →  Tier 3
```

**Google DC, Service Desk Contract-To-Hire, 8 openings, 9:15 AM:**

```
  company[Google DC]                 0.811 × 0.45 = 0.3650
  category[Service Desk]             0.334 × 0.20 = 0.0668
  openings_bin['6-10']               0.412 × 0.15 = 0.0618
  job_type[Contract To Hire]         0.232 × 0.10 = 0.0232
  client_type[Technology]            0.135 × 0.05 = 0.0068
  hour_bin[morning]                  0.093 × 0.05 = 0.0047
  ──────────────────────────────────────────────
  priority_score                                 = 0.5283  →  Tier 1
```

---

## 7. Lookup pipeline — how rates are computed

Runs at initial load + every quarterly retrain. Single `INSERT … SELECT`
per lookup table.

```sql
INSERT INTO company_fill_rate_lookup
    (version_id, company_id, total_orders, placed_orders,
     fill_rate, smoothed_fill_rate, fill_rate_tier)
SELECT
    :version_id,
    j.company_id,
    COUNT(*)                                     AS total_orders,
    COUNT(*) FILTER (WHERE j.is_placed)          AS placed_orders,
    (COUNT(*) FILTER (WHERE j.is_placed)::numeric
        / NULLIF(COUNT(*), 0))::numeric(5,4)     AS fill_rate,
    ((COUNT(*) FILTER (WHERE j.is_placed)::numeric
            + v.smoothing_min_n * v.global_base_fill_rate)
        / (COUNT(*)::numeric + v.smoothing_min_n))::numeric(5,4)
                                                 AS smoothed_fill_rate,
    CASE
        WHEN smoothed >= 0.40 THEN 'T1'
        WHEN smoothed >= 0.15 THEN 'T2'
        ELSE                       'T3'
    END                                          AS fill_rate_tier
FROM   jobs j
CROSS  JOIN feature_lookup_version v
WHERE  v.version_id = :version_id
  AND  j.status NOT IN ('On Hold','Accepting Candidates')   -- non-terminal
  AND  j.date_added BETWEEN v.training_period_start AND v.training_period_end
GROUP BY j.company_id, v.smoothing_min_n, v.global_base_fill_rate;
```

The same shape applies to `category_fill_rate_lookup`,
`client_type_fill_rate_lookup`, `skill_fill_rate_lookup`,
`company_category_fill_rate_lookup` (with the `≥ 5 orders per cell`
threshold per spec §4.1).

---

## 8. UI design notes

### Primary user: all three roles (recruiter + team lead + leadership)
### Score detail level: tier + score + plain-language reasons

### Three views

**1. Recruiter Queue — "What do I work on next?"**

Single-screen, no navigation. Cards sorted T1 → T2 → T3, then by SLA
deadline within tier. Each card shows: company, role title, tier badge,
score, SLA timer, generated 1–2 line "Why T1" reason, [Claim] / [Details].

Reason generation: take the two largest components from
`job_priority_scores` and apply a template:

| Dominant component | Template |
|---|---|
| `company_component` | "{Company} historically fills {X}% of orders" |
| `openings_component` | "{N} openings — multi-seat orders fill {Y}% on avg" |
| `job_type_component` | "{Job Type} role — fills {Y}% vs Contract baseline 8%" |
| `category_component` | "{Category} roles fill {Y}% — {ratio}× base rate" |
| `client_type_component` | "{Client Type} segment fills {Y}%" |
| (low score) | "{Company} fills only {X}%. Single opening. Standard Contract role." |

**2. Team Lead — "What's stuck, who needs help?"**

Top metrics (SLA breaches today, stalled count, T3 escalations due).
Stalled-orders list with [Page] action that writes `job_alerts`.
Recruiter throughput table.

**3. Leadership — "Is the agent working?"**

Big numbers (current fill rate vs 8.5% baseline; annualized GP lift; median
T1 time-to-first-sub). Monthly fill-rate trend with a marker at agent
go-live. Account performance vs targets table (BoA, Capital One, Google DC).
Tier composition stacked bar.

### Information architecture

Single shell, three landing pages by role, two drill-down pages (single
order, single account) shared across roles.

### Tech-stack-agnostic decisions still open

1. **Real-time vs polling** for the recruiter queue. T1's 30-min SLA argues
   for live updates (websockets / SSE); 30-second polling is a valid v1
   with much less infra.
2. **Mobile or desktop only?** Senior recruiters working a 30-min T1 SLA
   may want phone alerts. Responsive layout from day one is much harder to
   bolt on later.
3. **Recruiter identity model.** Team-lead throughput and "claim this
   order" both need a `recruiter_id` that isn't in the schema yet. Either
   spec a `recruiters` + `job_assignments` table, or treat as out of scope
   for v1.

---

## 9. Open items / pending edits

| # | Item | Status |
|---|---|---|
| 1 | Drop `job_routing`; add `v_job_routing` view + `job_alerts` table; rewrite `v_sla_breaches` against the view | **Agreed, not yet applied** |
| 2 | Replace `COALESCE(cfr.smoothed_fill_rate, 0)` with active-version global prior in `v_priority_score_calc` | Deferred until new-client onboarding |
| 3 | Add `closed_at TIMESTAMPTZ` on routing (decouples SLA logic from `jobs.status`) | Optional polish |
| 4 | Recruiter identity: `recruiters` + `job_assignments` tables for throughput metrics and claim/release actions | Out of scope for v1; needed for full team-lead UI |
| 5 | UI tech decisions (real-time vs poll, responsive vs desktop) | Pending answer |

---

## 10. Known gaps & risks

- **Heuristic vs SLM divergence.** When the SLM ships, it may disagree with
  the heuristic. `job_priority_scores.scoring_method` lets both write
  side-by-side for A/B; the operational system needs a config switch for
  which one drives `tier`.
- **Lookup version drift.** A scoring event uses the active lookup version
  at write time. If the same order is rescored after a retrain, components
  may shift even though the formula didn't change. Each row stores
  `feature_version_id`, so this is auditable, but worth flagging in the
  recruiter UI ("re-scored after model refresh").
- **Submission-velocity feature is real-time only.** `sub_velocity` per
  the spec is "for live queue re-ranking, not training feature." The
  schema doesn't materialize it; live re-ranking would compute it
  on-the-fly.
- **Status leakage prevention is by convention, not enforcement.** Nothing
  in the schema *prevents* a future engineer from joining `jobs.status`
  into the SLM training matrix. A view that exposes `jobs` minus `status`
  for training purposes would be safer.
