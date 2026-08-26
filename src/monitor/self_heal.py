"""自愈看门狗 — 自动检查各组件运行情况, 发现异常自动修复, 发送修复报告。

用户需求(2026-08-16): 增量扫描/实时拉新代码有时没真正运行, 要一个功能自动检查、
自动处理、再发修复报告。

检查项 + 自动修复:
  1. 守护进程存活(ps + 心跳文件 .pipeline_heartbeat):
       - 进程没了(launchd KeepAlive 应该会拉起, 但双保险) → kickstart 重启
       - 心跳 > 15min(卡死/死循环) → kickstart 重启
  2. 增量扫描停滞(对比文件 bb_vs_pinnacle_comparison*.json mtime):
       - > 45min 没更新(预期 urgent 15min + 抖动) → kickstart 重启守护进程
  3. BB 数据陈旧(bb_odds_extracted.json mtime > 3h) → 告警(不自动重拉, 避免并发推送)
  4. Pinnacle 连通(api_get /sports):
       - 断 → 仅告警, 不自动换节点(2026-08-24 用户铁律: 换节点由用户手动决定)
  4b. Pin 缓存健康(pin_matches_cache.json):
       - 空(0场)或陈旧(>1h) 且 Pin 可达 → 主动重拉缓存(--pin-cache, 30min 冷却)
         (空缓存会致增量扫描读空缓存→对比无结果→不推, 心跳却正常, 是静默失效)
  5. 陈旧锁文件(.pipeline_daemon.lock 的 PID 已死) → 清除

只发"修复报告"当本轮有动作; 否则静默(不打扰)。launchd 每 10 分钟跑一次。

用法: .venv312/bin/python -m src.monitor.self_heal
"""
import os, sys, time, subprocess, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "storage"
LOGS_DIR = ROOT / "data" / "logs"
HEARTBEAT = DATA_DIR / ".pipeline_heartbeat"
LOCK_FILE = DATA_DIR / ".pipeline_daemon.lock"
COMPARISON_FILES = sorted(DATA_DIR.glob("bb_vs_pinnacle_comparison*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if DATA_DIR.exists() else []
BB_EXTRACTED = DATA_DIR / "bb_odds_extracted.json"
DAEMON_LABEL = "com.sportsbettingpro.daemon"

CHECK_INTERVAL = 5 * 60        # 心跳超时阈值(秒) — 之前15min太松, 卡8分钟丢一堆机会才发现
SCAN_STALE = 45 * 60           # 增量扫描停滞阈值(秒)
SCAN_START_STALE = 15 * 60     # 扫描"开始"心跳新鲜阈值(秒): 15min 内有开始 = 在飞不杀
BB_STALE = 3 * 3600            # BB 数据陈旧阈值(秒)
PIN_CACHE = DATA_DIR / "pin_matches_cache.json"
PIN_CACHE_STALE = 10 * 60      # Pin 缓存空/陈旧阈值(秒): 空且>此值, 或陈旧>1h, 且 Pin 可达 → 主动重拉
PIN_CACHE_REPAIR_COOLDOWN_FILE = DATA_DIR / "pin_cache_repair_cooldown.json"
PIN_CACHE_REPAIR_COOLDOWN = 30 * 60  # 缓存修复冷却(秒): 30min 只修一次, 避免每 5min 重拉


def _daemon_pid():
    try:
        out = subprocess.run(["pgrep", "-f", "pipeline_orchestrator"],
                             capture_output=True, text=True, timeout=5)
        pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _heartbeat_age():
    if not HEARTBEAT.exists():
        return None
    try:
        return time.time() - float(HEARTBEAT.read_text().strip())
    except Exception:
        return None


def _file_age(p):
    if not p or not p.exists():
        return None
    return time.time() - p.stat().st_mtime


def _scan_in_flight():
    """扫描/推送子进程是否正在跑。返回进程名(便于日志)或 None。

    kickstart -k 会硬杀守护进程连带其子进程。near 一轮 8-9min、推送一轮 1-3min,
    在飞时重启等于永远跑不完(2026-08-21 全天零投注即此故障), 故重启前必须先看这个。
    """
    for pat, name in ((r"src\.report\.bb_ev_push", "bb_ev_push"),
                      (r"src\.scrapers\.bb_api_fetcher", "bb_api_fetcher")):
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return name
        except Exception:
            pass
    return None


def _kickstart_daemon():
    """重启守护进程(launchd kickstart)。"""
    uid = os.getuid()
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{DAEMON_LABEL}"],
                       capture_output=True, text=True, timeout=30)
        return True
    except Exception:
        return False


def _clear_stale_lock():
    """清除 PID 已死的陈旧锁文件。"""
    if not LOCK_FILE.exists():
        return False
    try:
        txt = LOCK_FILE.read_text()
        # 锁文件内容是裸 PID(pipeline_orchestrator 写的是 str(os.getpid()), 无 "PID " 前缀
        # —— "PID xxx 已锁定" 只出现在日志里)。原正则只认 r"PID\s+(\d+)", 永远匹配不上
        # → 每轮都落到下面的 unlink(), 把**活着**的锁删掉, 单实例保护长期形同虚设。
        # 兼容两种写法: 取文件里第一个整数即为持锁 PID。
        import re
        m = re.search(r"(?:PID\s+)?(\d+)", txt)
        if m:
            pid = int(m.group(1))
            # 检查该 PID 是否还活着
            if subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0:
                return False  # 锁的进程还活着, 不动
        LOCK_FILE.unlink()
        return True
    except Exception:
        return False


def _cache_repair_allowed():
    """缓存修复冷却: 30min 内只修一次, 避免每 5min 重拉(重拉要 1min+ 且打 Pin)。"""
    try:
        m = json.loads(PIN_CACHE_REPAIR_COOLDOWN_FILE.read_text())
        last = m.get("ts", 0)
    except (OSError, ValueError):
        last = 0
    return time.time() - last > PIN_CACHE_REPAIR_COOLDOWN


def _mark_cache_repair():
    try:
        PIN_CACHE_REPAIR_COOLDOWN_FILE.write_text(json.dumps({"ts": time.time()}))
    except OSError:
        pass


def _repopulate_pin_cache():
    """主动修复: 重拉 Pin 缓存(打破"空缓存→对比无结果→提前return→预取被跳过→缓存
    继续空"的死循环)。用 subprocess 跑 --pin-cache(全量拉 415 联赛存缓存), nice 10 不抢增量扫描。
    """
    try:
        subprocess.Popen(
            [sys.executable, "-m", "src.scrapers.bb_vs_pinnacle", "--pin-cache"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def check_pin():
    # V5.10 修复(2026-08-21 用户"总是收到"自愈报告): 根因是 api_get 被"全局限速
    # pause"拦住返回 None(SSL 风暴触发的保护), 而不是 Pin 真断连。检查必须
    # bypass_pause=True 真实请求(同 pin_proxy_pool 的自检), 否则 pause 期间每 10 分钟
    # 误报一次"断连→无需切换"的自相矛盾报告。
    import time as _t
    for i in range(3):
        try:
            from src.scrapers.pinnacle_api import api_get
            data = api_get("/sports", bypass_pause=True)
            if data:
                return True, f"连通({len(data)}运动)"
        except Exception as e:
            pass
        if i < 2:
            _t.sleep(3)
    return False, "返回空"


def recover_pin():
    """触发代理池自动换节点。返回 (ok, detail, switched)。

    switched=False 表示代理池判定"Pin 正常无需切换"(瞬时故障已自愈),
    调用方不应把它当作一次"修复"发告警 —— 否则会出现"断连"却"无需切换"的自相矛盾报告。
    """
    try:
        r = subprocess.run([sys.executable, "-m", "src.scrapers.pin_proxy_pool", "--recover"],
                           capture_output=True, text=True, timeout=300, cwd=ROOT)
        out = (r.stdout or r.stderr)
        switched = "无需切换" not in out
        return r.returncode == 0, out[-300:], switched
    except Exception as e:
        return False, f"异常: {str(e)[:60]}", True


def main():
    fixes = []
    statuses = []

    # 1) 守护进程 + 心跳
    pid = _daemon_pid()
    hb_age = _heartbeat_age()
    if pid is None:
        statuses.append(f"守护进程: ❌ 未运行")
        if _kickstart_daemon():
            fixes.append("守护进程未运行 → 已 kickstart 重启")
        else:
            fixes.append("守护进程未运行 → kickstart 失败")
    elif hb_age is not None and hb_age > CHECK_INTERVAL:
        statuses.append(f"守护进程: ⚠️ 心跳 {hb_age/60:.0f}min 未更新(卡死)")
        if _kickstart_daemon():
            fixes.append(f"守护进程卡死(心跳 {hb_age/60:.0f}min) → 已 kickstart 重启")
        else:
            fixes.append("守护进程卡死 → kickstart 失败")
    else:
        statuses.append(f"守护进程: ✅ PID {pid} 心跳 {hb_age/60:.1f}min" if hb_age is not None else f"守护进程: ✅ PID {pid}(心跳文件未生成)")

    # 2) 增量扫描停滞
    # V5.10 修复: 原先用 glob 最新文件(COMPARISON_FILES[0]) —— 但 _FB.json 总是最
    # 新鲜(独立刷新), 恒等于最新, 于是 urgent/near 停摆几小时也测不出来(实测 2026-08-19
    # urgent/near 停摆 186 分钟而 _FB 仅 23 分钟, self_heal 全程误判"正常")。
    # 改为分别检查 urgent 和 near 各自的 mtime, 只有两者都陈旧才算停滞。
    # V5.10 再修: 对比文件 mtime 也不是可靠的存活信号 —— run_incremental 在"BB+Pin
    # 均无变动"时提前 return 且不重写对比文件, 于是"跑了但无变动"和"死了"外观一致。
    # 2026-08-21 实测: near 一轮需 8-9min, 而 self_heal 据 near 文件陈旧每 5min
    # kickstart 一次, 每次都把 near 杀在半路 → 文件永远刷不新 → 无限重启(20min 内 6 次),
    # 推送同样被杀, 全天零投注。改为读扫描心跳(每轮跑完必写, 与有无变动无关)。
    _urgent_age = _file_age(DATA_DIR / ".scan_heartbeat_urgent")
    _near_age = _file_age(DATA_DIR / ".scan_heartbeat_near")
    _worst = None
    for _a in (_urgent_age, _near_age):
        if _a is not None and (_worst is None or _a > _worst):
            _worst = _a
    scan_age = _worst
    if _urgent_age is None and _near_age is None:
        # 心跳文件尚未生成(首次部署/刚重启) → 回退看对比文件, 避免误判为"必须重启"
        _fallback = _file_age(DATA_DIR / "bb_vs_pinnacle_comparison_urgent.json")
        if _fallback is None:
            statuses.append("扫描心跳: ❌ 不存在(且无对比文件)")
            fixes.append("无扫描心跳 → 守护进程重启后会自动生成")
        else:
            statuses.append(f"增量扫描: ⏳ 心跳未生成, 回退对比文件 {_fallback/60:.0f}min 前")
    elif scan_age is not None and scan_age > SCAN_STALE:
        statuses.append(f"增量扫描: ⚠️ 心跳 {scan_age/60:.0f}min 未更新(urgent {(_urgent_age or 0)/60:.0f}min / near {(_near_age or 0)/60:.0f}min)")
        # 在飞保护: 扫描/推送子进程还在跑就别重启 —— 长轮次(near 8-9min)被腰斩比停滞更糟
        _busy = _scan_in_flight()
        # 扫描开始心跳: near 一轮要 5min+, .scan_heartbeat_near 在跑完前一直是旧的,
        # pgrep 又认不到进程内线程 → 光看 _scan_in_flight 会误判"停滞"反复 kickstart(互杀)。
        # 最近 SCAN_START_STALE 内有"开始"信号 = 扫描在飞, 不杀。
        _start_fresh = False
        for _tier in ("urgent", "near"):
            _sa = _file_age(DATA_DIR / f".scan_start_{_tier}")
            if _sa is not None and _sa < SCAN_START_STALE:
                _start_fresh = True
                break
        if _busy:
            statuses.append(f"增量扫描: ⏳ 有子进程在跑({_busy}), 本轮不重启")
        elif _start_fresh:
            statuses.append("增量扫描: ⏳ 扫描在飞(开始心跳新鲜), 本轮不重启")
        elif _kickstart_daemon():
            fixes.append(f"增量扫描停滞({scan_age/60:.0f}min, 无子进程在跑) → 已 kickstart 重启守护进程")
        else:
            fixes.append("增量扫描停滞 → kickstart 失败")
    else:
        statuses.append(f"增量扫描: ✅ 心跳 {scan_age/60:.0f}min 前更新")

    # 3) BB 数据陈旧
    bb_age = _file_age(BB_EXTRACTED)
    if bb_age is not None and bb_age > BB_STALE:
        statuses.append(f"BB 数据: ⚠️ {bb_age/60:.0f}min 未更新")
    elif bb_age is not None:
        statuses.append(f"BB 数据: ✅ {bb_age/60:.0f}min 前更新")

    # 4) Pinnacle 连通
    pin_ok, pin_detail = check_pin()
    statuses.append(f"Pinnacle: {'✅' if pin_ok else '❌'} {pin_detail}")
    if not pin_ok:
        # 铁律(2026-08-24 用户): 看门狗只告警, 不允许自动换节点。换节点由用户手动决定。
        fixes.append(f"Pinnacle 断连: {pin_detail} → 仅告警, 不自动换节点(请手动处理)")

    # 4b) Pin 缓存健康: 缓存空了(0场)会致增量扫描读空缓存→对比无结果→不推, 且
    # 扫描心跳正常(跑完了但没产出) —— 传统"看心跳/进程"看门狗测不出的静默失效。
    # 主动修复: 空缓存/陈旧且 Pin 可达 → 重拉缓存(30min 冷却)。
    if PIN_CACHE.exists():
        _cache_age = _file_age(PIN_CACHE)
        try:
            _cache_count = len(json.loads(PIN_CACHE.read_text()))
        except Exception:
            _cache_count = 0
        _cache_stale_h = _cache_age is not None and _cache_age > 3600
        if pin_ok and (_cache_count == 0 or _cache_stale_h):
            _why = "空(0场)" if _cache_count == 0 else f"{_cache_count}场陈旧"
            statuses.append(f"Pin 缓存: ⚠️ {_why} {(_cache_age or 0)/60:.0f}min 未更新")
            if _cache_repair_allowed():
                if _repopulate_pin_cache():
                    fixes.append("Pin 缓存空/陈旧 → 已触发重拉缓存")
                    _mark_cache_repair()
        elif _cache_count == 0:
            statuses.append("Pin 缓存: ⚠️ 空(0场), 但 Pin 不可达, 待恢复后重拉")
        else:
            statuses.append(f"Pin 缓存: ✅ {_cache_count}场 {(_cache_age or 0)/60:.0f}min 前更新")
    else:
        statuses.append("Pin 缓存: ⚠️ 文件不存在")

    # 5) 陈旧锁文件
    if _clear_stale_lock():
        fixes.append("清除陈旧锁文件 .pipeline_daemon.lock")

    # 报告
    if fixes:
        # 统一走 config.settings 入口: 自动注入机器人关键词(缺了会被服务端以 errcode
        # 310000 静默拒收 —— 2026-08-21 查出自愈报告因此从未送达过一次, 看门狗等于哑的),
        # 且 urgent=True 跳过非投注每日配额(自愈报告是故障告警, 不该被例行日报挤掉)。
        from config.settings import send_dingtalk
        body = "## 🔧 自愈看门狗修复报告\n\n"
        body += "**检查结果**:\n" + "\n".join(f"- {s}" for s in statuses) + "\n\n"
        body += "**自动修复**:\n" + "\n".join(f"- {f}" for f in fixes)
        # 必须校验返回值: 原先无论成败都打印"已发送", 于是关键词被拒收(errcode 310000)
        # 数月无人察觉 —— 告警自身的失败也必须可见, 否则看门狗哑了都不知道。
        try:
            _sent = send_dingtalk("自愈看门狗修复报告", body, urgent=True)
        except Exception as e:
            print(f"  ⚠️ 自愈报告发送异常: {e}")
            _sent = False
        if _sent:
            print("已发送修复报告:")
        else:
            print("⚠️ 修复报告发送失败(钉钉未送达, 检查关键词/网络), 内容如下:")
        for s in statuses:
            print(" ", s)
        for f in fixes:
            print("  ✅", f)
    else:
        print("全部正常:", "; ".join(statuses))


if __name__ == "__main__":
    main()
