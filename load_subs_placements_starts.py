"""
load_subs_placements_starts.py — Load Client Subs, Placements, and Starts CSVs
into <schema>.client_submissions, <schema>.placements, <schema>.starts
(default schema: jop).

Order is fixed by referential integrity:
    1. client_submissions   (FK → jobs)
    2. placements           (FK → jobs)
    3. starts               (FK → placements, jobs)

Per the schema:
  - company_id in these three tables has NO FK (placements alone has ~1,514
    distinct sub-entity IDs vs 122 canonical companies). We therefore do
    NOT insert into <schema>.companies from these CSVs — that's done by
    the Jobs Report loader. Sub-entity IDs are kept verbatim;
    canonicalization via <schema>.company_alias is a follow-up step
    (see design notes §5 Phase 0).
  - submission_status is fixed to 'Client Submission' (CHECK constraint).
  - placement_type must exist in <schema>.placement_type_ref.

Usage:
    pip install 'psycopg[binary]>=3.1'
    python load_subs_placements_starts.py \\
        --subs       'Jobs Data - Prasad.xlsx - Client Subs.csv' \\
        --placements 'Jobs Data - Prasad.xlsx - Placements.csv' \\
        --starts     'Jobs Data - Prasad.xlsx - Starts.csv' \\
        --schema jop \\
        --dsn 'postgresql://user:pwd@host:5432/dbname'

Pass any subset of --subs/--placements/--starts to load only those tabs.
If --dsn is omitted, libpq picks up PG* env vars. Pass --dry-run to
validate without committing.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from load_jobs_csv import ErrorLog, make_error_writer


# ─────────────────────────────────────────────────────────────────────────────
# Schema helper
# ─────────────────────────────────────────────────────────────────────────────

def set_search_path(conn, schema: str) -> None:
    """Set search_path so unqualified table names resolve to <schema>, public."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )


# ─────────────────────────────────────────────────────────────────────────────
# Parsers (shared with load_jobs_csv.py conventions)
# ─────────────────────────────────────────────────────────────────────────────

def empty_to_none(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def canon_int(raw: str | None) -> int | None:
    s = empty_to_none(raw)
    return int(s) if s is not None else None


_DATE_FORMATS = (
    '%m/%d/%y %H:%M', '%m/%d/%Y %H:%M',
    '%m/%d/%y %H:%M:%S', '%m/%d/%Y %H:%M:%S',
    '%m/%d/%y', '%m/%d/%Y',
)


def parse_dt(raw: str | None, *, field: str) -> datetime | None:
    s = empty_to_none(raw)
    if s is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unparseable {field}: {s!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Reference data
# ─────────────────────────────────────────────────────────────────────────────

def load_existing_ids(conn) -> tuple[set[int], set[int], set[str]]:
    """Pull job_id, placement_id, placement_type sets from the DB for FK validation."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT job_id FROM jobs")
        job_ids = {r['job_id'] for r in cur}

        cur.execute("SELECT placement_id FROM placements")
        placement_ids = {r['placement_id'] for r in cur}

        cur.execute("SELECT placement_type FROM placement_type_ref")
        placement_types = {r['placement_type'] for r in cur}

    return job_ids, placement_ids, placement_types


# ─────────────────────────────────────────────────────────────────────────────
# Per-table transforms
# ─────────────────────────────────────────────────────────────────────────────

SUBS_COLS       = ('link_id', 'job_id', 'company_id', 'submission_status', 'date_added')
PLACEMENTS_COLS = ('placement_id', 'job_id', 'company_id', 'placement_type', 'date_added')
STARTS_COLS     = ('placement_id', 'job_id', 'company_id', 'placement_type', 'start_date')


def transform_sub(raw: dict) -> dict:
    status = empty_to_none(raw.get('Status')) or 'Client Submission'
    return {
        'link_id':           canon_int(raw.get('Link ID')),
        'job_id':            canon_int(raw.get('Job ID')),
        'company_id':        canon_int(raw.get('Company ID')),
        'submission_status': status,
        'date_added':        parse_dt(raw.get('Date Added'), field='Date Added'),
    }


def transform_placement(raw: dict) -> dict:
    return {
        'placement_id':   canon_int(raw.get('Placement ID')),
        'job_id':         canon_int(raw.get('Job ID')),
        'company_id':     canon_int(raw.get('Company ID')),
        'placement_type': empty_to_none(raw.get('Placement Type')),
        'date_added':     parse_dt(raw.get('Date Added'), field='Date Added'),
    }


def transform_start(raw: dict) -> dict:
    return {
        'placement_id':   canon_int(raw.get('Placement ID')),
        'job_id':         canon_int(raw.get('Job ID')),
        'company_id':     canon_int(raw.get('Company ID')),
        'placement_type': empty_to_none(raw.get('Placement Type')),
        'start_date':     parse_dt(raw.get('Start Date'), field='Start Date'),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Generic CSV pipeline
# ─────────────────────────────────────────────────────────────────────────────

def read_and_validate(
    csv_path: Path,
    transform,
    required_cols: tuple[str, ...],
    validators,
    error_writer=None,
) -> tuple[list[dict], list[tuple[int, str]]]:
    """
    Read CSV, transform rows, drop+report invalid ones.

    `validators` is a list of (predicate, message) pairs run after transform;
    predicate(row) returning truthy → row is rejected with that message.

    `error_writer`, if provided, is a callable (line_no, raw_dict, reason)
    invoked for every rejected row — used to append to a shared ErrorLog.
    """
    rows: list[dict] = []
    bad:  list[tuple[int, str]] = []

    def reject(line_no: int, raw: dict, msg: str) -> None:
        bad.append((line_no, msg))
        if error_writer is not None:
            error_writer(line_no, raw, msg)

    with csv_path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for line_no, raw in enumerate(reader, start=2):  # +1 for header
            try:
                r = transform(raw)
            except Exception as e:                       # noqa: BLE001
                reject(line_no, raw, f"transform error: {e}")
                continue

            # Hard required: every column in the schema is NOT NULL
            missing = [c for c in required_cols if r.get(c) is None]
            if missing:
                reject(line_no, raw, f"missing required: {missing}")
                continue

            failed = next(((msg) for pred, msg in validators if pred(r)), None)
            if failed:
                reject(line_no, raw, failed)
                continue

            rows.append(r)

    return rows, bad


def insert_batch(
    conn,
    table: str,
    cols: tuple[str, ...],
    rows: list[dict],
    conflict_target: str,
    batch_size: int = 1000,
) -> int:
    if not rows:
        return 0
    placeholders = ', '.join(['%s'] * len(cols))
    insert_sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_target}) DO NOTHING"
    )
    inserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            tuples = [tuple(r[c] for c in cols) for r in batch]
            cur.executemany(insert_sql, tuples)
            inserted += cur.rowcount if cur.rowcount >= 0 else len(tuples)
            done = min(i + batch_size, len(rows))
            print(f"  {table}: {done} / {len(rows)}", end='\r')
    print()
    return inserted


def report_bad(label: str, bad: list[tuple[int, str]]) -> None:
    if not bad:
        return
    print(f"  {label}: {len(bad):,} bad rows. First 10:")
    for ln, msg in bad[:10]:
        print(f"    line {ln}: {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Per-table drivers
# ─────────────────────────────────────────────────────────────────────────────

def load_subs(conn, csv_path: Path, job_ids: set[int], dry_run: bool,
              schema: str = 'jop',
              error_log: 'ErrorLog | None' = None) -> None:
    print(f"\n[client_submissions] {csv_path}")
    rows, bad = read_and_validate(
        csv_path, transform_sub, SUBS_COLS,
        validators=[
            (lambda r: r['job_id'] not in job_ids,
             f"job_id not in {schema}.jobs"),
            (lambda r: r['submission_status'] != 'Client Submission',
             "submission_status must be 'Client Submission'"),
        ],
        error_writer=make_error_writer(
            error_log, csv_path.name, 'client_submissions',
            primary_key='Link ID',
        ),
    )
    print(f"  parsed: {len(rows):,} valid · {len(bad):,} bad")
    report_bad('client_submissions', bad)
    if dry_run:
        print("  [DRY RUN] skipped insert")
        return
    inserted = insert_batch(conn, 'client_submissions', SUBS_COLS, rows, 'link_id')
    print(f"  inserted: {inserted:,} (skipped on conflict: {len(rows) - inserted:,})")


def load_placements(
    conn, csv_path: Path,
    job_ids: set[int], placement_types: set[str],
    dry_run: bool,
    schema: str = 'jop',
    error_log: 'ErrorLog | None' = None,
) -> set[int]:
    """Returns the set of placement_ids successfully ingested (incl. pre-existing)."""
    print(f"\n[placements] {csv_path}")
    rows, bad = read_and_validate(
        csv_path, transform_placement, PLACEMENTS_COLS,
        validators=[
            (lambda r: r['job_id'] not in job_ids,
             f"job_id not in {schema}.jobs"),
            (lambda r: r['placement_type'] not in placement_types,
             f"placement_type not in {schema}.placement_type_ref"),
        ],
        error_writer=make_error_writer(
            error_log, csv_path.name, 'placements',
            primary_key='Placement ID',
        ),
    )
    print(f"  parsed: {len(rows):,} valid · {len(bad):,} bad")
    report_bad('placements', bad)
    valid_placement_ids = {r['placement_id'] for r in rows}
    if dry_run:
        print("  [DRY RUN] skipped insert")
        return valid_placement_ids
    inserted = insert_batch(conn, 'placements', PLACEMENTS_COLS, rows, 'placement_id')
    print(f"  inserted: {inserted:,} (skipped on conflict: {len(rows) - inserted:,})")
    return valid_placement_ids


def load_starts(
    conn, csv_path: Path,
    job_ids: set[int], placement_ids: set[int], placement_types: set[str],
    dry_run: bool,
    schema: str = 'jop',
    error_log: 'ErrorLog | None' = None,
) -> None:
    print(f"\n[starts] {csv_path}")
    rows, bad = read_and_validate(
        csv_path, transform_start, STARTS_COLS,
        validators=[
            (lambda r: r['job_id'] not in job_ids,
             f"job_id not in {schema}.jobs"),
            (lambda r: r['placement_id'] not in placement_ids,
             f"placement_id not in {schema}.placements"),
            (lambda r: r['placement_type'] not in placement_types,
             f"placement_type not in {schema}.placement_type_ref"),
        ],
        error_writer=make_error_writer(
            error_log, csv_path.name, 'starts',
            primary_key='Placement ID',
        ),
    )
    print(f"  parsed: {len(rows):,} valid · {len(bad):,} bad")
    report_bad('starts', bad)
    if dry_run:
        print("  [DRY RUN] skipped insert")
        return
    inserted = insert_batch(conn, 'starts', STARTS_COLS, rows, 'placement_id')
    print(f"  inserted: {inserted:,} (skipped on conflict: {len(rows) - inserted:,})")


# ─────────────────────────────────────────────────────────────────────────────
# company_alias derivation
# ─────────────────────────────────────────────────────────────────────────────
#
# The 122 canonical companies are populated by the Jobs Report loader.
# Placements / Starts / Client Subs carry sub-entity Company IDs (~1,514
# distinct in placements, ~102 in starts) that are NOT in companies.
#
# The canonical mapping comes from the data itself: for each row, the
# canonical company is whatever jobs.company_id is for that row's job_id.
# Per-source ordering is intentional — placements is processed first because
# it has the largest distinct-alias count, so its source_tab annotation
# wins under ON CONFLICT DO NOTHING.

ALIAS_SOURCES = (
    ('placements',  'placements'),
    ('starts',      'starts'),
    ('client_subs', 'client_submissions'),
)


def populate_company_aliases(conn, schema: str = 'jop',
                              dry_run: bool = False) -> dict[str, int]:
    """Insert one row into company_alias for every sub-entity company_id
    seen in placements / starts / client_submissions that isn't already
    a canonical company.

    Returns dict of source_tab → rows inserted (or rows-that-would-be in
    dry-run).
    """
    print(f"\n[company_alias] deriving from jobs ⨝ "
          f"{{placements, starts, client_submissions}} "
          f"(schema {schema!r})")
    counts: dict[str, int] = {}

    with conn.cursor() as cur:
        for source_tab, table in ALIAS_SOURCES:
            select_sql = f"""
                SELECT DISTINCT ON (t.company_id)
                       t.company_id, j.company_id, %s
                  FROM {table} t
                  JOIN jobs    j ON j.job_id = t.job_id
                 WHERE t.company_id IS NOT NULL
                   AND t.company_id <> j.company_id
                   AND NOT EXISTS (
                         SELECT 1 FROM companies      c
                          WHERE c.company_id        = t.company_id)
                   AND NOT EXISTS (
                         SELECT 1 FROM company_alias ca
                          WHERE ca.alias_company_id = t.company_id)
                 ORDER BY t.company_id, j.company_id
            """

            if dry_run:
                cur.execute(
                    f"SELECT COUNT(*) FROM ({select_sql}) sub",
                    (source_tab,),
                )
                (n,) = cur.fetchone()
            else:
                cur.execute(
                    f"INSERT INTO company_alias "
                    f"(alias_company_id, canonical_company_id, source_tab) "
                    f"{select_sql} "
                    f"ON CONFLICT (alias_company_id) DO NOTHING",
                    (source_tab,),
                )
                n = cur.rowcount if cur.rowcount >= 0 else 0

            counts[source_tab] = n
            verb = "would insert" if dry_run else "inserted"
            print(f"  {source_tab:<12} {verb} {n:>5,} alias(es)")

    return counts


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Load client_submissions, placements, and starts into <schema>.*"
    )
    p.add_argument('--subs',       type=Path, help="Client Subs CSV")
    p.add_argument('--placements', type=Path, help="Placements CSV")
    p.add_argument('--starts',     type=Path, help="Starts CSV")
    p.add_argument('--schema', default='jop',
                   help="Postgres schema for the jop tables (default: jop)")
    p.add_argument('--dsn', default='', help="Postgres DSN (default: PG* env vars)")
    p.add_argument('--dry-run', action='store_true',
                   help="Parse and validate without writing to DB")
    p.add_argument('--log-file', type=Path,
                   help="CSV file to record every rejected row with full "
                        "raw_row, line_number and primary id details. "
                        "Defaults to load_errors_<timestamp>.csv in CWD.")
    p.add_argument('--aliases-only', action='store_true',
                   help="Skip CSV loading; only derive company_alias from "
                        "data already in the DB")
    p.add_argument('--skip-aliases', action='store_true',
                   help="Skip the company_alias derivation step")
    args = p.parse_args(argv)

    if args.aliases_only:
        if args.subs or args.placements or args.starts:
            p.error("--aliases-only is exclusive of --subs/--placements/--starts")
    elif not (args.subs or args.placements or args.starts):
        p.error("supply at least one of --subs / --placements / --starts "
                "(or --aliases-only)")

    for label, path in (('--subs', args.subs),
                        ('--placements', args.placements),
                        ('--starts', args.starts)):
        if path is not None and not path.exists():
            print(f"error: {label} {path} does not exist", file=sys.stderr)
            return 1

    log_path = args.log_file or Path(
        f"load_errors_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    )

    with psycopg.connect(args.dsn or '') as conn, ErrorLog(log_path) as elog:
        set_search_path(conn, args.schema)
        print(f"Error log → {log_path.resolve()}")

        if not args.aliases_only:
            print(f"Reading FK reference data from schema {args.schema!r} …")
            job_ids, placement_ids, placement_types = load_existing_ids(conn)
            print(f"  jobs: {len(job_ids):,} · placements: {len(placement_ids):,} "
                  f"· placement_types: {len(placement_types)}")

            if args.subs:
                load_subs(conn, args.subs, job_ids, args.dry_run,
                          args.schema, elog)

            if args.placements:
                new_placement_ids = load_placements(
                    conn, args.placements, job_ids, placement_types,
                    args.dry_run, args.schema, elog,
                )
                placement_ids |= new_placement_ids

            if args.starts:
                load_starts(
                    conn, args.starts,
                    job_ids, placement_ids, placement_types,
                    args.dry_run, args.schema, elog,
                )

        if not args.skip_aliases:
            populate_company_aliases(conn, args.schema, args.dry_run)

        if elog.count:
            print(f"\n  → {elog.count:,} rejected row(s) appended to {log_path}")
        else:
            print(f"\n  → no rows rejected (log file: {log_path})")

        if not args.dry_run:
            conn.commit()
            print("\nCommitted.")
        else:
            print("\n[DRY RUN] no commit issued.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
