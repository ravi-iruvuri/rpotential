"""
export_tier.py — Score and route specific job IDs, then return their tier codes.

Runs the full score_and_route pipeline (INSERT into job_priority_scores,
UPSERT into job_routing) for the given job IDs and prints a table of results.

Usage:
    python export_tier.py 1567631
    python export_tier.py 1567631 1567632 1567633
    python export_tier.py 1567631,1567632,1567633
"""

from __future__ import annotations

import sys

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

DSN = (
    "postgresql://neondb_owner:npg_5Zwp0SHDabWL"
    "@ep-twilight-grass-akoi1hzc-pooler.c-3.us-west-2.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)
SCHEMA = "jobs"
SCORING_METHOD = "heuristic_v1"

SCORE_AND_ROUTE_SQL = """
WITH target_jobs AS (
    SELECT job_id FROM jobs WHERE job_id = ANY(%(job_ids)s)
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
)
SELECT job_id, current_tier FROM routed ORDER BY current_tier
"""


def set_search_path(conn, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )


def require_active_version(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT version_id FROM feature_lookup_version WHERE is_active")
        row = cur.fetchone()
    if row is None:
        sys.exit(
            "error: no active feature_lookup_version. "
            "Run populate_feature_lookups.py with --activate first."
        )
    return row[0]


def score_and_export(conn, job_ids: list[int]) -> list[dict]:
    params = {"job_ids": job_ids, "method": SCORING_METHOD}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SCORE_AND_ROUTE_SQL, params)
        return cur.fetchall()


def print_table(rows: list[dict], requested: list[int]) -> None:
    col_w = max(len("job_id"), max((len(str(r["job_id"])) for r in rows), default=0))
    tier_w = max(len("tier_code"), max((len(str(r["current_tier"])) for r in rows), default=0))

    header = f"{'job_id':<{col_w}}  {'tier_code':<{tier_w}}"
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    scored_ids = {r["job_id"] for r in rows}
    for r in sorted(rows, key=lambda r: r["current_tier"]):
        print(f"{r['job_id']:<{col_w}}  {r['current_tier']:<{tier_w}}")

    missing = [jid for jid in requested if jid not in scored_ids]
    for jid in missing:
        print(f"{jid:<{col_w}}  {'NOT FOUND':<{tier_w}}")

    print(sep)
    print(f"{len(rows)} scored  |  {len(missing)} not found")


def parse_job_ids(argv: list[str]) -> list[int]:
    ids: list[int] = []
    for arg in argv:
        for part in arg.split(","):
            part = part.strip()
            if part:
                try:
                    ids.append(int(part))
                except ValueError:
                    sys.exit(f"error: '{part}' is not a valid job_id integer")
    if not ids:
        sys.exit("usage: python export_tier.py <job_id> [job_id ...]")
    return ids


def main() -> int:
    job_ids = parse_job_ids(sys.argv[1:])

    with psycopg.connect(DSN) as conn:
        set_search_path(conn, SCHEMA)
        active_version = require_active_version(conn)
        print(f"feature_lookup_version: {active_version}  |  jobs: {job_ids}\n")

        rows = score_and_export(conn, job_ids)
        conn.commit()

    print_table(rows, job_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
