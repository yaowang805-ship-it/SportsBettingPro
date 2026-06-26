#!/usr/bin/env python3
"""今日全球推荐 — 快速入口，使用 EnsemblePredictor 统一预测。

自动扫描 NBA / 足球 / NFL 三个运动，输出正 EV 推荐并推送钉钉。

用法:
    python src/predict_today.py
"""
import json
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR

import requests

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DINGTALK_WEBHOOK

MIN_EV = 0.05   # 最低 EV 5%
MAX_RECS = 5

# ── 钉钉推送 ──

def _send_dingtalk(title: str, text: str):
    from config.settings import send_dingtalk as _sd
    _sd(title, text)

# ── 主逻辑 ──

def _format_rec(rec: dict) -> str:
    h = rec.get("home_team", "?")
    a = rec.get("away_team", "?")
    tp = rec.get("type", "?")
    odds = rec.get("odds", 0)
    ev = rec.get("ev", 0)
    stake = rec.get("stake", 0)
    prob = rec.get("model_prob", 0)
    return (f"  {tp.upper()} {h} vs {a}\n"
            f"    模型{prob:.1%} 赔率{odds:.2f} EV+{ev:.1%} 注额¥{stake:.0f}")


def main():
    logger.info("=" * 60)
    logger.info("📊 今日全球推荐 - %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    sports = [
        ("nba", "src.predict.daily_bb", "NBA"),
        ("football", "src.predict.daily_fb", "足球"),
        ("nfl", "src.predict.daily_nfl", "NFL"),
        ("wc", "src.predict.daily_wc", "世界杯"),
    ]

    all_recs = []
    for sport_key, daily_module, sport_name in sports:
        try:
            import importlib
            mod = importlib.import_module(daily_module)
            mod.main()
            # 读取生成的推荐文件
            rec_file = Path(DATA_DIR) / f"daily_{sport_key}_recommendations.json"
            if rec_file.exists():
                data = json.loads(rec_file.read_text())
                recs = data.get("recommendations", [])
                for r in recs:
                    r["_sport"] = sport_name
                    r["ev"] = r.get("ev", r.get("_ev", 0))
                all_recs.extend(recs)
                logger.info("  %s: %d 条推荐", sport_name, len(recs))
        except Exception as e:
            logger.warning("  ⚠️ %s 预测跳过: %s", sport_name, e)

    # 按 EV 排序取 Top N
    all_recs.sort(key=lambda x: x.get("ev", 0), reverse=True)
    top = all_recs[:MAX_RECS]

    lines = [f"📊 今日全球 Top {len(top)} 推荐\n"]
    lines.append(f"更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    for i, r in enumerate(top, 1):
        ev_pct = r.get("ev", 0) * 100
        lines.append(f"#{i} [{r.get('_sport', '?')}] EV+{ev_pct:.1f}%")
        lines.append(_format_rec(r))
        lines.append("")

    output = "\n".join(lines)
    print("\n" + output)

    if top:
        _send_dingtalk("今日推荐", output)
        logger.info("✅ 已推送 %d 条至钉钉", len(top))
    else:
        msg = "今日未检测到显著正 EV 机会，系统保持沉默。"
        _send_dingtalk("今日推荐", msg)
        logger.info("✅ 已通知钉钉: 无推荐")

    # 模拟交易报告（非阻塞）
    try:
        from src.betting.paper_trader import PaperTrader
        pt = PaperTrader()
        print()
        pt.print_report()
    except Exception:
        pass

    # 数据库同步（非阻塞）
    try:
        import subprocess
        sync_script = ROOT / "scripts" / "sync_db.py"
        if sync_script.exists():
            subprocess.run([sys.executable, str(sync_script)], check=True,
                           capture_output=True, text=True, timeout=60)
    except Exception:
        pass


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
