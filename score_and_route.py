"""
score_and_route.py — Compute heuristic priority scores and routing
state for active job orders.

Workflow (one SQL transaction):
  1. Resolve the active feature_lookup_version (errors if none).
  2. Pull rows from v_priority_score_calc joined to priority_tier_ref
     (single SQL pass; no Python loop).
  3. INSERT one row per job into job_priority_scores (append-only audit
     trail — every run adds new rows).
  4. UPSERT job_routing: one row per job_id, last_score_id pointed at
     the just-inserted score, sla_deadline = now() + tier.sla_minutes
     (NULL for T3 since its sla_minutes is NULL).
     Stall flags reset on rescore.

Default target set: jobs in non-terminal statuses (Accepting Candidates,
On Hold). Historical / terminal jobs are training data — they don't get
scored, per design notes §5.

Usage:
    # Score all active orders (typical batch / cron call)
    python score_and_route.py --schema jop --dsn '...'

    # Score one specific job (Phase 1: a new order arrives)
    python score_and_route.py --job-id 1567631 --schema jop --dsn '...'

    # Score every job in the table regardless of status
    # (rare — only useful for backfills or experimentation)
    python score_and_route.py --all-jobs --schema jop --dsn '...'

Pass --dry-run to count target rows without writing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import psycopg
from psycopg.rows import dict_row

from load_jobs_csv import set_search_path


SCORING_METHOD = 'heuristic_v1'


# ─────────────────────────────────────────────────────────────────────────────
# Preflight
# ─────────────────────────────────────────────────────────────────────────────

def require_active_version(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM feature_lookup_version WHERE is_active"
        )
        row = cur.fetchone()
    if row is None:
        sys.exit(
            "error: no active feature_lookup_version. "
            "Run populate_feature_lookups.py with --activate first."
        )
    return row[0]


def count_targets(conn, *, job_id: int | None, upload_id: int | None,
                  all_jobs: bool) -> tuple[int, str]:
    """Return (number of jobs that would be scored, human label)."""
    if job_id is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE job_id = %s", (job_id,))
            return (1 if cur.fetchone() else 0, f"job_id={job_id}")

    if upload_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE upload_id = %s",
                (upload_id,),
            )
            return (cur.fetchone()[0], f"upload_id={upload_id}")

    if all_jobs:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            return (cur.fetchone()[0], "all jobs (incl. historical)")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM jobs j
              JOIN job_status_ref s ON s.status_code = j.status
             WHERE s.is_terminal = FALSE
        """)
        return (cur.fetchone()[0], "non-terminal status (active orders)")


# ─────────────────────────────────────────────────────────────────────────────
# Score + route in a single CTE chain
# ─────────────────────────────────────────────────────────────────────────────
#
# v_priority_score_calc already reads from the active feature_lookup_version
# (its CROSS JOIN active_version inside the view). We just filter to the
# target set, look up the tier, INSERT into job_priority_scores, RETURN
# the score_id, then UPSERT job_routing.

SCORE_AND_ROUTE_SQL = """
WITH target_jobs AS (
    {target_filter}
),
scored AS (
    SELECT s.*, t.tier
      FROM v_priority_score_calc s
      JOIN priority_tier_ref     t
        ON s.priority_score BETWEEN t.min_score AND t.max_score
     WHERE s.job_id IN (SELECT job_id FROM target_jobs)
),
inserted AS (
    INSERT INTO job_priority_scores (
        job_id, priority_score, tier,
        company_component, category_component, openings_component,
        job_type_component, client_type_component, hour_bin_component,
        scoring_method, feature_version_id)
    SELECT job_id, priority_score, tier,
           company_component, category_component, openings_component,
           job_type_component, client_type_component, hour_bin_component,
           %(method)s, feature_version_id
      FROM scored
    RETURNING score_id, job_id, tier
)
INSERT INTO job_routing (job_id, current_tier, sla_deadline, last_score_id)
SELECT i.job_id, i.tier,
       CASE WHEN t.sla_minutes IS NULL THEN NULL
            ELSE now() + (t.sla_minutes * INTERVAL '1 minute')
       END,
       i.score_id
  FROM inserted i
  JOIN priority_tier_ref t ON t.tier = i.tier
ON CONFLICT (job_id) DO UPDATE SET
       current_tier       = EXCLUDED.current_tier,
       routed_at          = now(),
       sla_deadline       = EXCLUDED.sla_deadline,
       last_score_id      = EXCLUDED.last_score_id,
       is_stalled         = FALSE,
       stalled_flagged_at = NULL,
       escalated_at       = NULL
RETURNING job_id, current_tier
"""


def build_target_filter(*, job_id: int | None, upload_id: int | None,
                         all_jobs: bool) -> tuple[str, dict]:
    if job_id is not None:
        return ("SELECT job_id FROM jobs WHERE job_id = %(job_id)s",
                {'job_id': job_id})
    if upload_id is not None:
        return ("SELECT job_id FROM jobs WHERE upload_id = %(upload_id)s",
                {'upload_id': upload_id})
    if all_jobs:
        return ("SELECT job_id FROM jobs", {})
    return (
        "SELECT j.job_id FROM jobs j "
        "  JOIN job_status_ref s ON s.status_code = j.status "
        " WHERE s.is_terminal = FALSE",
        {},
    )


def score_and_route(conn, *, job_id, upload_id, all_jobs) -> list[dict]:
    target_sql, target_params = build_target_filter(
        job_id=job_id, upload_id=upload_id, all_jobs=all_jobs,
    )
    sql = SCORE_AND_ROUTE_SQL.format(target_filter=target_sql)
    params = {**target_params, 'method': SCORING_METHOD}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compute heuristic priority scores and routing state."
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument('--job-id', type=int,
                     help="Score one specific job (Phase 1 callers).")
    grp.add_argument('--upload-id', type=int,
                     help="Score every job in a specific job_uploads batch.")
    grp.add_argument('--all-jobs', action='store_true',
                     help="Score every row in jobs (incl. historical — "
                          "unusual; default skips terminal statuses).")
    p.add_argument('--schema', default='jop',
                   help="Postgres schema (default: jop)")
    p.add_argument('--dsn', default='',
                   help="Postgres DSN (default: PG* env vars)")
    p.add_argument('--dry-run', action='store_true',
                   help="Count target rows; write nothing")
    args = p.parse_args(argv)

    with psycopg.connect(args.dsn or '') as conn:
        set_search_path(conn, args.schema)

        active_version = require_active_version(conn)
        print(f"  active feature_lookup_version: {active_version}")

        n, label = count_targets(
            conn, job_id=args.job_id, upload_id=args.upload_id,
            all_jobs=args.all_jobs,
        )
        print(f"  target set: {label}  →  {n:,} job(s)")

        if n == 0:
            print("  nothing to score.")
            return 0

        if args.dry_run:
            print("\n[DRY RUN] no rows written.")
            return 0

        results = score_and_route(
            conn, job_id=args.job_id, upload_id=args.upload_id,
            all_jobs=args.all_jobs,
        )

        by_tier = Counter(r['current_tier'] for r in results)
        print(f"\n  scored & routed: {len(results):,} job(s)")
        for tier in ('T1', 'T2', 'T3'):
            print(f"    {tier}: {by_tier.get(tier, 0):>6,}")

        conn.commit()
        print("\nCommitted.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
