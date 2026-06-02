"""
train_weights.py — Learn empirical feature weights from historical outcomes
using logistic regression, then compare against the current heuristic weights.

Supports a time-based train / validation split to prevent look-ahead bias:
  - Training window  : jobs posted up to --train-end
  - Validation window: jobs posted between --val-start and --val-end
  - Feature lookups  : scoped to --feature-version-id (training period only)
    so December fill-rate information never leaks into the training features.

Workflow:
  1. Pull terminal jobs in the training window using the specified feature
     lookup version (not the global active one) to avoid leakage.
  2. Impute any remaining NULLs with the global base fill rate (0.085).
  3. Train logistic regression with class-weight balancing (handles class skew).
  4. Normalise coefficients → weights that sum to 1.0.
  5. If --val-start / --val-end provided: score the validation window jobs
     using the same feature version, report AUC + tier fill rates on that set.
  6. Report: learned weights vs heuristic, AUC / PR-AUC, tier shift simulation.
  7. Optionally write the new blended weights to model_versions (--save).

Usage:
    # Full dataset (no date split)
    python train_weights.py --schema rpotential --dsn '...'

    # Time-based train / validation split (client-specified windows)
    python train_weights.py \\
        --train-end    2025-11-30 \\
        --val-start    2025-12-01 --val-end 2025-12-31 \\
        --feature-version-id 2 \\
        --schema rpotential --dsn '...'

    # Save blended weights to model_versions
    python train_weights.py --train-end 2025-11-30 --save --schema rpotential --dsn '...'
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score, average_precision_score
    from sklearn.preprocessing import StandardScaler
except ImportError:
    sys.exit(
        "error: scikit-learn and numpy are required.\n"
        "Install with: pip install scikit-learn numpy"
    )

from load_jobs_csv import set_search_path


FEATURES = [
    'company_fill_rate',
    'category_fill_rate',
    'openings_weight',
    'job_type_weight',
    'client_type_fill_rate',
    'hour_bin_weight',
]

HEURISTIC_WEIGHTS = {
    'company_fill_rate':    0.45,
    'category_fill_rate':   0.20,
    'openings_weight':      0.15,
    'job_type_weight':      0.10,
    'client_type_fill_rate':0.05,
    'hour_bin_weight':      0.05,
}

GLOBAL_BASE_FILL_RATE = 0.0850
TIER_THRESHOLDS = {'T1': 0.40, 'T2': 0.15}


# ── Data ──────────────────────────────────────────────────────────────────

def get_active_feature_version(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM feature_lookup_version WHERE is_active LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        sys.exit("error: no active feature_lookup_version found.")
    return row[0]


def fetch_window_data(conn, version_id: int,
                      start_date: date | None = None,
                      end_date: date | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    Fetch terminal jobs within the given date window, joining feature lookups
    directly against version_id to avoid using the active-version view.
    This prevents look-ahead bias when evaluating on a held-out window.

    start_date inclusive, end_date inclusive. None = unbounded.
    """
    date_clauses = []
    if start_date:
        date_clauses.append(f"AND j.date_added >= '{start_date}'")
    if end_date:
        next_day = end_date + timedelta(days=1)
        date_clauses.append(f"AND j.date_added < '{next_day}'")
    date_filter = ' '.join(date_clauses)

    sql = f"""
        SELECT
            COALESCE(cfr.smoothed_fill_rate,  {GLOBAL_BASE_FILL_RATE}) AS company_fill_rate,
            COALESCE(catfr.fill_rate,         {GLOBAL_BASE_FILL_RATE}) AS category_fill_rate,
            COALESCE(ow.fill_rate_weight,     {GLOBAL_BASE_FILL_RATE}) AS openings_weight,
            COALESCE(jt.fill_rate_weight,     {GLOBAL_BASE_FILL_RATE}) AS job_type_weight,
            COALESCE(ctfr.fill_rate,          {GLOBAL_BASE_FILL_RATE}) AS client_type_fill_rate,
            COALESCE(hb.fill_rate_weight,     {GLOBAL_BASE_FILL_RATE}) AS hour_bin_weight,
            j.is_placed::int                                            AS label
        FROM jobs j
        JOIN job_status_ref s ON s.status_code = j.status
        LEFT JOIN company_fill_rate_lookup cfr
               ON cfr.version_id = %(vid)s AND cfr.company_id = j.company_id
        LEFT JOIN category_fill_rate_lookup catfr
               ON catfr.version_id = %(vid)s AND catfr.category = j.category
        LEFT JOIN client_type_fill_rate_lookup ctfr
               ON ctfr.version_id = %(vid)s AND ctfr.client_type = j.client_type
        LEFT JOIN openings_bin_weights ow ON ow.openings_bin =
            CASE WHEN j.num_openings=0 THEN '0' WHEN j.num_openings=1 THEN '1'
                 WHEN j.num_openings=2 THEN '2' WHEN j.num_openings=3 THEN '3'
                 WHEN j.num_openings<=5 THEN '4-5' WHEN j.num_openings<=10 THEN '6-10'
                 ELSE '11+' END
        LEFT JOIN job_type_ref jt ON jt.job_type = j.job_type
        LEFT JOIN hour_bin_weights hb ON hb.hour_bin =
            CASE WHEN EXTRACT(HOUR FROM j.date_added) < 8  THEN 'overnight'
                 WHEN EXTRACT(HOUR FROM j.date_added) < 12 THEN 'morning'
                 WHEN EXTRACT(HOUR FROM j.date_added) < 16 THEN 'midday'
                 WHEN EXTRACT(HOUR FROM j.date_added) < 18 THEN 'afternoon'
                 ELSE 'evening' END
        WHERE s.is_terminal = TRUE {date_filter}
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, {'vid': version_id})
        rows = cur.fetchall()

    X = np.array([[float(r[f]) for f in FEATURES] for r in rows])
    y = np.array([r['label'] for r in rows])
    return X, y


# ── Model ─────────────────────────────────────────────────────────────────

def train(X: np.ndarray, y: np.ndarray):
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.20, random_state=42, stratify=y
    )

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s  = scaler.transform(X_test)

    model = LogisticRegression(
        class_weight='balanced',   # handles 9:1 class imbalance
        max_iter=1000,
        random_state=42,
    )
    model.fit(X_train_s, y_train)

    y_prob = model.predict_proba(X_test_s)[:, 1]
    auc    = roc_auc_score(y_test, y_prob)
    pr_auc = average_precision_score(y_test, y_prob)

    return model, scaler, auc, pr_auc, X_test, y_test, y_prob


def normalise_weights(model) -> dict[str, float]:
    """
    Derive feature importance from the logistic regression coefficients on
    the *standardised* scale.  Using |coef| on the standardised scale tells
    us: "how much does a 1-sigma change in each feature shift the log-odds?"
    This is the correct per-feature importance measure regardless of the
    original value ranges (avoids amplifying narrow-range features like
    hour_bin when dividing back to original units).
    """
    coefs = model.coef_[0]
    importance = np.abs(coefs)
    importance = np.clip(importance, 0, None)
    total = importance.sum()
    if total == 0:
        return HEURISTIC_WEIGHTS.copy()
    return {f: float(round(importance[i] / total, 4)) for i, f in enumerate(FEATURES)}


# ── Simulation ────────────────────────────────────────────────────────────

def simulate_tiers_prob(probs: np.ndarray, y: np.ndarray,
                        t1_thresh: float, t2_thresh: float) -> dict[str, dict]:
    tiers = np.where(probs >= t1_thresh, 'T1',
            np.where(probs >= t2_thresh, 'T2', 'T3'))
    result = {}
    for tier in ('T1', 'T2', 'T3'):
        mask   = tiers == tier
        n      = mask.sum()
        placed = y[mask].sum()
        result[tier] = {
            'count':     int(n),
            'pct':       round(n / len(y) * 100, 1),
            'placed':    int(placed),
            'fill_rate': round(float(placed / n) if n else 0, 4),
        }
    return result


def simulate_tiers(X: np.ndarray, y: np.ndarray,
                   weights: dict[str, float]) -> dict[str, dict]:
    w = np.array([weights[f] for f in FEATURES])
    scores = X @ w

    tiers = np.where(scores >= TIER_THRESHOLDS['T1'], 'T1',
            np.where(scores >= TIER_THRESHOLDS['T2'], 'T2', 'T3'))

    result = {}
    for tier in ('T1', 'T2', 'T3'):
        mask = tiers == tier
        n    = mask.sum()
        placed = y[mask].sum()
        result[tier] = {
            'count':      int(n),
            'pct':        round(n / len(y) * 100, 1),
            'placed':     int(placed),
            'fill_rate':  round(float(placed / n) if n else 0, 4),
        }
    return result


# ── Reporting ─────────────────────────────────────────────────────────────

def print_weights_table(learned: dict[str, float], blended: dict[str, float]) -> None:
    LABELS = {
        'company_fill_rate':    'Company fill rate',
        'category_fill_rate':   'Category fill rate',
        'openings_weight':      'Openings',
        'job_type_weight':      'Job type',
        'client_type_fill_rate':'Client type fill rate',
        'hour_bin_weight':      'Hour bin',
    }
    print(f"\n  {'Feature':<28} {'Heuristic':>10} {'Learned':>10} {'Blended':>10} {'Delta':>8}")
    print(f"  {'-'*28} {'-'*10} {'-'*10} {'-'*10} {'-'*8}")
    for f in FEATURES:
        h = HEURISTIC_WEIGHTS[f]
        l = learned[f]
        b = blended[f]
        d = l - h
        sign = '+' if d >= 0 else ''
        print(f"  {LABELS[f]:<28} {h:>10.4f} {l:>10.4f} {b:>10.4f} {sign}{d:>7.4f}")


def print_tier_table(label: str, sim: dict) -> None:
    print(f"\n  {label}")
    print(f"  {'Tier':<6} {'Jobs':>8} {'%':>6} {'Placed':>8} {'Fill Rate':>10}")
    print(f"  {'-'*6} {'-'*8} {'-'*6} {'-'*8} {'-'*10}")
    for tier in ('T1', 'T2', 'T3'):
        r = sim[tier]
        print(f"  {tier:<6} {r['count']:>8,} {r['pct']:>5.1f}% "
              f"{r['placed']:>8,} {r['fill_rate']:>10.4f}")


# ── Save ──────────────────────────────────────────────────────────────────

def save_model_version(conn, blended: dict, auc: float, pr_auc: float,
                       n_rows: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rpotential.model_versions SET is_active = FALSE WHERE is_active"
        )
        cur.execute("""
            INSERT INTO rpotential.model_versions
                (model_version, feature_version_id, training_rows,
                 auc, pr_auc, notes, is_active,
                 w_company, w_category, w_openings,
                 w_job_type, w_client_type, w_hour_bin)
            SELECT 'logistic_v1',
                   version_id, %s, %s, %s,
                   'Blended weights (60%% heuristic + 40%% logistic regression).',
                   TRUE,
                   %s, %s, %s, %s, %s, %s
            FROM rpotential.feature_lookup_version WHERE is_active
            ON CONFLICT (model_version) DO UPDATE SET
                training_rows  = EXCLUDED.training_rows,
                auc            = EXCLUDED.auc,
                pr_auc         = EXCLUDED.pr_auc,
                notes          = EXCLUDED.notes,
                is_active      = EXCLUDED.is_active,
                w_company      = EXCLUDED.w_company,
                w_category     = EXCLUDED.w_category,
                w_openings     = EXCLUDED.w_openings,
                w_job_type     = EXCLUDED.w_job_type,
                w_client_type  = EXCLUDED.w_client_type,
                w_hour_bin     = EXCLUDED.w_hour_bin
            RETURNING model_version
        """, (
            n_rows, round(auc, 4), round(pr_auc, 4),
            blended['company_fill_rate'],
            blended['category_fill_rate'],
            blended['openings_weight'],
            blended['job_type_weight'],
            blended['client_type_fill_rate'],
            blended['hour_bin_weight'],
        ))
        row = cur.fetchone()
        return row[0] if row else None


# ── CLI ───────────────────────────────────────────────────────────────────

def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description='Learn feature weights from historical placement outcomes.'
    )
    p.add_argument('--schema', default='rpotential')
    p.add_argument('--dsn',    default='')
    p.add_argument('--save',   action='store_true',
                   help='Write the blended model to model_versions table')
    p.add_argument('--train-end', type=parse_date, default=None,
                   help='Last date (inclusive) of the training window '
                        '(e.g. 2025-11-30). Omit to use all terminal jobs.')
    p.add_argument('--val-start', type=parse_date, default=None,
                   help='First date (inclusive) of the validation window '
                        '(e.g. 2025-12-01).')
    p.add_argument('--val-end', type=parse_date, default=None,
                   help='Last date (inclusive) of the validation window '
                        '(e.g. 2025-12-31).')
    p.add_argument('--feature-version-id', type=int, default=None,
                   help='feature_lookup_version.version_id to use for feature '
                        'values. Defaults to the currently active version.')
    args = p.parse_args(argv)

    if (args.val_start or args.val_end) and not (args.val_start and args.val_end):
        p.error('--val-start and --val-end must be provided together.')

    with psycopg.connect(args.dsn or '') as conn:
        set_search_path(conn, args.schema)

        # Resolve feature lookup version
        fv_id = args.feature_version_id or get_active_feature_version(conn)
        print(f'Feature lookup version : {fv_id}')
        if args.train_end:
            print(f'Training window        : up to {args.train_end}')
        else:
            print(f'Training window        : all terminal jobs')
        if args.val_start:
            print(f'Validation window      : {args.val_start} → {args.val_end}')
        print()

        # ── Fetch training data ───────────────────────────────────────────
        print('Fetching training data...')
        X, y = fetch_window_data(conn, fv_id, end_date=args.train_end)
        print(f'  {len(y):,} terminal jobs  |  '
              f'{y.sum():,} placed ({y.mean()*100:.1f}%)\n')

        # ── Train ─────────────────────────────────────────────────────────
        print('Training logistic regression (80/20 internal split, class-balanced)...')
        model, scaler, train_auc, train_pr_auc, _, _, _ = train(X, y)
        print(f'  Train AUC-ROC : {train_auc:.4f}')
        print(f'  Train PR-AUC  : {train_pr_auc:.4f}\n')

        learned = normalise_weights(model)

        blended = {
            f: round(0.60 * HEURISTIC_WEIGHTS[f] + 0.40 * learned[f], 4)
            for f in FEATURES
        }
        blend_total = sum(blended.values())
        blended = {f: round(v / blend_total, 4) for f, v in blended.items()}

        print('── Learned weights vs heuristic ────────────────────────────')
        print_weights_table(learned, blended)

        # ── Simulate tiers on training set ────────────────────────────────
        sim_heuristic = simulate_tiers(X, y, HEURISTIC_WEIGHTS)
        sim_blended   = simulate_tiers(X, y, blended)
        sim_learned   = simulate_tiers(X, y, learned)

        y_prob_full = model.predict_proba(scaler.transform(X))[:, 1]
        t1_thresh = float(np.percentile(y_prob_full, 100 * (1 - 0.007)))
        t2_thresh = float(np.percentile(y_prob_full, 100 * (1 - 0.135)))
        sim_prob = simulate_tiers_prob(y_prob_full, y, t1_thresh, t2_thresh)

        print('\n── Tier distribution — training set ────────────────────────')
        print_tier_table('Heuristic weights (current)',              sim_heuristic)
        print_tier_table('Blended weights (60% heuristic + 40% LR)', sim_blended)
        print_tier_table('Learned weights (logistic regression)',     sim_learned)
        print_tier_table(
            f'Probability-based (T1≥{t1_thresh:.3f} / T2≥{t2_thresh:.3f})', sim_prob,
        )

        print(f"\n── T1 fill-rate — training set ─────────────────────────────")
        for label, sim in [
            ('Heuristic',   sim_heuristic),
            ('Blended',     sim_blended),
            ('Learned',     sim_learned),
            ('Probability', sim_prob),
        ]:
            fr  = sim['T1']['fill_rate']
            bar = '█' * int(fr * 20)
            print(f"  {label:<12} {fr:.4f}  {bar}")

        # ── Validation set ────────────────────────────────────────────────
        if args.val_start and args.val_end:
            print(f'\n── Validation set  ({args.val_start} → {args.val_end}) ──────────')
            X_val, y_val = fetch_window_data(
                conn, fv_id, start_date=args.val_start, end_date=args.val_end
            )
            print(f'  {len(y_val):,} terminal jobs  |  '
                  f'{y_val.sum():,} placed ({y_val.mean()*100:.1f}%)\n')

            X_val_s  = scaler.transform(X_val)
            y_val_prob = model.predict_proba(X_val_s)[:, 1]
            val_auc    = roc_auc_score(y_val, y_val_prob)
            val_pr_auc = average_precision_score(y_val, y_val_prob)

            print(f'  Train AUC-ROC : {train_auc:.4f}   Val AUC-ROC : {val_auc:.4f}'
                  f'   Gap : {abs(train_auc - val_auc):.4f}'
                  f'{"  ✓ no overfit" if abs(train_auc - val_auc) < 0.05 else "  ⚠ possible overfit"}')
            print(f'  Train PR-AUC  : {train_pr_auc:.4f}   Val PR-AUC  : {val_pr_auc:.4f}')

            vsim_heuristic = simulate_tiers(X_val, y_val, HEURISTIC_WEIGHTS)
            vsim_blended   = simulate_tiers(X_val, y_val, blended)
            vsim_learned   = simulate_tiers(X_val, y_val, learned)
            vsim_prob      = simulate_tiers_prob(y_val_prob, y_val, t1_thresh, t2_thresh)

            print('\n── Tier distribution — validation set ──────────────────────')
            print_tier_table('Heuristic weights',                        vsim_heuristic)
            print_tier_table('Blended weights (60% heuristic + 40% LR)', vsim_blended)
            print_tier_table('Learned weights',                           vsim_learned)
            print_tier_table(
                f'Probability-based (T1≥{t1_thresh:.3f} / T2≥{t2_thresh:.3f})', vsim_prob,
            )

            print(f"\n── T1 fill-rate — validation set ───────────────────────────")
            for label, sim in [
                ('Heuristic',   vsim_heuristic),
                ('Blended',     vsim_blended),
                ('Learned',     vsim_learned),
                ('Probability', vsim_prob),
            ]:
                fr  = sim['T1']['fill_rate']
                bar = '█' * int(fr * 20)
                print(f"  {label:<12} {fr:.4f}  {bar}")

        # ── Save ──────────────────────────────────────────────────────────
        if args.save:
            save_model_version(conn, blended, train_auc, train_pr_auc, len(y))
            conn.commit()
            print('\nModel version saved to model_versions (is_active = TRUE).')
        else:
            print('\n[INFO] Pass --save to persist this model to model_versions.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
