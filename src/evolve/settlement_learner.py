"""V5.1 结算自学习 — 真实投注结果回写权重调整

核心理念:
  职业团队不在真空中迭代。每笔真实结算都是对权重矩阵的校准信号。

机制:
  1. 读取 bet_log 中的已结算记录
  2. 按 (sport, league, market) 分组
  3. 计算实际 ROI vs 预期 ROI
  4. 学习调整系数 (conservative: 至少 10 笔才调整)
  5. 写入 learned_adjustments.json, 供 weight_matrix 调用
"""
import json, logging
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "storage"
LEARNED_FILE = DATA_DIR / "learned_adjustments.json"
MIN_SAMPLES = 10        # 最少 10 笔才学习
MAX_ADJUSTMENT = 0.30   # 最多 ±30%
CONFIDENCE_DECAY = 0.95 # 旧数据 95% 权重
logger = logging.getLogger(__name__)


def load_settled_bets():
    """从数据库加载所有已结算的投注记录。"""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DATA_DIR / "sportsbetting.db"))
        cursor = conn.execute("""
            SELECT home_team, away_team, sport, bet_type, stake, odds,
                   result, profit, notes
            FROM bet_log
            WHERE result IS NOT NULL AND result != ''
        """)
        cols = [d[0] for d in cursor.description]
        bets = [dict(zip(cols, row)) for row in cursor]
        conn.close()
        return bets
    except Exception as e:
        logger.warning(f"数据库读取失败: {e}")
        return []


def compute_roi_by_group(bets: list) -> dict:
    """按 (sport, league, market) 分组计算实际 ROI。"""
    import re
    groups = defaultdict(lambda: {"stakes": [], "pnls": [], "odds": [], "results": []})

    for b in bets:
        league = ""
        notes = b.get("notes", "") or ""
        league_match = re.search(r'\[([^\]]+)\]', notes)
        if league_match:
            league = league_match.group(1)

        sport = b.get("sport", "football")
        market = b.get("bet_type", "h2h")
        stake = b.get("stake", 0)
        pnl = b.get("profit", 0)
        odds = b.get("odds", 0)
        result = b.get("result", "")

        if stake <= 0 or odds <= 0:
            continue

        key = f"{sport}|{league}|{market}"
        groups[key]["stakes"].append(stake)
        groups[key]["pnls"].append(pnl)
        groups[key]["odds"].append(odds)
        groups[key]["results"].append(result)

    # Compute ROI per group
    learned = {}
    for key, data in groups.items():
        n = len(data["stakes"])
        if n < MIN_SAMPLES:
            continue

        total_stake = sum(data["stakes"])
        total_pnl = sum(data["pnls"])
        roi = total_pnl / total_stake if total_stake > 0 else 0
        win_rate = sum(1 for r in data["results"] if r == "win") / n

        avg_odds = sum(data["odds"]) / n
        implied_prob = 1.0 / avg_odds if avg_odds > 0 else 0
        edge = win_rate - implied_prob

        sport, league, market = key.split("|", 2)

        # Conservative: limit adjustment range
        if roi > 0.15:
            adjustment = min(roi * 0.3, MAX_ADJUSTMENT)  # 正 ROI → 提权重
        elif roi < -0.10:
            adjustment = max(roi * 0.5, -MAX_ADJUSTMENT)  # 负 ROI → 降权重
        else:
            adjustment = 0.0  # -10% ~ +15%: no adjustment

        learned[key] = {
            "sport": sport,
            "league": league,
            "market": market,
            "n": n,
            "roi": round(roi, 4),
            "win_rate": round(win_rate, 4),
            "edge": round(edge, 4),
            "adjustment": round(adjustment, 4),
            "total_stake": round(total_stake, 2),
            "total_pnl": round(total_pnl, 2),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    return learned


def merge_with_history(new_learned: dict) -> dict:
    """合并新学习结果与历史数据（指数衰减）。"""
    existing = {}
    if LEARNED_FILE.exists():
        try:
            existing = json.loads(LEARNED_FILE.read_text())
        except: pass

    merged = {}
    all_keys = set(new_learned.keys()) | set(existing.keys())

    for key in all_keys:
        new_data = new_learned.get(key)
        old_data = existing.get(key)

        if new_data and old_data:
            # Merge with decay
            old_n = old_data.get("n", 0)
            new_n = new_data.get("n", 0)
            total_n = old_n + new_n

            # Weighted average (new has full weight, old decays)
            decay_weight = old_n * CONFIDENCE_DECAY
            new_weight = new_n * 1.0
            total_weight = decay_weight + new_weight

            merged[key] = {
                **new_data,
                "n": total_n,
                "roi": round(
                    (old_data["roi"] * decay_weight + new_data["roi"] * new_weight) / total_weight, 4
                ) if total_weight > 0 else new_data["roi"],
                "win_rate": round(
                    (old_data["win_rate"] * decay_weight + new_data["win_rate"] * new_weight) / total_weight, 4
                ) if total_weight > 0 else new_data["win_rate"],
                "adjustment": round(
                    (old_data["adjustment"] * decay_weight + new_data["adjustment"] * new_weight) / total_weight, 4
                ) if total_weight > 0 else new_data["adjustment"],
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        elif new_data:
            merged[key] = new_data
        else:
            merged[key] = old_data

    return merged


def get_adjustment(sport: str, league: str = "", market: str = "") -> float:
    """获取某个 (sport, league, market) 的学习调整系数。"""
    if not LEARNED_FILE.exists():
        return 1.0

    try:
        learned = json.loads(LEARNED_FILE.read_text())
    except:
        return 1.0

    # Exact match
    key = f"{sport}|{league}|{market}"
    if key in learned:
        adj = learned[key].get("adjustment", 0)
        return 1.0 + adj

    # Sport+market match (aggregate)
    sport_market_matches = [
        v for k, v in learned.items()
        if k.startswith(f"{sport}|") and k.endswith(f"|{market}")
    ]
    if sport_market_matches:
        total_n = sum(v["n"] for v in sport_market_matches)
        if total_n >= MIN_SAMPLES:
            weighted_adj = sum(v["adjustment"] * v["n"] for v in sport_market_matches) / total_n
            return 1.0 + weighted_adj

    # Sport-level aggregate
    sport_matches = [v for k, v in learned.items() if k.startswith(f"{sport}|")]
    if sport_matches:
        total_n = sum(v["n"] for v in sport_matches)
        if total_n >= MIN_SAMPLES * 2:
            weighted_adj = sum(v["adjustment"] * v["n"] for v in sport_matches) / total_n
            return 1.0 + weighted_adj

    return 1.0


def run_learning_cycle():
    """执行一次完整的学习周期。"""
    logger.info("🧠 结算自学习: 读取已结算记录...")
    bets = load_settled_bets()
    if not bets:
        logger.info("  无已结算记录, 跳过")
        return {}

    logger.info(f"  {len(bets)} 笔已结算记录")

    new_learned = compute_roi_by_group(bets)
    merged = merge_with_history(new_learned)

    LEARNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2))

    n_groups = len(merged)
    n_active = sum(1 for v in merged.values() if v.get("n", 0) >= MIN_SAMPLES)
    logger.info(f"  ✅ 已保存: {n_groups} 组 ({n_active} 组达标 ≥{MIN_SAMPLES}笔)")

    return merged


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_learning_cycle()
    if result:
        print(f"\n学习结果预览:")
        for key, data in sorted(result.items(), key=lambda x: -x[1]["n"])[:5]:
            adj = data["adjustment"]
            direction = "↑" if adj > 0.01 else "↓" if adj < -0.01 else "→"
            print(f"  {direction} {key}: n={data['n']} ROI={data['roi']:+.1%} adj={adj:+.2f}")
