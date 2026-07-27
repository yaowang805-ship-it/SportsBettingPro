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
import fcntl
import os
import shutil
import signal
import sys
import time
import threading
import traceback
from typing import Optional
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

# 日志清理：删除超过 7 天的轮转日志（保留当前文件，launchd 持有其 fd）
# 不再使用 copy+truncate 轮转（会产生递归文件名），直接依赖 TimedRotatingFileHandler
_now_ts = time.time()
_seven_days_sec = 7 * 86400
for _lf in sorted(LOG_DIR.iterdir()):
    if not _lf.is_file():
        continue
    _age_sec = _now_ts - _lf.stat().st_mtime
    # 跳过当前活跃的日志文件（pipeline_daemon.log 等）
    if _lf.name == "pipeline_daemon.log":
        continue
    if _age_sec > _seven_days_sec:
        _lf.unlink()
        print(f"  🗑️ 清理旧日志: {_lf.name} ({(int(_age_sec / 86400))}天前)")

SCAN_WINDOW = (7, 22)              # 07:00 ~ 22:00
INCREMENTAL_INTERVAL_NEAR = 600    # 10 分钟 — 24h内临场(赔率波动大)
INCREMENTAL_INTERVAL_FAR = 3600    # 60 分钟 — 24-72h早盘(赔率几乎不动)
CHECK_INTERVAL = 30                # 调度循环检查间隔（秒）

# 定时任务表：(名称, HH:MM, 处理函数, 参数字典)
SCHEDULE = [
    ("health_check",       "06:55", "do_health_check", {}),
    ("full_scan_morning",  "07:00", "do_full_scan",  {"bet": True}),
    ("settle_morning",     "08:30", "do_settle",      {}),
    ("daily_report",       "09:00", "do_daily_report",{}),
    ("memory_update",      "09:05", "do_memory_update", {}),
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
        self._last_incremental_near: Optional[float] = None
        self._last_incremental_far: Optional[float] = None
        self._last_scan_success: float = 0             # 最后一次成功完成的时间戳
        self._scan_failure_count: int = 0              # 连续失败计数
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
        # 原子锁: 使用 fcntl.flock 避免 TOCTOU 竞态
        self._lock_fd = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("另一实例正在运行，退出")
            sys.exit(0)
        # 写入当前 PID
        self._lock_fd.seek(0)
        self._lock_fd.truncate()
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()
        logger.info("PID %s 已锁定 %s", os.getpid(), LOCK_FILE)

    def _cleanup_lock(self):
        if hasattr(self, "_lock_fd"):
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            self._lock_fd.close()
        if LOCK_FILE.exists():
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
    def _run_task(self, name: str, task_callable, background: bool = False, **kwargs) -> bool:
        """执行任务。settle/report 类慢任务在后台线程运行，不阻塞主循环。

        Args:
            background: True = 后台线程（settle/report），False = 同步运行（scan）
        """
        if name in self._active_tasks:
            logger.warning("[%s] 上一轮还未完成，跳过本次调度", name)
            return False

        if background:
            # 后台线程运行，不阻塞主循环
            def _bg_runner():
                self._active_tasks.add(f"{name}_bg")
                logger.info("[%s] ====== START (后台) ======", name)
                t0 = time.time()
                try:
                    task_callable(**kwargs)
                    elapsed = time.time() - t0
                    logger.info("[%s] ====== DONE (后台, %ds) ======", name, elapsed)
                except Exception as e:
                    elapsed = time.time() - t0
                    logger.error("[%s] FAILED after %ds: %s", name, elapsed, e)
                    logger.error(traceback.format_exc())
                    self._send_alert(name, str(e))
                finally:
                    self._active_tasks.discard(f"{name}_bg")
            t = threading.Thread(target=_bg_runner, daemon=True)
            t.start()
            return True

        self._active_tasks.add(name)
        logger.info("[%s] ====== START ======", name)
        t0 = time.time()

        try:
            if self.dry_run:
                logger.info("[%s] (dry-run) 跳过实际执行", name)
            else:
                task_callable(**kwargs)
            elapsed = time.time() - t0
            logger.info("[%s] ====== DONE (%ds) ======", name, elapsed)
            if "incremental" in name:
                self._mark_scan_ok()
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
        # 设置推送标签（保存/恢复避免影响增量扫描）
        _prev_label = os.environ.get("PUSH_LABEL", "")
        os.environ["PUSH_LABEL"] = "每日定时全量推送"

        try:
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
        finally:
            os.environ["PUSH_LABEL"] = _prev_label

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
        try:
            result = subprocess.run(
                [sys.executable, str(SRC_DIR / "health_check_system.py")],
                capture_output=True, text=True, cwd=SRC_DIR,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            logger.error("健康检查超时 (120s)")
            self._send_alert("health_check", "超时 (120s)")
            return

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        for line in stdout.splitlines():
            logger.info("  %s", line)

        # 只在真正 FAIL 时推送钉钉，WARN 只记录日志
        has_fail = "❌" in stdout
        if result.returncode != 0 or has_fail:
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
        """守护进程启动时检查今天及之前错过的 git_commit 并补执行。

        其他任务（扫描/结算/报告）不追赶：重启后立即执行会导致数据重复。
        只有 git_commit 需要追赶，确保未提交的改动不丢失。
        """
        from datetime import timedelta
        now = datetime.now()
        # 最多往前补 2 天（含今天）
        check_dates = [now.date(), (now - timedelta(days=1)).date(), (now - timedelta(days=2)).date()]

        for name, time_str, method_name, kwargs in SCHEDULE:
            if name != "git_commit":
                continue
            for cd in check_dates:
                if self._last_run.get(name) == cd:
                    continue
                dt = datetime(cd.year, cd.month, cd.day, *(int(x) for x in time_str.split()[-1].split(":")))
                if dt > now:
                    continue
                method = getattr(self, method_name, None)
                if not method:
                    continue
                logger.info("[追赶] git_commit 错过的提交 (%s), 立即执行...", cd)
                self._run_task(name, method, background=True, **kwargs)
                self._last_run[name] = cd
                break

    # ------------------------------------------------------------------
    # 主循环单次迭代
    # ------------------------------------------------------------------
    def _tick(self):
        """主循环的一次迭代：检查定时任务 + 增量扫描。"""
        now = datetime.now()

        # 1) 定时任务 (settle/report → 后台线程)
        _BACKGROUND_TASKS = {"settle", "report", "git_commit", "memory_update"}
        for name, time_str, method_name, kwargs in SCHEDULE:
            weekday, dom, hour, minute = _parse_schedule_time(time_str)
            if not self._is_time_match(weekday, dom, hour, minute, now):
                continue
            if not self._should_run_wall_clock(name, now):
                continue
            method = getattr(self, method_name, None)
            if not method:
                continue
            is_settle = "settle" in name
            is_bg = any(t in name for t in _BACKGROUND_TASKS) or is_settle
            if is_settle and self._active_tasks_settle():
                continue  # 已有结算在跑, 等下一轮
            self._run_task(name, method, background=is_bg, **kwargs)
            self._last_run[name] = now.date()

        # 2) 增量扫描 — 双层: 临场10min / 早盘60min
        if self._is_in_scan_window(now):
            if self._last_incremental_near is None:
                self._last_incremental_near = time.time()
            elapsed_near = (now - datetime.fromtimestamp(self._last_incremental_near)).total_seconds()
            if elapsed_near >= INCREMENTAL_INTERVAL_NEAR:
                self._last_incremental_near = time.time()  # START 时刻记录
                self._run_task("incremental_near", self.do_incremental, time_window="near")

            if self._last_incremental_far is None:
                self._last_incremental_far = time.time()
            elapsed_far = (now - datetime.fromtimestamp(self._last_incremental_far)).total_seconds()
            if elapsed_far >= INCREMENTAL_INTERVAL_FAR:
                self._last_incremental_far = time.time()  # START 时刻记录
                self._run_task("incremental_far", self.do_incremental, time_window="far")

        # 3) 自检看门狗
        self._watchdog()

    def _active_tasks_settle(self) -> bool:
        """检查是否有结算任务正在运行（比 str(set) 更可靠）。"""
        return any("settle" in t for t in self._active_tasks)

    def _watchdog(self):
        """自检看门狗：检测增量扫描是否停滞，Pinnacle 连接是否异常。"""
        now = time.time()
        if not self._is_in_scan_window(datetime.now()):
            return  # 非扫描时段不检查

        # 检查增量扫描是否按时运行（允许 1.5 倍间隔容忍度）
        near_elapsed = now - self._last_incremental_near if self._last_incremental_near else 0
        far_elapsed = now - self._last_incremental_far if self._last_incremental_far else 0
        near_timeout = INCREMENTAL_INTERVAL_NEAR * 1.5
        far_timeout = INCREMENTAL_INTERVAL_FAR * 1.5

        warnings = []
        if self._last_incremental_near and near_elapsed > near_timeout:
            warnings.append(f"near扫描停滞 {near_elapsed/60:.0f}分钟(预期{INCREMENTAL_INTERVAL_NEAR/60:.0f}min)")
        if self._last_incremental_far and far_elapsed > far_timeout:
            warnings.append(f"far扫描停滞 {far_elapsed/60:.0f}分钟(预期{INCREMENTAL_INTERVAL_FAR/60:.0f}min)")

        if warnings:
            logger.warning("🐕 看门狗: %s", "; ".join(warnings))
            # 连续两次告警才推送（只发一次，走冷却）
            self._scan_failure_count += 1
            if self._scan_failure_count >= 2:
                self._send_alert("scan_watchdog", "; ".join(warnings))
                self._scan_failure_count = 0  # 重置，等下次触发
        else:
            self._scan_failure_count = 0  # 恢复正常

    def _mark_scan_ok(self):
        """标记增量扫描成功完成（供 _run_task 钩子）。"""
        self._last_scan_success = time.time()
        self._scan_failure_count = 0

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

        # 启动时设置增量扫描初始时间为 (now - interval + 60s)，首次 tick 即可触发
        # （避免与全量扫描同时触发，但不会等待完整的 10 分钟周期）
        now = time.time()
        self._last_incremental_near = now - INCREMENTAL_INTERVAL_NEAR + 60
        self._last_incremental_far = now - INCREMENTAL_INTERVAL_FAR + 60

        # 启动时追赶今天已错过的定时任务
        if not self.dry_run:
            self._catch_up_missed_tasks()

        try:
            while self._running:
                try:
                    self._tick()
                except Exception as e:
                    logger.error("主循环异常: %s", e)
                    logger.error(traceback.format_exc())
                    self._send_alert("main_loop_crash", str(e))
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
