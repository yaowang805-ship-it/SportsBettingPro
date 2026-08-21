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
       - 断 → 触发代理池自动换节点(pin_proxy_pool --recover)
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
BB_STALE = 3 * 3600            # BB 数据陈旧阈值(秒)


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


def check_pin():
    try:
        from src.scrapers.pinnacle_api import api_get
        data = api_get("/sports")
        return bool(data), (f"连通({len(data)}运动)" if data else "返回空")
    except Exception as e:
        return False, f"异常: {str(e)[:60]}"


def recover_pin():
    """触发代理池自动换节点。"""
    try:
        r = subprocess.run([sys.executable, "-m", "src.scrapers.pin_proxy_pool", "--recover"],
                           capture_output=True, text=True, timeout=300, cwd=ROOT)
        return r.returncode == 0, (r.stdout or r.stderr)[-300:]
    except Exception as e:
        return False, f"异常: {str(e)[:60]}"


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
        if _busy:
            statuses.append(f"增量扫描: ⏳ 有子进程在跑({_busy}), 本轮不重启")
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
        ok, detail = recover_pin()
        if ok:
            fixes.append(f"Pinnacle 断连 → 自动换节点成功: {detail[:80]}")
        else:
            fixes.append(f"Pinnacle 断连 → 自动换节点失败: {detail[:80]}")

    # 5) 陈旧锁文件
    if _clear_stale_lock():
        fixes.append("清除陈旧锁文件 .pipeline_daemon.lock")

    # 报告
    if fixes:
        from config.dingtalk import send_dingtalk
        # 钉钉机器人配了关键词过滤(必须含"投注推荐"), 缺了会被服务端静默拒收 ——
        # 2026-08-21 查出自愈报告因此从未送达过任何一次("关键词不匹配"), 等于看门狗哑了。
        body = "## 🔧 自愈看门狗修复报告 (投注推荐系统)\n\n"
        body += "**检查结果**:\n" + "\n".join(f"- {s}" for s in statuses) + "\n\n"
        body += "**自动修复**:\n" + "\n".join(f"- {f}" for f in fixes)
        # 必须校验返回值: 原先无论成败都打印"已发送", 于是关键词被拒收(errcode 310000)
        # 数月无人察觉 —— 告警自身的失败也必须可见, 否则看门狗哑了都不知道。
        try:
            _sent = send_dingtalk(body, msgtype="markdown", title="自愈看门狗修复报告")
        except Exception as e:
            print(f"  ⚠️ markdown 发送异常({e}), 回退 text")
            _sent = send_dingtalk(body, msgtype="text", title="自愈看门狗修复报告")
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
