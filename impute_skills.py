"""
impute_skills.py — Fill NULL required_skill values on jobs using two strategies:

  Tier 1 — Clean-title exact match:
    Strip the VMS job-order prefix from job_title, then look up the most-frequent
    required_skill assigned to that same clean title among already-skilled jobs.

  Tier 2 — Category + keyword rules:
    For titles with no Tier-1 match, use the job's category (or title keywords)
    to assign a representative skill value.

  Skip:
    Noisy / uninformative titles where imputation would be unreliable.

All updates set skill_imputed = TRUE so downstream queries can distinguish
imputed values from recruiter-assigned ones.

Usage:
    python impute_skills.py --schema jop --dsn 'postgresql://...'
    python impute_skills.py --dry-run --schema jop
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import psycopg
from psycopg.rows import dict_row

from load_jobs_csv import set_search_path
from impute_categories import clean_title, is_noisy


# ── Tier 2: category + keyword → skill ────────────────────────────────────
# Each entry: (keywords_in_title, category_match_or_None, skill)
# Evaluated in order; first match wins.
# category_match is checked against the job's category column (None = ignore).

SKILL_RULES: list[tuple[list[str], str | None, str]] = [
    # ── Software Development ──────────────────────────────────────────────
    (['python', 'django', 'flask'],         None,                   'Python Django'),
    (['java', 'spring boot', 'spring'],     None,                   'Java Spring Boot'),
    (['.net', 'c#', 'asp.net'],             None,                   '.NET Development'),
    (['react', 'angular', 'vue', 'frontend', 'front-end', 'front end'],
                                            None,                   'React / Frontend Development'),
    (['node', 'express', 'typescript'],     None,                   'Node.js / TypeScript'),
    (['golang', 'go lang'],                 None,                   'Go Development'),
    (['ruby', 'rails'],                     None,                   'Ruby on Rails'),
    (['mobile', 'ios', 'android', 'swift', 'kotlin'],
                                            None,                   'Mobile Development'),
    (['salesforce', 'sfdc'],                None,                   'Salesforce Development'),
    (['sap', 'abap'],                       None,                   'SAP Development'),
    (['oracle', 'pl/sql', 'plsql'],         None,                   'Oracle / PL-SQL'),
    (['pega'],                              None,                   'Pega BPM'),
    (['servicenow'],                        None,                   'ServiceNow Development'),

    # ── Data & BI ────────────────────────────────────────────────────────
    (['data scientist', 'machine learning', 'ml ', 'ai engineer'],
                                            None,                   'Machine Learning / AI'),
    (['data engineer', 'etl', 'pipeline'],  None,                   'ETL / Data Engineering'),
    (['data analyst', 'bi analyst', 'business intelligence'],
                                            None,                   'Business Intelligence'),
    (['tableau', 'power bi', 'looker'],     None,                   'BI Visualization (Tableau/Power BI)'),
    (['databricks', 'spark', 'hadoop'],     None,                   'Big Data (Spark/Hadoop)'),
    (['snowflake', 'redshift', 'data warehouse'],
                                            None,                   'Cloud Data Warehouse'),
    (['sql server', 'sql developer', 't-sql'],
                                            None,                   'SQL Server / T-SQL'),

    # ── Infrastructure / Cloud / Security ────────────────────────────────
    (['aws ', 'amazon web services'],       None,                   'AWS Cloud'),
    (['azure'],                             None,                   'Microsoft Azure'),
    (['gcp', 'google cloud'],              None,                   'Google Cloud Platform'),
    (['kubernetes', 'k8s'],                None,                   'Kubernetes / Container Orchestration'),
    (['docker', 'container'],              None,                   'Docker / Containers'),
    (['devops', 'devsecops', 'site reliability', 'sre'],
                                            None,                   'DevOps / CI-CD'),
    (['terraform', 'ansible', 'puppet', 'chef'],
                                            None,                   'Infrastructure as Code'),
    (['cisco', 'ccna', 'ccnp', 'routing', 'switching', 'network engineer', 'network infrastructure'],
                                            None,                   'Cisco Routing & Switching'),
    (['firewall', 'fortinet', 'palo alto', 'checkpoint'],
                                            None,                   'Network Security / Firewall'),
    (['cybersecurity', 'security analyst', 'soc analyst', 'siem', 'splunk'],
                                            None,                   'Cybersecurity / SOC'),
    (['linux', 'unix', 'sysadmin', 'system admin'],
                                            None,                   'Linux / Unix Administration'),
    (['windows server', 'active directory', 'exchange'],
                                            None,                   'Windows Server / Active Directory'),
    (['vmware', 'vsphere', 'virtualization'],
                                            None,                   'VMware Virtualization'),
    (['storage', 'san', 'nas', 'netapp'],  None,                   'Storage Administration'),

    # ── Service Desk / Support ───────────────────────────────────────────
    (['itsm', 'ticketing', 'servicenow', 'remedy', 'jira service'],
                                            'Service Desk',         'ITSM Ticketing Systems'),
    (['help desk', 'helpdesk', 'tier 1', 'tier 2', 'tier1', 'tier2',
      'desktop support', 'end user'],       'Service Desk',         'Help Desk Tier 2 Support'),
    (['service desk', 'it support'],        'Service Desk',         'Help Desk Tier 2 Support'),

    # ── Technician ───────────────────────────────────────────────────────
    (['hardware', 'break-fix', 'break fix', 'deskside'],
                                            'Technician',           'Hardware Break-Fix'),
    (['field technician', 'field tech', 'av ', 'audio visual'],
                                            'Technician',           'Field Technician Support'),
    (['lab technician', 'laboratory'],      'Technician',           'Lab Technician'),

    # ── Project / Program Management ─────────────────────────────────────
    (['pmp', 'project manager', 'program manager'],
                                            None,                   'PMP Certification'),
    (['agile', 'scrum master', 'scrum'],   None,                   'Agile / Scrum'),
    (['product owner', 'product manager'], None,                   'Product Management'),
    (['delivery manager'],                 None,                   'Delivery Management'),

    # ── Business Analysis ────────────────────────────────────────────────
    (['requirements', 'business analyst', 'business systems analyst'],
                                            None,                   'Requirements Gathering'),
    (['process improvement', 'process analyst'],
                                            None,                   'Business Process Improvement'),

    # ── Manufacturing / Engineering ───────────────────────────────────────
    (['lean six sigma', 'six sigma', 'lean manufacturing'],
                                            None,                   'Lean Six Sigma'),
    (['manufacturing engineer', 'process engineer'],
                                            None,                   'Manufacturing Process Engineering'),
    (['quality engineer', 'quality assurance', 'qa '],
                                            None,                   'Quality Assurance / QA'),
    (['mechanical engineer'],               None,                   'Mechanical Engineering'),
    (['electrical engineer'],              None,                   'Electrical Engineering'),
    (['civil engineer', 'structural'],     None,                   'Civil / Structural Engineering'),
    (['embedded', 'firmware'],             None,                   'Embedded Systems Development'),
    (['cad ', 'autocad', 'solidworks', 'catia', 'drafter'],
                                            None,                   'CAD / Drafting'),

    # ── Business / Finance / HR ───────────────────────────────────────────
    (['financial analyst', 'finance analyst', 'fp&a'],
                                            None,                   'Financial Analysis'),
    (['accounts payable', 'accounts receivable'],
                                            None,                   'Accounting / AP-AR'),
    (['human resources', 'hr business partner', 'hrbp'],
                                            None,                   'Human Resources'),
    (['recruiter', 'talent acquisition'],   None,                   'Talent Acquisition'),
    (['supply chain', 'procurement', 'sourcing'],
                                            None,                   'Supply Chain / Procurement'),

    # ── Broad category-based fallbacks (evaluated last) ───────────────────
    ([],  'Software Development',           'Software Development'),
    ([],  'Infrastructure',                 'Network / Infrastructure Engineering'),
    ([],  'Service Desk',                   'Help Desk Tier 2 Support'),
    ([],  'Technician',                     'Field Technician Support'),
    ([],  'Project Management',             'PMP Certification'),
    ([],  'Business Analysis',              'Requirements Gathering'),
    ([],  'Data & Business Intelligence',   'Business Intelligence'),
    ([],  'Manufacturing Engineering',      'Manufacturing Process Engineering'),
    ([],  'Quality Engineering',            'Quality Assurance / QA'),
    ([],  'Business Professional',          'Business Process Improvement'),
    ([],  'Artificial Intelligence',        'Machine Learning / AI'),
    ([],  'Research & Development',         'Research Engineering'),
    ([],  'Electrical Engineering',         'Electrical Engineering'),
    ([],  'Civil Engineering',              'Civil / Structural Engineering'),
    ([],  'Embedded Systems',               'Embedded Systems Development'),
    ([],  'Design/Drafting',               'CAD / Drafting'),
    ([],  'Regulatory Affairs',             'Regulatory Compliance'),
    ([],  'A&E',                            'Architecture & Engineering'),
    ([],  'Engineering Admin/Documentation','Technical Writing / Documentation'),
    ([],  'Product Development Engineering','Product Management'),
    ([],  'Emerging Technologies',          'Agile / Scrum'),
]


def keyword_impute_skill(title: str, category: str | None) -> str | None:
    t = title.lower()
    for keywords, cat_match, skill in SKILL_RULES:
        # If the rule has a category constraint, it must match
        if cat_match is not None and category != cat_match:
            # Still allow if keywords match (title is authoritative)
            if not keywords:
                continue
        if keywords and not any(kw in t for kw in keywords):
            continue
        if not keywords and cat_match and category != cat_match:
            continue
        return skill
    return None


# ── DB helpers ────────────────────────────────────────────────────────────

def build_tier1_lookup(conn) -> dict[str, str]:
    """For every clean title that already has required_skill, return the modal skill."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                CASE WHEN job_title LIKE '%% - %%'
                     THEN TRIM(SPLIT_PART(job_title, ' - ', 2))
                     ELSE TRIM(job_title)
                END  AS ctitle,
                required_skill,
                COUNT(*) AS support
            FROM jobs
            WHERE required_skill IS NOT NULL
              AND job_title IS NOT NULL
            GROUP BY 1, 2
        """)
        rows = cur.fetchall()

    best: dict[str, tuple[str, int]] = {}
    for ctitle, skill, support in rows:
        if ctitle and (ctitle not in best or support > best[ctitle][1]):
            best[ctitle] = (skill, support)

    return {k: v[0] for k, v in best.items()}


def fetch_null_skill_jobs(conn) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT job_id, job_title, category
            FROM jobs
            WHERE required_skill IS NULL
              AND job_title IS NOT NULL
        """)
        return cur.fetchall()


def impute_all(jobs: list[dict], tier1: dict[str, str]) -> list[dict]:
    results = []
    for job in jobs:
        ct = clean_title(job['job_title'])

        if is_noisy(ct):
            results.append({
                'job_id': job['job_id'], 'skill': None,
                'tier': 'skip', 'clean_title': ct,
            })
            continue

        if ct in tier1:
            results.append({
                'job_id': job['job_id'], 'skill': tier1[ct],
                'tier': 'T1', 'clean_title': ct,
            })
            continue

        skill = keyword_impute_skill(ct, job.get('category'))
        if skill:
            results.append({
                'job_id': job['job_id'], 'skill': skill,
                'tier': 'T2', 'clean_title': ct,
            })
            continue

        results.append({
            'job_id': job['job_id'], 'skill': None,
            'tier': 'unresolved', 'clean_title': ct,
        })

    return results


def apply_updates(conn, results: list[dict], dry_run: bool) -> int:
    to_update = [
        (r['skill'], r['job_id'])
        for r in results if r['skill'] is not None
    ]
    if not to_update or dry_run:
        return len(to_update)

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE jobs SET required_skill = %s, skill_imputed = TRUE "
            "WHERE job_id = %s",
            to_update,
        )
    return len(to_update)


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description='Impute NULL required_skill values on jobs from title/category matching.'
    )
    p.add_argument('--schema', default='jop',
                   help='Postgres schema (default: jop)')
    p.add_argument('--dsn', default='',
                   help='Postgres DSN (default: PG* env vars)')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what would be updated without writing')
    args = p.parse_args(argv)

    with psycopg.connect(args.dsn or '') as conn:
        set_search_path(conn, args.schema)

        print('Building Tier 1 lookup from existing skilled jobs...')
        tier1 = build_tier1_lookup(conn)
        print(f'  {len(tier1):,} distinct clean titles with known skill\n')

        print('Fetching NULL-skill jobs...')
        jobs = fetch_null_skill_jobs(conn)
        print(f'  {len(jobs):,} jobs to process\n')

        results = impute_all(jobs, tier1)

        by_tier = Counter(r['tier'] for r in results)
        by_skill = Counter(r['skill'] for r in results if r['skill'])

        print('  ── Imputation summary ──────────────────────────────────')
        print(f'  Tier 1 (title match) : {by_tier["T1"]:>7,}')
        print(f'  Tier 2 (keyword rule): {by_tier["T2"]:>7,}')
        print(f'  Skipped (noisy title): {by_tier["skip"]:>7,}')
        print(f'  Unresolved           : {by_tier["unresolved"]:>7,}')
        print(f'  ────────────────────────────────────────────────────────')
        print(f'  Total to update      : {by_tier["T1"] + by_tier["T2"]:>7,}')

        print('\n  ── Skill distribution of imputations ───────────────────')
        for skill, cnt in by_skill.most_common(20):
            bar = '█' * (cnt // 50)
            print(f'  {skill:<50} {cnt:>6,}  {bar}')

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
