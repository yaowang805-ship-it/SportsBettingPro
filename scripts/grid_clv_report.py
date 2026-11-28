#!/usr/bin/env python3
"""格子级 CLV 报告 — 运动×盘口×方向×概率桶 四维(铁律: 释放/撤回分析禁止粗化到盘口方向级)。

职业团队做法(2026-11-16 查证): CLV 是 process metric(不需结算, 50 注出信号); 必须分段到
概率桶(favorite-longshot bias 概率依赖, 整盘口平均会掩盖方向相反的分化); 去水公平价口径;
阈值 +1~3%边际 / +3~5%专业 / >5%顶级。

用法:
    .venv312/bin/python scripts/grid_clv_report.py              # 早盘(默认)
    .venv312/bin/python scripts/grid_clv_report.py --source live    # 滚球
    .venv312/bin/python scripts/grid_clv_report.py --days 30        # 时间窗(默认 11-11 起)
"""
import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.odds_interval import prob_bucket

SPORT_MAP = {1: 'football', 2: 'ice_hockey', 3: 'basketball', 5: 'tennis',
             7: 'baseball', 6: 'american_football', 13: 'volleyball'}
LIVE_SRC = ('drop', 'poll', 'betfair_ws', 'pin')
PREM_SRC = ('ws_scan', 'pin_drop', 'early')


def _ns(s):
    return SPORT_MAP.get(int(s), str(s)) if (s is not None and (isinstance(s, int) or str(s).isdigit())) else str(s)


def _nsub(s):
    return {'handicap': 'hc', 'over_under': 'ou', 'opportunities': '1x2',
            'double_chance': 'dc', 'handicap_games': 'hc_games'}.get(s, s or '?')


def _dire(desig, sub):
    d = (desig or "")
    if sub in ('dc', 'double_chance'):
        if '主' in d and '和' in d:
            return '1X'
        if '主' in d and '客' in d:
            return '12'
        if '和' in d and '客' in d:
            return 'X2'
        return '其他'
    if '大' in d or 'over' in d.lower():
        return '大'
    if '小' in d or 'under' in d.lower():
        return '小'
    if ('和' in d or '平' in d or 'draw' in d.lower()) and '客' not in d and '主' not in d:
        return '平'
    if '客' in d or 'away' in d.lower():
        return '客'
    if '主' in d or 'home' in d.lower():
        return '主'
    return '其他'


def _verdict(avg, n):
    if n < 3:
        return '⚪样本少'
    if avg > 5:
        return '🟢🟢顶级'
    if avg > 3:
        return '🟢专业'
    if avg > 1:
        return '🟢边际'
    if avg > -1:
        return '⚪中性'
    if avg > -3:
        return '🔴负'
    return '🔴🔴强负'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['early', 'live'], default='early')
    ap.add_argument('--days', type=int, default=0, help='只看最近 N 天(0=从 11-11 起)')
    ap.add_argument('--min_n', type=int, default=0)
    args = ap.parse_args()

    if args.days > 0:
        cutoff = time.time() - args.days * 86400
    else:
        cutoff = datetime(2026, 11, 11, 0, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
    src_set = LIVE_SRC if args.source == 'live' else PREM_SRC

    clv = defaultdict(lambda: {'n': 0, 'sum': 0.0})
    f = ROOT / 'data' / 'storage' / 'clv_results.csv'
    for r in csv.DictReader(open(f)):
        if r.get('source', '') not in src_set:
            continue
        try:
            ts = time.mktime(time.strptime(r.get('push_time', '')[:19], '%Y-%m-%dT%H:%M:%S'))
        except Exception:
            continue
        if ts < cutoff:
            continue
        s = _ns(r.get('sport'))
        m = _nsub(r.get('sub_market'))
        d = _dire(r.get('designation'), r.get('sub_market'))
        fp = r.get('push_fair_price')
        try:
            fp = float(fp) if fp else None
        except Exception:
            fp = None
        pb = prob_bucket(1.0 / fp, s) if fp and fp > 1 else '?'
        try:
            tcv = float(r.get('true_clv_pct') or 0)
        except Exception:
            tcv = 0.0
        clv[(s, m, d, pb)]['n'] += 1
        clv[(s, m, d, pb)]['sum'] += tcv

    rows = [(s, m, d, pb, v['n'], v['sum'] / v['n']) for (s, m, d, pb), v in clv.items() if v['n'] >= args.min_n]
    rows.sort(key=lambda x: -x[5])

    label = '早盘' if args.source == 'early' else '滚球'
    print(f'{label}观察库 CLV 格子级(运动×盘口×方向×概率桶, {args.days}天) — 职业阈值 +1~3%边际/+3~5%专业/>5%顶级')
    print(f'{"运动":<11}{"盘口":<5}{"方向":<4}{"概率桶":<9}{"n":>5}{"CLV%":>8}  判定')
    for s, m, d, pb, n, avg in rows:
        print(f'{s:<11}{m:<5}{d:<4}{pb:<9}{n:>5}{avg:>8.1f}  {_verdict(avg, n)}')


if __name__ == '__main__':
    main()
