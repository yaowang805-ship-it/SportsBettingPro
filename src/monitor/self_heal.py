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

CHECK_INTERVAL = 15 * 60       # 心跳超时阈值(秒)
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
        # 锁文件里通常有 "PID xxx"
        import re
        m = re.search(r"PID\s+(\d+)", txt)
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
    scan_age = _file_age(COMPARISON_FILES[0]) if COMPARISON_FILES else None
    if scan_age is None:
        statuses.append("对比文件: ❌ 不存在")
        fixes.append("无对比文件 → 守护进程重启后会自动扫描")
    elif scan_age > SCAN_STALE:
        statuses.append(f"增量扫描: ⚠️ 对比文件 {scan_age/60:.0f}min 未更新(预期<45min)")
        # 守护进程心跳正常但扫描停滞 → 可能 Pin 封禁卡住, 重启
        if _kickstart_daemon():
            fixes.append(f"增量扫描停滞({scan_age/60:.0f}min) → 已 kickstart 重启守护进程")
        else:
            fixes.append("增量扫描停滞 → kickstart 失败")
    else:
        statuses.append(f"增量扫描: ✅ 对比文件 {scan_age/60:.0f}min 前更新")

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
        body = "## 🔧 自愈看门狗修复报告\n\n"
        body += "**检查结果**:\n" + "\n".join(f"- {s}" for s in statuses) + "\n\n"
        body += "**自动修复**:\n" + "\n".join(f"- {f}" for f in fixes)
        try:
            send_dingtalk(body, msgtype="markdown", title="自愈看门狗修复报告")
        except Exception:
            send_dingtalk(body, msgtype="text", title="自愈看门狗修复报告")
        print("已发送修复报告:")
        for s in statuses:
            print(" ", s)
        for f in fixes:
            print("  ✅", f)
    else:
        print("全部正常:", "; ".join(statuses))


if __name__ == "__main__":
    main()
