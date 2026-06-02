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

import json
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


ERROR_MESSAGES = {
    "no_active_feature_version": "No active feature_lookup_version found. Run populate_feature_lookups.py --activate first.",
    "invalid_job_id":            "One or more job IDs are not valid integers.",
    "missing_arguments":         "Usage: export_tier.py <job_id> [job_id ...]",
}


def emit_error(code: str, detail: str | None = None) -> None:
    err: dict = {"error": code, "message": ERROR_MESSAGES.get(code, code)}
    if detail:
        err["detail"] = detail
    print(json.dumps(err, indent=2))


def require_active_version(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT version_id FROM feature_lookup_version WHERE is_active")
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("no_active_feature_version")
    return row[0]


def score_and_export(conn, job_ids: list[int]) -> list[dict]:
    params = {"job_ids": job_ids, "method": SCORING_METHOD}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SCORE_AND_ROUTE_SQL, params)
        return cur.fetchall()


def print_job_table(rows: list[dict], requested: list[int],
                    active_version: int | None = None,
                    upload_id: int | None = None,
                    total_jobs: int | None = None,
                    scored_count: int | None = None) -> dict:
    found_ids = {r["job_id"] for r in rows}
    missing = [jid for jid in requested if jid not in found_ids]

    routed = [
        {
            "job_id":       r["job_id"],
            "status":       r.get("status"),
            "tier":         r["current_tier"],
            "score":        r.get("priority_score"),
            "sla_deadline": str(r["sla_deadline"])[:16] if r.get("sla_deadline") else None,
            "is_stalled":   r.get("is_stalled"),
        }
        for r in rows
    ]

    summary: dict = {
        "routed_count":    len(routed),
        "not_found_count": len(missing),
    }
    if total_jobs is not None:
        summary["total_jobs"]    = total_jobs
        summary["scored_count"]  = scored_count
        summary["skipped_count"] = total_jobs - (scored_count or 0)

    result: dict = {"feature_lookup_version": str(active_version)}
    if upload_id is not None:
        result["upload_id"] = upload_id
    result["routed"]    = routed
    result["not_found"] = missing
    result["summary"]   = summary

    print(json.dumps(result, indent=2, default=str))
    return result


def parse_job_ids(argv: list[str]) -> list[int]:
    ids: list[int] = []
    for arg in argv:
        for part in arg.split(","):
            part = part.strip()
            if part:
                try:
                    ids.append(int(part))
                except ValueError:
                    raise ValueError("invalid_job_id")
    if not ids:
        raise ValueError("missing_arguments")
    return ids


def main() -> int:
    try:
        job_ids = parse_job_ids(sys.argv[1:])
        with psycopg.connect(DSN) as conn:
            set_search_path(conn, SCHEMA)
            active_version = require_active_version(conn)
            rows = score_and_export(conn, job_ids)
            conn.commit()
        print_job_table(rows, job_ids, active_version)
    except ValueError as e:
        emit_error(str(e))
        return 1
    except RuntimeError as e:
        emit_error(str(e))
        return 1
    except Exception as e:
        emit_error("connection_error", detail=str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
