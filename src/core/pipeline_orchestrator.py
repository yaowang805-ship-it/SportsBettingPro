"""
标准化管道编排器 — 整个系统的唯一自动化后台进程。

取代：
- bb_incremental_daemon.py
- 所有 launchd 定时触发 plist（scanmorning、scanevening、daily、dailyreport 等）
- 所有 cron 定时任务

用法:
    python3 -m src.core.pipeline_orchestrator [--dry-run]
    python3 -m src.core.pipeline_orchestrator --task scan
    python3 -m src.core.pipeline_orchestrator --task settle
"""
import os
import signal
import sys
import time
import traceback
from datetime import datetime, date
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SRC_DIR))

from config.logging_config import setup_logging, get_logger
from config.settings import send_dingtalk

# 确保 logging 初始化（root logger 配置）
setup_logging(log_level="INFO", log_to_file=True, log_to_console=True)

logger = get_logger("pipeline")

# ---------------------------------------------------------------------------
# 调度常量
# ---------------------------------------------------------------------------
LOCK_FILE = SRC_DIR / "data" / "storage" / ".pipeline_daemon.lock"
LOG_DIR = SRC_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SCAN_WINDOW = (8, 22)         # 08:00 ~ 22:00
INCREMENTAL_INTERVAL = 1200    # 20 分钟（秒）
CHECK_INTERVAL = 30            # 调度循环检查间隔（秒）

# 定时任务表：(名称, HH:MM, 处理函数, 参数字典)
SCHEDULE = [
    ("full_scan_morning",  "08:00", "do_full_scan",  {"bet": True}),
    ("settle_morning",     "08:30", "do_settle",      {}),
    ("daily_report",       "08:35", "do_daily_report",{}),
    ("full_scan_evening",  "20:00", "do_full_scan",   {"bet": False}),
    ("settle_evening",     "20:30", "do_settle",      {}),
    # 周报：周日 21:00
    ("weekly_report",      "Sun 21:00", "do_weekly_report", {}),
    # 月报：1日 10:00
    ("monthly_report",     "1 10:00",  "do_monthly_report",{}),
    ("git_commit",         "23:57", "do_git_commit",  {}),
]


def _parse_schedule_time(raw: str):
    """解析调度时间，返回 (weekday_or_None, dom_or_None, hour, minute)。"""
    parts = raw.split()
    if len(parts) == 2:
        # "Sun 21:00" 或 "1 10:00"
        first, hhmm = parts
        hour, minute = (int(x) for x in hhmm.split(":"))
        weekday = None
        dom = None
        if first.isdigit():
            dom = int(first)
        else:
            weekday_names = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
            weekday = weekday_names.get(first.lower()[:3])
        return weekday, dom, hour, minute
    # "HH:MM"
    hour, minute = (int(x) for x in raw.split(":"))
    return None, None, hour, minute


class PipelineOrchestrator:
    """管道编排器 — 管理所有定时任务的调度和执行。"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._running = True
        self._active_tasks: set[str] = set()
        self._last_run: dict[str, date] = {}          # 定时任务 → 最后执行日期
        self._last_incremental: float = 0              # 增量扫描时间戳
        self._alert_cooldown: dict[str, float] = {}    # 告警冷却
        self._setup_signal_handlers()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _setup_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

    def _handle_stop(self, signum, frame):
        logger.warning("收到信号 %s，优雅退出...", signum)
        self._running = False

    def _ensure_single_instance(self):
        if not LOCK_FILE.parent.exists():
            LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        pid = str(os.getpid())
        if LOCK_FILE.exists():
            old = LOCK_FILE.read_text().strip()
            if old:
                try:
                    os.kill(int(old), 0)
                    logger.error("另一实例正在运行 (PID %s)，退出", old)
                    sys.exit(0)
                except (ValueError, OSError):
                    pass
        LOCK_FILE.write_text(pid)
        logger.info("PID %s 已锁定 %s", pid, LOCK_FILE)

    def _cleanup_lock(self):
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()

    # ------------------------------------------------------------------
    # 调度逻辑
    # ------------------------------------------------------------------
    def _is_in_scan_window(self, now: datetime) -> bool:
        return SCAN_WINDOW[0] <= now.hour < SCAN_WINDOW[1]

    def _is_time_match(self, weekday, dom, hour, minute, now: datetime) -> bool:
        """检查当前时间是否符合调度条件。"""
        if dom is not None and now.day != dom:
            return False
        if weekday is not None and now.weekday() != weekday:
            return False
        return (now.hour, now.minute) == (hour, minute)

    def _should_run_wall_clock(self, name: str, now: datetime) -> bool:
        """判断定时任务是否该执行（每天一次）。"""
        last = self._last_run.get(name)
        if last == now.date():
            return False  # 今天已执行
        return True

    # ------------------------------------------------------------------
    # 任务执行（统一错误处理）
    # ------------------------------------------------------------------
    def _run_task(self, name: str, task_callable, **kwargs) -> bool:
        """执行任务，捕获所有异常，失败时发 DingTalk 告警。"""
        if name in self._active_tasks:
            logger.warning("[%s] 上一轮还未完成，跳过本次调度", name)
            return False

        self._active_tasks.add(name)
        logger.info("[%s] ====== START ======", name)
        t0 = time.time()

        try:
            if self.dry_run:
                logger.info("[%s] (dry-run) 跳过实际执行", name)
                result = None
            else:
                result = task_callable(**kwargs)
            elapsed = time.time() - t0
            logger.info("[%s] ====== DONE (%ds) ======", name, elapsed)
            return True

        except Exception as e:
            elapsed = time.time() - t0
            err_msg = f"{e}"
            logger.error("[%s] FAILED after %ds: %s", name, elapsed, err_msg)
            logger.error(traceback.format_exc())
            self._send_alert(name, err_msg)
            return False

        finally:
            self._active_tasks.discard(name)

    def _send_alert(self, task_name: str, error: str):
        """发送 DingTalk 告警（带冷却：同一任务每 30 分钟最多一次）。"""
        now = time.time()
        last = self._alert_cooldown.get(task_name, 0)
        if now - last < 1800:
            logger.info("[%s] 告警冷却中，跳过 (%.0f秒前刚发过)", task_name, now - last)
            return
        self._alert_cooldown[task_name] = now

        body = (
            f"**Pipeline Alert**\n\n"
            f"任务: {task_name}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"错误: {error[:200]}"
        )
        try:
            send_dingtalk("Pipeline Alert", body)
            logger.info("[%s] 告警已发送", task_name)
        except Exception as e:
            logger.error("[%s] 告警发送失败: %s", task_name, e)

    # ------------------------------------------------------------------
    # 具体任务实现
    # ------------------------------------------------------------------
    def do_full_scan(self, bet: bool = True):
        """全量扫描：提取 → 对比 → 推送。"""
        logger.info("Step 1/3: BB/FB API 提取...")
        from src.scrapers.bb_api_fetcher import main as fetch
        # bb_api_fetcher.main() 读取 sys.argv，需要临时设置
        old_argv = sys.argv
        sys.argv = ["bb_api_fetcher", "--all-sports"]
        try:
            fetch()
        finally:
            sys.argv = old_argv
        logger.info("Step 1/3: 完成")

        logger.info("Step 2/3: Pinnacle 对比...")
        from src.scrapers.bb_vs_pinnacle import main as compare
        old_argv = sys.argv
        sys.argv = ["bb_vs_pinnacle"]
        try:
            compare()
        finally:
            sys.argv = old_argv
        logger.info("Step 2/3: 完成")

        logger.info("Step 3/3: +EV 推送...")
        from src.report.bb_ev_push import main as push
        old_argv = sys.argv
        sys.argv = ["bb_ev_push", "--no-bet"] if not bet else ["bb_ev_push"]
        try:
            push()
        finally:
            sys.argv = old_argv
        logger.info("Step 3/3: 完成")

    def do_incremental(self):
        """增量扫描。"""
        from src.scrapers.bb_incremental_scanner import run_incremental
        run_incremental()

    def do_settle(self):
        """自动结算。"""
        from src.monitor.auto_settle import main as settle_main
        old_argv = sys.argv
        sys.argv = ["auto_settle"]
        try:
            settle_main()
        finally:
            sys.argv = old_argv

    def do_daily_report(self):
        """日报推送。"""
        from src.report.daily_settlement import main as dr
        old_argv = sys.argv
        sys.argv = ["daily_settlement"]
        try:
            dr()
        finally:
            sys.argv = old_argv

    def do_weekly_report(self):
        from src.report.periodic_report import main as pr
        old_argv = sys.argv
        sys.argv = ["periodic_report", "--weekly"]
        try:
            pr()
        finally:
            sys.argv = old_argv

    def do_monthly_report(self):
        from src.report.periodic_report import main as pr
        old_argv = sys.argv
        sys.argv = ["periodic_report", "--monthly"]
        try:
            pr()
        finally:
            sys.argv = old_argv

    def do_git_commit(self):
        """自动 git 提交。"""
        import subprocess
        result = subprocess.run(
            ["git", "add", "-u"],
            cwd=SRC_DIR,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning("git add 失败: %s", result.stderr[:200])
            return
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=SRC_DIR,
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("无变更，跳过提交")
            return
        result = subprocess.run(
            ["git", "commit", "-m", f"日常自动存档 {datetime.now().strftime('%Y-%m-%d')}"],
            cwd=SRC_DIR,
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            logger.info("已提交变更")
        else:
            logger.warning("提交失败: %s", result.stderr[:200])

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run_once(self, task_name: str, **kwargs):
        """执行单个任务（供 --task 参数使用）。"""
        task_map = {
            "scan": self.do_full_scan,
            "settle": self.do_settle,
            "daily_report": self.do_daily_report,
            "weekly_report": self.do_weekly_report,
            "monthly_report": self.do_monthly_report,
            "incremental": self.do_incremental,
        }
        func = task_map.get(task_name)
        if not func:
            logger.error("未知任务: %s", task_name)
            return
        self._run_task(task_name, func, **kwargs)

    def run_forever(self):
        """守护进程主循环。"""
        self._ensure_single_instance()
        logger.info("=" * 50)
        logger.info("Pipeline Orchestrator 启动")
        logger.info("扫描时段: %02d:00~%02d:00 | 增量间隔: %dmin",
                     SCAN_WINDOW[0], SCAN_WINDOW[1], INCREMENTAL_INTERVAL // 60)
        logger.info("定时任务: %s", ", ".join(name for name, *_ in SCHEDULE))
        logger.info("dry-run: %s", self.dry_run)
        logger.info("=" * 50)

        if not self.dry_run and not self._is_in_scan_window(datetime.now()):
            logger.info("当前不在扫描时段，等待 %02d:00...", SCAN_WINDOW[0])

        try:
            while self._running:
                now = datetime.now()

                # 1) 定时任务
                for name, time_str, method_name, kwargs in SCHEDULE:
                    weekday, dom, hour, minute = _parse_schedule_time(time_str)
                    if not self._is_time_match(weekday, dom, hour, minute, now):
                        continue
                    if not self._should_run_wall_clock(name, now):
                        continue
                    method = getattr(self, method_name, None)
                    if method:
                        self._run_task(name, method, **kwargs)
                        self._last_run[name] = now.date()

                # 2) 增量扫描
                if self._is_in_scan_window(now):
                    elapsed = (now - datetime.fromtimestamp(self._last_incremental)).total_seconds() if self._last_incremental else INCREMENTAL_INTERVAL + 1
                    if elapsed >= INCREMENTAL_INTERVAL:
                        self._run_task("incremental_scan", self.do_incremental)
                        self._last_incremental = time.time()

                time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("收到 KeyboardInterrupt")
        finally:
            self._cleanup_lock()
            logger.info("Pipeline Orchestrator 已停止")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="不实际执行任务")
    parser.add_argument("--task", help="执行单个任务后退出 (scan|settle|incremental|daily_report|weekly_report|monthly_report)")
    args = parser.parse_args()

    orch = PipelineOrchestrator(dry_run=args.dry_run)

    if args.task:
        orch.run_once(args.task)
    else:
        orch.run_forever()


if __name__ == "__main__":
    main()
