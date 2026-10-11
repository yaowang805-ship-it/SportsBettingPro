"""共享统计计数器(2026-10-11) —— 滚球/早盘跳过分桶统一入口。

消除「某条路径静默无数据」的故障模式: second_level_monitor 和 bb_real_bet_flow 都 import 这里,
同一个 dict 计数, 同一个 summary 打印。模块级 Counter, 进程内共享。

用法:
    from src.scrapers.stats import count, snapshot, maybe_summary
    count("line_zero_seen_live")
    count("line_zero_filtered_live", n=3)
    maybe_summary(30.0)  # 每 30s 打一次摘要
"""
import time
from collections import Counter

_STATS = Counter()
_LAST_SUMMARY = {"ts": 0.0}


def count(key, n=1):
    _STATS[key] += n


def snapshot():
    return dict(_STATS)


def maybe_summary(interval=30.0):
    """每 interval 秒打一次摘要(非阻塞)。返回是否打了(供调用方判断)。"""
    now = time.time()
    if now - _LAST_SUMMARY["ts"] < interval:
        return False
    _LAST_SUMMARY["ts"] = now
    if _STATS:
        print(f"[stats] {dict(_STATS)}", flush=True)
    return True
