"""V5.1 赔率策略自动优化器 — 每日根据累积数据重算最优 EV 阈值

数据源:
  1. settlement_log.csv (240+笔, edge_pct + outcome)
  2. virtual_portfolio.json (663笔, odds + pnl)
  3. push_clv (2704条, bb_odds + fair_price + ev_pct)
  4. Pinnacle历史收盘数据 (10万+条, WR by odds bin)

输出: config/odds_strategy.json
"""
import json, csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "storage"
CONFIG_FILE = ROOT / "config" / "odds_strategy.json"
MIN_BETS = 5  # 每桶最少样本


def load_all_settlement_data():
    """加载所有可用的结算数据。"""
    bets = []

    # settlement_log.csv (best: has edge_pct + outcome)
    csv_path = DATA_DIR / "settlement_log.csv"
    if csv_path.exists():
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    bets.append({
                        "odds": float(row["odds"]),
                        "stake": float(row["stake"]),
                        "pnl": float(row["profit"]),
                        "edge": float(row.get("edge_pct", 0)),
                        "outcome": row.get("outcome", ""),
                        "source": "settlement_log",
                    })
                except (ValueError, KeyError):
                    pass

    # virtual_portfolio.json
    vp_path = DATA_DIR / "virtual_portfolio.json"
    if vp_path.exists():
        vp = json.loads(vp_path.read_text())
        seen = set()
        for key in ["history", "settled"]:
            val = vp.get(key, [])
            items = val if isinstance(val, list) else val.values() if isinstance(val, dict) else []
            for b in items:
                if not isinstance(b, dict):
                    continue
                bid = b.get("id", str(b))
                if bid in seen:
                    continue
                seen.add(bid)
                try:
                    stake = float(b.get("stake", 0))
                    odds = float(b.get("odds", 0))
                    profit = float(b.get("profit", 0))
                    if stake <= 0 or odds <= 1.0:
                        continue
                    bets.append({
                        "odds": odds,
                        "stake": stake,
                        "pnl": profit,
                        "edge": 0,  # no edge data in virtual bets
                        "outcome": "won" if profit > 0 else "lost",
                        "source": "virtual",
                    })
                except (ValueError, TypeError):
                    pass

    return bets


def find_optimal_ev(bets: list, odds_min: float, odds_max: float) -> dict:
    """对给定赔率区间，找到最小盈利 EV 阈值。"""
    bucket = [b for b in bets if odds_min <= b["odds"] < odds_max and b["edge"] > 0]

    if len(bucket) < MIN_BETS:
        return None

    total_s = sum(b["stake"] for b in bucket)
    total_p = sum(b["pnl"] for b in bucket)
    current_roi = total_p / total_s * 100 if total_s > 0 else 0

    # Find minimum EV that yields positive ROI
    best_ev = None
    for ev in [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 12.0, 15.0, 20.0]:
        filtered = [b for b in bucket if b["edge"] >= ev]
        if len(filtered) < 3:
            continue
        fs = sum(b["stake"] for b in filtered)
        fp = sum(b["pnl"] for b in filtered)
        froi = fp / fs * 100 if fs > 0 else 0
        if froi > 0:
            best_ev = ev
            break

    # Also check: even without edge data, what's the ROI of ALL bets in this range?
    all_bucket = [b for b in bets if odds_min <= b["odds"] < odds_max]
    all_roi = sum(b["pnl"] for b in all_bucket) / sum(b["stake"] for b in all_bucket) * 100 \
        if sum(b["stake"] for b in all_bucket) > 0 else 0

    return {
        "odds_min": odds_min,
        "odds_max": odds_max,
        "n": len(bucket),
        "n_all": len(all_bucket),
        "current_roi": round(current_roi, 1),
        "all_roi": round(all_roi, 1),
        "optimal_ev": best_ev or max(2.0, round(abs(all_roi) * 0.3 + 2.0, 1)),
        "needs_more_data": len(bucket) < 20,
    }


def run_optimization():
    """执行一次完整优化，更新 config/odds_strategy.json。"""
    bets = load_all_settlement_data()
    print(f"[OddsOptimizer] 加载 {len(bets)} 笔结算数据")

    config = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {"tiers": []}

    new_tiers = []
    for tier in config.get("tiers", []):
        odds_min = tier["odds_min"]
        odds_max = tier["odds_max"]

        result = find_optimal_ev(bets, odds_min, odds_max)

        if result and not result.get("needs_more_data"):
            # Adjust if significantly different
            old_ev = tier.get("min_ev", 2.0)
            new_ev = result["optimal_ev"]
            adjustment = min(0.3, abs(new_ev - old_ev) / max(old_ev, 0.01))

            tier["min_ev"] = round(old_ev + (new_ev - old_ev) * adjustment, 1)
            tier["_n_bets"] = result["n_all"]
            tier["_current_roi"] = result["all_roi"]
            print(f"  {tier['label']}: EV {old_ev}% → {tier['min_ev']}% (ROI={result['all_roi']:+.1f}%, n={result['n_all']})")
        elif result:
            tier["_n_bets"] = result["n_all"]
            tier["_current_roi"] = result["all_roi"]
            print(f"  {tier['label']}: 保持 EV={tier.get('min_ev', 2.0)}% (数据不足, n={result['n_all']})")

        new_tiers.append(tier)

    config["tiers"] = new_tiers
    config["updated"] = datetime.now(timezone.utc).isoformat()
    config["_total_bets"] = len(bets)

    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    print(f"[OddsOptimizer] ✅ 已保存到 {CONFIG_FILE}")
    return config


def get_odds_strategy(bb_odds: float) -> dict:
    """获取给定 BB 赔率的策略参数。"""
    if not CONFIG_FILE.exists():
        return {"min_ev": 2.0, "kelly_mult": 1.0, "max_odds": 20.0}

    config = json.loads(CONFIG_FILE.read_text())

    for tier in config.get("tiers", []):
        if tier["odds_min"] <= bb_odds < tier["odds_max"]:
            return {
                "min_ev": tier.get("min_ev", 2.0),
                "kelly_mult": tier.get("kelly_mult", 1.0),
                "max_odds": tier.get("odds_max", 20.0),
                "label": tier.get("label", ""),
            }

    return {"min_ev": 2.0, "kelly_mult": 1.0, "max_odds": 20.0}


if __name__ == "__main__":
    run_optimization()
