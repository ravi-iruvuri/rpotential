"""
load_pipeline.py — End-to-end loader for the four Jobs Data CSVs.

Runs the loaders in FK-safe order on a single connection:

    1. jobs                  (creates <schema>.companies on the fly,
                              upserts new categories / client_types)
    2. client_submissions    (FK → jobs)
    3. placements            (FK → jobs)
    4. starts                (FK → placements, jobs)
    5. company_alias         (post-load: derives sub-entity → canonical
                              mappings from rows that just landed in 2-4,
                              using jobs.company_id as the canonical lookup)

Reference tables (job_status_ref, job_type_ref, client_type_ref,
category_ref, placement_type_ref, openings_bin_weights, hour_bin_weights,
priority_tier_ref) are seeded by the schema DDL itself — this pipeline
assumes they already exist.

Note on company_alias: placements / starts carry sub-entity Company IDs
that are NOT in the canonical 122-row <schema>.companies table. Per the
schema, those columns intentionally have NO foreign key. Step 5 of this
pipeline derives the sub-entity → canonical mapping from `jobs` itself
(the canonical company_id is whatever jobs.company_id is for the row's
job_id) and writes it to <schema>.company_alias.

Usage:
    pip install 'psycopg[binary]>=3.1'
    python load_pipeline.py \\
        --jobs       jobs_report.csv \\
        --subs       client_subs.csv \\
        --placements placements.csv \\
        --starts     starts.csv \\
        --schema jop \\
        --dsn 'postgresql://user:pwd@host:5432/dbname'

Pass --dry-run to validate everything without writing.
Pass --skip-jobs if jobs are already loaded and you only need the
downstream three tabs.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import psycopg

import load_jobs_csv
import load_subs_placements_starts as spls


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics
# ─────────────────────────────────────────────────────────────────────────────

TABLES = ('companies', 'company_alias', 'jobs',
          'client_submissions', 'placements', 'starts')


def report_counts(conn, label: str, schema: str) -> None:
    print(f"\n  {label}:")
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            (n,) = cur.fetchone()
            print(f"    {schema}.{t:<20} {n:>10,}")


def banner(text: str) -> None:
    print()
    print("═" * 72)
    print(f"  {text}")
    print("═" * 72)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    conn,
    jobs_csv: Path | None,
    subs_csv: Path | None,
    placements_csv: Path | None,
    starts_csv: Path | None,
    dry_run: bool,
    schema: str,
    skip_aliases: bool = False,
    aliases_only: bool = False,
    error_log: 'load_jobs_csv.ErrorLog | None' = None,
) -> None:

    load_jobs_csv.set_search_path(conn, schema)
    report_counts(conn, "Row counts BEFORE load", schema)

    upload_id: int | None = None
    if not aliases_only:
        # ── Step 1 ─ jobs ────────────────────────────────────────────────
        # load_jobs_csv.load() commits internally on success, so subs/placements/
        # starts (which read job_ids from the DB) see the freshly loaded jobs.
        # It also creates a job_uploads batch row and returns the upload_id —
        # we surface it at the end so the caller can score just this batch
        # with: score_and_route.py --upload-id N
        if jobs_csv is not None:
            banner("STEP 1/5 — jobs (+ companies, +new categories/client_types)")
            upload_id = load_jobs_csv.load(
                jobs_csv, conn, dry_run=dry_run, schema=schema,
                error_log=error_log,
            )
        else:
            print("\n[skip] jobs (no --jobs path supplied)")

        # ── Steps 2-4 ─ subs / placements / starts ───────────────────────
        # Pull current FK target sets in one round-trip; mutate placement_ids
        # locally as new placements come in so the starts loader sees them
        # even in dry-run mode.
        banner("Reading FK reference data from DB")
        job_ids, placement_ids, placement_types = spls.load_existing_ids(conn)
        print(f"  jobs: {len(job_ids):,}  ·  "
              f"placements: {len(placement_ids):,}  ·  "
              f"placement_types: {len(placement_types)}")

        if subs_csv is not None:
            banner("STEP 2/5 — client_submissions")
            spls.load_subs(conn, subs_csv, job_ids, dry_run, schema, error_log)
        else:
            print("\n[skip] client_submissions")

        if placements_csv is not None:
            banner("STEP 3/5 — placements")
            new_pids = spls.load_placements(
                conn, placements_csv, job_ids, placement_types, dry_run,
                schema, error_log,
            )
            placement_ids |= new_pids
        else:
            print("\n[skip] placements")

        if starts_csv is not None:
            banner("STEP 4/5 — starts")
            spls.load_starts(
                conn, starts_csv,
                job_ids, placement_ids, placement_types, dry_run,
                schema, error_log,
            )
        else:
            print("\n[skip] starts")

    # ── Step 5 ─ company_alias (always runs unless --skip-aliases) ──────────
    if skip_aliases:
        print("\n[skip] company_alias (--skip-aliases)")
    else:
        banner("STEP 5/5 — company_alias  (sub-entity → canonical via jobs)")
        spls.populate_company_aliases(conn, schema, dry_run)

    if dry_run:
        print("\n[DRY RUN] no commit issued.")
    else:
        conn.commit()
        print("\nCommitted.")
        report_counts(conn, "Row counts AFTER load", schema)
        if upload_id is not None:
            print(f"\n  this batch is upload_id = {upload_id}")
            print(f"  score it with:  score_and_route.py --upload-id {upload_id} "
                  f"--schema {schema} --dsn ...")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="End-to-end Jobs Data load pipeline (jobs → subs → placements → starts)"
    )
    p.add_argument('--jobs',       type=Path, help="Jobs Report CSV")
    p.add_argument('--subs',       type=Path, help="Client Subs CSV")
    p.add_argument('--placements', type=Path, help="Placements CSV")
    p.add_argument('--starts',     type=Path, help="Starts CSV")
    p.add_argument('--skip-jobs',  action='store_true',
                   help="Jobs already loaded; start at submissions")
    p.add_argument('--skip-aliases', action='store_true',
                   help="Skip the company_alias derivation step")
    p.add_argument('--aliases-only', action='store_true',
                   help="Skip CSV loading; only derive company_alias from "
                        "data already in the DB")
    p.add_argument('--schema', default='jop',
                   help="Postgres schema for the jop tables (default: jop)")
    p.add_argument('--dsn', default='', help="Postgres DSN (default: PG* env vars)")
    p.add_argument('--dry-run', action='store_true',
                   help="Parse and validate without writing to DB")
    p.add_argument('--log-file', type=Path,
                   help="CSV file to record every rejected row with full "
                        "raw_row, line_number and primary id details. "
                        "All four loaders share the same file. Defaults "
                        "to load_errors_<timestamp>.csv in CWD.")
    args = p.parse_args(argv)

    if args.aliases_only:
        if any((args.jobs, args.subs, args.placements, args.starts)):
            p.error("--aliases-only is exclusive of all CSV inputs")
        if args.skip_aliases:
            p.error("--aliases-only and --skip-aliases are contradictory")
        jobs_csv = None
    else:
        jobs_csv = None if args.skip_jobs else args.jobs
        if not args.skip_jobs and args.jobs is None:
            p.error("supply --jobs (or --skip-jobs if jobs are already loaded, "
                    "or --aliases-only)")

        paths = {
            '--jobs':       jobs_csv,
            '--subs':       args.subs,
            '--placements': args.placements,
            '--starts':     args.starts,
        }
        for label, path in paths.items():
            if path is not None and not path.exists():
                print(f"error: {label} {path} does not exist", file=sys.stderr)
                return 1

        if not any(paths.values()):
            p.error("nothing to load — supply at least one of --jobs / --subs / "
                    "--placements / --starts (or --aliases-only)")

    log_path = args.log_file or Path(
        f"load_errors_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    )

    with psycopg.connect(args.dsn or '') as conn, \
         load_jobs_csv.ErrorLog(log_path) as elog:
        print(f"Error log → {log_path.resolve()}")
        run_pipeline(
            conn,
            jobs_csv=jobs_csv,
            subs_csv=args.subs,
            placements_csv=args.placements,
            starts_csv=args.starts,
            dry_run=args.dry_run,
            schema=args.schema,
            skip_aliases=args.skip_aliases,
            aliases_only=args.aliases_only,
            error_log=elog,
        )
        if elog.count:
            print(f"\n{elog.count:,} rejected row(s) written to {log_path}")
        else:
            print(f"\nNo rows rejected (log file: {log_path})")

    return 0


if __name__ == '__main__':
    sys.exit(main())
