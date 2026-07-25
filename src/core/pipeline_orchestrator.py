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

# 日志轮转：启动时自动归档过大的旧日志（跳过已轮转过的文件）
# 使用 copy+truncate 而非 rename，因为 launchd 持有 pipeline_daemon.log 的 fd，
# rename 只改目录项不会改变 fd 指向的 inode，会导致新日志丢失。
_date_str = datetime.now().strftime('%Y%m%d')
for _lf in sorted(LOG_DIR.iterdir()):
    if _lf.is_file() and _lf.stat().st_size > 2 * 1024 * 1024 and not _lf.name.endswith(_date_str):
        _rotated = _lf.parent / f"{_lf.name}.{_date_str}"
        import shutil
        shutil.copy2(str(_lf), str(_rotated))
        _lf.write_text("")  # truncate in-place，fd 仍然有效
        print(f"  📦 日志轮转: {_lf.name} → {_rotated.name}")

SCAN_WINDOW = (8, 22)              # 08:00 ~ 22:00
INCREMENTAL_INTERVAL_NEAR = 900    # 15 分钟 — 24h内临场比赛
INCREMENTAL_INTERVAL_FAR = 1800    # 30 分钟 — 24-72h早盘比赛
CHECK_INTERVAL = 30                # 调度循环检查间隔（秒）

# 定时任务表：(名称, HH:MM, 处理函数, 参数字典)
SCHEDULE = [
    ("health_check",       "07:55", "do_health_check", {}),
    ("full_scan_morning",  "08:00", "do_full_scan",  {"bet": True}),
    ("settle_morning",     "09:30", "do_settle",      {}),
    ("daily_report",       "10:00", "do_daily_report",{}),
    ("memory_update",      "10:05", "do_memory_update", {}),
    ("settle_noon",        "14:00", "do_settle",      {}),  # 午后结算
    ("settle_afternoon",   "17:00", "do_settle",      {}),  # 傍晚结算
    ("full_scan_evening",  "20:00", "do_full_scan",   {"bet": True}),
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
        self._last_incremental_near: float = 0          # 近场增量扫描时间戳
        self._last_incremental_far: float = 0           # 远场增量扫描时间戳
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

    def _is_time_match(self, weekday, dom, hour, minute, now: datetime, wide_window=False) -> bool:
        """检查当前时间是否符合调度条件。

        wide_window=False: 2 分钟窗口（正常调度循环用）
        wide_window=True: 只要当前时间 >= 调度时间即匹配（启动追赶用）
        """
        if dom is not None and now.day != dom:
            return False
        if weekday is not None and now.weekday() != weekday:
            return False
        scheduled_total = hour * 60 + minute
        now_total = now.hour * 60 + now.minute
        diff = now_total - scheduled_total
        if wide_window:
            return diff >= 0
        return 0 <= diff <= 2

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

        except SystemExit as e:
            elapsed = time.time() - t0
            err_msg = f"SystemExit({e.code})"
            logger.error("[%s] FAILED after %ds: %s", name, elapsed, err_msg)
            logger.error(traceback.format_exc())
            self._send_alert(name, err_msg)
            return False

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

        logger.info("Step 2/3: Pinnacle 对比 (BB+FB合并)...")
        from src.scrapers.bb_vs_pinnacle import main as compare
        old_argv = sys.argv
        sys.argv = ["bb_vs_pinnacle"]
        try:
            compare()
        finally:
            sys.argv = old_argv
        logger.info("Step 2/3: 完成")

        logger.info("Step 2b/3: Pinnacle 对比 (FB独立)...")
        sys.argv = ["bb_vs_pinnacle",
                     "--input=bb_odds_extracted_FB.json",
                     "--output=bb_vs_pinnacle_comparison_FB.json"]
        try:
            compare()
        finally:
            sys.argv = old_argv
        logger.info("Step 2b/3: 完成")

        logger.info("Step 3/3: +EV 推送 (合并双对比)...")
        from src.report.bb_ev_push import main as push
        old_argv = sys.argv
        sys.argv = ["bb_ev_push", "--no-bet"] if not bet else ["bb_ev_push"]
        try:
            push()
        finally:
            sys.argv = old_argv
        logger.info("Step 3/3: 完成")

    def do_incremental(self, time_window: str = "all"):
        """增量扫描。time_window = "near" | "far" | "all" """
        from src.scrapers.bb_incremental_scanner import run_incremental
        run_incremental(time_window=time_window)

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

    def do_memory_update(self):
        """更新 Claude 记忆库，同步最新系统状态。"""
        from src.core.memory_updater import update_all
        update_all(dry_run=False)

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

    def do_health_check(self):
        """运行系统健康检查，有 WARN/FAIL 时发 DingTalk。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "health_check_system.py")],
            capture_output=True, text=True, cwd=SRC_DIR,
            timeout=120,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        for line in stdout.splitlines():
            logger.info("  %s", line)

        # 检查是否有 FAIL/WARN
        has_fail = "❌" in stdout
        has_warn = "⚠️" in stdout
        if result.returncode != 0 or has_fail or has_warn:
            summary = f"Health Check {'❌ FAIL' if has_fail else '⚠️ WARN'}"
            body = (
                f"**{summary}**\n\n"
                f"```\n{stdout[:1500]}```"
            )
            try:
                send_dingtalk(summary, body)
                logger.info("健康检查告警已发送")
            except Exception as e:
                logger.error("告警发送失败: %s", e)
        else:
            logger.info("健康检查: 全部通过")

    # ------------------------------------------------------------------
    # 启动追赶 — 重启后补执行当天已错过的定时任务
    # ------------------------------------------------------------------
    def _catch_up_missed_tasks(self):
        """守护进程启动时检查今天及昨天错过的定时任务并补执行。

        场景：daemon 在 09:00 重启，08:00 的 full_scan 已被跳过。
        场景：daemon 停机多日，重启后补跑之前错过的每日任务。
        此方法遍历 SCHEDULE，对昨天/今天已过时间点且未执行的任务立即执行一次。
        """
        from datetime import timedelta
        now = datetime.now()
        caught_up = []
        # 最多往前补 2 天（含今天）
        check_dates = [now.date(), (now - timedelta(days=1)).date(), (now - timedelta(days=2)).date()]

        for name, time_str, method_name, kwargs in SCHEDULE:
            for cd in check_dates:
                if self._last_run.get(name) == cd:
                    continue  # 这天已执行过
                dt = datetime(cd.year, cd.month, cd.day, *(int(x) for x in time_str.split()[-1].split(":")))
                if dt > now:
                    continue  # 未来的不补
                weekday, dom, hour, minute = _parse_schedule_time(time_str)
                if dom is not None and cd.day != dom:
                    continue
                if weekday is not None and cd.weekday() != weekday:
                    continue

                method = getattr(self, method_name, None)
                if not method:
                    continue

                logger.info("[追赶] 发现错过的任务 %s (原定 %s %s), 立即执行...", name, cd, time_str)
                self._run_task(name, method, **kwargs)
                self._last_run[name] = cd
                caught_up.append(name)
                break  # 同一任务只补最近一次

        if caught_up:
            logger.info("[追赶] 已完成 %d 个错过的任务: %s", len(caught_up), ", ".join(caught_up))
        else:
            logger.info("[追赶] 无错过的定时任务")

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
            "health_check": self.do_health_check,
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
        logger.info("扫描时段: %02d:00~%02d:00 | 增量: 临场%dmin / 早盘%dmin",
                     SCAN_WINDOW[0], SCAN_WINDOW[1],
                     INCREMENTAL_INTERVAL_NEAR // 60,
                     INCREMENTAL_INTERVAL_FAR // 60)
        logger.info("定时任务: %s", ", ".join(name for name, *_ in SCHEDULE))
        logger.info("dry-run: %s", self.dry_run)
        logger.info("=" * 50)

        if not self.dry_run and not self._is_in_scan_window(datetime.now()):
            logger.info("当前不在扫描时段，等待 %02d:00...", SCAN_WINDOW[0])

        # 启动时追赶今天已错过的定时任务
        if not self.dry_run:
            self._catch_up_missed_tasks()

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

                # 2) 增量扫描 — 双层: 临场15min / 早盘30min
                if self._is_in_scan_window(now):
                    # 临场扫描(24h内): 15分钟间隔
                    elapsed_near = (now - datetime.fromtimestamp(self._last_incremental_near)).total_seconds() if self._last_incremental_near else INCREMENTAL_INTERVAL_NEAR + 1
                    if elapsed_near >= INCREMENTAL_INTERVAL_NEAR:
                        self._run_task("incremental_near", self.do_incremental, time_window="near")
                        self._last_incremental_near = time.time()

                    # 早盘扫描(24-72h): 30分钟间隔
                    elapsed_far = (now - datetime.fromtimestamp(self._last_incremental_far)).total_seconds() if self._last_incremental_far else INCREMENTAL_INTERVAL_FAR + 1
                    if elapsed_far >= INCREMENTAL_INTERVAL_FAR:
                        self._run_task("incremental_far", self.do_incremental, time_window="far")
                        self._last_incremental_far = time.time()

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
    parser.add_argument("--task", help="执行单个任务后退出 (scan|settle|incremental|daily_report|weekly_report|monthly_report|health_check)")
    args = parser.parse_args()

    orch = PipelineOrchestrator(dry_run=args.dry_run)

    if args.task:
        orch.run_once(args.task)
    else:
        orch.run_forever()


if __name__ == "__main__":
    main()
