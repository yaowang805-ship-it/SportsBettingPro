#!/usr/bin/env python3
"""世界杯蒙特卡洛模拟 — 小组出线 + 夺冠概率。

用法:
    python src/predict/wc_simulation.py           # 完整模拟
    python src/predict/wc_simulation.py --iters 50000  # 更多模拟次数
"""
import sys
import json
import random
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR
from fetchers.odds_api import fetch_odds_api

# ── 48 支参赛队中文名 ──
_CN = {
    "Algeria": "阿尔及利亚", "Argentina": "阿根廷", "Australia": "澳大利亚",
    "Austria": "奥地利", "Belgium": "比利时", "Bosnia & Herzegovina": "波黑",
    "Brazil": "巴西", "Canada": "加拿大", "Cape Verde": "佛得角",
    "Colombia": "哥伦比亚", "Croatia": "克罗地亚", "Curaçao": "库拉索",
    "Czech Republic": "捷克", "DR Congo": "刚果金", "Ecuador": "厄瓜多尔",
    "Egypt": "埃及", "England": "英格兰", "France": "法国",
    "Germany": "德国", "Ghana": "加纳", "Haiti": "海地",
    "Iran": "伊朗", "Iraq": "伊拉克", "Ivory Coast": "科特迪瓦",
    "Japan": "日本", "Jordan": "约旦", "Mexico": "墨西哥",
    "Morocco": "摩洛哥", "Netherlands": "荷兰", "New Zealand": "新西兰",
    "Norway": "挪威", "Panama": "巴拿马", "Paraguay": "巴拉圭",
    "Portugal": "葡萄牙", "Qatar": "卡塔尔", "Saudi Arabia": "沙特",
    "Scotland": "苏格兰", "Senegal": "塞内加尔", "South Africa": "南非",
    "South Korea": "韩国", "Spain": "西班牙", "Sweden": "瑞典",
    "Switzerland": "瑞士", "Tunisia": "突尼斯", "Turkey": "土耳其",
    "USA": "美国", "Uruguay": "乌拉圭", "Uzbekistan": "乌兹别克斯坦",
}


def _build_groups(matches: list) -> dict:
    """从比赛数据构建小组（每组4队，每队打3场）。"""
    opponents = defaultdict(set)
    for g in matches:
        opponents[g["home_team"]].add(g["away_team"])
        opponents[g["away_team"]].add(g["home_team"])

    # Union-find 分 group
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    def union(a, b):
        parent[find(a)] = find(b)

    for team, opps in opponents.items():
        for opp in opps:
            union(team, opp)

    groups = defaultdict(list)
    for team in opponents:
        groups[find(team)].append(team)

    return {i: sorted(members) for i, (_, members) in enumerate(sorted(groups.items()))}


def _match_key(home: str, away: str) -> str:
    return f"{home} vs {away}"


def _simulate_match(home_win_prob: float, draw_prob: float = None) -> str:
    """模拟一场比赛，返回结果。"""
    if draw_prob is None:
        draw_prob = 0.25  # fallback
    r = random.random()
    if r < home_win_prob:
        return "home"
    elif r < home_win_prob + draw_prob:
        return "draw"
    else:
        return "away"


def _group_standings(matches, model_probs: dict, market_probs: dict) -> dict:
    """模拟小组赛，返回小组排名。"""
    points = defaultdict(int)
    gd = defaultdict(int)  # goal difference
    gs = defaultdict(int)  # goals scored

    for m in matches:
        h, a = m["home_team"], m["away_team"]
        mk = _match_key(h, a)
        mp = model_probs.get(mk, {})
        hp = mp.get("home_win", 0.4)

        # Get draw probability from market
        m3 = market_probs.get(mk, {})
        dp = m3.get("draw", 0.25)
        result = _simulate_match(hp, dp)

        # Random goals (Poisson-ish, centered on match expectation)
        hg = max(0, int(np.random.poisson(mp.get("lambda_home", 1.5))))
        ag = max(0, int(np.random.poisson(mp.get("lambda_away", 1.2))))

        # Override goals for consistency with result
        if result == "home" and hg <= ag:
            hg, ag = max(1, hg), max(0, ag - 1)
            if hg <= ag:
                hg = ag + 1
        elif result == "away" and ag <= hg:
            ag, hg = max(1, ag), max(0, hg - 1)
            if ag <= hg:
                ag = hg + 1
        elif result == "draw" and hg != ag:
            hg = ag = max(1, (hg + ag) // 2)

        if result == "home":
            points[h] += 3
        elif result == "away":
            points[a] += 3
        else:
            points[h] += 1
            points[a] += 1

        gd[h] += hg - ag
        gd[a] += ag - hg
        gs[h] += hg
        gs[a] += ag

    # 排序：积分 → 净胜球 → 进球数
    standings = sorted(points.keys(), key=lambda t: (-points[t], -gd[t], -gs[t]))
    return standings, points, gd


def _knockout_winner(team1: str, team2: str, model_probs: dict, market_probs: dict) -> str:
    """模拟一场淘汰赛（平局进加时/点球，近似处理）。"""
    mk12 = _match_key(team1, team2)
    mk21 = _match_key(team2, team1)

    # 找合适的对阵方向
    mp = model_probs.get(mk12, model_probs.get(mk21, {}))
    hp = mp.get("home_win", 0.4)
    m3 = market_probs.get(mk12, market_probs.get(mk21, {}))
    dp = m3.get("draw", 0.25)

    # 中立场，home_win 优势减半
    hp = 0.5 - (1 - hp) * 0.5 if _match_key(team2, team1) in model_probs else hp

    r = random.random()
    if r < hp:
        return team1 if mk12 in model_probs else team2
    elif r < hp + dp * 0.6:  # 60% 的平局最终由点球决出
        return team1 if random.random() < 0.5 else team2  # 点球 50/50
    else:
        return team2 if mk12 in model_probs else team1


def simulate_tournament(
    groups: dict,
    model_probs: dict,
    market_probs: dict,
    n_simulations: int = 10000,
) -> dict:
    """蒙特卡洛模拟完整世界杯。"""
    group_advance = defaultdict(int)
    champion = defaultdict(int)
    semi = defaultdict(int)
    quarter = defaultdict(int)
    round16 = defaultdict(int)

    group_names = list(groups.keys())
    all_teams = set()
    for members in groups.values():
        all_teams.update(members)

    for sim in range(n_simulations):
        # 小组赛
        group_winners = {}  # group_id -> [1st, 2nd, 3rd, 4th]
        group_third = []
        for gid in group_names:
            members = groups[gid]
            matches = []
            for i in range(4):
                for j in range(i + 1, 4):
                    matches.append({"home_team": members[i], "away_team": members[j]})
            standings, points, gd = _group_standings(matches, model_probs, market_probs)
            group_winners[gid] = standings
            group_third.append((gid, standings[2], points[standings[2]], gd[standings[2]]))

        # 选出8个成绩最好的小组第三
        group_third.sort(key=lambda x: (-x[2], -x[3]))

        # 晋级队伍：每组前2名（共24队）+ 8个成绩最好的小组第三
        advanced = []
        for gid in group_names:
            for i, team in enumerate(group_winners[gid][:2]):
                advanced.append(team)
                group_advance[team] += 1
        for gid, team, _, _ in group_third[:8]:
            if team not in advanced:
                advanced.append(team)
                group_advance[team] += 1

        # ── 淘汰赛 ──
        # 32强阵容：12个小组第一 + 12个小组第二 + 8个小组第三
        first_placed = [group_winners[gid][0] for gid in group_names]
        second_placed = [group_winners[gid][1] for gid in group_names]
        third_pool = [t for _, t, _, _ in group_third[:8]]

        # 种子：小组第一为1-12号种子，按成绩排序
        # 简化版对阵：小组第一 vs 小组第三，小组第二 vs 小组第二
        remaining = []
        for i in range(8):  # 前8个小组第一 vs 8个小组第三
            if i < len(first_placed) and i < len(third_pool):
                w = _knockout_winner(first_placed[i], third_pool[i], model_probs, market_probs)
                remaining.append(w)

        for i in range(4):  # 剩下4个小组第一 vs 小组第二（交叉）
            if i + 8 < len(first_placed) and i < len(second_placed):
                w = _knockout_winner(first_placed[i + 8], second_placed[i], model_probs, market_probs)
                remaining.append(w)

        for i in range(4, 12):  # 剩下8个小组第二 vs 小组第二
            if i < len(second_placed):
                j = i + 4 if i + 4 < len(second_placed) else i
                if j != i and j < len(second_placed) and i < len(second_placed):
                    w = _knockout_winner(second_placed[i], second_placed[j], model_probs, market_probs)
                    remaining.append(w)

        # Round of 16
        for t in remaining:
            round16[t] += 1

        # 16强 → 8强 → 4强 → 决赛
        rd_stages = [("round16", None), ("quarter", quarter), ("semi", semi), ("champion", champion)]
        for stage_name, stage_counter in rd_stages:
            if len(remaining) <= 1:
                break
            next_round = []
            n = len(remaining)
            # 种子配对：第1 vs 最后
            for i in range(n // 2):
                t1 = remaining[i]
                t2 = remaining[n - 1 - i]
                w = _knockout_winner(t1, t2, model_probs, market_probs)
                next_round.append(w)
                if stage_counter is not None:
                    stage_counter[w] += 1
            remaining = next_round

    # Aggregate results
    cn = _CN
    results = []
    for team in sorted(all_teams, key=lambda t: -champion.get(t, 0)):
        results.append({
            "team": team,
            "team_cn": cn.get(team, team),
            "advance_pct": round(group_advance.get(team, 0) / n_simulations * 100, 1),
            "champion_pct": round(champion.get(team, 0) / n_simulations * 100, 1),
            "semi_pct": round(semi.get(team, 0) / n_simulations * 100, 1),
            "quarter_pct": round(quarter.get(team, 0) / n_simulations * 100, 1),
            "round16_pct": round(round16.get(team, 0) / n_simulations * 100, 1),
        })

    return {
        "n_simulations": n_simulations,
        "results": results,
        "generated_at": datetime.now().isoformat(),
    }


def main(n_simulations: int = 10000):
    logger.info("=" * 60)
    logger.info("🌍 世界杯蒙特卡洛模拟 - %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # 加载模型
    import joblib

    model_dir = Path(__file__).resolve().parent.parent.parent / "models"
    home_win_path = model_dir / "model_wc_home_win_ensemble.pkl"

    if not home_win_path.exists():
        logger.error("❌ WC 模型未找到: %s", home_win_path)
        return

    home_model = joblib.load(home_win_path)
    logger.info("✅ WC 主胜模型已加载")

    # 拉取赔率
    odds_data = fetch_odds_api("soccer_fifa_world_cup", force=True, markets="h2h,totals")
    logger.info("✅ 赔率: %d 场", len(odds_data))

    # 构建小组
    groups = _build_groups(odds_data)
    logger.info("✅ 小组: %d 组", len(groups))
    for gid, members in groups.items():
        logger.info("  Group %d: %s", gid + 1, ", ".join(_CN.get(t, t) for t in members))

    # 构建特征 + 模型预测
    from src.predict.daily_wc import _build_feature_matrix
    X, match_keys = _build_feature_matrix(odds_data)

    model_probs = {}
    market_probs = {}
    for i, mk in enumerate(match_keys):
        home, away = mk.split(" @ ")
        feats = X.iloc[i:i+1]

        # Model prediction
        hp = float(home_model.predict_proba(feats)[0, 1])
        hp = float(np.clip(hp, 0.02, 0.98))

        # Market 3-way
        game = next(g for g in odds_data if g["home_team"] == home and g["away_team"] == away)
        for m in game.get("markets", []):
            if m.get("key") == "h2h":
                outcomes = {o["name"]: o["price"] for o in m.get("outcomes", [])}
                h_odds = outcomes.get(home, 0)
                d_odds = outcomes.get("Draw", 0)
                a_odds = outcomes.get(away, 0)
                if h_odds > 0 and d_odds > 0 and a_odds > 0:
                    total_imp = 1/h_odds + 1/d_odds + 1/a_odds
                    market_probs[_match_key(home, away)] = {
                        "home": (1/h_odds) / total_imp,
                        "draw": (1/d_odds) / total_imp,
                        "away": (1/a_odds) / total_imp,
                    }
                break

        # Goals expectation (from over market)
        lambda_h, lambda_a = 1.5, 1.2
        for m in game.get("markets", []):
            if m.get("key") == "totals":
                for o in m.get("outcomes", []):
                    if o.get("name") == "Over" and o.get("point") == 2.5:
                        total_odds = o["price"]
                        over_prob = 1.0 / total_odds
                        lambda_total = np.log(1 / (1 - over_prob)) if over_prob < 1 else 3.0
                        lambda_h = lambda_total * 0.55
                        lambda_a = lambda_total * 0.45
                        break
                break

        model_probs[_match_key(home, away)] = {
            "home_win": hp,
            "lambda_home": lambda_h,
            "lambda_away": lambda_a,
        }

    logger.info("✅ 模型预测: %d 场比赛", len(match_keys))

    # 模拟
    logger.info("🔄 蒙特卡洛模拟: %d 次...", n_simulations)
    result = simulate_tournament(groups, model_probs, market_probs, n_simulations)

    # 输出
    print("\n" + "=" * 70)
    print(f"  世界杯蒙特卡洛模拟结果 ({n_simulations:,} 次)")
    print("=" * 70)
    print(f"  {'排名':<4} {'球队':<12} {'出线':<8} {'16强':<8} {'8强':<8} {'4强':<8} {'夺冠':<8}")
    print(f"  {'-'*56}")
    for i, r in enumerate(result["results"][:20], 1):
        print(f"  {i:<4} {r['team_cn']:<12} {r['advance_pct']:<7}% {r['round16_pct']:<7}% "
              f"{r['quarter_pct']:<7}% {r['semi_pct']:<7}% {r['champion_pct']:<7}%")

    # 夺冠热门 Top 5
    top5 = sorted(result["results"], key=lambda x: -x["champion_pct"])[:5]
    print("\n  🏆 夺冠热门:")
    for i, r in enumerate(top5, 1):
        print(f"    {i}. {r['team_cn']} — {r['champion_pct']}%")

    # 小组出线热门 Top 5
    top_adv = sorted(result["results"], key=lambda x: -x["advance_pct"])[:5]
    print("\n  📈 出线热门:")
    for i, r in enumerate(top_adv, 1):
        print(f"    {i}. {r['team_cn']} — {r['advance_pct']}%")

    # 保存
    out_path = DATA_DIR / "wc_simulation_results.json"
    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("✅ 模拟结果已保存至 %s", out_path)

    # 钉钉通知
    try:
        from src.notify.dingtalk import get_notifier
        notifier = get_notifier()

        lines = [f"🌍 世界杯模拟 ({n_simulations:,} 次)\n"]
        lines.append("🏆 夺冠概率\n")
        for i, r in enumerate(top5, 1):
            lines.append(f"  {i}. {r['team_cn']} {r['champion_pct']}%")
        lines.append("\n📈 出线概率 Top5\n")
        for i, r in enumerate(top_adv, 1):
            lines.append(f"  {i}. {r['team_cn']} {r['advance_pct']}%")

        msg = notifier.build_markdown_message("🌍 世界杯模拟", "\n".join(lines))
        notifier.send(msg, "世界杯模拟结果")
        logger.info("✅ 已推送至钉钉")
    except Exception:
        pass

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--iters", type=int, default=10000)
    args = parser.parse_args()
    main(n_simulations=args.iters)
