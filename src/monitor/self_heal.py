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
SELF_HEAL_PUSH_COOLDOWN_FILE = DATA_DIR / "self_heal_push_cooldown.json"
SELF_HEAL_PUSH_COOLDOWN = 30 * 60  # 修复报告推送冷却(秒): 30min 只推一次, 长故障期防刷屏(2026-09-15)


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

    防重复: 已有 --pin-cache 在跑则跳过 —— 否则 Pin 挂时每 30min 冷却后就 Popen 一个
    卡死进程, 累积 6+ 个孤儿进程耗尽 fd(2026-09-09 熔断+看门狗互杀+投注全停的根因)。
    """
    try:
        r = subprocess.run(["pgrep", "-f", r"bb_vs_pinnacle.*--pin-cache"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return False  # 已有进程在跑, 不重复启动
        subprocess.Popen(
            [sys.executable, "-m", "src.scrapers.bb_vs_pinnacle", "--pin-cache"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _parse_etime(etime):
    """解析 ps etime: 'MM:SS' / 'HH:MM:SS' / 'D-HH:MM:SS' → 秒。失败返回 None。"""
    try:
        parts = etime.split("-")
        days = int(parts[0]) if len(parts) == 2 else 0
        hms = parts[-1].split(":")
        if len(hms) == 2:
            h, m, s = 0, int(hms[0]), int(hms[1])
        else:
            h, m, s = int(hms[0]), int(hms[1]), int(hms[2])
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return None


def _kill_stale_pin_cache(max_age_min=15):
    """清理卡死超时的 --pin-cache 孤儿进程(防 Pin 挂时累积 fd 耗尽)。

    正常 --pin-cache 拉全量联赛 1-2min 完成; 超过 15min 说明 Pin 请求卡死(重试循环不退出),
    直接 kill, 让下一轮 self_heal 重新评估缓存健康。
    """
    try:
        r = subprocess.run(["pgrep", "-f", r"bb_vs_pinnacle.*--pin-cache"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return
        for pid_s in r.stdout.split():
            pid = pid_s.strip()
            if not pid.isdigit():
                continue
            et = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                                capture_output=True, text=True, timeout=5)
            secs = _parse_etime((et.stdout or "").strip())
            if secs is not None and secs > max_age_min * 60:
                subprocess.run(["kill", pid], capture_output=True, timeout=5)
                print(f"[self_heal] 清理卡死 --pin-cache PID {pid} (运行 {secs/60:.0f}min)", flush=True)
    except Exception:
        pass


def check_pin():
    # 2026-09-20 锚点已换 odds-api.io(Betfair公平价+Sbobet置信度), Pin 已暂停(15min CDN陈旧)。
    # 此检查从「Pin 连通」改为「odds-api.io 连通」(新锚点健康)。
    import time as _t
    for i in range(3):
        try:
            from src.scrapers.odds_api_io import get_sports
            data = get_sports()
            if data:
                return True, f"连通({len(data)}运动)"
        except Exception:
            pass
        if i < 2:
            _t.sleep(3)
    return False, "返回空"


def check_no_bets():
    """自检(2026-09-20): 有滚球比赛但长时间没实盘投注 → 诊断是「真 bug」还是「正常无机会」。

    阈值: 有 live 比赛但 >30min 没投注 → 深入诊断(不直接告警)。这是传统「看进程/心跳」看门狗
    测不出的静默失效(监控活着、扫描在跑、但就是不下单), 用户要求主动发现。
    2026-09-23 修误报: 只在「释放清单空」或「监控日志>5min未更新(静默失效)」时告警;
    释放清单非空 + 监控在比价 = 只是当前无释放盘口的+EV机会(假溢价被数据驱动拦截), 正常不告警。
    """
    try:
        from src.scrapers.pinnacle_live import fetch_bb_live_matches
        live = fetch_bb_live_matches(sport_ids=(1, 3), platform="BB")
    except Exception as e:
        return True, f"BB getList 拉取失败({str(e)[:40]})"
    n_live = len(live)
    if n_live == 0:
        return True, "无滚球比赛(正常)"
    # 最近实盘投注时间(last_bet_ts.txt, 由 record_global_bet 落盘)
    _last_bet_file = DATA_DIR / "last_bet_ts.txt"
    last_bet = 0.0
    if _last_bet_file.exists():
        try:
            last_bet = float(_last_bet_file.read_text().strip() or 0)
        except (OSError, ValueError):
            pass
    idle_min = (time.time() - last_bet) / 60 if last_bet > 0 else float('inf')
    if idle_min < 30:
        return True, f"{n_live}场滚球, 最近投注{idle_min:.0f}min前(正常)"
    # 诊断: 区分「真 bug(该投没投)」vs「正常(数据驱动拦截假溢价不投)」。
    # 2026-09-23 修误报: 之前只看「释放清单空」, 释放清单非空时也照告警「原因待查」, 把
    # 「假溢价被赔率区间拦截、释放盘口无+EV机会」的正常情况误判成静默失效, 反复刷屏。
    reasons = []
    # 1) 释放清单是否为空(空 = 无已释放盘口, 永远投不了 = 真 bug)
    released = []
    try:
        rl = json.loads((DATA_DIR / "market_release.json").read_text())
        released = [x for x in rl.get("observe_released", []) if len(x) >= 5 and x[-1] == "live"]
    except Exception:
        pass
    if not released:
        reasons.append("滚球释放清单为空(无已释放盘口)")
    # 2) 监控是否在正常比价: second_level_monitor.log 每 2s 轮询 + 每 60s 结算, 持续写;
    #    >5min 未更新 = 进程假死/崩溃 = 静默失效 = 真 bug
    _mon_age = _file_age(LOGS_DIR / "second_level_monitor.log")
    if _mon_age is None or _mon_age > 5 * 60:
        reasons.append(f"监控日志{(0 if _mon_age is None else _mon_age)/60:.0f}min未更新(疑似静默失效)")
    if reasons:
        return False, f"⚠️ {n_live}场滚球但{idle_min:.0f}min没投注: {'; '.join(reasons)}"
    # 释放清单非空 + 监控在比价 = 只是当前无释放盘口的+EV机会(假溢价被数据驱动拦截), 正常
    return True, f"{n_live}场滚球, {idle_min:.0f}min没投(释放盘口无+EV机会, 正常)"


def check_gubbing():
    """gubbing 限注监控(2026-09-21): 下单被拒率飙升是软书限注的前兆(CLV 转正就封, 甚至盈利前就封)。

    读 bet_health.json 的 success/rejected 计数, 近 24h 被拒率 >30% 且 total>=20 → 告警。
    """
    f = DATA_DIR / "bet_health.json"
    if not f.exists():
        return True, "无投注健康数据(未下过单, 正常)"
    try:
        d = json.loads(f.read_text())
    except Exception:
        return True, "投注健康数据读取失败"
    total = d.get("total", 0)
    rejected = d.get("rejected", 0)
    if total < 20:
        return True, f"投注样本不足({total}笔, 需>=20才判)"
    rate = rejected / total * 100
    if rate < 30:
        return True, f"下单被拒率 {rate:.0f}% ({rejected}/{total}, 正常)"
    return False, f"⚠️ 下单被拒率 {rate:.0f}% ({rejected}/{total}) 偏高, 疑似被限注(gubbing)"


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
    # 先清理卡死超时的 --pin-cache 孤儿进程(防累积 fd 耗尽, 2026-09-09 根因)
    _kill_stale_pin_cache()
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

    # 4c) 有滚球比赛但没实盘投注(2026-09-20 自检: 监控活着但不下单的静默失效)
    _nb_ok, _nb_detail = check_no_bets()
    statuses.append(f"滚球投注: {'✅' if _nb_ok else '❌'} {_nb_detail}")
    if not _nb_ok:
        fixes.append(f"滚球投注异常: {_nb_detail}")

    # 4d) gubbing 限注监控(2026-09-21: 下单被拒率飙升是软书限注前兆)
    _gb_ok, _gb_detail = check_gubbing()
    statuses.append(f"限注监控: {'✅' if _gb_ok else '❌'} {_gb_detail}")
    if not _gb_ok:
        fixes.append(f"gubbing 疑似: {_gb_detail}")

    # 5) 陈旧锁文件
    if _clear_stale_lock():
        fixes.append("清除陈旧锁文件 .pipeline_daemon.lock")

    # 报告
    if fixes:
        # 推送冷却(2026-09-15): urgent=True 绕过标题冷却, 长故障期间每 10min 推一次刷屏。
        # 加独立冷却: 30min 内同类报告只推一次(用户反馈"看门狗总是钉钉推送")。
        _now = time.time()
        _last_push = 0.0
        try:
            if SELF_HEAL_PUSH_COOLDOWN_FILE.exists():
                _last_push = float(json.loads(SELF_HEAL_PUSH_COOLDOWN_FILE.read_text()).get("ts", 0) or 0)
        except Exception:
            pass
        if _now - _last_push < SELF_HEAL_PUSH_COOLDOWN:
            print(f"修复报告冷却中(距上次 {(_now - _last_push) / 60:.0f}min), 跳过推送:")
            for s in statuses:
                print(" ", s)
            for f in fixes:
                print("  ✅", f)
            return
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
        # 记录推送时间戳(无论成败), 30min 内不再推
        try:
            SELF_HEAL_PUSH_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
            SELF_HEAL_PUSH_COOLDOWN_FILE.write_text(json.dumps({"ts": time.time()}))
        except Exception:
            pass
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
