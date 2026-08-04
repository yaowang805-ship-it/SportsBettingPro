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
INCREMENTAL_INTERVAL = 1800  # 30 分钟 — 0-72h全量对比 (BB+Pin同时拉, 无条件)
CHECK_INTERVAL = 30                # 调度循环检查间隔（秒）

# 定时任务表：(名称, HH:MM, 处理函数, 参数字典)
SCHEDULE = [
    ("self_repair",       "06:45", "do_self_repair", {}),       # 自检+自动修复: 锁文件/缓存/指纹/连通性
    ("time_calibration",  "06:50", "do_time_calibration", {}),  # 时间校准: BB/Pin/系统时钟对齐
    ("health_check",       "06:55", "do_health_check", {}),
    ("full_scan_morning",  "07:00", "do_full_scan",  {"bet": True}),
    ("settle_morning",     "08:30", "do_settle",      {}),
    ("daily_report",       "09:00", "do_daily_report",{}),
    ("memory_update",      "09:05", "do_memory_update", {}),
    ("daily_cleanup",      "09:10", "do_cleanup",      {}),  # 指纹+临时文件清理
    ("evolve_daily",       "09:15", "do_evolve_daily", {}),  # V4 BB溢价累积
    ("download_data",      "09:20", "do_download_data", {}), # V4.5: 自动下载新数据源
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
        self._last_incremental: Optional[float] = None
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
            (DATA_DIR / "pinnacle_league_structure.json", dict, 5),
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

        from datetime import timezone as _tz, timedelta as _td
        bj_time = datetime.now(_tz(_td(hours=8))).strftime('%m/%d %H:%M')
        body = (
            f"**Pipeline Alert**\n\n"
            f"任务: {task_name}\n"
            f"时间: {bj_time} (北京时间)\n"
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
        """每日清理：过期记录 + 旧日志 + 临时文件 + 备份关键数据。"""
        # 0. 备份关键映射文件 (防止误删)
        import shutil as _shutil
        backup_dir = SRC_DIR / "data" / "manual_backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for fname in ["league_keywords.json", "team_name_map.json", "league_tiers.json"]:
            src = SRC_DIR / "data" / "storage" / fname
            dst = backup_dir / fname
            if src.exists():
                _shutil.copy2(src, dst)
        # 1. 清理过期记录 (SQLite指纹 + 文件去重)
        import json as _json, time as _time
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        try:
            from config.database import load_fingerprints, save_fingerprints
            fps = load_fingerprints()
            expired = [fp for fp in fps if fp.split("|")[-1] < today]
            for fp in expired: del fps[fp]
            if expired:
                save_fingerprints(fps)
                logger.info("清理 %d 条过期SQLite指纹", len(expired))
        except Exception as e:
            logger.warning("SQLite指纹清理失败: %s", e)

        # 清理文件去重中的过期记录
        try:
            opps_file = SRC_DIR / "data" / "storage" / "pushed_opportunities.json"
            if opps_file.exists():
                opps = _json.loads(opps_file.read_text())
                clean = {}
                for key, val in opps.items():
                    # key格式: ...|epoch[:10], 最后一段是epoch前10位(日期)
                    ts = val.get("ts", 0)
                    if ts > _time.time() - 86400 * 3:  # 保留3天
                        clean[key] = val
                if len(clean) < len(opps):
                    opps_file.write_text(_json.dumps(clean, ensure_ascii=False))
                    logger.info("清理 %d 条过期推送记录", len(opps) - len(clean))
        except Exception as e:
            logger.warning("推送记录清理失败: %s", e)

        # 2. 清理旧的临时文件 + 快照 + 日志
        try:
            import os as _os, time as _time, shutil as _shutil
            now = _time.time()
            # 临时文件
            for f in (SRC_DIR / "data" / "storage").glob("*.tmp"):
                if now - f.stat().st_mtime > 86400:
                    f.unlink()
            # Push staging
            for f in (SRC_DIR / "data" / "storage").glob("push_staging*"):
                if now - f.stat().st_mtime > 86400:
                    f.unlink()
            # BB快照 (保留最新2个)
            snap_files = sorted((SRC_DIR / "data" / "storage").glob("bb_odds_snapshot*.json"),
                                key=lambda x: x.stat().st_mtime, reverse=True)
            for f in snap_files[2:]:
                f.unlink()
            # Pin快照
            for f in (SRC_DIR / "data" / "storage").glob(".pin_snapshot_*.json"):
                if now - f.stat().st_mtime > 86400 * 3:
                    f.unlink()
            # 对比文件备份
            for f in (SRC_DIR / "data" / "storage").glob("*.bak"):
                if now - f.stat().st_mtime > 86400:
                    f.unlink()
            # 日志轮转: 7天以上的压缩归档
            log_dir = SRC_DIR / "data" / "logs"
            for lf in sorted(log_dir.glob("pipeline_daemon.log*")):
                age = now - lf.stat().st_mtime
                if age > 86400 * 7 and not lf.name.endswith('.gz'):
                    import gzip
                    gz_path = lf.with_suffix(lf.suffix + '.gz')
                    with open(lf, 'rb') as fi, gzip.open(gz_path, 'wb') as fo:
                        _shutil.copyfileobj(fi, fo)
                    lf.unlink()
                    logger.info("日志归档: %s", gz_path.name)
        except Exception as e:
            logger.warning("临时文件清理失败: %s", e)

    def do_download_data(self):
        """V4.5: 每日尝试从已知数据源下载新的历史赔率数据。"""
        import subprocess, os
        logger.info("📥 自动下载历史数据...")
        script = SRC_DIR / "scripts" / "download_v4_data.sh"
        if script.exists():
            try:
                result = subprocess.run(
                    ["bash", str(script)],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(SRC_DIR)
                )
                for line in (result.stdout or "").splitlines()[-5:]:
                    if line.strip():
                        logger.info(f"  {line.strip()}")
                if result.returncode != 0:
                    logger.warning("下载脚本返回非0: %d", result.returncode)
            except subprocess.TimeoutExpired:
                logger.warning("下载脚本超时")
            except Exception as e:
                logger.warning("下载脚本失败: %s", e)

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

    def do_self_repair(self):
        """V4.5: 自检+自动修复 — 在每天任务开始前修复常见问题。

        检查项: 锁文件/缓存/指纹DB/API连通性/磁盘空间
        修复项: 清理僵尸锁/过期缓存/损坏指纹/磁盘告警
        """
        import shutil, json as _json
        from config.settings import send_dingtalk

        logger.info("🔧 自检+自动修复开始...")
        issues_found = []
        issues_fixed = []

        # 1) 清理僵尸锁文件 (进程已死但锁还在)
        lock_path = SRC_DIR / "data" / "storage" / ".pipeline_daemon.lock"
        if lock_path.exists():
            try:
                lock_pid = int(lock_path.read_text().strip())
                try: os.kill(lock_pid, 0)  # 检查进程是否存在
                except OSError:  # 进程不存在 → 僵尸锁
                    lock_path.unlink()
                    issues_fixed.append("僵尸锁文件已清理")
            except: pass

        # 2) 清理 __pycache__ (防止 .pyc 导致跑旧代码)
        pyc_count = 0
        for pyc in SRC_DIR.rglob("__pycache__"):
            try:
                shutil.rmtree(pyc)
                pyc_count += 1
            except: pass
        if pyc_count > 0:
            issues_fixed.append(f"清理{pyc_count}个__pycache__目录")

        # 3) 指纹DB完整性检查
        try:
            from config.database import load_fingerprints, save_fingerprints
            fps = load_fingerprints()
            # 检查是否有异常空指纹或过期指纹 (>30天)
            bad_fps = 0
            import time as _t
            for fp, val in list(fps.items()):
                if isinstance(val, dict):
                    ts = val.get("ts", 0)
                    if ts > 0 and _t.time() - ts > 30 * 86400:
                        del fps[fp]; bad_fps += 1
                elif not fp or len(fp) < 10:
                    del fps[fp]; bad_fps += 1
            if bad_fps > 0:
                save_fingerprints(fps)
                issues_fixed.append(f"清理{bad_fps}条损坏/过期指纹")
        except Exception as e:
            issues_found.append(f"指纹DB异常: {e}")

        # 4) API 连通性检查
        try:
            from src.scrapers.pinnacle_api import check_pinnacle_connectivity
            if not check_pinnacle_connectivity(verbose=False):
                issues_found.append("Pinnacle API 不可达")
        except Exception as e:
            issues_found.append(f"Pinnacle 连通性检查失败: {e}")

        try:
            from src.scrapers.bb_api_fetcher import _ensure_token
            token = _ensure_token()
            if not token:
                issues_found.append("BB API Token 缺失")
        except Exception as e:
            issues_found.append(f"BB Token 检查失败: {e}")

        try:
            from config.settings import send_dingtalk as _sd
            if not _sd("系统自检", "SportsBettingPro 自检消息"):
                issues_found.append("钉钉推送不可用")
        except: pass

        # 5) 磁盘空间检查
        try:
            usage = shutil.disk_usage(SRC_DIR)
            free_gb = usage.free / (1024**3)
            if free_gb < 1:
                issues_found.append(f"磁盘空间不足: {free_gb:.1f}GB")
            else:
                logger.info(f"  磁盘: {free_gb:.1f}GB 可用")
        except: pass

        # 汇总 & 上报
        logger.info(f"🔧 自检: {len(issues_fixed)}个修复, {len(issues_found)}个问题")
        if issues_found:
            _sd("系统自检异常", "🔧 自检发现问题:\n" + "\n".join(f"  ❌ {i}" for i in issues_found))
        if issues_fixed:
            logger.info("  已修复: " + "; ".join(issues_fixed))

    def do_time_calibration(self):
        """时间校准: 检查 BB API / Pinnacle / 系统时钟三方时间偏差。

        时间偏差过大会导致:
        - 比赛匹配错位 (BB时间和Pin时间对不上)
        - 指纹日期错乱 (match_date 偏移到前一天/后一天)
        - 增量扫描窗口判断错误

        每天 06:50 执行, 偏差 >60s 发送钉钉告警。
        """
        import time as _time
        from config.settings import send_dingtalk

        logger.info("🕐 时间校准开始...")
        issues = []

        # 1. 系统时间 vs HTTP 时间 (WorldTimeAPI, 无需额外依赖)
        try:
            import urllib.request, json as _json
            req = urllib.request.Request('https://worldtimeapi.org/api/timezone/etc/utc')
            with urllib.request.urlopen(req, timeout=5) as resp:
                wt_data = _json.loads(resp.read())
                utc_time = wt_data.get('unixtime', 0)
                sys_time = _time.time()
                http_offset = abs(sys_time - utc_time)
                logger.info(f"  系统时钟 vs WorldTimeAPI: 偏差 {http_offset:.1f}s")
                if http_offset > 60:
                    issues.append(f"系统时钟偏差 {http_offset:.0f}s (vs WorldTimeAPI)")
        except Exception as e:
            logger.warning(f"  HTTP 时间查询失败: {e}")

        # 2. BB / FB API 连通性 & 延迟
        try:
            from src.scrapers.bb_api_fetcher import api_post
            t0 = _time.time()
            resp = api_post('/api/v1/sports', {})  # Light endpoint
            bb_latency = _time.time() - t0
            logger.info(f"  BB API 延迟: {bb_latency:.1f}s (status={'ok' if resp else 'fail'})")
            if bb_latency > 10:
                issues.append(f"BB API 延迟 {bb_latency:.0f}s (>10s)")
            if resp is None:
                issues.append("BB API 不可达")
        except Exception as e:
            logger.warning(f"  BB API 连接失败: {e}")
            issues.append(f"BB API 连接失败: {str(e)[:50]}")

        # 3. Pinnacle API 时间（从 league 数据推断）
        try:
            from src.scrapers.pinnacle_api import api_get as pin_get
            t0 = _time.time()
            resp = pin_get('/0.1/leagues/29')  # Football sport, light endpoint
            pin_time = _time.time()
            pin_latency = pin_time - t0
            logger.info(f"  Pinnacle API 延迟: {pin_latency:.1f}s")
            if pin_latency > 10:
                issues.append(f"Pinnacle API 延迟 {pin_latency:.0f}s (>10s)")
        except Exception as e:
            logger.warning(f"  Pinnacle 连接失败: {e}")

        # 4. 汇总 & 告警
        if issues:
            msg = "🕐 时间校准异常:\n" + "\n".join(f"  - {i}" for i in issues)
            logger.warning(msg)
            send_dingtalk("时间校准异常", msg)
        else:
            logger.info("🕐 时间校准: 全部正常 ✅")

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

        # V4.5: 结算追赶只跑最近错过的一个, 防多个结算同时运行
        started_settle = False
        for name, time_str, method_name, kwargs in SCHEDULE:
            # git_commit 总是追赶
            # settle 只追赶 2 小时以上的, 且一次只追一个
            if name != "git_commit" and "settle" not in name:
                continue
            if "settle" in name and started_settle:
                continue  # 已有结算在追, 跳过其他的
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
                    started_settle = True
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

        # 2) 增量扫描 — 统一15分钟, 0-72h全量 (BB+Pin同时拉, 指纹去重防重复)
        if self._is_in_scan_window(now):
            import random as _random
            _jitter = lambda base: base * (0.85 + _random.random() * 0.3)

            if self._last_incremental is None:
                self._last_incremental = time.time()
            if (now - datetime.fromtimestamp(self._last_incremental)).total_seconds() >= _jitter(INCREMENTAL_INTERVAL):
                self._last_incremental = time.time()
                self._run_task("incremental", self.do_incremental, time_window="all")

        # 3) 自检看门狗
        self._watchdog()

        # 4) V4.4: 代码热更新检测 — 源文件变更后自动重启，防止跑旧代码
        self._check_code_changes()

    def _active_tasks_settle(self) -> bool:
        """检查是否有结算任务正在运行（比 str(set) 更可靠）。"""
        return any("settle" in t for t in self._active_tasks)

    # ------------------------------------------------------------------
    # V4.4: 代码热更新 — 检测源文件变更后自动重启
    # ------------------------------------------------------------------
    _CODE_MTIME_SNAPSHOT: dict = {}
    _CODE_CHECK_INTERVAL = 300  # 每 5 分钟检查一次

    def _snapshot_code_mtimes(self):
        """记录 src/ 下所有 .py 文件的修改时间。"""
        src_dir = SRC_DIR / "src"
        mtimes = {}
        for f in src_dir.rglob("*.py"):
            if '__pycache__' not in str(f):
                try:
                    mtimes[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        self._CODE_MTIME_SNAPSHOT = mtimes
        logger.info("📸 代码快照: %d 个源文件", len(mtimes))

    def _check_code_changes(self):
        """检测源文件是否被修改，如有则自动重启。"""
        now = time.time()
        if not hasattr(self, '_last_code_check'):
            self._last_code_check = now
            self._snapshot_code_mtimes()
            return
        if now - self._last_code_check < self._CODE_CHECK_INTERVAL:
            return
        self._last_code_check = now

        changed = []
        for path_str, old_mtime in self._CODE_MTIME_SNAPSHOT.items():
            try:
                new_mtime = Path(path_str).stat().st_mtime
                if new_mtime > old_mtime + 1:  # 1秒容忍度
                    changed.append(Path(path_str).name)
            except OSError:
                pass

        if changed:
            logger.warning("🔄 检测到 %d 个源文件变更: %s... 自动重启", len(changed), ", ".join(changed[:5]))
            # 释放锁文件 → exit(42) → launchd KeepAlive 自动重启
            lock_path = SRC_DIR / "data" / "storage" / ".pipeline_daemon.lock"
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except OSError:
                pass
            logger.info("🔄 锁文件已释放, 退出等待 launchd 重启...")
            os._exit(42)  # 特殊退出码, launchd KeepAlive 会自动重启

    def _watchdog(self):
        """自检看门狗：检测增量扫描是否停滞，Pinnacle 连接是否异常。"""
        now = time.time()
        if not self._is_in_scan_window(datetime.now()):
            return  # 非扫描时段不检查

        # 检查增量扫描是否按时运行（允许 1.5 倍间隔容忍度）
        incr_elapsed = now - self._last_incremental if self._last_incremental else 0
        incr_timeout = INCREMENTAL_INTERVAL * 1.5

        warnings = []
        if self._last_incremental and incr_elapsed > incr_timeout:
            warnings.append(f"增量扫描停滞 {incr_elapsed/60:.0f}分钟(预期{INCREMENTAL_INTERVAL/60:.0f}min)")

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
                    # V4.5: 日志也加冷却 (30min), 防每秒刷屏
                    last_log = self._alert_cooldown.get("settle_watchdog_log", 0)
                    if time.time() - last_log > 1800:
                        logger.warning("🐕 看门狗(结算): %d笔超48h未结算!", stale_48h)
                        self._alert_cooldown["settle_watchdog_log"] = time.time()
                    # 告警冷却: 每4小时只发一次
                    last_alert = self._alert_cooldown.get("settle_watchdog", 0)
                    if time.time() - last_alert > 14400:
                        self._send_alert("settle_watchdog", f"{stale_48h}笔投注超48小时未结算")
                        self._alert_cooldown["settle_watchdog"] = time.time()
                elif stale_24h > 3:
                    last_log = self._alert_cooldown.get("settle_watchdog_log", 0)
                    if time.time() - last_log > 1800:
                        logger.warning("🐕 看门狗(结算): %d笔超24h未结算", stale_24h)
                        self._alert_cooldown["settle_watchdog_log"] = time.time()
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
        logger.info("扫描时段: %02d:00~%02d:00 | 增量: %dmin (0-72h全量)",
                     SCAN_WINDOW[0], SCAN_WINDOW[1],
                     INCREMENTAL_INTERVAL // 60)
        logger.info("定时任务: %s", ", ".join(name for name, *_ in SCHEDULE))
        logger.info("dry-run: %s", self.dry_run)
        logger.info("=" * 50)

        if not self.dry_run and not self._is_in_scan_window(datetime.now()):
            logger.info("当前不在扫描时段，等待 %02d:00...", SCAN_WINDOW[0])

        # 启动时设置增量扫描初始时间为 (now - interval + 60s)，首次 tick 即可触发
        # （避免与全量扫描同时触发，但不会等待完整的周期）
        now = time.time()
        self._last_incremental = now - INCREMENTAL_INTERVAL + 60

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
