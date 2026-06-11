"""统一告警日志 — 将各模块告警持久化到文件，供监控面板使用。"""
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

ALERT_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "monitor" / "alert_log.jsonl"
MAX_ENTRIES = 500


def log_alert(category: str, title: str, detail: str = "", level: str = "WARNING"):
    """写入一条告警记录。

    Args:
        category: 告警分类 (performance/model/data/risk/system)
        title: 简短标题
        detail: 详细描述（可选）
        level: 级别 (INFO/WARNING/ERROR/CRITICAL)
    """
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "level": level,
        "title": title,
        "detail": detail,
    }
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 控制文件大小
    _trim_log()


def _trim_log():
    if not ALERT_LOG.exists():
        return
    lines = ALERT_LOG.read_text().strip().split("\n")
    if len(lines) > MAX_ENTRIES:
        ALERT_LOG.write_text("\n".join(lines[-MAX_ENTRIES:]) + "\n")


def get_alerts(limit: int = 100, category: Optional[str] = None, level: Optional[str] = None, since: Optional[str] = None):
    """读取最近的告警记录。"""
    if not ALERT_LOG.exists():
        return []
    entries = []
    for line in ALERT_LOG.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if category and entry.get("category") != category:
                continue
            if level and entry.get("level") != level:
                continue
            if since and entry.get("timestamp", "") < since:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            continue
    return entries[-limit:]
