#!/usr/bin/env python3
"""掩护注(mug bets) — 防风控: 每天随机下几注主流联赛小额注, 装散户。

原理(职业团队防 gubbing): 软书靠「持续跑赢 CLV + 只下冷门 + 追线 + 注额规律」识别 sharp,
混入少量主流联赛的随意小注能让账户像普通玩家, 换账户寿命。牺牲一点 EV 换不被限注
(gubbing 是最大威胁, 见 memory/mission-stable-profit)。

用法: .venv312/bin/python scripts/mug_bets.py [--max-n 2] [--stake-min 20] [--stake-max 50]
定时: launchd com.sportsbettingpro.mug_bets (每天 20:00)。
"""
import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.pinnacle_live import fetch_bb_live_matches
from src.betting.bb_auto_bet import place_single_bet

# 掩护注只下主流盘口(1x2/让球/大小球), 不下冷门特殊盘口
_MUG_SUBS = ("1x2", "hc", "ou")


def run(max_n=2, stake_min=20, stake_max=50):
    bb = fetch_bb_live_matches(platform="BB")
    if not bb:
        print("[mug] 无比赛, 跳过")
        return
    # 收集所有有市场的主流盘口候选
    candidates = []
    for bmid, b in bb.items():
        for mk in b.get("markets", []):
            if mk.get("sub") in _MUG_SUBS and mk.get("market_id") and mk.get("odds"):
                candidates.append((bmid, b, mk))
    if not candidates:
        print("[mug] 无可下注市场, 跳过")
        return
    random.shuffle(candidates)
    placed = 0
    for bmid, b, mk in candidates[:max_n]:
        # 随机注额 20-50(取整到 10), 不是 Kelly 的固定档 → 装散户
        stake = random.randint(stake_min // 10, stake_max // 10) * 10
        # check_limit=False + match_id=None: 掩护注不进 stake limit 记录, 不占 +EV 限额
        code, order_id, msg = place_single_bet(
            mk["market_id"], mk["odds"], mk["option_type"], stake=stake,
            check_limit=False, verify_price=False)
        print(f"[mug] {b.get('home_en','')} vs {b.get('away_en','')} "
              f"{mk.get('sub')}/{mk.get('direction')} ¥{stake} → code={code} {msg}")
        if code == 0:
            placed += 1
    print(f"[mug] 掩护注完成: {placed}/{max_n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="掩护注(防风控, 装散户)")
    ap.add_argument("--max-n", type=int, default=2, help="最多下几注(默认2)")
    ap.add_argument("--stake-min", type=int, default=20)
    ap.add_argument("--stake-max", type=int, default=50)
    args = ap.parse_args()
    run(args.max_n, args.stake_min, args.stake_max)
