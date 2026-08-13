"""CLV → ROI 闭环验证。

验证 CLV（收盘线价值）是否真的预测实际 ROI：
  - 正 CLV 投注的 ROI 若显著高于负 CLV，且 CLV 与单笔 ROI 正相关 → CLV 是有效 edge 代理
  - 否则 CLV 门禁/调权无效，应降权或停用

数据 join:
  - CLV: clv_results.csv (clv_collector 产出, true_clv_pct)
  - 盈亏: tracked_bets.json (settled bets 的 profit/stake)

用法: python3 -m src.monitor.clv_validate
"""
import json, csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

TRACKED_BETS = DATA_DIR / "tracked_bets.json"
CLV_RESULTS = DATA_DIR / "clv_results.csv"
MIN_SAMPLE = 30  # 至少 30 笔才给出结论


def validate_clv_predicts_roi() -> dict:
    """把 CLV 结果与结算盈亏 join，计算 CLV→ROI 相关性。"""
    # 1. 读结算盈亏 (tracked_bets)
    bets = {}
    if TRACKED_BETS.exists():
        try:
            data = json.loads(TRACKED_BETS.read_text())
            for b in data.get("bets", []):
                if b.get("status") != "settled":
                    continue
                key = (b.get("home", ""), b.get("away", ""),
                       b.get("sub_market", ""), b.get("designation", ""))
                bets[key] = {
                    "stake": b.get("stake", 0) or 0,
                    "profit": b.get("profit", 0) or 0,
                }
        except Exception as e:
            logger.warning("读取 tracked_bets 失败: %s", e)

    # 2. 读 CLV (clv_results.csv)
    clv_rows = []
    if CLV_RESULTS.exists():
        try:
            with open(CLV_RESULTS, newline="") as f:
                clv_rows = list(csv.DictReader(f))
        except Exception as e:
            logger.warning("读取 clv_results 失败: %s", e)

    # 3. join (按 home/away/sub_market/designation)
    joined = []
    for r in clv_rows:
        key = (r.get("home", ""), r.get("away", ""),
               r.get("sub_market", ""), r.get("designation", ""))
        if key in bets:
            bet = bets[key]
            try:
                clv = float(r.get("true_clv_pct", 0) or 0)
            except (ValueError, TypeError):
                clv = 0.0
            roi = bet["profit"] / bet["stake"] if bet["stake"] else 0.0
            joined.append({"clv": clv, "roi": roi, "profit": bet["profit"], "stake": bet["stake"]})

    n = len(joined)
    if n < MIN_SAMPLE:
        return {"status": "insufficient", "n": n, "need": MIN_SAMPLE,
                "message": f"CLV样本不足 ({n} < {MIN_SAMPLE})，暂无法验证 CLV 预测力"}

    # 4. 相关性 + 分桶对比
    clvs = [j["clv"] for j in joined]
    rois = [j["roi"] for j in joined]
    corr = _pearson(clvs, rois) if len(clvs) > 1 else 0.0

    pos = [j for j in joined if j["clv"] > 0]
    neg = [j for j in joined if j["clv"] <= 0]
    pos_stake = sum(j["stake"] for j in pos)
    neg_stake = sum(j["stake"] for j in neg)
    pos_roi = sum(j["profit"] for j in pos) / pos_stake if pos_stake else 0.0
    neg_roi = sum(j["profit"] for j in neg) / neg_stake if neg_stake else 0.0

    verdict = "valid" if corr > 0.1 and pos_roi > neg_roi else "questionable"
    return {
        "status": "ok", "n": n,
        "corr_clv_roi": round(corr, 4),
        "pos_clv_n": len(pos), "pos_clv_roi": round(pos_roi, 4),
        "neg_clv_n": len(neg), "neg_clv_roi": round(neg_roi, 4),
        "verdict": verdict,
        "message": (f"CLV→ROI 相关 {corr:.3f}; 正CLV ROI {pos_roi:+.1%} vs 负CLV {neg_roi:+.1%} "
                    f"→ {'CLV 有效' if verdict == 'valid' else 'CLV 存疑（需进一步审视）'}"),
    }


def _pearson(x, y):
    n = len(x)
    if n < 2:
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = (sum((a - mx) ** 2 for a in x)) ** 0.5
    dy = (sum((b - my) ** 2 for b in y)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


if __name__ == "__main__":
    result = validate_clv_predicts_roi()
    print(json.dumps(result, ensure_ascii=False, indent=2))
