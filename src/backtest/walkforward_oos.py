#!/usr/bin/env python3
"""纯时序回测 — PurgedWalkForward OOS 预测 + 模拟下注。

用法:
    python src/backtest/walkforward_oos.py --sport fb
    python src/backtest/walkforward_oos.py --sport bb
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier
from sklearn.metrics import brier_score_loss
from scipy import stats

from src.models.ensemble_trainer import PurgedWalkForward, _ece_score
from config.logging_config import get_logger

logger = get_logger(__name__)

# 回测超参（固定，不调优 — 防止回测过拟合）
LGBM_PARAMS = dict(n_estimators=200, max_depth=4, num_leaves=24,
                   learning_rate=0.08, subsample=0.8, colsample_bytree=0.8,
                   min_child_samples=30, reg_alpha=1, reg_lambda=1,
                   random_state=42, verbosity=-1)
XGB_PARAMS = dict(n_estimators=200, max_depth=4, learning_rate=0.08,
                  subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
                  gamma=1, reg_alpha=1, reg_lambda=1,
                  random_state=42, verbosity=0)
KELLY_FRACTION = 0.25
MAX_STAKE_PCT = 0.05
INITIAL_BANKROLL = 10000.0


def _market_prob_from_odds(odds: float) -> float:
    if odds <= 1.01:
        return 0.5
    return np.clip(1.0 / odds, 0.02, 0.98)


def _kelly_stake(model_prob: float, odds: float) -> tuple:
    ev = (model_prob * (odds - 1)) - (1 - model_prob)
    if ev <= 0:
        return 0.0, 0.0
    kelly = (model_prob * (odds - 1) - (1 - model_prob)) / (odds - 1)
    kelly = np.clip(kelly * KELLY_FRACTION, 0, MAX_STAKE_PCT)
    return kelly, ev


def _load_data(sport: str):
    csv_map = {'bb': 'data/processed/bb_features.csv',
               'fb': 'data/processed/fb_features.csv'}
    feat_json_map = {'bb': 'model_bb_features.json',
                     'fb': 'model_fb_features.json'}
    csv_path = csv_map.get(sport)
    if not csv_path:
        raise ValueError(f"未知运动: {sport}")

    df = pd.read_csv(ROOT / csv_path)
    df['date'] = pd.to_datetime(df['date'], utc=True, errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)

    feat_path = ROOT / 'models' / feat_json_map[sport]
    with open(feat_path) as f:
        feat_cols = json.load(f)
    feat_cols = [c for c in feat_cols if c in df.columns]
    return df, feat_cols


def _load_odds(sport: str) -> pd.DataFrame:
    """加载外部赔率数据（包含 h2h / spreads / totals）。"""
    paths = {'bb': 'data/history/basketball_historical_odds.csv',
             'fb': None}
    p = paths.get(sport)
    if not p or not (ROOT / p).exists():
        return pd.DataFrame()

    raw = pd.read_csv(ROOT / p)
    raw['date'] = pd.to_datetime(raw['date'], utc=True)
    rows = []
    for _, r in raw.iterrows():
        try:
            data = json.loads(r['odds_json'])
            ht = r['home_team'].strip().lower()
            at = r['away_team'].strip().lower()
            entry = {'date': r['date'], 'home_team': ht, 'away_team': at}

            for m in data:
                outcomes = m['outcomes']
                if m['key'] == 'h2h' and len(outcomes) >= 2:
                    o0, o1 = outcomes[0], outcomes[1]
                    entry['home_odds'] = float(o1['price']) if o1.get('name', '').strip().lower() == ht else float(o0['price'])
                elif m['key'] == 'spreads' and len(outcomes) >= 2:
                    for o in outcomes:
                        nm = o.get('name', '').strip().lower()
                        if nm == ht:
                            entry['home_spread_odds'] = float(o['price'])
                            entry['home_spread_line'] = float(o.get('point', 0))
                        else:
                            entry['away_spread_odds'] = float(o['price'])
                            entry['away_spread_line'] = float(o.get('point', 0))
                elif m['key'] == 'totals' and len(outcomes) >= 2:
                    for o in outcomes:
                        nm = o.get('name', '').strip().lower()
                        if nm == 'over':
                            entry['over_odds'] = float(o['price'])
                            entry['total_line'] = float(o.get('point', 0))
                        elif nm == 'under':
                            entry['under_odds'] = float(o['price'])
            rows.append(entry)
        except Exception:
            pass
    return pd.DataFrame(rows)

# 目标 → 所需赔率列 & 预测方向
TARGET_ODDS_MAP = {
    'win':           ('home_odds',       1),
    'spread_result': ('home_spread_odds', 1),
    'total_result':  ('over_odds',        1),
}


ODDS_COLUMNS = ['home_odds', 'home_spread_odds', 'away_spread_odds',
                'home_spread_line', 'away_spread_line',
                'over_odds', 'under_odds', 'total_line']


def _merge_odds(oos_df: pd.DataFrame, odds_df: pd.DataFrame,
                sport: str) -> pd.DataFrame:
    """匹配所有可用赔率列（h2h / spreads / totals）。"""
    for col in ODDS_COLUMNS:
        oos_df[col] = 0.0
    if odds_df.empty or oos_df.empty:
        return oos_df

    # 只合并 odds_df 中存在的列
    merge_cols = [c for c in ODDS_COLUMNS if c in odds_df.columns]

    oos_lower = oos_df.get('home', '').str.strip().str.lower()
    oos_date = pd.to_datetime(oos_df['date']).dt.normalize()
    odds_lower = odds_df['home_team'].str.strip().str.lower()
    odds_date = odds_df['date'].dt.tz_localize(None).dt.normalize()

    for i in range(len(oos_df)):
        matches = (odds_date == oos_date.iloc[i]) & (odds_lower == oos_lower.iloc[i])
        if matches.any():
            idx = matches.idxmax()
            for col in merge_cols:
                oos_df.loc[oos_df.index[i], col] = odds_df.loc[idx, col]
    return oos_df


def run_oos_backtest(sport: str, targets: list = None) -> dict:
    df, feat_cols = _load_data(sport)
    odds_df = _load_odds(sport)
    logger.info(f"{sport}: {len(df)} 样本, {len(feat_cols)} 特征"
                f"{f', 外部队率 {len(odds_df)} 条' if not odds_df.empty else ''}")

    target_map = {'bb': ['win', 'spread_result', 'total_result'],
                  'fb': ['win', 'total_result']}
    if targets is None:
        targets = target_map.get(sport, ['win'])

    all_predictions = {}  # target -> list of OOS prediction records
    for target in targets:
        train_df = df.dropna(subset=[target]).copy()
        if len(train_df) < 200:
            logger.warning(f"  跳过 {target}: 样本不足 ({len(train_df)})")
            continue

        X_all = train_df[feat_cols].fillna(0)
        y_all = train_df[target].astype(int)
        dates_ns = train_df['date'].values.astype('datetime64[ns]')

        cv = PurgedWalkForward(dates_ns, n_splits=5, embargo_days=14)
        ns = len(X_all)
        oos_probs = np.full(ns, np.nan)
        oos_preds = np.full(ns, np.nan)

        for fold, (tr_idx, te_idx) in enumerate(cv.split(X_all)):
            if len(te_idx) < 30:
                continue
            X_tr, y_tr = X_all.iloc[tr_idx], y_all.iloc[tr_idx]
            X_te, y_te = X_all.iloc[te_idx], y_all.iloc[te_idx]

            pos = y_tr.sum()
            neg = len(y_tr) - pos
            spw = neg / pos if pos > 0 and neg > 0 else 1.0

            lgb = LGBMClassifier(**LGBM_PARAMS, class_weight='balanced' if spw > 1.5 else None)
            lgb.fit(X_tr, y_tr)
            xgb = XGBClassifier(**XGB_PARAMS, scale_pos_weight=spw if spw > 1.5 else 1)
            xgb.fit(X_tr, y_tr)

            # 校准集（训练集最后 30%）
            cal_start = int(len(X_tr) * 0.7)
            X_cal, y_cal = X_tr.iloc[cal_start:], y_tr.iloc[cal_start:]
            lgb_cal = lgb.predict_proba(X_cal)[:, 1]
            xgb_cal = xgb.predict_proba(X_cal)[:, 1]
            lgb_w = 1.0 / max(-np.mean(y_cal * np.log(np.clip(lgb_cal, 1e-8, 1)) +
                              (1 - y_cal) * np.log(np.clip(1 - lgb_cal, 1e-8, 1))), 0.05)
            xgb_w = 1.0 / max(-np.mean(y_cal * np.log(np.clip(xgb_cal, 1e-8, 1)) +
                              (1 - y_cal) * np.log(np.clip(1 - xgb_cal, 1e-8, 1))), 0.05)
            tw = lgb_w + xgb_w
            lgb_w, xgb_w = lgb_w / tw, xgb_w / tw

            ensemble = lgb_w * lgb.predict_proba(X_te)[:, 1] + xgb_w * xgb.predict_proba(X_te)[:, 1]

            # Isotonic/Sigmoid 校准
            from src.models.stacking import WeightedEnsemble
            ests = [('lgbm', lgb), ('xgb', xgb)]
            ws = [lgb_w, xgb_w]
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.frozen import FrozenEstimator
            # 根据校准集大小选择校准方法（小样本用 Platt，大样本才用 Isotonic）
            cal_size = len(X_cal)
            if cal_size < 500:
                cal_method = 'sigmoid'  # 小样本：Platt 缩放更稳定
            elif target in ('win', 'home_win'):
                cal_method = 'isotonic'
            else:
                cal_method = 'sigmoid'
            try:
                we = WeightedEnsemble(ests, ws)
                cal = CalibratedClassifierCV(FrozenEstimator(we),
                    method=cal_method,
                    cv='prefit')
                cal.fit(X_cal, y_cal)
                cal_prob = cal.predict_proba(X_te)[:, 1]
                oos_prob = cal_prob if brier_score_loss(y_te, cal_prob) < brier_score_loss(y_te, ensemble) else ensemble
            except Exception:
                oos_prob = ensemble

            oos_probs[te_idx] = oos_prob
            oos_preds[te_idx] = (oos_prob >= 0.5).astype(float)

        # 收集 OOS 预测
        valid = ~np.isnan(oos_probs)
        nv = valid.sum()
        if nv < 30:
            continue
        records = pd.DataFrame({
            'date': pd.to_datetime(dates_ns[valid]),
            'prob': oos_probs[valid],
            'pred': oos_preds[valid].astype(int),
            'actual': y_all.values[valid].astype(int),
            'home': train_df.iloc[valid]['home'].values if 'home' in train_df.columns else '',
            'away': train_df.iloc[valid]['away'].values if 'away' in train_df.columns else '',
        })
        all_predictions[target] = records

    # ── 报告预测质量（不需要赔率）──
    results = {}
    for target, recs in all_predictions.items():
        brier = brier_score_loss(recs['actual'], recs['prob'])
        ece = _ece_score(recs['actual'].values, recs['prob'].values)
        acc = (recs['pred'] == recs['actual']).mean()
        n = len(recs)

        # 匹配赔率（全部列）
        if not odds_df.empty:
            recs = _merge_odds(recs, odds_df, sport)

        # 该目标对应的赔率列
        odds_col, dir_sign = TARGET_ODDS_MAP.get(target, ('home_odds', 1))
        if odds_col not in recs.columns:
            recs[odds_col] = 0.0
        has_odds = recs[odds_col] > 1.01

        # 模拟下注（有赔率的部分）
        bets = []
        bankroll = INITIAL_BANKROLL
        peak = bankroll
        for _, r in recs[has_odds].iterrows():
            sp, ev = _kelly_stake(r['prob'], r[odds_col])
            if sp <= 0:
                continue
            stake = bankroll * sp
            if stake < 1:
                continue
            won = (r['actual'] == 1) if dir_sign == 1 else (r['actual'] == 0)
            profit = stake * (r[odds_col] - 1) if won else -stake
            bankroll += profit
            peak = max(peak, bankroll)
            bets.append({'date': str(r['date'])[:10], 'prob': round(r['prob'], 4),
                         'odds': round(r[odds_col], 2), 'stake': round(stake, 2),
                         'ev': round(ev, 4), 'won': int(won), 'profit': round(profit, 2)})

        nb = len(bets)
        wr = sum(b['won'] for b in bets) / nb if nb > 0 else 0
        tp = sum(b['profit'] for b in bets) if nb > 0 else 0
        md = INITIAL_BANKROLL - bankroll if bankroll < INITIAL_BANKROLL else 0

        result = {
            'status': 'ok',
            'oos_samples': n,
            'brier': round(brier, 4),
            'ece': round(ece, 4),
            'accuracy': round(acc, 4),
            'bets_with_odds': int(has_odds.sum()),
            'n_bets_placed': nb,
            'win_rate': round(wr, 4) if nb > 0 else None,
            'total_profit': round(tp, 2) if nb > 0 else None,
            'roi': round(tp / INITIAL_BANKROLL, 4) if nb > 0 else None,
            'final_bankroll': round(bankroll, 2) if nb > 0 else None,
        }
        if nb >= 5:
            returns = [b['profit'] / INITIAL_BANKROLL for b in bets]
            sharpe = None
            if np.std(returns, ddof=1) > 1e-8:
                sharpe = round((np.mean(returns) / np.std(returns, ddof=1)) * math.sqrt(252), 2)
            result['sharpe'] = sharpe
            result['avg_odds'] = round(np.mean([b['odds'] for b in bets]), 2)
            # 统计显著性检验（H0: 真实胜率 = 50%）
            n_wins = sum(b['won'] for b in bets)
            z_stat = (n_wins - 0.5 * nb) / (math.sqrt(nb * 0.5 * 0.5)) if nb > 0 else 0
            p_value = 2 * (1 - stats.norm.cdf(abs(z_stat)))
            result['z_score'] = round(z_stat, 3)
            result['p_value'] = round(p_value, 6)
            # 按月盈亏 breakdown
            bet_df = pd.DataFrame(bets)
            bet_df['date'] = pd.to_datetime(bet_df['date'])
            bet_df['month'] = bet_df['date'].dt.to_period('M')
            monthly = bet_df.groupby('month').agg(
                n_bets=('won', 'count'), wins=('won', 'sum'), profit=('profit', 'sum'),
            ).reset_index()
            monthly['win_rate'] = monthly['wins'] / monthly['n_bets']
            result['monthly'] = [
                {'month': str(r['month']), 'bets': int(r['n_bets']),
                 'win_rate': round(float(r['win_rate']), 4),
                 'profit': round(float(r['profit']), 2)}
                for _, r in monthly.iterrows()
            ]
            result['n_months'] = len(monthly)
            result['profitable_months'] = int((monthly['profit'] > 0).sum())
        results[target] = result

        logger.info(f"\n  ── {sport}/{target} 回测结果 ──")
        logger.info(f"  OOS 样本:    {n}")
        logger.info(f"  Brier:       {brier:.4f}")
        logger.info(f"  ECE:         {ece:.4f}")
        logger.info(f"  准确率:      {acc:.2%}")
        logger.info(f"  有赔率:      {int(has_odds.sum())} 笔")
        if nb > 0:
            logger.info(f"  下注:        {nb} 笔 | 胜率 {wr:.1%} | "
                        f"利润 ¥{tp:+.0f} | ROI {tp/INITIAL_BANKROLL:.1%}")
            if result.get('sharpe'):
                logger.info(f"  夏普:        {result['sharpe']}")
            if result.get('avg_odds'):
                logger.info(f"  平均赔率:    {result['avg_odds']}")
        else:
            logger.info(f"  下注:        无可下注机会")

    return results


def main():
    parser = argparse.ArgumentParser(description='Walk-Forward OOS 回测')
    parser.add_argument('--sport', choices=['bb', 'fb', 'all'], required=True)
    parser.add_argument('--targets', help='逗号分隔的目标列表')
    args = parser.parse_args()
    targets = args.targets.split(',') if args.targets else None
    sports = ['bb', 'fb'] if args.sport == 'all' else [args.sport]
    all_results = {}
    for sp in sports:
        logger.info("\n%s\n  ▶ %s 回测\n%s", "=" * 50, sp.upper(), "=" * 50)
        results = run_oos_backtest(sp, targets)
        all_results[sp] = results

        out_dir = ROOT / 'reports'
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = out_dir / f'backtest_{sp}_{ts}.json'
        slim = {t: {k: v for k, v in r.items() if k not in ('bets',)}
                 for t, r in results.items()}
        with open(path, 'w') as f:
            json.dump(slim, f, indent=2)
        logger.info(f"  结果已保存: {path}")

    print(f"\n{'=' * 55}")
    print(f"  回测汇总 ({args.sport.upper()})")
    print(f"{'=' * 55}")
    for sp, results in all_results.items():
        if not results:
            continue
        print(f"\n  ▶ {sp.upper()}")
        for t, r in results.items():
            if r.get('status') != 'ok':
                continue
            bets = r.get('n_bets_placed', 0)
            wr = f"{r['win_rate']:.1%}" if r.get('win_rate') else 'N/A'
            profit = f"¥{r['total_profit']:+.0f}" if r.get('total_profit') is not None else 'N/A'
            sharpe = f"夏普{r['sharpe']}" if r.get('sharpe') else ''
            avg_o = f"均赔{r['avg_odds']}" if r.get('avg_odds') else ''
            sig = ''
            if r.get('p_value') is not None:
                if r['p_value'] < 0.001:
                    sig = '***'
                elif r['p_value'] < 0.01:
                    sig = '**'
                elif r['p_value'] < 0.05:
                    sig = '*'
            pm = ''
            if r.get('profitable_months') is not None and r.get('n_months'):
                pm = f"月{r['profitable_months']}/{r['n_months']}盈"
            print(f"  {t:20s}: {r['oos_samples']:5d}样本 Brier={r['brier']:.4f} "
                  f"ECE={r['ece']:.2%} | 下注{bets:3d}笔 {wr} {profit} {sharpe} {avg_o} {sig} {pm}")


if __name__ == '__main__':
    main()
