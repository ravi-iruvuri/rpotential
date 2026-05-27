"""
load_jobs_csv.py — Load Jobs Report CSV into <schema>.jobs (default schema: jop).

Workflow:
  1. Set search_path so unqualified table names resolve to <schema>.
  2. Resolve canonicalization aliases (job_type_alias, category_alias) and
     valid reference values (statuses, job_types, client_types, categories)
     from the database.
  3. Pre-scan the CSV row-by-row to:
       - canonicalize Job Type / Category via aliases,
       - recode Country of Placement ('United States' → 'US', 'No'/'' → NULL),
       - discover new client_types / categories / companies not yet seeded.
  4. Upsert any new reference rows and the company directory.
  5. Bulk-insert jobs in batches with ON CONFLICT (job_id) DO NOTHING.

Usage:
    pip install 'psycopg[binary]>=3.1'
    python load_jobs_csv.py /path/to/jobs.csv \\
        --schema jop \\
        --dsn 'postgresql://user:pwd@host:5432/dbname'

If --dsn is omitted, libpq picks up PG* env vars (PGHOST, PGUSER, PGDATABASE,
PGPASSWORD, PGPORT). Pass --dry-run to validate without committing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


# ─────────────────────────────────────────────────────────────────────────────
# Error logging (shared with the other loaders via import)
# ─────────────────────────────────────────────────────────────────────────────

class ErrorLog:
    """Append-only CSV log for rows rejected by the loaders.

    Multiple loader runs / processes can target the same path safely — the
    header is written only when the file is empty/new. Pass the same
    ErrorLog instance to every loader in a pipeline so all rejects land
    in one file.
    """

    HEADER = (
        'logged_at', 'source_csv', 'table', 'line_number',
        'job_id', 'primary_key', 'primary_id', 'reason', 'raw_row_json',
    )

    def __init__(self, path: Path):
        self.path = Path(path)
        is_new = (not self.path.exists()) or self.path.stat().st_size == 0
        self._fh = self.path.open('a', encoding='utf-8', newline='')
        self._w  = csv.writer(self._fh)
        if is_new:
            self._w.writerow(self.HEADER)
        self.count = 0

    def write(self, *, source_csv: str, table: str, line_number: int,
              reason: str, raw_row,
              primary_key: str | None = None,
              job_id_field: str = 'Job ID') -> None:
        if isinstance(raw_row, dict):
            raw_serialized = json.dumps(raw_row, ensure_ascii=False, default=str)
            primary_id = raw_row.get(primary_key, '') if primary_key else ''
            job_id     = raw_row.get(job_id_field, '')
        else:
            raw_serialized = str(raw_row)
            primary_id = ''
            job_id     = ''

        self._w.writerow([
            datetime.now().isoformat(timespec='seconds'),
            source_csv, table, line_number,
            job_id, primary_key or '', primary_id, reason, raw_serialized,
        ])
        self._fh.flush()                                    # survive abort
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def make_error_writer(error_log: 'ErrorLog | None',
                      source_csv: str, table: str,
                      primary_key: str = 'Job ID',
                      job_id_field: str = 'Job ID'):
    """Return a (line_no, raw_dict, reason) callable that appends to
    `error_log`, or None if `error_log` is None — caller can pass the
    return value straight to read_and_validate."""
    if error_log is None:
        return None

    def writer(line_no, raw, reason):
        error_log.write(
            source_csv=source_csv, table=table, line_number=line_no,
            reason=reason, raw_row=raw,
            primary_key=primary_key, job_id_field=job_id_field,
        )
    return writer


# ─────────────────────────────────────────────────────────────────────────────
# Schema helper (shared with the other loaders via import)
# ─────────────────────────────────────────────────────────────────────────────

def set_search_path(conn, schema: str) -> None:
    """Set search_path so unqualified table names resolve to <schema>, public."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )


# ─────────────────────────────────────────────────────────────────────────────
# Canonicalization helpers
# ─────────────────────────────────────────────────────────────────────────────

def empty_to_none(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def canon_int(raw: str | None) -> int | None:
    s = empty_to_none(raw)
    return int(s) if s is not None else None


def canon_country(raw: str | None) -> str | None:
    """Per spec: 'United States' → 'US', 'No' → NULL, blank → NULL, 'US' → 'US'."""
    v = empty_to_none(raw)
    if v is None or v == 'No':
        return None
    if v in ('US', 'United States'):
        return 'US'
    # any other value (rare) → NULL; the CHECK constraint only allows 'US'/NULL
    return None


_DATE_FORMATS = ('%m/%d/%y %H:%M', '%m/%d/%Y %H:%M',
                 '%m/%d/%y %H:%M:%S', '%m/%d/%Y %H:%M:%S')


def parse_date_added(raw: str | None) -> datetime | None:
    s = empty_to_none(raw)
    if s is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unparseable Date Added: {s!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RefData:
    job_type_aliases:   dict[str, str]
    category_aliases:   dict[str, str]
    valid_job_types:    set[str]
    valid_client_types: set[str]
    valid_categories:   set[str]
    valid_statuses:     set[str]


JOB_COLS = (
    'job_id', 'vms_req_number', 'status', 'num_openings', 'job_title',
    'category', 'required_skill', 'publishing_status', 'job_type',
    'date_added', 'client_type', 'city', 'state_or_province',
    'country_of_placement', 'company_id', 'upload_id', 'source_row_hash',
)

# Columns that participate in the source_row_hash. Audit columns (job_id,
# upload_id, ingested_at, category_imputed, source_row_hash) deliberately
# excluded — re-uploading the same CSV must produce the same hash, and an
# agent flipping category_imputed must not bloat history.
HASH_COLS = (
    'vms_req_number', 'status', 'num_openings', 'job_title',
    'category', 'required_skill', 'publishing_status', 'job_type',
    'date_added', 'client_type', 'city', 'state_or_province',
    'country_of_placement', 'company_id',
)


def row_hash(r: dict) -> str:
    """SHA-256 over the source data fields, '|'-separated. Stable across
    runs because datetimes use ISO format and None becomes ''."""
    parts = []
    for c in HASH_COLS:
        v = r.get(c)
        if v is None:
            parts.append('')
        elif isinstance(v, datetime):
            parts.append(v.isoformat())
        else:
            parts.append(str(v))
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def create_upload_batch(conn, *, source: str, source_file: str | None,
                         uploaded_by: str | None = None,
                         notes: str | None = None) -> int:
    """Insert one row into job_uploads and return its upload_id.
    Call once per ingest batch; pass the returned id into load()."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_uploads (source, source_file, uploaded_by, notes)
            VALUES (%s, %s, %s, %s)
            RETURNING upload_id
            """,
            (source, source_file, uploaded_by, notes),
        )
        (upload_id,) = cur.fetchone()
    return upload_id


def finalize_upload_batch(conn, upload_id: int, row_count: int) -> None:
    """Stamp the upload row with the final row_count once load completes."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_uploads SET row_count = %s WHERE upload_id = %s",
            (row_count, upload_id),
        )


def load_refs(conn) -> RefData:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT raw_value, canonical_value FROM job_type_alias")
        jt_alias = {r['raw_value']: r['canonical_value'] for r in cur}

        cur.execute("SELECT raw_value, canonical_value FROM category_alias")
        cat_alias = {r['raw_value']: r['canonical_value'] for r in cur}

        cur.execute("SELECT job_type     FROM job_type_ref")
        jt_valid = {r['job_type'] for r in cur}

        cur.execute("SELECT client_type  FROM client_type_ref")
        ct_valid = {r['client_type'] for r in cur}

        cur.execute("SELECT category     FROM category_ref")
        cat_valid = {r['category'] for r in cur}

        cur.execute("SELECT status_code  FROM job_status_ref")
        st_valid = {r['status_code'] for r in cur}

    return RefData(jt_alias, cat_alias, jt_valid, ct_valid, cat_valid, st_valid)


def transform_row(raw: dict, refs: RefData) -> dict:
    """Apply canonicalization to one CSV row → dict of column values."""
    jt_raw = empty_to_none(raw.get('Job Type')) or 'Contract'
    job_type = refs.job_type_aliases.get(jt_raw, jt_raw)

    cat_raw = empty_to_none(raw.get('Category'))
    category = refs.category_aliases.get(cat_raw, cat_raw) if cat_raw else None

    return {
        'job_id':              canon_int(raw['Job ID']),
        'vms_req_number':      empty_to_none(raw.get('VMS Req #')),
        'status':              empty_to_none(raw.get('Status')),
        'num_openings':        canon_int(raw.get('# of Openings')) or 0,
        'job_title':           empty_to_none(raw.get('Job Title')),
        'category':            category,
        'required_skill':      empty_to_none(raw.get('Required Skill')),
        'publishing_status':   empty_to_none(raw.get('Publishing Status1')),
        'job_type':            job_type,
        'date_added':          parse_date_added(raw.get('Date Added')),
        'client_type':         empty_to_none(raw.get('Client type')),
        'city':                empty_to_none(raw.get('City')),
        'state_or_province':   empty_to_none(raw.get('State or Province')),
        'country_of_placement': canon_country(raw.get('Country of Placement')),
        'company_id':          canon_int(raw.get('Company ID')),
        'upload_id':           None,    # set by load() after batch row exists
        'source_row_hash':     None,    # computed by load() over HASH_COLS
        '_company_name':       empty_to_none(raw.get('Company Name')),
    }


def upsert_missing_refs(conn, refs: RefData,
                        new_categories: set[str],
                        new_client_types: set[str],
                        new_companies: dict[int, str]) -> None:
    with conn.cursor() as cur:
        if new_categories:
            cur.executemany(
                "INSERT INTO category_ref (category) "
                "VALUES (%s) ON CONFLICT DO NOTHING",
                [(c,) for c in new_categories],
            )
            print(f"  + {len(new_categories)} new categories: {sorted(new_categories)}")

        if new_client_types:
            cur.executemany(
                "INSERT INTO client_type_ref (client_type) "
                "VALUES (%s) ON CONFLICT DO NOTHING",
                [(c,) for c in new_client_types],
            )
            print(f"  + {len(new_client_types)} new client types: {sorted(new_client_types)}")

        if new_companies:
            cur.executemany(
                "INSERT INTO companies (company_id, company_name) "
                "VALUES (%s, %s) "
                "ON CONFLICT (company_id) DO UPDATE "
                "  SET company_name = EXCLUDED.company_name",
                list(new_companies.items()),
            )
            print(f"  + {len(new_companies)} companies upserted")


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Keep the last occurrence per job_id within a single batch.
    Postgres's ON CONFLICT DO UPDATE forbids two rows with the same
    conflict key in one statement, so we collapse first.
    Returns (deduped_rows, num_dropped)."""
    by_id: dict[int, dict] = {}
    for r in rows:
        by_id[r['job_id']] = r              # later occurrence wins
    dropped = len(rows) - len(by_id)
    return list(by_id.values()), dropped


def classify_rows(conn, rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Single SELECT round-trip — split rows into (new, changed, unchanged).
    The actual UPSERT still WHERE-filters on hash as a safety net, but
    classifying up front gives accurate counts to print and lets us skip
    sending unchanged rows over the wire."""
    if not rows:
        return [], [], []
    job_ids = [r['job_id'] for r in rows]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, source_row_hash FROM jobs "
            "WHERE job_id = ANY(%s::bigint[])",
            (job_ids,),
        )
        existing = {jid: h for jid, h in cur.fetchall()}

    new_rows, changed_rows, unchanged_rows = [], [], []
    for r in rows:
        prior = existing.get(r['job_id'])
        if prior is None:
            new_rows.append(r)
        elif prior != r['source_row_hash']:
            changed_rows.append(r)
        else:
            unchanged_rows.append(r)
    return new_rows, changed_rows, unchanged_rows


_DATA_COLS_FOR_UPDATE = tuple(c for c in JOB_COLS if c != 'job_id')

_UPDATE_SET_CLAUSE = ',\n        '.join(
    f"{c} = EXCLUDED.{c}" for c in _DATA_COLS_FOR_UPDATE if c != 'ingested_at'
) + ',\n        ingested_at = now()'


def insert_jobs(conn, rows: list[dict], batch_size: int = 1000) -> int:
    """UPSERT in batches. ON CONFLICT DO UPDATE fires only when the hash
    differs — a safety net since classify_rows already filtered. The
    BEFORE UPDATE trigger captures OLD into jobs_history per change."""
    if not rows:
        return 0
    placeholders = ', '.join(['%s'] * len(JOB_COLS))
    upsert_sql = (
        f"INSERT INTO jobs ({', '.join(JOB_COLS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (job_id) DO UPDATE SET\n        {_UPDATE_SET_CLAUSE}\n"
        f"  WHERE jobs.source_row_hash IS DISTINCT FROM EXCLUDED.source_row_hash"
    )

    affected = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            tuples = [tuple(r[c] for c in JOB_COLS) for r in batch]
            cur.executemany(upsert_sql, tuples)
            affected += cur.rowcount if cur.rowcount >= 0 else len(tuples)
            done = min(i + batch_size, len(rows))
            print(f"  jobs: {done} / {len(rows)}", end='\r')
    print()
    return affected


def load(csv_path: Path, conn, dry_run: bool = False, schema: str = 'jop',
         error_log: 'ErrorLog | None' = None,
         upload_id: int | None = None) -> int | None:
    """Load a jobs CSV. If upload_id is None, creates a fresh job_uploads
    row tagged 'csv' for this file. Returns the upload_id (or None in
    dry-run mode) so callers / the pipeline can reference the batch."""
    set_search_path(conn, schema)
    print(f"Reading reference data from {schema!r} …")
    refs = load_refs(conn)

    print(f"Scanning {csv_path} …")
    new_companies: dict[int, str] = {}
    new_categories: set[str] = set()
    new_client_types: set[str] = set()
    bad_rows: list[tuple[int, str]] = []
    rows: list[dict] = []

    def add_bad(line_no: int, raw: dict, msg: str) -> None:
        bad_rows.append((line_no, msg))
        if error_log is not None:
            error_log.write(
                source_csv=csv_path.name, table='jobs',
                line_number=line_no, reason=msg, raw_row=raw,
                primary_key='Job ID',
            )

    with csv_path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for line_no, raw in enumerate(reader, start=2):     # +1 for header
            try:
                r = transform_row(raw, refs)
            except Exception as e:                          # noqa: BLE001
                add_bad(line_no, raw, f"transform error: {e}")
                continue

            # Validate hard-required fields
            if r['job_id'] is None or r['date_added'] is None or r['company_id'] is None:
                add_bad(line_no, raw, "missing job_id/date_added/company_id")
                continue
            if r['status'] not in refs.valid_statuses:
                add_bad(line_no, raw, f"unknown status: {r['status']!r}")
                continue
            if r['job_type'] not in refs.valid_job_types:
                add_bad(line_no, raw, f"unknown job_type: {r['job_type']!r}")
                continue

            # Discover new ref values
            if r['category'] and r['category'] not in refs.valid_categories:
                new_categories.add(r['category'])
            if r['client_type'] and r['client_type'] not in refs.valid_client_types:
                new_client_types.add(r['client_type'])

            cname = r.pop('_company_name')
            if cname:
                new_companies[r['company_id']] = cname

            rows.append(r)

    print(f"  parsed: {len(rows):,} valid rows · {len(bad_rows):,} bad rows")

    if bad_rows:
        print("First 10 problems:")
        for ln, msg in bad_rows[:10]:
            print(f"  line {ln}: {msg}")

    # Dedupe within the CSV: ON CONFLICT DO UPDATE forbids the same
    # conflict key appearing twice in one INSERT, so the last occurrence
    # of each job_id wins. Typically 0 dropped on a clean source export.
    rows, dropped = dedupe_rows(rows)
    if dropped:
        print(f"  deduped: dropped {dropped:,} duplicate job_id row(s) "
              f"(last occurrence kept)")

    # Compute source_row_hash for every surviving row.
    for r in rows:
        r['source_row_hash'] = row_hash(r)

    # Classify before insert so we can show new/changed/unchanged counts
    # and skip sending unchanged rows over the wire.
    new_rows, changed_rows, unchanged_rows = classify_rows(conn, rows)
    print(f"  vs. DB: {len(new_rows):,} new · "
          f"{len(changed_rows):,} changed · "
          f"{len(unchanged_rows):,} unchanged")

    if dry_run:
        print("\n[DRY RUN] Would upsert:")
        print(f"  - 1 job_uploads batch row")
        print(f"  - {len(new_companies):,} companies")
        print(f"  - {len(new_categories):,} new categories: {sorted(new_categories)}")
        print(f"  - {len(new_client_types):,} new client types: {sorted(new_client_types)}")
        print(f"  - {len(new_rows):,} new jobs inserted")
        print(f"  - {len(changed_rows):,} existing jobs updated "
              f"(prior versions captured into jobs_history)")
        print(f"  - {len(unchanged_rows):,} jobs untouched (hash matches)")
        return None

    # Create (or accept caller's) upload batch BEFORE inserting jobs so
    # the FK is satisfied.
    if upload_id is None:
        upload_id = create_upload_batch(
            conn, source='csv', source_file=csv_path.name,
            notes=f"Loaded by load_jobs_csv.py from {csv_path.name}",
        )
        print(f"  created job_uploads.upload_id = {upload_id}")
    else:
        print(f"  using caller-supplied upload_id = {upload_id}")

    rows_to_send = new_rows + changed_rows
    for r in rows_to_send:
        r['upload_id'] = upload_id

    print("Upserting reference data + companies …")
    upsert_missing_refs(conn, refs, new_categories, new_client_types, new_companies)

    print("Inserting / updating jobs …")
    affected = insert_jobs(conn, rows_to_send)

    finalize_upload_batch(conn, upload_id, len(new_rows))

    conn.commit()
    print(
        f"\nDone. upload_id={upload_id}: "
        f"{len(new_rows):,} inserted · "
        f"{len(changed_rows):,} updated (history captured) · "
        f"{len(unchanged_rows):,} unchanged (skipped)."
    )
    return upload_id


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Load Jobs Report CSV into <schema>.jobs")
    p.add_argument('csv_path', type=Path)
    p.add_argument('--schema', default='jop',
                   help="Postgres schema for the jop tables (default: jop)")
    p.add_argument('--dsn', default='',
                   help="Postgres DSN (default: PG* env vars)")
    p.add_argument('--dry-run', action='store_true',
                   help="Parse and validate without writing to DB")
    p.add_argument('--log-file', type=Path,
                   help="CSV file to record every rejected row with full "
                        "raw_row, line_number and Job ID. Defaults to "
                        "load_errors_<timestamp>.csv in CWD.")
    args = p.parse_args(argv)

    if not args.csv_path.exists():
        print(f"error: {args.csv_path} does not exist", file=sys.stderr)
        return 1

    log_path = args.log_file or Path(
        f"load_errors_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    )

    with psycopg.connect(args.dsn or '') as conn, ErrorLog(log_path) as elog:
        print(f"Error log → {log_path.resolve()}")
        load(args.csv_path, conn, dry_run=args.dry_run,
             schema=args.schema, error_log=elog)
        if elog.count:
            print(f"  → {elog.count:,} rejected row(s) appended to {log_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
