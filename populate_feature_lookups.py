"""
populate_feature_lookups.py — Compute and insert the five feature lookup
tables for one training window. All fill-rate inputs to
v_priority_score_calc are produced here.

Workflow:
  1. Insert one row into feature_lookup_version for (start, end).
     UNIQUE(start, end) prevents duplicates; pass --replace to recompute.
  2. Aggregate <schema>.jobs filtered to is_terminal=TRUE statuses
     (excludes 'On Hold' and 'Accepting Candidates' from training).
  3. Populate the five lookups:
       - company_fill_rate_lookup           (smoothed + tier from
                                             priority_tier_ref)
       - company_category_fill_rate_lookup  (≥5 orders/cell — schema CHECK)
       - category_fill_rate_lookup
       - skill_fill_rate_lookup             (NULL skills excluded)
       - client_type_fill_rate_lookup       (NULL client_types excluded)
  4. Optionally set is_active = TRUE on the new version (the schema's
     partial unique index forces deactivation of any prior active row).

Smoothing formula (company table only — Empirical Bayes shrinkage toward
the global prior):

        smoothed = (placed + base_rate × min_n) / (total + min_n)

Defaults: base_rate = 0.0850, min_n = 20 (per the schema spec).

Usage:
    python populate_feature_lookups.py \\
        --start 2023-01-01 --end 2025-12-31 \\
        --schema jop --activate \\
        --dsn 'postgresql://user:pwd@host:5432/db'

    # Re-compute / replace an existing version:
    python populate_feature_lookups.py \\
        --start 2023-01-01 --end 2025-12-31 \\
        --replace --activate --dsn '...'
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import psycopg

from load_jobs_csv import set_search_path


GLOBAL_BASE_FILL_RATE_DEFAULT = 0.0850
SMOOTHING_MIN_N_DEFAULT       = 20


# ─────────────────────────────────────────────────────────────────────────────
# Version row management
# ─────────────────────────────────────────────────────────────────────────────

def find_version(conn, start: date, end: date) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM feature_lookup_version "
            "WHERE training_period_start = %s AND training_period_end = %s",
            (start, end),
        )
        row = cur.fetchone()
        return row[0] if row else None


def delete_version(conn, version_id: int) -> None:
    """Hard-delete a version and every lookup row pointing at it.
    The schema doesn't ON DELETE CASCADE, so we clear children first."""
    with conn.cursor() as cur:
        for tbl in (
            'company_fill_rate_lookup',
            'company_category_fill_rate_lookup',
            'category_fill_rate_lookup',
            'skill_fill_rate_lookup',
            'client_type_fill_rate_lookup',
        ):
            cur.execute(f"DELETE FROM {tbl} WHERE version_id = %s", (version_id,))
        cur.execute(
            "DELETE FROM feature_lookup_version WHERE version_id = %s",
            (version_id,),
        )


def insert_version(conn, start: date, end: date,
                   base_rate: float, min_n: int, notes: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO feature_lookup_version
                (training_period_start, training_period_end,
                 global_base_fill_rate, smoothing_min_n, notes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING version_id
            """,
            (start, end, base_rate, min_n, notes),
        )
        (vid,) = cur.fetchone()
        return vid


def activate(conn, version_id: int) -> None:
    """Make this version the active one. The partial unique index
    on is_active=TRUE means we must deactivate any other active row first."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE feature_lookup_version SET is_active = FALSE "
            "WHERE is_active AND version_id <> %s",
            (version_id,),
        )
        cur.execute(
            "UPDATE feature_lookup_version SET is_active = TRUE "
            "WHERE version_id = %s",
            (version_id,),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Lookup populations
# ─────────────────────────────────────────────────────────────────────────────
#
# Date filter is half-open [start, end+1 day) so date_added rows on the
# end date itself are included regardless of timezone coercion.
# is_terminal=TRUE excludes 'On Hold' and 'Accepting Candidates' from
# training, per the schema comments.

DATE_FILTER = """
    JOIN job_status_ref s ON s.status_code = j.status
   WHERE s.is_terminal = TRUE
     AND j.date_added >= %(start)s::date
     AND j.date_added <  (%(end)s::date + INTERVAL '1 day')
"""


def pop_company_fill_rate(conn, vid, start, end, base_rate, min_n) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH agg AS (
                SELECT j.company_id,
                       COUNT(*)                            AS total_orders,
                       COUNT(*) FILTER (WHERE j.is_placed) AS placed_orders
                  FROM jobs j
                  {DATE_FILTER}
                 GROUP BY j.company_id
            ),
            scored AS (
                SELECT a.*,
                       ROUND(a.placed_orders::numeric / a.total_orders, 4)
                                                                     AS fill_rate,
                       ROUND(
                           (a.placed_orders + %(base)s * %(n)s)::numeric
                         / (a.total_orders + %(n)s)
                       , 4)                                          AS smoothed
                  FROM agg a
            )
            INSERT INTO company_fill_rate_lookup
                   (version_id, company_id, total_orders, placed_orders,
                    fill_rate, smoothed_fill_rate, fill_rate_tier)
            SELECT %(vid)s, s.company_id, s.total_orders, s.placed_orders,
                   s.fill_rate, s.smoothed, t.tier
              FROM scored s
              JOIN priority_tier_ref t
                ON s.smoothed BETWEEN t.min_score AND t.max_score
            """,
            dict(start=start, end=end, vid=vid, base=base_rate, n=min_n),
        )
        return cur.rowcount


def pop_company_category_fill_rate(conn, vid, start, end) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO company_category_fill_rate_lookup
                   (version_id, company_id, category,
                    total_orders, placed_orders, fill_rate)
            SELECT %(vid)s, j.company_id, j.category,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE j.is_placed),
                   ROUND(
                     COUNT(*) FILTER (WHERE j.is_placed)::numeric / COUNT(*)
                   , 4)
              FROM jobs j
              {DATE_FILTER}
                 AND j.category IS NOT NULL
             GROUP BY j.company_id, j.category
            HAVING COUNT(*) >= 5
            """,
            dict(start=start, end=end, vid=vid),
        )
        return cur.rowcount


def pop_category_fill_rate(conn, vid, start, end) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO category_fill_rate_lookup
                   (version_id, category, total_orders, placed_orders, fill_rate)
            SELECT %(vid)s, j.category,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE j.is_placed),
                   ROUND(
                     COUNT(*) FILTER (WHERE j.is_placed)::numeric / COUNT(*)
                   , 4)
              FROM jobs j
              {DATE_FILTER}
                 AND j.category IS NOT NULL
             GROUP BY j.category
            """,
            dict(start=start, end=end, vid=vid),
        )
        return cur.rowcount


def pop_skill_fill_rate(conn, vid, start, end) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO skill_fill_rate_lookup
                   (version_id, required_skill, total_orders, placed_orders, fill_rate)
            SELECT %(vid)s, j.required_skill,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE j.is_placed),
                   ROUND(
                     COUNT(*) FILTER (WHERE j.is_placed)::numeric / COUNT(*)
                   , 4)
              FROM jobs j
              {DATE_FILTER}
                 AND j.required_skill IS NOT NULL
             GROUP BY j.required_skill
            """,
            dict(start=start, end=end, vid=vid),
        )
        return cur.rowcount


def pop_client_type_fill_rate(conn, vid, start, end) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO client_type_fill_rate_lookup
                   (version_id, client_type, total_orders, placed_orders, fill_rate)
            SELECT %(vid)s, j.client_type,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE j.is_placed),
                   ROUND(
                     COUNT(*) FILTER (WHERE j.is_placed)::numeric / COUNT(*)
                   , 4)
              FROM jobs j
              {DATE_FILTER}
                 AND j.client_type IS NOT NULL
             GROUP BY j.client_type
            """,
            dict(start=start, end=end, vid=vid),
        )
        return cur.rowcount


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────

def run(conn, start: date, end: date, *, base_rate: float, min_n: int,
        replace: bool, do_activate: bool, notes: str | None,
        dry_run: bool) -> None:

    existing = find_version(conn, start, end)

    if existing is not None:
        if replace:
            print(f"  replacing existing version {existing} for "
                  f"({start} → {end})")
            if not dry_run:
                delete_version(conn, existing)
        else:
            print(f"error: a version for ({start} → {end}) already exists "
                  f"(version_id = {existing}). "
                  "Pass --replace to recompute, or pick a different window.",
                  file=sys.stderr)
            sys.exit(2)

    if dry_run:
        print("\n[DRY RUN] Would insert version row and populate lookups. "
              "No counts available without actually executing.")
        return

    vid = insert_version(conn, start, end, base_rate, min_n, notes)
    print(f"  feature_lookup_version → version_id = {vid}")

    counts = {
        'company':          pop_company_fill_rate(conn, vid, start, end,
                                                  base_rate, min_n),
        'company_category': pop_company_category_fill_rate(conn, vid, start, end),
        'category':         pop_category_fill_rate(conn, vid, start, end),
        'skill':            pop_skill_fill_rate(conn, vid, start, end),
        'client_type':      pop_client_type_fill_rate(conn, vid, start, end),
    }
    for k, n in counts.items():
        print(f"    {k+'_fill_rate_lookup':<40} {n:>6,}")

    if do_activate:
        activate(conn, vid)
        print(f"  set version_id={vid} as is_active = TRUE")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compute and insert feature lookup tables for one training window."
    )
    p.add_argument('--start', type=parse_date, required=True,
                   help="Training window start (YYYY-MM-DD, inclusive)")
    p.add_argument('--end', type=parse_date, required=True,
                   help="Training window end (YYYY-MM-DD, inclusive)")
    p.add_argument('--base-rate', type=float, default=GLOBAL_BASE_FILL_RATE_DEFAULT,
                   help=f"Global prior fill rate for smoothing "
                        f"(default {GLOBAL_BASE_FILL_RATE_DEFAULT})")
    p.add_argument('--min-n', type=int, default=SMOOTHING_MIN_N_DEFAULT,
                   help=f"Smoothing virtual sample size (default {SMOOTHING_MIN_N_DEFAULT})")
    p.add_argument('--replace', action='store_true',
                   help="Delete and recompute if a version for (start,end) exists")
    p.add_argument('--activate', action='store_true',
                   help="Set is_active = TRUE on the new version "
                        "(deactivates any prior active version)")
    p.add_argument('--notes', help="Free-form note recorded on the version row")
    p.add_argument('--schema', default='jop',
                   help="Postgres schema (default: jop)")
    p.add_argument('--dsn', default='', help="Postgres DSN (default: PG* env vars)")
    p.add_argument('--dry-run', action='store_true',
                   help="Validate inputs and report what would happen, no writes")
    args = p.parse_args(argv)

    if args.start > args.end:
        p.error(f"--start ({args.start}) must be on or before --end ({args.end})")

    print(f"Training window: {args.start} → {args.end}  "
          f"(inclusive of both endpoints)")
    print(f"Smoothing: base_rate={args.base_rate}, min_n={args.min_n}")

    with psycopg.connect(args.dsn or '') as conn:
        set_search_path(conn, args.schema)
        run(
            conn,
            args.start, args.end,
            base_rate=args.base_rate,
            min_n=args.min_n,
            replace=args.replace,
            do_activate=args.activate,
            notes=args.notes,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            print("\n[DRY RUN] no commit issued.")
        else:
            conn.commit()
            print("\nCommitted.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
