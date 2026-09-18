"""早盘 Betfair 公平价并行收集器 — 与现有 Pin 流程并存, 不改 Pin, 只记录 Betfair 公平价。

目的(2026-09-18): 验证 Betfair(交易所)公平价 是否比 Pin(15分钟陈旧) 更准。
运行方式: 与 bb_vs_pinnacle 并行跑(launchd 或手动), 每轮:
  1. 拉 odds-api.io 的早盘足球事件(upcoming)
  2. 逐个拉 Betfair ML HT + ML 公平价(交易所中间价)
  3. 存 data/storage/betfair_fair_prices.json(带时间戳, 供后续比对结算)

后续比对: 拿本文件记录的 Betfair 公平价 vs 同一场的 Pin 公平价 vs 真实结算,
看谁的隐含概率更接近实际赢率(即谁更能筛出真溢价)。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.odds_api_io import get_events, fair_price
OUT_FILE = ROOT / "data" / "storage" / "betfair_fair_prices.json"

# 关注的盘口: ht(半场独赢, 早盘 ht主胜 核心) + 1x2(全场独赢)
SUB_MARKETS = ["ht", "1x2"]


def _load():
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"records": {}}


def _save(data):
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def collect_once(max_events=30):
    """拉一轮早盘足球事件, 记录 Betfair 公平价。返回新增条数。"""
    events = get_events("football")  # 早盘(非 live)足球
    data = _load()
    records = data["records"]
    added = 0
    now = time.time()
    for e in events[:max_events]:
        eid = str(e.get("id"))
        # 已记录且没超过 1 小时的跳过(避免每轮重复拉)
        prev = records.get(eid)
        if prev and now - prev.get("_ts", 0) < 3600:
            continue
        fair = {}
        for sub in SUB_MARKETS:
            f = fair_price(int(eid), sub)
            if f:
                fair[sub] = f
        if not fair:
            continue
        records[eid] = {
            "home": e.get("home", ""), "away": e.get("away", ""),
            "league": (e.get("league") or {}).get("name", ""),
            "date": e.get("date", ""),
            "fair": fair,
            "_ts": now,
        }
        added += 1
    _save(data)
    return added


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    n = collect_once()
    data = _load()
    print(f"本轮新增 {n} 场 Betfair 公平价记录, 累计 {len(data['records'])} 场")
    # 打印 2 场样例
    for i, (eid, rec) in enumerate(list(data["records"].items())[:2]):
        print(f"  {rec['home']} vs {rec['away']} ({rec['league']}): ht={rec['fair'].get('ht')}")
