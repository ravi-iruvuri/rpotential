"""
impute_categories.py — Fill NULL category values on jobs using two strategies:

  Tier 1 — Clean-title exact match:
    Strip the VMS job-order prefix (everything up to the first ' - ') from
    job_title, then look up the most-frequent category assigned to that same
    clean title among already-categorised jobs. Covers ~96% of NULL rows.

  Tier 2 — Keyword rules:
    For titles with no Tier-1 match (mostly USA_* generic placeholders),
    apply a keyword rule table to assign the closest valid category.

  Skip:
    Noisy / uninformative titles ('US', 'USA', 'x', single-char, etc.)
    where imputation would be unreliable.  These remain NULL.

All updates set category_imputed = TRUE so downstream queries can
distinguish imputed values from recruiter-assigned ones.

Usage:
    python impute_categories.py --schema rpotential --dsn '...'
    python impute_categories.py --dry-run --schema rpotential --dsn '...'
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import psycopg
from psycopg.rows import dict_row

from load_jobs_csv import set_search_path


# ── Tier 2 keyword rules ───────────────────────────────────────────────────
# Evaluated in order; first match wins.
# All category values must exist in category_ref.
KEYWORD_RULES: list[tuple[list[str], str]] = [
    # --- Specialised / narrow first ---
    (['machine learning', 'ml engineer', 'ai engineer', 'artificial intelligence',
      'sd - machine learning', 'gen ai', 'llm'],
     'Artificial Intelligence'),
    (['data scientist', 'data engineer', 'big data', 'data analyst',
      'business intelligence', 'bi developer', 'bi analyst', 'etl',
      'data warehouse', 'dw-', 'dwh'],
     'Data & Business Intelligence'),
    (['electrical engineer', 'electronics engineer'],
     'Electrical Engineering'),
    (['civil engineer', 'structural engineer'],
     'Civil Engineering'),
    (['embedded', 'firmware engineer'],
     'Embedded Systems'),
    (['manufacturing engineer', 'mechanical engineer', 'industrial engineer',
      'process engineer', 'manufacturing technician', 'materials scheduler',
      'planner/scheduler', 'scheduler', 'planner'],
     'Manufacturing Engineering'),
    (['quality assurance', 'qa engineer', 'qa analyst', 'test engineer',
      'test analyst', 'quality assurance consultant'],
     'QA & Testing'),
    (['quality engineer', 'quality control', 'quality specialist'],
     'Quality Engineering'),
    (['regulatory affairs', 'regulatory specialist', 'compliance analyst'],
     'Regulatory Affairs'),
    (['research scientist', 'research engineer', 'r&d', 'engineering fellow'],
     'Research & Development'),
    (['security engineer', 'security analyst', 'cybersecurity',
      'info security', 'infosec', 'network admin', 'network engineer',
      'system admin', 'sysadmin', 'infrastructure engineer',
      'cloud engineer', 'devops', 'site reliability', 'platform engineer'],
     'Infrastructure'),
    (['enterprise architect', 'solution architect', 'solutions architect',
      'platform architect', 'product architect', 'chief architect',
      'agile architect', 'technical architect', 'it architect',
      'systems architect', 'system architect',
      'pega', 'salesforce', 'oracle', 'sap', 'dynamics', 'servicenow',
      'app dev', 'appl dev', 'java', 'python', '.net', 'react', 'angular',
      'developer', 'programmer', 'software engineer', 'technical lead',
      'tech lead', 'full stack', 'fullstack', 'solutions engineer',
      'solution engineer', 'application engineer', 'integration engineer',
      'systems engineer', 'principal engineer', 'staff engineer',
      'advanced systems engineer'],
     'Software Development'),
    (['agile', 'scrum master', 'scrum'],
     'Emerging Technologies'),
    (['functional consultant', 'functional analyst', 'business analyst',
      'business systems analyst'],
     'Business Analysis'),
    (['project manager', 'program manager', 'project coordinator',
      'delivery manager', 'it program manager', 'it project manager'],
     'Project Management'),
    (['product manager', 'product owner', 'associate product manager'],
     'Product Development Engineering'),
    (['business consultant', 'management consultant', 'strategy consultant',
      'talent partner', 'hr ', 'human resources', 'recruiter',
      'accounts receivable', 'accounts payable', 'finance analyst',
      'communications specialist', 'general clerk', 'clerk',
      'administrative', 'operations specialist', 'operations analyst',
      'dc associate', 'derivatives operations'],
     'Business Professional'),
    (['support executive', 'support specialist', 'help desk', 'service desk',
      'customer service', 'it support', 'desktop support',
      'customer support engineering'],
     'Service Desk'),
    (['technician', 'field technician', 'lab technician',
      'vehicle operations', 'assembly', 'manufacturing associate',
      'production associate'],
     'Technician'),
    (['design engineer', 'cad ', 'drafter', 'drafting'],
     'Design/Drafting'),
    (['a&e', 'architect engineer', 'architectural'],
     'A&E'),
    (['engineering admin', 'technical writer', 'documentation'],
     'Engineering Admin/Documentation'),
    # --- Broad catch-all patterns (evaluated last) ---
    (['dev ops', 'devsecops'],
     'Infrastructure'),
    (['semiconductor', 'vlsi', 'fpga', 'asic'],
     'Electrical Engineering'),
    (['buyer', 'procurement', 'sourcing specialist', 'engagement manager',
      'materials analyst', 'supply chain', 'accounts ', 'finance ',
      'technical support assistant', 'process expert', 'operations manager'],
     'Business Professional'),
    (['materials engineer', 'materials scheduler', 'materials planner',
      'manufacturing associate', 'production planner', 'lean'],
     'Manufacturing Engineering'),
    (['software', 'engineer - software', 'eng - ', '- developer',
      '- engineer', 'principal engineer', 'staff engineer'],
     'Software Development'),
    (['technical support', 'it support analyst'],
     'Service Desk'),
]

# Clean titles too short or generic to impute reliably
SKIP_TITLES: set[str] = {
    '', 'us', 'usa', 'x', 'dc', 'na', 'n/a', '-', 'tbd', 'none', 'null',
}


# ── Helpers ────────────────────────────────────────────────────────────────

def clean_title(raw: str) -> str:
    """
    Strip VMS order-number prefix(es) from job title.
    Handles single prefix:  'BACJP00199254 - Network Engineer V'
    Handles double prefix:  'BACJP00199254 - US - Sr Systems Engineer'
    Country codes (US/USA) after an order number are also stripped.
    """
    result = raw
    if ' - ' in result:
        result = result.split(' - ', 1)[1].strip()
    # Strip a second-level US / USA country prefix if still present
    upper = result.upper()
    if upper.startswith('US - ') or upper.startswith('USA - '):
        result = result.split(' - ', 1)[1].strip()
    return result


def keyword_impute(title: str) -> str | None:
    t = title.lower()
    for keywords, category in KEYWORD_RULES:
        if any(kw in t for kw in keywords):
            return category
    return None


def is_noisy(ct: str) -> bool:
    low = ct.lower().strip()
    return low in SKIP_TITLES or len(low) <= 2


# ── Core logic ─────────────────────────────────────────────────────────────

def build_tier1_lookup(conn) -> dict[str, str]:
    """
    For every clean title that already has a category in jobs,
    pick the category with the highest frequency (mode).
    Returns {clean_title: category}.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                CASE WHEN job_title LIKE '%% - %%'
                     THEN TRIM(SPLIT_PART(job_title, ' - ', 2))
                     ELSE TRIM(job_title)
                END  AS ctitle,
                category,
                COUNT(*) AS support
            FROM jobs
            WHERE category IS NOT NULL
              AND job_title IS NOT NULL
            GROUP BY 1, 2
        """)
        rows = cur.fetchall()

    best: dict[str, tuple[str, int]] = {}
    for ctitle, category, support in rows:
        if ctitle and (ctitle not in best or support > best[ctitle][1]):
            best[ctitle] = (category, support)

    return {k: v[0] for k, v in best.items()}


def fetch_null_category_jobs(conn) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT job_id, job_title
            FROM jobs
            WHERE category IS NULL
              AND job_title IS NOT NULL
        """)
        return cur.fetchall()


def impute_all(jobs: list[dict], tier1: dict[str, str]) -> list[dict]:
    results = []
    for job in jobs:
        ct = clean_title(job['job_title'])

        if is_noisy(ct):
            results.append({
                'job_id': job['job_id'], 'category': None,
                'tier': 'skip', 'clean_title': ct,
            })
            continue

        # Tier 1 — exact clean-title match
        if ct in tier1:
            results.append({
                'job_id': job['job_id'], 'category': tier1[ct],
                'tier': 'T1', 'clean_title': ct,
            })
            continue

        # Tier 2 — keyword rules
        cat = keyword_impute(ct)
        if cat:
            results.append({
                'job_id': job['job_id'], 'category': cat,
                'tier': 'T2', 'clean_title': ct,
            })
            continue

        results.append({
            'job_id': job['job_id'], 'category': None,
            'tier': 'unresolved', 'clean_title': ct,
        })

    return results


def apply_updates(conn, results: list[dict], dry_run: bool) -> int:
    to_update = [
        (r['category'], r['job_id'])
        for r in results if r['category'] is not None
    ]
    if not to_update or dry_run:
        return len(to_update)

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE jobs SET category = %s, category_imputed = TRUE "
            "WHERE job_id = %s",
            to_update,
        )
    return len(to_update)


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description='Impute NULL category values on jobs from title matching and keyword rules.'
    )
    p.add_argument('--schema', default='rpotential',
                   help='Postgres schema (default: rpotential)')
    p.add_argument('--dsn', default='',
                   help='Postgres DSN (default: PG* env vars)')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be updated without writing')
    args = p.parse_args(argv)

    with psycopg.connect(args.dsn or '') as conn:
        set_search_path(conn, args.schema)

        print('Building Tier 1 lookup from existing categorised jobs...')
        tier1 = build_tier1_lookup(conn)
        print(f'  {len(tier1):,} distinct clean titles with known category\n')

        print('Fetching NULL-category jobs...')
        jobs = fetch_null_category_jobs(conn)
        print(f'  {len(jobs):,} jobs to process\n')

        results = impute_all(jobs, tier1)

        by_tier = Counter(r['tier'] for r in results)
        by_cat  = Counter(r['category'] for r in results if r['category'])

        print('  ── Imputation summary ──────────────────────────────────')
        print(f'  Tier 1 (title match) : {by_tier["T1"]:>7,}')
        print(f'  Tier 2 (keyword rule): {by_tier["T2"]:>7,}')
        print(f'  Skipped (noisy title): {by_tier["skip"]:>7,}')
        print(f'  Unresolved           : {by_tier["unresolved"]:>7,}')
        print(f'  ────────────────────────────────────────────────────────')
        print(f'  Total to update      : {by_tier["T1"] + by_tier["T2"]:>7,}')

        print('\n  ── Category distribution of imputations ────────────────')
        for cat, cnt in by_cat.most_common():
            bar = '█' * (cnt // 200)
            print(f'  {cat:<40} {cnt:>6,}  {bar}')

        if by_tier['unresolved']:
            print(f'\n  ── Unresolved titles (sample) ──────────────────────────')
            seen: Counter = Counter()
            for r in results:
                if r['tier'] == 'unresolved':
                    seen[r['clean_title']] += 1
            for title, cnt in seen.most_common(15):
                print(f'  {cnt:>4}  {title}')

        n = apply_updates(conn, results, args.dry_run)

        if args.dry_run:
            print(f'\n[DRY RUN] would update {n:,} rows. No changes written.')
        else:
            conn.commit()
            print(f'\nCommitted. {n:,} rows updated.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
