#!/usr/bin/env python3
"""足球 Line Shopping 回测 — Pinnacle 基准 vs 零售最佳赔率。

核心理念：Pinnacle 是 sharpest 博彩公司，其去 vig 概率 ≈ 真实概率。
零售博彩公司（Bet365、WH 等）提供 softer 赔率。
当零售赔率隐含概率 < Pinnacle 概率时，存在 +EV 机会。

这是职业博彩的经典"Positive CLV"策略。
"""
import sys, logging
from pathlib import Path
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('fb_ls_backtest')

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd


# 零售博彩公司赔率列名（football-data.co.uk 格式）
RETAIL_BOOKS = {
    'B365': ('B365H', 'B365D', 'B365A'),
    'BW':   ('BWH', 'BWD', 'BWA'),
    'IW':   ('IWH', 'IWD', 'IWA'),
    'LB':   ('LBH', 'LBD', 'LBA'),
    'WH':   ('WHH', 'WHD', 'WHA'),
    'SJ':   ('SJH', 'SJD', 'SJA'),
    'VC':   ('VCH', 'VCD', 'VCA'),
}


def _remove_vig(h_odds, d_odds, a_odds):
    imp_h, imp_d, imp_a = 1.0/h_odds, 1.0/d_odds, 1.0/a_odds
    vig = imp_h + imp_d + imp_a - 1.0
    if vig <= 0:
        return imp_h, imp_d, imp_a, 0.0
    return imp_h/(1+vig), imp_d/(1+vig), imp_a/(1+vig), vig


def _best_retail_odds(row):
    """返回所有零售博彩公司中每个结果的最佳赔率和对应概率。"""
    best = {}
    for outcome, cols in [('H', 0), ('D', 1), ('A', 2)]:
        best_odds = 0.0
        best_implied = 1.0  # 最低隐含概率
        for book, (hc, dc, ac) in RETAIL_BOOKS.items():
            cols_map = {'H': hc, 'D': dc, 'A': ac}
            col = cols_map[outcome]
            odds = row.get(col, 0)
            if pd.isna(odds) or odds <= 1:
                continue
            implied = 1.0 / odds
            if implied < best_implied:
                best_implied = implied
                best_odds = odds
        best[outcome] = {'odds': best_odds, 'implied': best_implied if best_odds > 0 else None}
    return best


def main():
    logger.info('=' * 60)
    logger.info('FB Line Shopping 回测（Pinnacle 基准 vs 零售最佳）')
    logger.info('=' * 60)

    # ── 1. 加载 ──
    df = pd.read_csv('data/raw/fb_odds_raw.csv', low_memory=False)

    # 筛选有 Pinnacle 赔率的
    df = df[df['PSH'].notna() & (df['PSH'] != '')].copy()

    # 数值化所有赔率列
    odds_cols = ['PSH', 'PSD', 'PSA', 'FTHG', 'FTAG']
    for cols in RETAIL_BOOKS.values():
        odds_cols.extend(cols)
    for c in odds_cols:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    # 至少一个零售博彩有完整赔率
    retail_valid = pd.Series(False, index=df.index)
    for hc, dc, ac in RETAIL_BOOKS.values():
        retail_valid |= df[hc].notna() & df[dc].notna() & df[ac].notna()
    df = df[retail_valid & df['PSH'].notna() & df['PSD'].notna() & df['PSA'].notna()].copy()

    df['date'] = pd.to_datetime(df['Date'], dayfirst=True, errors='coerce')
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)

    logger.info(f'加载: {len(df)} 行 ({df["date"].min().date()} ~ {df["date"].max().date()})')
    logger.info(f'联赛: {df["_league"].nunique()} 个')

    # 标签
    df['home_win'] = (df['FTHG'] > df['FTAG']).astype(int)
    df['draw'] = (df['FTHG'] == df['FTAG']).astype(int)
    df['away_win'] = (df['FTHG'] < df['FTAG']).astype(int)

    # ── 2. 计算每场比赛的 +EV 机会 ──
    logger.info('计算零售 vs Pinnacle 价差...')
    bets = []

    for idx, r in df.iterrows():
        # Pinnacle 去 vig 概率 = 基准真实概率
        pinny_prob_h, pinny_prob_d, pinny_prob_a, vig = _remove_vig(r['PSH'], r['PSD'], r['PSA'])

        # 零售最佳赔率
        best = _best_retail_odds(r)

        # 检查每个结果
        for outcome, pinny_prob in [('H', pinny_prob_h), ('D', pinny_prob_d), ('A', pinny_prob_a)]:
            retail = best[outcome]
            if retail['odds'] is None:
                continue

            # EV = 零售赔率隐含概率 vs Pinnacle真实概率
            # 如果 Pinnacle 概率 > 零售赔率隐含概率 → 零售赔率被高估 → +EV
            retail_implied = 1.0 / retail['odds']
            ev = (pinny_prob - retail_implied) / retail_implied  # % edge over retail

            if ev > 0:
                # Kelly
                kelly = (pinny_prob * retail['odds'] - 1) / (retail['odds'] - 1) * 0.25
                kelly = min(kelly, 0.05)  # cap at 5%
                if kelly <= 0:
                    continue

                actual_win = r['home_win'] if outcome == 'H' else (r['draw'] if outcome == 'D' else r['away_win'])

                bets.append({
                    'date': r['date'],
                    'home': r['HomeTeam'],
                    'away': r['AwayTeam'],
                    'league': r['_league'],
                    'outcome': outcome,
                    'pinny_prob': pinny_prob,
                    'retail_odds': retail['odds'],
                    'retail_implied': retail_implied,
                    'edge_pct': ev * 100,
                    'kelly': kelly,
                    'won': actual_win,
                    'pnl': kelly * (retail['odds'] - 1) if actual_win else -kelly,
                })

    bets = pd.DataFrame(bets)
    bets = bets.sort_values('date').reset_index(drop=True)

    logger.info(f'\n投注统计:')
    logger.info(f'  总比赛: {len(df)}, 总下注: {len(bets)} ({len(bets)/len(df)*100:.1f}%)')
    logger.info(f'  每比赛: {len(bets)/len(df):.2f} 注/场')

    if len(bets) == 0:
        logger.info('  无下注 — 没找到 +EV 机会')
        return df, bets

    won = bets['won'].sum()
    total_pnl = bets['pnl'].sum()
    roi = total_pnl / len(bets) * 100

    logger.info(f'  胜: {won}/{len(bets)} ({won/len(bets)*100:.1f}%)')
    logger.info(f'  总利润: {total_pnl:.4f} (单位: 1本金)')
    logger.info(f'  ROI: {roi:.2f}%')
    logger.info(f'  平均零售赔率: {bets["retail_odds"].mean():.2f}')
    logger.info(f'  平均 Edge: {bets["edge_pct"].mean():.2f}%  vs Pinnacle')
    logger.info(f'  平均 Kelly: {bets["kelly"].mean()*100:.2f}%')
    logger.info(f'  平均 Pinnacle 概率: {bets["pinny_prob"].mean()*100:.2f}%')

    # 按 outcome 拆分
    for oc in ['H', 'D', 'A']:
        sub = bets[bets['outcome'] == oc]
        if len(sub) >= 5:
            sr = sub['won'].sum() / len(sub)
            logger.info(f'  {oc}: {len(sub)} 注, 胜率 {sr*100:.1f}%, ROI {sub["pnl"].sum()/len(sub)*100:.2f}%')

    # 按 Edge 阈值
    logger.info(f'\nEdge 阈值分析:')
    for thresh in [0, 2, 5, 10, 20]:
        sub = bets[bets['edge_pct'] >= thresh]
        if len(sub) >= 5:
            sr = sub['won'].sum() / len(sub)
            logger.info(f'  edge>={thresh}%: {len(sub)} 注, 胜率 {sr*100:.1f}%, ROI {sub["pnl"].sum()/len(sub)*100:.2f}%')

    # 回撤
    equity = (1 + bets['pnl'].cumsum()).values
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = drawdown.min()
    logger.info(f'\n  最大回撤: {max_dd*100:.2f}%')
    sr_val = bets['pnl']
    sharpe = np.sqrt(365) * sr_val.mean() / sr_val.std() if sr_val.std() > 0 else 0
    logger.info(f'  夏普比率(年化): {sharpe:.2f}')

    # 连败
    cur = mx = 0
    for w in bets['won']:
        cur = cur + 1 if w == 0 else 0
        mx = max(mx, cur)
    logger.info(f'  最大连败: {mx}')

    # 月度
    bets['month'] = bets['date'].dt.to_period('M')
    monthly = bets.groupby('month')['pnl'].sum()
    logger.info(f'\n  月度利润:')
    for m, pnl in monthly.items():
        logger.info(f'    {m}: {pnl:+.4f}')

    return df, bets


if __name__ == '__main__':
    main()
