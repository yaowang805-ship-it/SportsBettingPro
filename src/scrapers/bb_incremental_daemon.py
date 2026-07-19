"""BB体育 增量扫描守护进程。

由 launchd 管理（~/Library/LaunchAgents/），替代 crontab。

特性：
- 每60秒循环
- 仅 08:00 ~ 22:00 时段内扫描，每20分钟一次
- 非扫描时段静默（不消耗资源，不写日志）
- 合盖唤醒后自动补跑
- 掉线自动重启（launchd KeepAlive）
- 单实例锁，防止重复启动
"""
import json
import os
import sys
import time
import signal
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

LOG_DIR = SRC_DIR.parent / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "incremental_daemon.log"

SCAN_START_HOUR = 8    # 08:00 开始扫描
SCAN_END_HOUR = 22      # 22:00 停止扫描
_running = True


def _in_scan_hours() -> bool:
    """当前是否在扫描时段 08:00~22:00"""
    h = time.localtime().tm_hour
    return SCAN_START_HOUR <= h < SCAN_END_HOUR


def log(msg):
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{t}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _ensure_single_instance():
    """单实例锁：已运行的守护进程直接退出。"""
    lock = SRC_DIR.parent / "data" / "storage" / ".incremental_daemon.lock"
    try:
        if lock.exists():
            old_pid = lock.read_text().strip()
            if old_pid:
                try:
                    old_pid = int(old_pid)
                    # check if process with this PID is still running
                    os.kill(old_pid, 0)
                    log(f"⚠️ 另一实例正在运行 (PID {old_pid})，退出")
                    sys.exit(0)
                except (ValueError, OSError):
                    # PID invalid or process not running → stale lock, overwrite
                    pass
        pid = str(os.getpid())
        lock.write_text(pid)
    except Exception as e:
        log(f"⚠️ 无法写入锁文件: {e}")


def handler(signum, frame):
    global _running
    log(f"收到信号 {signum}，准备退出...")
    _running = False


def main():
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    _ensure_single_instance()

    CHECK_INTERVAL = 60       # 每60秒检查一次
    MAX_IDLE_MINUTES = 20     # 超过20分钟没扫描就执行
    MIN_SCAN_INTERVAL = 300   # 两次扫描之间至少间隔5分钟（防频繁）

    last_scan = 0
    scans_run = 0
    was_in_hours = _in_scan_hours()

    log("=" * 50)
    log("增量扫描守护进程启动")
    log(f"扫描时段: {SCAN_START_HOUR}:00 ~ {SCAN_END_HOUR}:00 | 间隔: {MAX_IDLE_MINUTES}min")
    if not was_in_hours:
        log(f"当前不在扫描时段，等待 {SCAN_START_HOUR}:00...")
    log("=" * 50)

    while _running:
        now = time.time()

        if not _in_scan_hours():
            if was_in_hours:
                log("⏰ 已过 22:00，停止今日扫描")
                was_in_hours = False
                last_scan = 0  # 重置，明天首次扫描不用等20分钟
            time.sleep(CHECK_INTERVAL)
            continue

        # 进入扫描时段的首次提醒
        if not was_in_hours:
            log(f"⏰ 已到 {SCAN_START_HOUR}:00，开始今日扫描")
            was_in_hours = True
            last_scan = 0  # 确保到点立即扫一次

        elapsed_min = (now - last_scan) / 60 if last_scan else MAX_IDLE_MINUTES + 1

        if elapsed_min >= MAX_IDLE_MINUTES and (now - last_scan) >= MIN_SCAN_INTERVAL:
            scans_run += 1
            log(f"[#{scans_run}] 上次扫描 {elapsed_min:.0f} 分钟前，开始扫描...")

            try:
                from src.scrapers.bb_incremental_scanner import run_incremental
                run_incremental()
            except Exception as e:
                log(f"  ❌ 扫描异常: {e}")

            last_scan = time.time()
            log(f"[#{scans_run}] 扫描完成\n")

        try:
            time.sleep(CHECK_INTERVAL)
        except InterruptedError:
            continue

    log("守护进程已停止")
    # 清理锁
    lock = SRC_DIR.parent / "data" / "storage" / ".incremental_daemon.lock"
    try:
        if lock.exists() and lock.read_text().strip() == str(os.getpid()):
            lock.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
