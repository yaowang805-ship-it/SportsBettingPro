#!/usr/bin/env python3
"""Pinnacle 限速/熔断状态的跨进程共享层。

为什么需要:
    pinnacle_api 的限速状态全是模块内存全局(_last_req_time/_BURST_COUNT/
    _CIRCUIT_OPEN_UNTIL/_BAN_COUNT)。但同时消费 Pinnacle 的是**四个独立进程**:
        pipeline_orchestrator(常驻)、clv_collector(cron 5min)、
        clv_backfill(cron 30min)、clv_archive_backfill(cron 30min)
    每个进程都以为自己独占 10 req/s → 撞在一起真实速率可达 20~40 req/s,
    而代码里写明封禁线是 16 req/s。这是"降级到 8 req/s 之后还被反复封"的根因:
    降的是单进程速率, 总速率没降。熔断器同理 —— 主扫描已熔断暂停, CLV 采集器
    完全不知道, 照样往上打。

设计要点:
  1. **总速率全局封顶**: 用共享的 next_slot 指针原子预约发号, 所有进程排一条队。
  2. **扫描推送隔离铁律**: 主扫描(high)不受影响 —— 它照常按基础间隔取号;
     低优先级(low, 即 CLV/回填这些后台任务)额外受自己的 LOW_MIN_INTERVAL 限制,
     只吃余量, 绝不和扫描抢带宽。
  3. **绝不阻塞扫描**: 任何异常/锁超时都 fail-open 退回调用方的进程内限速,
     宁可短暂超速也不能把扫描卡死。等待时间还有 MAX_WAIT 硬上限。
  4. 睡眠在事务外进行 —— 事务里只做"取号"这一下, 不持锁睡觉。
"""
import os
import sqlite3
import time

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

STATE_DB = DATA_DIR / "pin_rate_limit.db"

LOW_MIN_INTERVAL = 0.5   # 低优先级(后台任务)自身最快 2 req/s, 给扫描让带宽
MAX_WAIT = 5.0           # 单次取号最多等这么久, 超过就 fail-open 放行, 不卡死调用方
_LOCK_TIMEOUT = 0.5      # 拿不到库锁就退回进程内限速

_disabled = False        # 连续出错后彻底关掉共享层, 避免拖累主流程


def _connect():
    conn = sqlite3.connect(str(STATE_DB), timeout=_LOCK_TIMEOUT, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")   # 多进程读写, 避免 reader 阻塞 writer
    conn.execute("PRAGMA busy_timeout=%d" % int(_LOCK_TIMEOUT * 1000))
    conn.execute("""CREATE TABLE IF NOT EXISTS pin_rate (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        next_slot REAL DEFAULT 0,        -- 下一个可用发号时刻
        low_next_slot REAL DEFAULT 0,    -- 低优先级专用发号时刻
        burst_start REAL DEFAULT 0,
        burst_count INTEGER DEFAULT 0,
        circuit_until REAL DEFAULT 0,    -- 熔断到期(全进程共享)
        pause_until REAL DEFAULT 0,      -- Cloudflare 封禁冷却到期
        ban_count INTEGER DEFAULT 0,
        last_ban REAL DEFAULT 0
    )""")
    conn.execute("INSERT OR IGNORE INTO pin_rate (id) VALUES (1)")
    return conn


def _row(conn):
    cur = conn.execute(
        "SELECT next_slot, low_next_slot, burst_start, burst_count, "
        "circuit_until, pause_until, ban_count, last_ban FROM pin_rate WHERE id = 1")
    return cur.fetchone()


def reserve(min_interval, burst_limit, burst_window, priority="high"):
    """跨进程预约一个请求名额。

    Returns:
        (allowed, sleep_seconds, reason)
        allowed=False 表示当前处于熔断/封禁冷却, 调用方应直接跳过本次请求。
        allowed=True  时调用方需先 sleep(sleep_seconds) 再发请求。
        任何异常都返回 (None, 0, "unavailable") —— 表示共享层不可用,
        调用方退回自己的进程内限速(fail-open, 绝不阻塞扫描)。
    """
    global _disabled
    if _disabled:
        return None, 0.0, "disabled"
    try:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            (next_slot, low_next_slot, burst_start, burst_count,
             circuit_until, pause_until, ban_count, last_ban) = _row(conn)
            now = time.time()

            if pause_until > now:
                conn.execute("ROLLBACK")
                return False, 0.0, f"pause:{pause_until - now:.0f}s"
            if circuit_until > now:
                conn.execute("ROLLBACK")
                return False, 0.0, f"circuit:{circuit_until - now:.0f}s"

            # 突发窗口滚动
            if now - burst_start > burst_window:
                burst_start, burst_count = now, 0
            if burst_count >= burst_limit:
                # 窗口打满, 排到下个窗口开头
                next_slot = max(next_slot, burst_start + burst_window)
                burst_start, burst_count = next_slot, 0

            slot = max(next_slot, now)
            if priority == "low":
                # 后台任务额外自我限速, 只吃扫描剩下的带宽
                slot = max(slot, low_next_slot)

            wait = slot - now
            if wait > MAX_WAIT:
                # 队伍太长, 不让调用方干等 —— 放行由进程内限速兜底
                conn.execute("ROLLBACK")
                return None, 0.0, "queue_too_long"

            new_next = slot + min_interval
            new_low = (slot + LOW_MIN_INTERVAL) if priority == "low" else low_next_slot
            conn.execute(
                "UPDATE pin_rate SET next_slot=?, low_next_slot=?, burst_start=?, "
                "burst_count=? WHERE id=1",
                (new_next, new_low, burst_start, burst_count + 1))
            conn.execute("COMMIT")
            return True, max(0.0, wait), "ok"
        finally:
            conn.close()
    except Exception as e:
        logger.debug("共享限速不可用, 退回进程内限速: %s", e)
        return None, 0.0, "unavailable"


def _set(field, value):
    global _disabled
    if _disabled:
        return
    try:
        conn = _connect()
        try:
            conn.execute(f"UPDATE pin_rate SET {field}=? WHERE id=1", (value,))
        finally:
            conn.close()
    except Exception as e:
        logger.debug("共享限速写入失败(%s): %s", field, e)


def open_circuit(seconds):
    """熔断: 让所有进程一起暂停。"""
    _set("circuit_until", time.time() + seconds)


def set_pause(seconds):
    """Cloudflare 封禁冷却: 让所有进程一起暂停。

    V5.10 修复: 只在**当前没有有效 pause** 时才设置新 pause, 不续期。
    原先每次都 _set("pause_until", now+seconds) —— Pin 反复 403 封禁时,
    每次封禁都把 pause 往后推 30 分钟, 密集封禁 = 无限期暂停。实测 2026-08-19
    晚上 56 次封禁把 pause 反复续期, 比价从 19:18 停摆到扫描时段关闭。
    封禁冷却的目的是"让 IP 解封", 不是"每触发一次就从头再冷却一遍"。
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT pause_until FROM pin_rate WHERE id=1").fetchone()
        now = time.time()
        cur = row[0] if row else 0
        # 已有未过期的 pause 就保持原到期时间, 不延长
        if cur <= now:
            conn.execute("UPDATE pin_rate SET pause_until=? WHERE id=1",
                         (now + seconds,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        _set("pause_until", time.time() + seconds)  # 降级兜底
    finally:
        conn.close()


def record_ban():
    """记一次封禁, 返回全局连续封禁次数(用于降级请求频率)。"""
    try:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _row(conn)
            ban_count, last_ban = row[6], row[7]
            now = time.time()
            if last_ban and now - last_ban > 24 * 3600:
                ban_count = 0
            ban_count += 1
            conn.execute("UPDATE pin_rate SET ban_count=?, last_ban=? WHERE id=1",
                         (ban_count, now))
            conn.execute("COMMIT")
            return ban_count
        finally:
            conn.close()
    except Exception:
        return None


def get_ban_count():
    try:
        conn = _connect()
        try:
            row = _row(conn)
            ban_count, last_ban = row[6], row[7]
            if last_ban and time.time() - last_ban > 24 * 3600:
                return 0
            return ban_count
        finally:
            conn.close()
    except Exception:
        return None


def snapshot():
    """给诊断脚本看的当前共享状态。"""
    try:
        conn = _connect()
        try:
            r = _row(conn)
            now = time.time()
            return {
                "next_slot_in": round(r[0] - now, 2),
                "low_next_slot_in": round(r[1] - now, 2),
                "burst_count": r[3],
                "circuit_remaining": max(0, round(r[4] - now)),
                "pause_remaining": max(0, round(r[5] - now)),
                "ban_count": r[6],
                "db": str(STATE_DB),
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}
