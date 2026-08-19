#!/usr/bin/env python3
"""静默失效看门狗 — 任务"有活可干却连续零产出"时告警。

为什么需要:
    2026-08-19 查出 bb_score_settle 从 8/15 起就完全失效(fetch_bb_match_result 里
    completed 判定漏了 ms=0, 拿到比分也被丢弃)。它每 30 分钟被 cron 正常调起、
    正常退出、日志里老老实实写着 {"settled":0,"matched":0} —— 连续四天, 没有任何
    人或程序发现。同一天还查出 CLV 采集器日志天天写 "pending: 630条", 其中一大半
    是早就永久丢失的死记录, 也是一个"假装在工作的进度条"。

    这类故障的共同特征: **进程健康、退出码 0、日志无异常, 但产出恒为 0**。
    传统看门狗盯的是"进程死没死""有没有报错", 全都测不出来。

判据:
    只在"确实有活可干"时才计数 —— expected > 0 而 produced == 0。
    任务本来就没活干(如凌晨无待结算注)不算失效, 否则告警会变成噪声被忽略,
    那样就白做了。

用法:
    from src.monitor.silent_failure_watch import record_run
    record_run("bb_score_settle", produced=matched, expected=len(bets),
               detail="待结算 %d 笔但一笔都没匹配上" % len(bets))
"""
import json
import time

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

STATE_FILE = DATA_DIR / "silent_failure_state.json"

# 连续多少次"有活却零产出"才告警(避免偶发抖动误报)
DEFAULT_THRESHOLD = 3
# 同一任务的告警节流(秒), 避免刷屏
ALERT_COOLDOWN = 6 * 3600


def _load():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(state):
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.debug("静默失效状态写入失败: %s", e)


def record_run(job: str, produced: int, expected: int, detail: str = "",
               threshold: int = DEFAULT_THRESHOLD) -> bool:
    """记录一次任务运行。返回是否触发了告警。

    Args:
        job: 任务名(状态按此聚合)
        produced: 本次实际产出条数
        expected: 本次"本来应该能产出"的上限(有多少活可干)
        detail: 告警里附带的人话说明
        threshold: 连续多少次零产出才告警
    """
    state = _load()
    rec = state.get(job) or {"streak": 0, "last_alert": 0, "last_ok": 0}
    now = time.time()
    fired = False

    if expected <= 0:
        # 本来就没活干, 不是失效; 不重置 streak(避免夜里空转把计数洗掉)
        rec["last_idle"] = now
    elif produced > 0:
        if rec.get("streak", 0) >= threshold:
            logger.info("✅ %s 已恢复产出 (此前连续 %d 次零产出)", job, rec["streak"])
        rec["streak"] = 0
        rec["last_ok"] = now
    else:
        rec["streak"] = rec.get("streak", 0) + 1
        logger.warning("⚠️ %s: 有 %d 项待处理却零产出 (连续第 %d 次)",
                       job, expected, rec["streak"])
        if rec["streak"] >= threshold and now - rec.get("last_alert", 0) > ALERT_COOLDOWN:
            _alert(job, rec, expected, detail)
            rec["last_alert"] = now
            fired = True

    state[job] = rec
    _save(state)
    return fired


def _alert(job, rec, expected, detail):
    last_ok = rec.get("last_ok") or 0
    since = f"{(time.time() - last_ok) / 3600:.1f} 小时前" if last_ok else "从未成功过"
    msg = (f"【投注推荐】⚠️ 任务静默失效\n\n"
           f"任务: {job}\n"
           f"连续 {rec['streak']} 次「有活可干却零产出」\n"
           f"本次待处理: {expected} 项\n"
           f"上次成功: {since}\n")
    if detail:
        msg += f"\n{detail}\n"
    msg += ("\n注意: 该任务进程正常、退出码正常、日志无报错 —— 属于静默失效, "
            "常规看门狗测不出来, 需人工排查。")
    logger.error("静默失效告警: %s 连续 %d 次零产出", job, rec["streak"])
    try:
        from config.dingtalk import send_dingtalk
        send_dingtalk("任务静默失效", msg, timeout=10)
    except Exception as e:
        logger.warning("静默失效告警推送失败: %s", e)


def status():
    """给诊断脚本看的当前状态。"""
    out = {}
    now = time.time()
    for job, rec in _load().items():
        out[job] = {
            "连续零产出": rec.get("streak", 0),
            "上次成功": (f"{(now - rec['last_ok']) / 3600:.1f}h 前"
                         if rec.get("last_ok") else "从未"),
        }
    return out


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(status(), ensure_ascii=False, indent=2))
