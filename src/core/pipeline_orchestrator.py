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
import json
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
INCREMENTAL_NEAR_INTERVAL = 1200   # 20 分钟 — 24h内近场比赛 (赔率变动快, 高频抓机会)
INCREMENTAL_FAR_INTERVAL = 3600    # 60 分钟 — 24-72h早盘比赛 (赔率相对稳定)
CHECK_INTERVAL = 30                # 调度循环检查间隔（秒）

# 定时任务表：(名称, HH:MM, 处理函数, 参数字典)
SCHEDULE = [
    ("health_check",       "06:55", "do_health_check", {}),
    ("full_scan_morning",  "07:00", "do_full_scan",  {"bet": True}),
    ("settle_morning",     "08:30", "do_settle",      {}),
    ("daily_report",       "09:00", "do_daily_report",{}),
    ("memory_update",      "09:05", "do_memory_update", {}),
    ("daily_cleanup",      "09:10", "do_cleanup",      {}),  # 指纹+临时文件清理
    ("evolve_daily",       "09:15", "do_evolve_daily", {}),  # V4 BB溢价累积
    # 周报：周日 21:00
    ("evolve_weekly",      "Mon 06:07", "do_evolve_weekly", {}),  # V4 每周进化(结算反馈+溢价重算)
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

    def _startup_integrity_check(self):
        """启动前自检: 模块导入 + 文件格式 + 代码版本。

        任何失败 → 拒绝启动 + 钉钉告警。防止旧代码/坏数据破坏运行。
        """
        from config.settings import DATA_DIR, safe_load_json
        errors = []

        # 1) 模块导入检查 (防代码bug)
        CRITICAL_MODULES = [
            "config.weight_matrix_v4",
            "src.scrapers.bb_api_fetcher",
            "src.scrapers.bb_vs_pinnacle",
            "src.scrapers.pinnacle_api",
            "src.scrapers.pinnacle_league_map",
            "src.report.bb_ev_push",
        ]
        import importlib
        for mod_name in CRITICAL_MODULES:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                msg = f"[自检] import {mod_name} 失败: {e}"
                logger.error(msg)
                errors.append(msg)

        # 2) 关键文件格式检查 (防数据结构变更)
        critical_files = [
            (DATA_DIR / "pinnacle_league_structure.json", dict, 50),
            (DATA_DIR / "team_name_map.json", dict, 100),
            (DATA_DIR / "league_keywords.json", dict, 10),
        ]
        for fpath, expected_type, min_count in critical_files:
            if not fpath.exists():
                errors.append(f"[自检] {fpath.name} 不存在")
                continue
            data = safe_load_json(fpath)
            if data is None or not isinstance(data, expected_type) or len(data) < min_count:
                errors.append(f"[自检] {fpath.name} 损坏 (type={type(data).__name__}, len={len(data) if data else 0})")

        # 3) Pinnacle 结构格式验证 (防 flat/nested 混搭导致的崩溃)
        pin_struct = safe_load_json(DATA_DIR / "pinnacle_league_structure.json")
        if pin_struct and isinstance(pin_struct, dict):
            flat_count = sum(1 for v in pin_struct.values() if isinstance(v, dict) and "name" in v and isinstance(v["name"], str))
            if flat_count > 0:
                errors.append(f"[自检] Pinnacle结构含{flat_count}个flat格式运动(应为nested), 请运行 normalize")

        if errors:
            for e in errors:
                logger.error(e)
            self._send_alert("startup_check_failed", "\n".join(errors[:5]))
            raise SystemExit(f"启动自检失败: {len(errors)} 项不通过")

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

            # 增量扫描重试: 瞬时故障(API超时/网络抖动)不应错过整个窗口
            if "incremental" in name:
                for attempt in range(1, 4):
                    wait = 30 * attempt
                    logger.info("[%s] 重试 %d/3 (%ds后)...", name, attempt, wait)
                    time.sleep(wait)
                    try:
                        task_callable(**kwargs)
                        elapsed2 = time.time() - t0
                        logger.info("[%s] ====== DONE (重试%d, %ds) ======", name, attempt, elapsed2)
                        self._mark_scan_ok()
                        return True
                    except Exception as e2:
                        logger.error("[%s] 重试%d也失败: %s", name, attempt, e2)

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

            logger.info("Step 2c/3: 辅助数据源对比 (the-odds-api)...")
            try:
                from src.scrapers.odds_api_compare import run_all
                run_all()
            except Exception as e:
                logger.warning("辅助对比跳过: %s", e)
            logger.info("Step 2c/3: 完成")

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

    def do_cleanup(self):
        """每日清理：过期指纹 + 旧日志 + 临时文件。"""
        # 1. 清理过期指纹
        try:
            from config.database import load_fingerprints, save_fingerprints
            from datetime import date
            today = date.today().strftime("%Y-%m-%d")
            fps = load_fingerprints()
            expired = [fp for fp in fps if fp.split("|")[-1] < today]
            for fp in expired: del fps[fp]
            if expired:
                save_fingerprints(fps)
                logger.info("清理 %d 条过期指纹", len(expired))
        except Exception as e:
            logger.warning("指纹清理失败: %s", e)

        # 2. 清理旧的临时文件
        try:
            import os, time
            now = time.time()
            for f in (SRC_DIR / "data" / "storage").glob("*.tmp"):
                if now - f.stat().st_mtime > 86400:
                    f.unlink()
            for f in (SRC_DIR / "data" / "storage").glob("push_staging*"):
                if now - f.stat().st_mtime > 86400:
                    f.unlink()
        except Exception as e:
            logger.warning("临时文件清理失败: %s", e)

    def do_evolve_daily(self):
        """V4 每日进化: BB 溢价累积。"""
        from src.evolve.v4_evolver import evolve_daily
        evolve_daily()

    def do_evolve_weekly(self):
        """V4 每周进化: 结算反馈 + 溢价重算 + 健康检查。"""
        from src.evolve.v4_evolver import evolve_weekly
        evolve_weekly()

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
        """守护进程启动时检查今天及之前错过的任务并补执行。

        git_commit: 总是追赶（确保改动不丢失）
        settle: 追赶 2 小时前的，避免重启瞬间重复结算
        扫描/报告: 不追赶（会重复推送）
        """
        from datetime import timedelta
        now = datetime.now()
        check_dates = [now.date(), (now - timedelta(days=1)).date(), (now - timedelta(days=2)).date()]

        for name, time_str, method_name, kwargs in SCHEDULE:
            # git_commit 总是追赶
            # settle 只追赶 2 小时以上的
            if name != "git_commit" and "settle" not in name:
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
                # 结算追赶保护：只追 2 小时前的，避免重启时重复结算
                if "settle" in name:
                    settle_deadline = datetime(cd.year, cd.month, cd.day) + timedelta(hours=int(time_str.split()[-1].split(":")[0]) + 2,
                                 minutes=int(time_str.split()[-1].split(":")[1]))
                    if now < settle_deadline:
                        continue  # 还不够晚，跳过
                logger.info("[追赶] %s 错过 (%s %s), 立即执行...", name, cd, time_str)
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
        _BACKGROUND_TASKS = {"settle", "report", "git_commit", "memory_update", "evolve", "incremental"}
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

        # 2) 增量扫描 — 双层频率
        #    近场 (24h内):  30分钟 — 赔率变动快，需要高频
        #    早盘 (24-72h): 2小时  — 赔率相对稳定
        if self._is_in_scan_window(now):
            import random as _random
            _jitter = lambda base: base * (0.85 + _random.random() * 0.3)

            # Near scan: 30min interval, 24h window
            if self._last_incremental_near is None:
                self._last_incremental_near = time.time()
            if (now - datetime.fromtimestamp(self._last_incremental_near)).total_seconds() >= _jitter(INCREMENTAL_NEAR_INTERVAL):
                self._last_incremental_near = time.time()
                self._run_task("incremental_near", self.do_incremental, time_window="near")

            # Far scan: 2hr interval, 24-72h window
            if self._last_incremental_far is None:
                self._last_incremental_far = time.time()
            if (now - datetime.fromtimestamp(self._last_incremental_far)).total_seconds() >= _jitter(INCREMENTAL_FAR_INTERVAL):
                self._last_incremental_far = time.time()
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

        # 检查近场增量扫描是否按时运行（允许 1.5 倍间隔容忍度）
        incr_near_elapsed = now - self._last_incremental_near if self._last_incremental_near else 0
        incr_near_timeout = INCREMENTAL_NEAR_INTERVAL * 1.5
        incr_far_elapsed = now - self._last_incremental_far if self._last_incremental_far else 0
        incr_far_timeout = INCREMENTAL_FAR_INTERVAL * 1.5

        warnings = []
        if self._last_incremental_near and incr_near_elapsed > incr_near_timeout:
            warnings.append(f"近场扫描停滞 {incr_near_elapsed/60:.0f}分钟(预期{INCREMENTAL_NEAR_INTERVAL/60:.0f}min)")
        if self._last_incremental_far and incr_far_elapsed > incr_far_timeout:
            warnings.append(f"早盘扫描停滞 {incr_far_elapsed/60:.0f}分钟(预期{INCREMENTAL_FAR_INTERVAL/60:.0f}min)")

        if warnings:
            logger.warning("🐕 看门狗(扫描): %s", "; ".join(warnings))
            self._scan_failure_count += 1
            if self._scan_failure_count >= 2:
                self._send_alert("scan_watchdog", "; ".join(warnings))
                self._scan_failure_count = 0
        else:
            self._scan_failure_count = 0

        # 2) 结算看门狗：检测超时未结算的投注
        try:
            pf_path = SRC_DIR / "data" / "storage" / "virtual_portfolio.json"
            if pf_path.exists():
                pf = json.loads(pf_path.read_text())
                pending = pf.get("pending_bets", [])
                now_ts = time.time()
                stale_48h = sum(1 for b in pending if b.get("commence_time", 0) < now_ts - 172800)
                stale_24h = sum(1 for b in pending if 86400 < (now_ts - b.get("commence_time", 0)) <= 172800)
                if stale_48h > 0:
                    logger.warning("🐕 看门狗(结算): %d笔超48h未结算!", stale_48h)
                    # 告警冷却: 每4小时只发一次
                    last_alert = self._alert_cooldown.get("settle_watchdog", 0)
                    if time.time() - last_alert > 14400:
                        self._send_alert("settle_watchdog", f"{stale_48h}笔投注超48小时未结算")
                        self._alert_cooldown["settle_watchdog"] = time.time()
                elif stale_24h > 3:
                    logger.warning("🐕 看门狗(结算): %d笔超24h未结算", stale_24h)
        except Exception:
            pass

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
            "evolve_daily": self.do_evolve_daily,
            "evolve_weekly": self.do_evolve_weekly,
        }
        func = task_map.get(task_name)
        if not func:
            logger.error("未知任务: %s", task_name)
            return
        self._run_task(task_name, func, **kwargs)

    def run_forever(self):
        """守护进程主循环。"""
        self._ensure_single_instance()
        self._startup_integrity_check()
        logger.info("=" * 50)
        logger.info("Pipeline Orchestrator 启动")
        logger.info("扫描时段: %02d:00~%02d:00 | 近场: %dmin(24h) 早盘: %dmin(24-72h)",
                     SCAN_WINDOW[0], SCAN_WINDOW[1],
                     INCREMENTAL_NEAR_INTERVAL // 60,
                     INCREMENTAL_FAR_INTERVAL // 60)
        logger.info("定时任务: %s", ", ".join(name for name, *_ in SCHEDULE))
        logger.info("dry-run: %s", self.dry_run)
        logger.info("=" * 50)

        if not self.dry_run and not self._is_in_scan_window(datetime.now()):
            logger.info("当前不在扫描时段，等待 %02d:00...", SCAN_WINDOW[0])

        # 启动时设置增量扫描初始时间为 (now - interval + 60s)，首次 tick 即可触发
        # （避免与全量扫描同时触发，但不会等待完整的周期）
        now = time.time()
        self._last_incremental_near = now - INCREMENTAL_NEAR_INTERVAL + 60
        self._last_incremental_far = now - INCREMENTAL_FAR_INTERVAL + 60

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
