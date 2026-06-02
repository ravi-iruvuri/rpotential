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

Idempotency guard (default on, bypass with --force):
  A job is skipped if job_priority_scores already contains a row for
  that job under the current active feature_version_id with the same
  source_row_hash as the current jobs row. This prevents duplicate score
  rows when the same upload is re-submitted without any data changes.

Default target set: jobs in non-terminal statuses (Accepting Candidates,
On Hold). Historical / terminal jobs are training data — they don't get
scored, per design notes §5.

Usage:
    # Score all active orders (typical batch / cron call)
    python score_and_route.py --schema jop --dsn '...'

    # Score one specific job (Phase 1: a new order arrives)
    python score_and_route.py --job-id 1567631 --schema jop --dsn '...'

    # Score every job in a specific upload batch
    python score_and_route.py --upload-id 3 --schema jop --dsn '...'

    # Score every job in the table regardless of status
    # (rare — only useful for backfills or experimentation)
    python score_and_route.py --all-jobs --schema jop --dsn '...'

    # Force re-score even if already scored with same data
    python score_and_route.py --upload-id 3 --force --schema jop --dsn '...'

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
                  all_jobs: bool,
                  date_start: str | None, date_end: str | None) -> tuple[int, str]:
    """Return (number of jobs in the target set, human label).
    This is the gross count before the idempotency guard is applied."""
    date_clause = ""
    params: list = []
    if date_start:
        date_clause += " AND date_added >= %s::date"
        params.append(date_start)
    if date_end:
        date_clause += " AND date_added < (%s::date + INTERVAL '1 day')"
        params.append(date_end)

    if job_id is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE job_id = %s", (job_id,))
            return (1 if cur.fetchone() else 0, f"job_id={job_id}")

    if upload_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM jobs WHERE upload_id = %s{date_clause}",
                [upload_id, *params],
            )
            label = f"upload_id={upload_id}"
            if date_start or date_end:
                label += f"  date {date_start or ''}→{date_end or ''}"
            return (cur.fetchone()[0], label)

    if all_jobs:
        with conn.cursor() as cur:
            where = f"WHERE TRUE{date_clause}" if date_clause else ""
            cur.execute(f"SELECT COUNT(*) FROM jobs {where}", params)
            return (cur.fetchone()[0], "all jobs (incl. historical)")

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) FROM jobs j
              JOIN job_status_ref s ON s.status_code = j.status
             WHERE s.is_terminal = FALSE{date_clause}
        """, params)
        return (cur.fetchone()[0], "non-terminal status (active orders)")


# ─────────────────────────────────────────────────────────────────────────────
# Score + route in a single CTE chain
# ─────────────────────────────────────────────────────────────────────────────

# Appended to every target filter when force=False.
# Skips jobs that already have a score row for the current active feature
# version with the same source_row_hash — i.e. nothing has changed.
IDEMPOTENCY_GUARD = """
  AND NOT EXISTS (
      SELECT 1 FROM job_priority_scores ps
       WHERE ps.job_id = j.job_id
         AND ps.feature_version_id = (
             SELECT version_id FROM feature_lookup_version WHERE is_active
         )
         AND ps.source_row_hash IS NOT DISTINCT FROM j.source_row_hash
  )"""


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
        scoring_method, feature_version_id, source_row_hash)
    SELECT s.job_id, s.priority_score, s.tier,
           s.company_component, s.category_component, s.openings_component,
           s.job_type_component, s.client_type_component, s.hour_bin_component,
           %(method)s, s.feature_version_id, j.source_row_hash
      FROM scored s
      JOIN jobs j ON j.job_id = s.job_id
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
                        all_jobs: bool, force: bool,
                        date_start: str | None, date_end: str | None) -> tuple[str, dict]:
    guard = "" if force else IDEMPOTENCY_GUARD

    date_clause = ""
    params: dict = {}
    if date_start:
        date_clause += " AND j.date_added >= %(date_start)s::date"
        params['date_start'] = date_start
    if date_end:
        date_clause += " AND j.date_added < (%(date_end)s::date + INTERVAL '1 day')"
        params['date_end'] = date_end

    if job_id is not None:
        return (
            f"SELECT j.job_id FROM jobs j WHERE j.job_id = %(job_id)s{date_clause}{guard}",
            {**params, 'job_id': job_id},
        )
    if upload_id is not None:
        return (
            f"SELECT j.job_id FROM jobs j WHERE j.upload_id = %(upload_id)s{date_clause}{guard}",
            {**params, 'upload_id': upload_id},
        )
    if all_jobs:
        base = "WHERE TRUE" if not date_clause else f"WHERE TRUE{date_clause}"
        return (f"SELECT j.job_id FROM jobs j {base}{guard}", params)
    return (
        "SELECT j.job_id FROM jobs j "
        "  JOIN job_status_ref s ON s.status_code = j.status "
        f" WHERE s.is_terminal = FALSE{date_clause}{guard}",
        params,
    )


def score_and_route(conn, *, job_id, upload_id, all_jobs, force,
                    date_start, date_end) -> list[dict]:
    target_sql, target_params = build_target_filter(
        job_id=job_id, upload_id=upload_id, all_jobs=all_jobs, force=force,
        date_start=date_start, date_end=date_end,
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
    p.add_argument('--force', action='store_true',
                   help="Bypass the idempotency guard and re-score even if "
                        "a score already exists for the current feature "
                        "version and unchanged data.")
    p.add_argument('--date-start', default=None,
                   help="Only score jobs with date_added >= this date (YYYY-MM-DD).")
    p.add_argument('--date-end', default=None,
                   help="Only score jobs with date_added <= this date (YYYY-MM-DD).")
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
            date_start=args.date_start, date_end=args.date_end,
        )
        print(f"  target set: {label}  →  {n:,} job(s)")
        if not args.force:
            print("  idempotency guard: on  (pass --force to rescore unchanged jobs)")

        if n == 0:
            print("  nothing to score.")
            return 0

        if args.dry_run:
            print("\n[DRY RUN] no rows written.")
            return 0

        results = score_and_route(
            conn, job_id=args.job_id, upload_id=args.upload_id,
            all_jobs=args.all_jobs, force=args.force,
            date_start=args.date_start, date_end=args.date_end,
        )

        skipped = n - len(results)
        by_tier = Counter(r['current_tier'] for r in results)
        print(f"\n  scored & routed: {len(results):,} job(s)")
        if skipped > 0:
            print(f"  skipped (already up-to-date): {skipped:,}")
        for tier in ('T1', 'T2', 'T3'):
            print(f"    {tier}: {by_tier.get(tier, 0):>6,}")

        conn.commit()
        print("\nCommitted.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
