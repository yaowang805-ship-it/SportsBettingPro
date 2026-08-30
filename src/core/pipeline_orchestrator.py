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

SCAN_START_MIN = 0                 # V5.10: 扫描全天(原06:40), 夜里也产出投注方案加快数据累计
SCAN_END_MIN = 24 * 60             # V5.10: 扫描全天(原22:00)。推送时段已独立: _run_push 夜里加 --night(不推钉钉但照常投注)
# 推送时段(仅钉钉通知): 06:40~22:00, 夜里 skip_dingtalk 但仍投注+记录(见 bb_incremental_scanner._run_push)
PUSH_START_MIN = 6 * 60 + 40
PUSH_END_MIN = 22 * 60
INCREMENTAL_INTERVAL = 120  # V5.5: 120秒(2分钟) — 准实时发现新机会 (BB自有账号高频轮询+Pin按需拉变动联赛)
CHECK_INTERVAL = 30                # 调度循环检查间隔（秒）

# 定时任务表：(名称, HH:MM, 处理函数, 参数字典)
SCHEDULE = [
    # 全量扫描 09:00 独立窗口(2026-08-28 改): 从 06:40 移到 09:00 — 机器常睡眠导致 06:40 错过
    # (09:08 才追赶, 2026-08-28 实锤)。09:00 机器已醒, 全量扫描更稳。full_scan 是重任务
    # (Pin 415联赛+BB/FB提取, 10-20min, 内部8线程), 其他任务错开到 full_scan 之后 30min+,
    # 且 full_scan 运行中增量扫描+定时任务都跳过(_tick 里 _full_scan_running 检查), 防并发抢 Pin 风控。
    # 数据盘口日报 07:20: 各盘口门槛/CLV/ROI + 门槛变动, 推钉钉(只读本地数据不拉Pin, 轻量)
    ("market_report",     "07:20", "do_market_report", {}),
    ("full_scan_morning",  "09:00", "do_full_scan",  {"bet": True}),
    ("self_repair",       "09:30", "do_self_repair", {}),       # 自检+自动修复: 锁文件/缓存/指纹/连通性
    ("time_calibration",  "09:35", "do_time_calibration", {}),  # 时间校准: BB/Pin/系统时钟对齐
    ("health_check",       "09:40", "do_health_check", {}),
    ("settle_morning",     "09:45", "do_settle",      {}),
    ("daily_report",       "09:50", "do_daily_report",{}),
    ("data_sync_summary",  "09:50", "do_data_sync_summary",{}),  # V5.1: 数据积累量日报
    ("memory_update",      "09:55", "do_memory_update", {}),
    ("daily_cleanup",      "10:00", "do_cleanup",      {}),  # 指纹+临时文件清理
    ("evolve_daily",       "10:05", "do_evolve_daily", {}),  # V4 BB溢价累积
    ("download_data",      "10:10", "do_download_data", {}), # V4.5: 自动下载新数据源
    ("name_mapping",       "10:15", "do_name_mapping", {}), # V4.5: 拼音自动名映射
    # 周报：周日 21:00
    ("evolve_weekly",      "Mon 06:07", "do_evolve_weekly", {}),  # V4 每周进化(结算反馈+溢价重算)
    ("health_check_noon",  "13:55", "do_health_check", {}),  # 午后巡检
    ("settle_noon",        "14:00", "do_settle",      {}),  # 午后结算
    ("settle_afternoon",   "17:00", "do_settle",      {}),  # 傍晚结算
    ("clv_collect",        "12:00", "do_clv_collect", {}),  # CLV收盘采集
    ("clv_collect",        "16:00", "do_clv_collect", {}),
    ("clv_collect",        "18:00", "do_clv_collect", {}),
    ("clv_collect",        "20:00", "do_clv_collect", {}),
    ("settle_evening",     "20:30", "do_settle",      {}),
    # 数据盘口周报：周日 21:05(错开 weekly_report) 运动×盘口全表(门槛/CLV/ROI)
    ("market_weekly_report", "Sun 21:05", "do_market_weekly_report", {}),
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
        self._last_inc_urgent: Optional[float] = None   # 临场<6h
        self._last_inc_near: Optional[float] = None      # 中程6-24h
        self._last_inc_far: Optional[float] = None       # 早盘24-72h(2026-08-29 早盘聚焦加回)
        self._last_bb_settle: Optional[float] = None     # 高频BB结算(110min)
        self._full_scan_ok = False  # V5.4: 全量扫描成功+推送后才允许分层增量扫描
        self._last_scan_success: float = 0             # 最后一次成功完成的时间戳
        self._scan_failure_count: int = 0              # 连续失败计数
        self._alert_cooldown: dict[str, float] = {}    # 告警冷却
        # V5.1: 告警冷却持久化 — 防止守护进程重启后重复告警
        self._cooldown_file = SRC_DIR / "data" / "storage" / "alert_cooldown.json"
        try:
            if self._cooldown_file.exists():
                self._alert_cooldown = json.loads(self._cooldown_file.read_text())
        except Exception:
            pass
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
            "config.weight_matrix_v5",
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

        # 3) Pinnacle 结构格式验证 — flat {league_id: info} 为标准格式
        pin_struct = safe_load_json(DATA_DIR / "pinnacle_league_structure.json")
        if pin_struct and isinstance(pin_struct, dict):
            # 历史 nested 格式 {sport_id: {league_id: info}} → 展平为 flat
            nested_count = sum(1 for v in pin_struct.values()
                               if isinstance(v, dict) and "name" not in v)
            if nested_count > 0 and nested_count == len(pin_struct) and len(pin_struct) <= 20:
                logger.warning("[自检] Pinnacle结构为历史nested格式, 自动展平...")
                flat = {}
                for sport_data in pin_struct.values():
                    if isinstance(sport_data, dict):
                        for lid, info in sport_data.items():
                            if isinstance(info, dict):
                                flat[str(lid)] = info
                (DATA_DIR / "pinnacle_league_structure.json").write_text(
                    json.dumps(flat, ensure_ascii=False, indent=2))
                pin_struct = flat
                logger.info("[自检] Pinnacle结构已自动修复 (nested→flat, %d 联赛)", len(flat))

        # 4) Pinnacle API 连通性检查 (Shadowrocket 必须运行)
        # 2026-08-27 加重试: SSL EOF 是瞬时抖动, 单次失败就告警会刷屏。重试2次再判不可达。
        _pin_ok = False
        _pin_err = None
        for _pin_attempt in range(3):
            try:
                from src.scrapers.pinnacle_api import SESSION, API_BASE, _load_cookie
                _load_cookie()
                r = SESSION.get(f"{API_BASE}/sports", timeout=10)
                if r.status_code == 200:
                    sports = r.json()
                    logger.info("[自检] Pinnacle API 连通 ✅ (%d 运动)", len(sports))
                    _pin_ok = True
                    break
                _pin_err = f"[自检] Pinnacle API HTTP {r.status_code} — Shadowrocket运行了吗?"
            except Exception as e:
                _pin_err = f"[自检] Pinnacle API 不可达: {e} — 请启动 Shadowrocket"
            if _pin_attempt < 2:
                time.sleep(2)
        if not _pin_ok:
            errors.append(_pin_err)

        # 5) 联赛缓存最低数量检查 — 防误删: 从 manual_backup 恢复 > 直接删除
        if pin_struct and isinstance(pin_struct, dict) and len(pin_struct) < 50:
            logger.warning("[自检] 联赛缓存仅 %d 条 (<50, 疑似过期)", len(pin_struct))
            # 优先从 manual_backup 恢复
            manual_backup = DATA_DIR.parent / "manual_backup" / "pinnacle_league_structure.json"
            if manual_backup.exists():
                backup_data = safe_load_json(manual_backup)
                if backup_data and len(backup_data) > 100:
                    (DATA_DIR / "pinnacle_league_structure.json").write_text(
                        json.dumps(backup_data, ensure_ascii=False, indent=2))
                    logger.info("[自检] 已从 manual_backup 恢复联赛缓存 (%d 联赛)", len(backup_data))
                else:
                    logger.warning("[自检] manual_backup 也无效, 下次扫描时自动重建")
            else:
                # 最后手段: 删除让扫描重建
                stale_path = DATA_DIR / "pinnacle_league_structure.json"
                backup_path = DATA_DIR / f"pinnacle_league_structure.json.stale.{int(time.time())}"
                stale_path.rename(backup_path)
                logger.info("[自检] 已备份旧缓存, 下次扫描将自动重建")

        if errors:
            for e in errors:
                logger.error(e)
            # API 不可达(403/封禁) → 告警但继续启动, 主循环用 _SCAN_PAUSE_UNTIL/熔断器跳过 Pin 请求。
            # 不能 SystemExit: 否则 launchd(KeepAlive+ThrottleInterval=10) 每10s重启,
            # 反复重锤被封 IP 加重风控(2026-08-17/18 根因, 曾 2911 次 403)。
            api_errors = [e for e in errors if "API" in e or "Shadowrocket" in e or "Pinnacle" in e]
            if api_errors:
                self._send_alert("startup_check_failed", "\n".join(api_errors[:5]))
                logger.warning("[自检] Pinnacle API 不可用, 继续启动(主循环将暂停 Pin 请求, 不重锤被封IP)")
            else:
                logger.warning("[自检] %d 项非致命问题, 继续启动", len(errors))

    # ------------------------------------------------------------------
    # 调度逻辑
    # ------------------------------------------------------------------
    def _is_in_scan_window(self, now: datetime) -> bool:
        cur_min = now.hour * 60 + now.minute
        return SCAN_START_MIN <= cur_min < SCAN_END_MIN

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
        return 0 <= diff <= 30

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
        # 任务互斥键: 增量 urgent/near/far 各自独立锁; full_scan_* 共享 "scan" 锁;
        # 其余任务(settle/report/health/git_commit 等)各自独立锁。
        # (旧逻辑 `else "scan"` 把 settle/daily_report/data_sync_summary 全塞进同一把 scan 锁,
        #  导致 settle_morning 阻塞 daily_report → "上一轮还未完成" 死锁)
        if "incremental" in name:
            lock_key = name
        elif "scan" in name:
            lock_key = "scan"
        else:
            lock_key = name
        if background:
            lock_key = f"{lock_key}_bg"

        if lock_key in self._active_tasks:
            logger.warning("[%s] 上一轮还未完成，跳过本次调度", name)
            return False

        if background:
            # 后台线程运行，不阻塞主循环
            def _bg_runner():
                self._active_tasks.add(lock_key)
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
                    self._active_tasks.discard(lock_key)
            t = threading.Thread(target=_bg_runner, daemon=True)
            t.start()
            return True

        self._active_tasks.add(lock_key)
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
            self._active_tasks.discard(lock_key)

    def _send_alert(self, task_name: str, error: str):
        """发送 DingTalk 告警（带冷却：同一任务每 30 分钟最多一次）。"""
        now = time.time()
        last = self._alert_cooldown.get(task_name, 0)
        if now - last < 1800:
            logger.info("[%s] 告警冷却中，跳过 (%.0f秒前刚发过)", task_name, now - last)
            return
        self._alert_cooldown[task_name] = now
        # V5.1: 持久化冷却时间, 防止重启后重复告警
        try:
            self._cooldown_file.write_text(json.dumps(self._alert_cooldown))
        except Exception:
            pass

        # 任务名中文化(2026-08-25 用户要求: 钉钉信息必须全中文)
        _task_cn = {
            "full_scan_morning": "全量扫描", "startup_check_failed": "启动自检",
            "settle_watchdog": "结算看门狗", "scan_watchdog": "扫描看门狗",
            "main_loop_crash": "主循环崩溃", "settle": "结算",
        }
        _cn = _task_cn.get(task_name, task_name)

        from datetime import timezone as _tz, timedelta as _td
        bj_time = datetime.now(_tz(_td(hours=8))).strftime('%m/%d %H:%M')
        body = (
            f"**流水线告警**\n\n"
            f"任务: {_cn}\n"
            f"时间: {bj_time} (北京时间)\n"
            f"错误: {error[:200]}"
        )
        try:
            # 任务失败告警属故障类, urgent 跳过非投注每日配额(否则被例行日报挤掉而静默丢失)
            if send_dingtalk("流水线告警", body, urgent=True):
                logger.info("[%s] 告警已发送", task_name)
            else:
                logger.error("[%s] 告警未送达(钉钉返回失败)", task_name)
        except Exception as e:
            logger.error("[%s] 告警发送失败: %s", task_name, e)

    # ------------------------------------------------------------------
    # 具体任务实现
    # ------------------------------------------------------------------
    def _reload_critical_modules(self):
        """V4.5: 每次tick前重载所有src模块 — 代码改动立即生效."""
        import importlib
        to_reload = [m for m in sys.modules if any(m.startswith(p) for p in
            ("src.", "config.weight")) and "pipeline_orchestrator" not in m]
        for mod_name in to_reload:
            try: importlib.reload(sys.modules[mod_name])
            except Exception: pass

    def do_full_scan(self, bet: bool = True):
        """全量扫描：提取 → 对比 → 推送。"""
        self._reload_critical_modules()
        # 心跳保活: 全量扫描耗时~15min(Pin预取12min), 主循环阻塞期间后台线程持续写心跳,
        # 防止自愈看门狗(5min阈值)误判卡死重启而打断扫描。
        _scan_hb_stop = threading.Event()
        def _scan_hb():
            _hb_path = SRC_DIR / "data" / "storage" / ".pipeline_heartbeat"
            while not _scan_hb_stop.is_set():
                try:
                    _hb_path.write_text(str(time.time()))
                except Exception:
                    pass
                _scan_hb_stop.wait(60)  # 每60s写一次, 小于自愈5min阈值
        threading.Thread(target=_scan_hb, daemon=True, name="scan-heartbeat").start()
        # 设置推送标签（保存/恢复避免影响增量扫描）
        _prev_label = os.environ.get("PUSH_LABEL", "")
        os.environ["PUSH_LABEL"] = "全量扫描·24-72h"

        # 后台预加载 Pinnacle 联赛结构（与 BB 提取并行，省 10-20s）
        preload_done = threading.Event()
        def _preload_pin_leagues():
            try:
                from src.scrapers.pinnacle_league_map import refresh_league_structure
                refresh_league_structure()
            except Exception as e:
                logger.warning("联赛结构预加载失败: %s", e)
            finally:
                preload_done.set()

        preload_thread = threading.Thread(
            target=_preload_pin_leagues, daemon=True, name="preload-pin-leagues")
        preload_thread.start()

        try:
            logger.info("Step 1/4: BB 提取 (获取联赛列表, 用于 Pin 联赛映射)...")
            from src.scrapers.bb_api_fetcher import main as fetch
            from src.scrapers.bb_vs_pinnacle import main as compare
            # bb_api_fetcher.main() 读取 sys.argv，需要临时设置
            old_argv = sys.argv
            # 铁律(Pin先BB后): Step1 只拉 BB(获取联赛列表), 不能拉 FB —— FB 是零售价, 必须和 BB
            # 一起在 Pin 之后(Step3)拉, 否则 FB 早于 Pin = FB 陈旧(2026-08-23 用户提醒)。
            sys.argv = ["bb_api_fetcher", "--all-sports"]
            try:
                fetch()
            finally:
                sys.argv = old_argv
            logger.info("Step 1/4: 完成")

            # 确保联赛预加载已完成
            preload_done.wait()
            # Step 2/4: Pin 先拉取并缓存(慢, ~12min), 不对比
            logger.info("Step 2/4: Pin 预取并缓存...")
            sys.argv = ["bb_vs_pinnacle", "--pin-cache"]
            try:
                compare()
            finally:
                sys.argv = old_argv
            logger.info("Step 2/4: 完成")

            # Step 3/4: Pin 拉取后再提取 BB, 保证 BB 赔率新鲜(消除 12min 时间错位)
            logger.info("Step 3/4: BB 再提取 (新鲜赔率)...")
            sys.argv = ["bb_api_fetcher", "--all-sports", "--with-fb"]
            try:
                fetch()
            finally:
                sys.argv = old_argv
            logger.info("Step 3/4: 完成")

            # Step 4/4: 用缓存的 Pin + 新鲜的 BB 对比
            logger.info("Step 4/4: Pinnacle 对比 (缓存Pin + 新鲜BB)...")
            sys.argv = ["bb_vs_pinnacle", "--use-pin-cache"]
            try:
                compare()
            finally:
                sys.argv = old_argv
            logger.info("Step 4/4: 完成")

            # (2026-08-24 删除 FB 独立提取/对比: BB/FB 已在 Step3 --with-fb 合并取高值,
            #  FB 独立对比文件 bb_vs_pinnacle_comparison_FB.json 不再生成, 避免"FB 补 BB 缺口")
            logger.info("Step 2c/3: 辅助数据源对比 (the-odds-api)...")
            try:
                from src.scrapers.odds_api_compare import run_all
                run_all()
            except Exception as e:
                logger.warning("辅助对比跳过: %s", e)
            logger.info("Step 2c/3: 完成")

            logger.info("Step 3/3: +EV 推送 (子进程, 不阻塞扫描)...")
            import subprocess, shutil
            for pyc in (SRC_DIR / "src").rglob("__pycache__"):
                try: shutil.rmtree(pyc)
                except: pass
            push_args = [sys.executable, "-m", "src.report.bb_ev_push", "--incremental"]
            if not bet:
                push_args.append("--no-bet")
            subprocess.run(push_args, capture_output=True, text=True,
                          cwd=SRC_DIR.parent, timeout=600)
            logger.info("Step 3/3: 完成")
            self._full_scan_ok = True  # V5.4: 全量扫描+推送完成, 放行分层增量扫描
        finally:
            _scan_hb_stop.set()
            os.environ["PUSH_LABEL"] = _prev_label

    def do_incremental(self, time_window: str = "all"):
        """增量扫描。time_window = "near" | "far" | "all" """
        self._reload_critical_modules()
        # 扫描开始心跳: self_heal 用 .scan_heartbeat_* 判"跑完", 但 near 一轮要 5min+,
        # 期间 .scan_heartbeat_near 是旧的 → self_heal 误判"停滞"每 5min kickstart 一次,
        # 把跑到一半的 near 杀在半路 → 永远跑不完(互杀, 2026-08-23 排查)。开始心跳让
        # self_heal 知道"near 在跑", 不要杀。
        self._write_scan_start(time_window)
        from src.scrapers.bb_incremental_scanner import run_incremental
        run_incremental(time_window=time_window)
        # V5.10: 写扫描心跳 —— 对比文件只在"有变动"时才重写(run_incremental 无变动会
        # 提前 return), 所以文件 mtime 不能当存活信号: self_heal 曾据此误判 near 停滞
        # 813min 而每 5min kickstart 一次, 而 near 一轮要 8-9min → 每次都被杀在半路,
        # 文件永远刷不新, 形成互杀死循环(2026-08-21 实测 20min 内重启 6 次, 全天零投注)。
        # 心跳由本函数在 run_incremental 正常返回后写, 覆盖其全部 return 分支。
        self._write_scan_heartbeat(time_window)

    def _write_scan_start(self, time_window: str):
        """记录某一层增量扫描"开始跑"的时刻(self_heal 据此判在飞, 避免误杀慢扫描)。"""
        try:
            hb = SRC_DIR / "data" / "storage" / f".scan_start_{time_window}"
            hb.write_text(str(time.time()))
        except Exception as e:
            logger.warning("扫描开始心跳写入失败(%s): %s", time_window, e)

    def _write_scan_heartbeat(self, time_window: str):
        """记录某一层增量扫描"完整跑完一轮"的时刻(与是否有变动无关)。"""
        try:
            hb = SRC_DIR / "data" / "storage" / f".scan_heartbeat_{time_window}"
            hb.write_text(str(time.time()))
        except Exception as e:
            logger.warning("扫描心跳写入失败(%s): %s", time_window, e)

    def do_incremental_scan(self):
        """48h全量扫描(解耦: 只扫描不推送)。"""
        self._reload_critical_modules()
        from src.scrapers.bb_incremental_scanner import run_incremental_scan
        run_incremental_scan()

    def do_incremental_push(self, time_window: str = "urgent"):
        """分频推送(解耦: 只推送不扫描)。"""
        from src.scrapers.bb_incremental_scanner import run_incremental_push
        run_incremental_push(time_window=time_window)

    def do_clv_collect(self):
        """V5: CLV收盘价采集 — 对比赛前15-360分钟的投注拉取Pinnacle收盘赔率。"""
        try:
            from src.monitor.clv_collector import collect
            n = collect()
            if n > 0:
                logger.info("CLV采集: %d条", n)
        except Exception as e:
            logger.warning("CLV采集失败: %s", e)

    def do_settle(self):
        """自动结算 (原有 + 追踪投注结算 + BB比分结算)。"""
        from src.monitor.auto_settle import main as settle_main
        old_argv = sys.argv
        sys.argv = ["auto_settle"]
        try:
            settle_main()
        finally:
            sys.argv = old_argv
        # BB 比分结算 — 用 BB 自己的赛果结算(解决 ESPN 覆盖不到的联赛)
        try:
            from src.monitor.bb_score_settle import settle_via_bb
            r = settle_via_bb()
            if r.get("settled"):
                logger.info("BB比分结算: %d 笔", r["settled"])
        except Exception as e:
            logger.warning("BB比分结算失败: %s", e)

        # 追踪投注结算: 所有推送过的投注 → 赛果匹配 → 盈亏计算
        try:
            from src.monitor.result_fetcher import settle_pending_bets
            from src.monitor.bet_tracker import get_pnl_summary
            result = settle_pending_bets(dry_run=False)
            if result["settled"] > 0:
                summary = get_pnl_summary()
                if summary.get("settled", 0) > 0:
                    logger.info("📊 追踪盈亏: %d笔 ¥%.0f ROI=%.1f%%",
                               summary["settled"], summary["total_profit"], summary["roi_pct"])
        except Exception as e:
            logger.warning("追踪投注结算失败: %s", e)

    def do_settle_bb(self):
        """高频 BB 比分结算(2026-08-27): 只跑 settle_via_bb, 每 110min 一次。

        BB 赛果窗口实测只有 ~5h(开赛起), 而原 settle 一天只跑 4 次(间隙 3-12h),
        导致比赛结束到结算之间经常超窗口、BB 比分被清 → 大量注 timeout_void/unsettleable。
        这个高频任务只跑 BB(轻量), 不跑 auto_settle(ESPN,重), 保证每场结束 ~1h 内就结算。
        """
        if self._active_tasks_settle():
            return  # 已有 settle 任务在跑, 跳过(避免并发读写 tracked_bets)
        try:
            from src.monitor.bb_score_settle import settle_via_bb
            r = settle_via_bb()
            if r.get("settled"):
                logger.info("高频BB结算: %d 笔", r["settled"])
        except Exception as e:
            logger.warning("高频BB结算失败: %s", e)
        # 观察库纸面结算(2026-08-29): 给 validate 样本补 won/lost/void, 供自有标定扩面。
        # 复用 BB getMatchDetail(轻量), 与 tracked_bets 结算互不影响(独立 paper_bets.json)。
        try:
            from src.monitor.paper_settle import settle_paper, _normalize_daily_budget
            pr = settle_paper()
            _normalize_daily_budget()
            if pr.get("new_settled"):
                logger.info("观察库纸面结算: +%d 笔 (累计 %d)", pr["new_settled"], pr["total_paper"])
        except Exception as e:
            logger.warning("观察库纸面结算失败: %s", e)

    def do_market_report(self):
        """数据盘口日报: 各盘口门槛/CLV/真实ROI + 门槛变动, 推钉钉(只读本地数据不拉Pin)。"""
        try:
            import subprocess
            script = SRC_DIR / "scripts" / "daily_market_report.py"
            r = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, cwd=str(SRC_DIR), timeout=120,
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "")[-400:]
                logger.warning("盘口日报失败: %s", err)
            else:
                logger.info("盘口日报已推送")
        except Exception as e:
            logger.warning("盘口日报异常: %s", e)

    def do_market_weekly_report(self):
        """数据盘口周报: 运动×盘口全表(门槛/CLV/ROI), 推钉钉(只读本地数据不拉Pin)。"""
        try:
            import subprocess
            script = SRC_DIR / "scripts" / "weekly_market_report.py"
            r = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, cwd=str(SRC_DIR), timeout=120,
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "")[-400:]
                logger.warning("盘口周报失败: %s", err)
            else:
                logger.info("盘口周报已推送")
        except Exception as e:
            logger.warning("盘口周报异常: %s", e)

    def do_daily_report(self):
        """日报推送。"""
        from src.report.daily_settlement import main as dr
        old_argv = sys.argv
        sys.argv = ["daily_settlement"]
        try:
            dr()
        finally:
            sys.argv = old_argv

    def do_data_sync_summary(self):
        """V5.1: 每日9点数据积累量摘要 — 各数据源条数统计推钉钉。"""
        try:
            import sqlite3, json, csv
            from pathlib import Path
            DATA = SRC_DIR / "data" / "storage"
            lines = ["📊 数据积累日报", ""]

            # 1. 结算系统
            bet_log_n = 0
            bet_db = DATA / "sportsbetting.db"
            if bet_db.exists():
                c = sqlite3.connect(str(bet_db))
                bet_log_n = c.execute("SELECT COUNT(*) FROM bet_log").fetchone()[0]
                c.close()
            settle_csv = DATA / "settlement_log.csv"
            settle_n = sum(1 for _ in open(settle_csv, encoding='utf-8-sig')) - 1 if settle_csv.exists() else 0
            lines.append(f"💰 结算: bet_log {bet_log_n} + settlement_log {settle_n} 条")

            # 2. CLV 追踪
            clv_n = 0
            clv_db = DATA / "sportsbetting.db"
            if clv_db.exists():
                c = sqlite3.connect(str(clv_db))
                clv_n = c.execute("SELECT COUNT(*) FROM push_clv").fetchone()[0]
                c.close()
            lines.append(f"📈 CLV追踪: {clv_n} 条")

            # 3. Pin 历史赔率
            arch_n = 0
            arch_db = DATA / "pinnacle_odds_archive.db"
            if arch_db.exists():
                c = sqlite3.connect(str(arch_db))
                arch_n = c.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
                c.close()
            lines.append(f"🗄️ Pin历史赔率: {arch_n} 条")

            # 4. 实盘历史
            bh = DATA / "bet_history.csv"
            real_n = 0
            if bh.exists():
                real_n = sum(1 for r in csv.DictReader(open(bh, encoding='utf-8-sig')) if '2026' in r.get('date',''))
            lines.append(f"🎯 实盘投注(2026): {real_n} 条")

            # 5. 映射数据
            tm = DATA / "team_name_map.json"
            tm_n = len(json.load(open(tm))) - 1 if tm.exists() else 0
            lines.append(f"🗺️ 队名映射: {tm_n} 条")

            body = "\n".join(lines)
            # 例行日报, 不加 urgent(该受每日配额约束); 但必须如实记录成败
            if send_dingtalk("数据积累日报", body, timeout=10):
                logger.info("数据积累日报已推送")
            else:
                logger.warning("数据积累日报未送达(配额用尽或钉钉失败)")
        except Exception as e:
            logger.error("数据日报异常: %s", e)

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

        # 2. 清理归档库旧数据 (>7天) — 归档每天涨100万+条, 不清理会无限膨胀(2026-08-27)
        try:
            import sqlite3 as _sqlite3
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _arch = SRC_DIR / "data" / "storage" / "pinnacle_odds_archive.db"
            if _arch.exists():
                _cutoff = (_dt.now(_tz.utc) - _td(days=7)).isoformat()
                _conn = _sqlite3.connect(_arch)
                _n = _conn.execute("DELETE FROM odds_archive WHERE fetched_at < ?", (_cutoff,)).rowcount
                _conn.commit()
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                _conn.close()
                if _n:
                    logger.info("归档库清理: 删除 %d 条(>7天)", _n)
        except Exception as e:
            logger.warning("归档库清理失败: %s", e)

        # 清理文件去重中的过期记录
        try:
            opps_file = SRC_DIR / "data" / "storage" / "pushed_fingerprints.json"
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

    def do_name_mapping(self):
        """V4.5: 每日拼音自动名映射 — Pinnacle API拉选手名单→拼音匹配BB中文名."""
        import json as _json, re, logging
        _log = logging.getLogger(__name__)
        try:
            from pypinyin import pinyin, Style
            from difflib import SequenceMatcher
            from src.scrapers.pinnacle_api import api_get

            bb_file = SRC_DIR / "data" / "storage" / "bb_odds_extracted.json"
            with open(bb_file) as f:
                bb = _json.load(f)
            matches = bb if isinstance(bb, list) else bb.get("matches", bb.get("data", []))
            nm_file = SRC_DIR / "data" / "storage" / "team_name_map.json"
            with open(nm_file) as f:
                nm = _json.load(f)

            def _sim(cn, en):
                try:
                    py = "".join(p[0] for p in pinyin(cn, style=Style.NORMAL))
                    return SequenceMatcher(None, re.sub(r"[^a-z]", "", py), re.sub(r"[^a-z]", "", en.lower())).ratio()
                except Exception:
                    return 0

            SPORTS = {"tennis": 33, "boxing": 6, "mma": 22, "basketball": 4, "ice_hockey": 19}
            total_new = 0
            for sport_name, pin_id in SPORTS.items():
                bb_names = set()
                for m in matches:
                    if m.get("sport") != sport_name:
                        continue
                    for p in [m.get("home", ""), m.get("away", "")]:
                        p = re.sub(r"\s*[（(][^)）]*[)）]", "", p).strip()
                        if p and not p.isascii():
                            bb_names.add(p)

                resp = api_get(f"/sports/{pin_id}/leagues")
                if not resp:
                    continue
                leagues = resp if isinstance(resp, list) else resp.get("leagues", [])
                active = [l for l in leagues if l.get("matchupCount", 0) > 0]

                pin_names = set()
                for lg in active[:5]:
                    resp = api_get(f"/leagues/{lg['id']}/matchups")
                    if not resp:
                        continue
                    for mu in (resp if isinstance(resp, list) else []):
                        for p in mu.get("participants", []):
                            n = p.get("name", "")
                            if n:
                                pin_names.add(n)

                new_maps = 0
                for cn in bb_names:
                    if cn in nm:
                        continue
                    # 双信号: 拼音≥0.45 且 存在赔率相近的对手(同赛事同时段)
                    best, best_en = 0, ""
                    for en in pin_names:
                        s = _sim(cn, en)
                        if s > best:
                            best = s
                            best_en = en
                    # V4.5: 只有拼音≥0.55才学(提高门槛防错配)
                    if best >= 0.55:
                        nm[cn] = best_en
                        new_maps += 1
                total_new += new_maps
                if new_maps:
                    _log.info("名映射 [%s]: +%d", sport_name, new_maps)

            if total_new > 0:
                with open(nm_file, "w") as f:
                    _json.dump(nm, f, ensure_ascii=False, indent=2)
                _log.info("名映射: +%d 条 (总计 %d)", total_new, len(nm) - 1)
        except Exception as e:
            _log.warning("名映射失败: %s", e)

    def do_evolve_daily(self):
        """V4 每日进化: BB 溢价累积。"""
        from src.evolve.v4_evolver import evolve_daily
        evolve_daily()

    def do_evolve_weekly(self):
        """V4 每周进化: 结算反馈 + 溢价重算 + 健康检查。"""
        from src.evolve.v4_evolver import evolve_weekly
        evolve_weekly()

    def do_git_commit(self):
        """自动 git 提交 + push。"""
        import subprocess
        result = subprocess.run(
            ["git", "add", "-u"],
            cwd=SRC_DIR,
            capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.decode('utf-8', errors='replace')[:200] if result.stderr else ''
            logger.warning("git add 失败: %s", err)
            return
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=SRC_DIR,
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("无变更，跳过提交")
        else:
            result = subprocess.run(
                ["git", "commit", "-m", f"日常自动存档 {datetime.now().strftime('%Y-%m-%d')}"],
                cwd=SRC_DIR,
                capture_output=True,
            )
            if result.returncode == 0:
                logger.info("已提交变更")
            else:
                err = result.stderr.decode('utf-8', errors='replace')[:200] if result.stderr else ''
                logger.warning("提交失败: %s", err)

        # push 到 origin — 无论有无新提交, 清掉本地堆积的未推送提交 (2026-08-17)
        try:
            push_result = subprocess.run(
                ["git", "push"],
                cwd=SRC_DIR,
                capture_output=True,
                timeout=60,
            )
            if push_result.returncode == 0:
                logger.info("已 push 到 origin")
            else:
                err = push_result.stderr.decode('utf-8', errors='replace')[:200] if push_result.stderr else ''
                logger.warning("git push 失败: %s", err)
        except Exception as e:
            logger.warning("git push 异常: %s", e)

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
            # Light endpoint: 用真实提取路径 /v1/match/getList(1条) 探连通, 不用 /api/v1/sports(已 403/404)
            resp = api_post('/v1/match/getList', {"sportId": 1, "type": 2, "current": 1, "pageSize": 1, "isPC": True, "languageType": "EN"})
            bb_latency = _time.time() - t0
            bb_ok = bool(resp and resp.get("success"))
            logger.info(f"  BB API 延迟: {bb_latency:.1f}s (status={'ok' if bb_ok else 'fail'})")
            if bb_latency > 10:
                issues.append(f"BB API 延迟 {bb_latency:.0f}s (>10s)")
            if not bb_ok:
                issues.append("BB API 不可达")
        except Exception as e:
            logger.warning(f"  BB API 连接失败: {e}")
            issues.append(f"BB API 连接失败: {str(e)[:50]}")

        # 3. Pinnacle API 延迟（用 /sports 轻量端点; 原 /leagues/29 已 404 废弃, 2026-08-24 修）
        try:
            from src.scrapers.pinnacle_api import api_get as pin_get
            t0 = _time.time()
            resp = pin_get('/sports')  # 轻量端点 (api_get 已自动加 /0.1 前缀)
            pin_time = _time.time()
            pin_latency = pin_time - t0
            logger.info(f"  Pinnacle API 延迟: {pin_latency:.1f}s")
            if not resp:
                issues.append("Pinnacle API 返回空")
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
        """V5: 全系统健康自检 — 有问题推钉钉。"""
        try:
            from src.monitor.health_checker import run_health_check
            report = run_health_check(push=False, quiet=True)
            logger.info("健康度: %s/100", report.score)
            for i in report.issues:
                logger.warning("  ❌ %s", i)
            for w in report.warnings:
                logger.warning("  ⚠️ %s", w)

            # 有问题或警告时推钉钉
            if report.issues or report.warnings:
                lines = [f"🩺 健康度: {report.score}/100"]
                if report.issues:
                    lines.append(f"\n🔴 问题 ({len(report.issues)}):")
                    for i in report.issues[:10]:
                        lines.append(f"  ❌ {i}")
                if report.warnings:
                    lines.append(f"\n🟡 警告 ({len(report.warnings)}):")
                    for w in report.warnings[:10]:
                        lines.append(f"  ⚠️ {w}")
                body = "\n".join(lines)
                # 健康分低于 60 视为故障告警走 urgent, 否则算例行报告受配额约束
                _urgent = getattr(report, "score", 100) < 60
                if send_dingtalk(f"系统健康报告 {report.score}/100", body,
                                 timeout=10, urgent=_urgent):
                    logger.info("健康报告已推送")
                else:
                    logger.warning("健康报告未送达(配额用尽或钉钉失败), score=%s", report.score)
        except Exception as e:
            logger.error("健康检查异常: %s", e)

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
            # full_scan_morning 也追赶 — Pin临时故障(如Shadowrocket代理抖动)导致全量扫描失败时,
            #   重启后对比文件过期(>2h)则补跑, 否则增量扫描会一直卡在 _full_scan_ok=False
            is_full_scan = (name == "full_scan_morning")
            if name != "git_commit" and "settle" not in name and not is_full_scan:
                continue
            if is_full_scan:
                _cmp_file = SRC_DIR / "data" / "storage" / "bb_vs_pinnacle_comparison.json"
                if _cmp_file.exists() and (time.time() - _cmp_file.stat().st_mtime) < 7200:
                    continue  # 对比文件新鲜(<2h), 无需补跑
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
        self._reload_critical_modules()  # V4.5: 每次tick前重载, 代码0延迟生效
        now = datetime.now()

        # 全量扫描运行中(scan/scan_bg 锁 active) — 增量扫描+定时任务都跳过, 防并发抢 Pin 风控
        # (2026-08-28: 全量扫描移到 09:00, 期间暂停其他任务, 等它结束再放行)
        _full_scan_running = "scan" in self._active_tasks or "scan_bg" in self._active_tasks

        # 1) 定时任务 (settle/report → 后台线程)
        _BACKGROUND_TASKS = {"settle", "report", "git_commit", "memory_update", "evolve", "incremental",
                             "self_repair", "time_calibration", "health_check", "clv_collect",
                             "cleanup", "download_data", "name_mapping"}
        for name, time_str, method_name, kwargs in SCHEDULE:
            weekday, dom, hour, minute = _parse_schedule_time(time_str)
            if not self._is_time_match(weekday, dom, hour, minute, now):
                continue
            if not self._should_run_wall_clock(name, now):
                continue
            method = getattr(self, method_name, None)
            if not method:
                continue
            # 全量扫描运行中, 暂停其他定时任务(只放行 full_scan 本身) — 防并发抢 Pin/资源(2026-08-28)
            if _full_scan_running and name != "full_scan_morning":
                continue
            is_settle = "settle" in name
            is_bg = any(t in name for t in _BACKGROUND_TASKS) or is_settle
            if is_settle and self._active_tasks_settle():
                continue  # 已有结算在跑, 等下一轮
            self._run_task(name, method, background=is_bg, **kwargs)
            self._last_run[name] = now.date()

        # 2) V5 分层增量扫描 — 临场60s/中程300s (Pinnacle变动驱动)
        # V5.4: 全量扫描成功+推送后才放行(降频防风控, 用户要求)
        # 全量扫描运行中跳过增量(上方已算 _full_scan_running), 防并发抢 Pin
        if self._full_scan_ok and self._is_in_scan_window(now) and not _full_scan_running:
            import random as _random
            _jitter = lambda base: base * (0.85 + _random.random() * 0.3)

            # 2026-08-30 数据驱动(观察库 clv_results 改版后≥8-28): 早盘24-72h 中位CLV+2.96%/正率60.5% 强正edge,
            # 临场<6h 中位CLV-1.49%/正率41.3% 负edge假机会(近3天临场亏-1582), 近场6-24h +0.93% 弱正。
            # 故早盘升频 20→10min 多捞干净edge, 临场降频 5→10min 少碰假机会省Pin配额, near保持5min。
            # 净Pin请求 -24% (早盘+73联赛/20min, 临场-248联赛/20min), 风控下降同时早盘发现能力翻倍。
            for tw, interval, label in [("far", 600, "早盘24-72h"), ("near", 300, "中程6-24h"), ("urgent", 600, "临场<6h")]:
                last_key = f"_last_inc_{tw}"
                last_val = getattr(self, last_key, None)
                if last_val is None:
                    setattr(self, last_key, time.time())
                    last_val = time.time()
                if (now - datetime.fromtimestamp(last_val)).total_seconds() >= _jitter(interval):
                    # 早盘 far(重, 24-72h 联赛多) 不与 near/urgent 抢 Pin: 有增量在跑就跳过本轮, 下轮再试。
                    # (共享跨进程限速 7.8req/s 已挡封禁, 这里再避免任务挤在一起, 用户 2026-08-29 要求)
                    if tw == "far" and any(t.startswith("incremental_near") or t.startswith("incremental_urgent")
                                          for t in self._active_tasks):
                        continue
                    setattr(self, last_key, time.time())
                    self._run_task(f"incremental_{tw}", self.do_incremental, background=True, time_window=tw)

        # 2b) 高频 BB 结算(2026-08-27): 每 110min 一次, 保证卡进 BB ~5h 赛果窗口。
        # 原 settle 一天只跑 4 次(08:30/14:00/17:00/20:30, 间隙 3-12h), 比赛结束到结算之间
        # 常超 5h 窗口 → BB 比分被清 → 大量 timeout_void/unsettleable。高频只跑 BB(轻量)。
        _bb_interval = 110 * 60
        if self._last_bb_settle is None:
            self._last_bb_settle = time.time()
        if (now - datetime.fromtimestamp(self._last_bb_settle)).total_seconds() >= _bb_interval:
            self._last_bb_settle = time.time()
            self._run_task("settle_bb_highfreq", self.do_settle_bb, background=True)

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
    _CODE_CHECK_INTERVAL = 60  # 每 5 分钟检查一次

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
        """自检看门狗：检测增量扫描是否停滞，Pinnacle 连接是否异常。V5: 分层扫描, 只看最长的near间隔。"""
        now = time.time()
        if not self._is_in_scan_window(datetime.now()):
            return
        # 全量扫描运行中时, 增量扫描被有意跳过(防抢Pin, 见 _tick 的 _full_scan_running guard),
        # 这不是停滞, 别告警(2026-08-23 重启追赶全量扫描时误报"停滞30min")。
        if "scan" in self._active_tasks or "scan_bg" in self._active_tasks:
            return

        # V5.7: 远端24-72h层已移除, 最长的增量扫描间隔是near=5min, 允许2x容忍
        _near_elapsed = now - self._last_inc_near if self._last_inc_near else 0
        _near_timeout = 300 * 2  # 10分钟

        warnings = []
        if self._last_inc_near and _near_elapsed > _near_timeout:
            warnings.append(f"增量扫描停滞 {_near_elapsed/60:.0f}分钟(预期30min)")

        if warnings:
            logger.warning("🐕 看门狗(扫描): %s", "; ".join(warnings))
            self._scan_failure_count += 1
            if self._scan_failure_count >= 2:
                self._send_alert("scan_watchdog", "; ".join(warnings))
                self._scan_failure_count = 0
        else:
            self._scan_failure_count = 0

        # 2) 结算看门狗：检测 tracked_bets.json 中超时未结算的投注
        try:
            tb_path = SRC_DIR / "data" / "storage" / "tracked_bets.json"
            if tb_path.exists():
                tb = json.loads(tb_path.read_text())
                pending = [b for b in tb.get("bets", []) if b.get("status") == "pending"]
                now_ts = time.time()
                stale_48h = sum(1 for b in pending if b.get("match_epoch", 0) > 0 and (now_ts - b["match_epoch"]) > 172800)
                # V5.10: 1 天兜底 —— pending 超 1 天拿不到赛果的, 自动标 unsettleable。
                # BB 赛果 API 有 ~24-48h 时效窗口(老比赛 getMatchDetail 返回空壳), 超过窗口
                # 赛果永久丢失, 再挂 pending 只会污染看门狗和 ROI 分母(2026-08-25 实测)。
                try:
                    from src.monitor.bet_tracker import auto_mark_unsettleable
                    _n = auto_mark_unsettleable(days=1.0)
                    if _n:
                        logger.info("🐕 看门狗(结算): %d 笔超1天无赛果 → 标 unsettleable", _n)
                        # 处理完重读, 避免下面 stale_48h 把刚处置的也算进去
                        tb = json.loads(tb_path.read_text())
                        pending = [b for b in tb.get("bets", []) if b.get("status") == "pending"]
                        stale_48h = sum(1 for b in pending if b.get("match_epoch", 0) > 0 and (now_ts - b["match_epoch"]) > 172800)
                except Exception as _e:
                    logger.warning("超时作废兜底异常: %s", _e)
                if stale_48h > 5:  # V5: 只在新系统>5笔超时时告警
                    last_alert = self._alert_cooldown.get("settle_watchdog", 0)
                    if time.time() - last_alert > 28800:
                        self._send_alert("settle_watchdog", f"{stale_48h}笔投注超48小时未结算")
                        self._alert_cooldown["settle_watchdog"] = time.time()
                    last_log = self._alert_cooldown.get("settle_watchdog_log", 0)
                    if time.time() - last_log > 1800:
                        logger.warning("🐕 看门狗(结算): %d笔超48h未结算!", stale_48h)
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
        logger.info("扫描时段: %02d:%02d~%02d:00 | 增量: %dmin (0-72h全量)",
                     SCAN_START_MIN // 60, SCAN_START_MIN % 60, SCAN_END_MIN // 60,
                     INCREMENTAL_INTERVAL // 60)
        logger.info("定时任务: %s", ", ".join(name for name, *_ in SCHEDULE))
        logger.info("dry-run: %s", self.dry_run)
        logger.info("=" * 50)

        if not self.dry_run and not self._is_in_scan_window(datetime.now()):
            logger.info("当前不在扫描时段，等待 %02d:%02d...", SCAN_START_MIN // 60, SCAN_START_MIN % 60)

        # 启动时设置增量扫描初始时间为 (now - interval + 60s)，首次 tick 即可触发
        # （避免与全量扫描同时触发，但不会等待完整的周期）
        now = time.time()
        self._last_incremental = now - INCREMENTAL_INTERVAL + 60
        # V5.7: 分层扫描计时器也提前, 否则每次代码热更新重启后要等 15min 才扫(用户观察到长时间无推送)
        self._last_inc_urgent = now - 900 + 60
        self._last_inc_near = now - 1800 + 60

        # 启动时追赶今天已错过的定时任务
        if not self.dry_run:
            self._catch_up_missed_tasks()

        # V5.4: 启动时若近期有成功扫描(任一对比文件新鲜), 放行分层增量, 避免重启后整天不扫描
        try:
            _cmp_dir = SRC_DIR / "data" / "storage"
            _cmp_files = sorted(_cmp_dir.glob("bb_vs_pinnacle_comparison*.json"),
                                key=lambda f: f.stat().st_mtime, reverse=True)
            if _cmp_files and (time.time() - _cmp_files[0].stat().st_mtime) < 6 * 3600:
                self._full_scan_ok = True
                logger.info("检测到近期成功扫描(对比文件新鲜), 放行分层增量扫描")
        except Exception:
            pass

        # 心跳保活线程: 独立于主循环, 即使 _tick() 阻塞在长扫描(全量/增量)也持续写心跳,
        # 防止自愈看门狗误判卡死而反复 restart(否则会打断扫描、长时间无推送)。
        def _heartbeat_loop():
            _hb = SRC_DIR / "data" / "storage" / ".pipeline_heartbeat"
            while self._running:
                try:
                    _hb.write_text(str(time.time()))
                except Exception:
                    pass
                time.sleep(CHECK_INTERVAL)
        threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()

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
