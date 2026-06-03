#!/usr/bin/env python3
"""自动匹配已结束比赛并回写 `performance_history.csv` 的工具。
尝试从 `data/storage/finished_games_*.csv` 中匹配 pending 推荐并回写结果。
"""
from pathlib import Path
import pandas as pd
import numpy as np
import re
from config.settings import DATA_DIR, DEFAULT_BUDGET
import unicodedata
import json

# 可选的本地别名映射（优先）
ALIAS_FILE = DATA_DIR / 'team_aliases.json'
_ALIAS_MAP = {}
if ALIAS_FILE.exists():
    try:
        _ALIAS_MAP = json.loads(ALIAS_FILE.read_text(encoding='utf-8'))
    except Exception:
        _ALIAS_MAP = {}

PERF_FILE = DATA_DIR / "performance_history.csv"


def find_finished_files():
    return sorted(DATA_DIR.glob('finished_games_*.csv'))


def guess_score_columns(df: pd.DataFrame):
    """尝试找出主客队得分列名（返回 home_col, away_col）"""
    cols = [c.lower() for c in df.columns]
    # 常见列名关键词
    home_keys = ['home_score', 'home_points', 'home_pts', 'h_score', 'homefinal', 'home_final', 'home']
    away_keys = ['away_score', 'away_points', 'away_pts', 'a_score', 'awayfinal', 'away_final', 'away']
    home_col = None
    away_col = None
    for c in df.columns:
        lc = c.lower()
        for k in home_keys:
            if k in lc and home_col is None:
                home_col = c
        for k in away_keys:
            if k in lc and away_col is None:
                away_col = c
    return home_col, away_col


def match_pending_from_files():
    """扫描所有 finished_games 文件，尝试更新 PERF_FILE 中的 pending 记录。"""
    if not PERF_FILE.exists():
        print(f"❌ 未找到 {PERF_FILE}")
        return 0

    perf = pd.read_csv(PERF_FILE)
    pending = perf[perf['result'] == 'pending']
    if pending.empty:
        print("✅ 无待结算记录")
        return 0

    files = find_finished_files()
    if not files:
        print("⚠️ 未发现 finished_games_*.csv 文件")
        return 0

    updated = 0

    for f in files:
        try:
            finished = pd.read_csv(f)
        except Exception:
            continue

        # 规范列名
        finished_cols = {c: c for c in finished.columns}
        home_col, away_col = guess_score_columns(finished)

        # 标准化球队列名候选
        team_cols = [c for c in finished.columns if 'home' in c.lower() or 'away' in c.lower()]

        for idx, row in pending.iterrows():
            game = str(row.get('game', '')).strip()
            if not game:
                continue
            # 解析主客队
            if ' vs ' in game:
                a_home, a_away = [s.strip() for s in game.split(' vs ', 1)]
            else:
                parts = re.split(' - | vs | v ', game)
                if len(parts) >= 2:
                    a_home, a_away = parts[0].strip(), parts[1].strip()
                else:
                    continue

            # 标准化队名
            def normalize(n: str) -> str:
                if not n:
                    return ''
                # 删去括号及内容
                n = re.sub(r"\(.*?\)", "", n)
                # Unicode 归一化
                n = unicodedata.normalize('NFKD', n)
                n = ''.join(ch for ch in n if not unicodedata.combining(ch))
                n = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", ' ', n).strip().lower()
                # alias map
                return _ALIAS_MAP.get(n, n)

            a_home_n = normalize(a_home)
            a_away_n = normalize(a_away)

            # 在 finished 中寻找匹配行
            matched_rows = []
            for i, g in finished.iterrows():
                row_home = None
                row_away = None
                # 尝试从常见列名获取球队
                for c in finished.columns:
                    lc = c.lower()
                    if ('home' in lc and 'team' in lc) or ('home' in lc and 'name' in lc):
                        row_home = str(g[c]).strip()
                    if ('away' in lc and 'team' in lc) or ('away' in lc and 'name' in lc):
                        row_away = str(g[c]).strip()
                # 退化：尝试任何包含主队/客队名称的列
                if not row_home or not row_away:
                    for c in finished.columns:
                        val = str(g[c])
                        if a_home_n in normalize(val) and not row_home:
                            row_home = val.strip()
                        if a_away_n in normalize(val) and not row_away:
                            row_away = val.strip()

                if not row_home or not row_away:
                    continue

                if (a_home_n in normalize(row_home) and a_away_n in normalize(row_away)) or \
                   (a_home_n in normalize(row_away) and a_away_n in normalize(row_home)):
                    matched_rows.append((i, g))

            if not matched_rows:
                continue

            # 取第一个匹配，判断结果
            i, gm = matched_rows[0]
            # 尝试读取分数
            home_score = None
            away_score = None
            if home_col and away_col and home_col in gm and away_col in gm:
                try:
                    home_score = float(gm[home_col])
                    away_score = float(gm[away_col])
                except Exception:
                    home_score = None
                    away_score = None

            # 若能得分则判断胜负
            if home_score is not None and away_score is not None:
                # 确定哪个列映射到 a_home/a_away
                # 判断前先找出 gm 中的队名列
                winner = None
                # 检查是否 a_home 对应 finished 的 home
                # 先尝试从 gm 中简单匹配
                # 如果 home_score > away_score 则 home 赢
                if home_score > away_score:
                    winner = 'home'
                elif away_score > home_score:
                    winner = 'away'
                else:
                    winner = 'draw'

                # 判断推荐是否赢
                bet_desc = str(row.get('bet', ''))
                stake = float(row.get('stake', 0) or 0)
                odds = float(row.get('odds', 1) or 1)

                # 简单判定：如果 bet_desc 包含主队名则认为押主队
                bet_on = None
                if a_home.lower() in bet_desc.lower():
                    bet_on = 'home'
                elif a_away.lower() in bet_desc.lower():
                    bet_on = 'away'

                if winner == 'draw':
                    result = 'push'
                    profit = 0.0
                elif bet_on is None:
                    # 无法判断投注方向，跳过
                    continue
                else:
                    if bet_on == winner:
                        result = 'won'
                        profit = stake * (odds - 1.0)
                    else:
                        result = 'lost'
                        profit = -stake

                perf.at[idx, 'result'] = result
                perf.at[idx, 'profit'] = profit
                updated += 1

    if updated > 0:
        # 重新计算累计
        cumulative = DEFAULT_BUDGET
        for i, r in perf.iterrows():
            if r['result'] in ['won', 'lost']:
                cumulative += float(r.get('profit', 0) or 0)
            perf.at[i, 'cumulative_balance'] = cumulative
        perf.to_csv(PERF_FILE, index=False)
        print(f"🔁 自动匹配并回写 {updated} 条记录到 {PERF_FILE}")
    else:
        print("🔍 未匹配到新的已结束比赛")

    return updated


if __name__ == '__main__':
    match_pending_from_files()
