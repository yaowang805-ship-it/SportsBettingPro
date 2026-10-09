#!/usr/bin/env python3
"""Chrome 自动打开监控 —— 抓真凶(谁 open 了 Chrome)。

方案(2026-10-09):
1. 后台跑 `log stream` 实时监听 macOS 统一日志里 Chrome 的启动事件 —— 关键: open 命令的发起者
   进程会出现在日志里(process 字段), 这是事后 log show 查不到、实时 stream 能抓到的原因。
2. 主循环每 3s 轮询 Chrome 主进程, 检测新实例。
3. 新实例出现时: 记录 Chrome 信息(配置目录判投注系统还是普通) + 投注系统进程快照。

日志都写在 data/logs/chrome_launch_watch.log:
  - [stream] 行 = log stream 实时捕获的 Chrome 事件(含发起者)
  - [检测] 行 = 轮询检测到的新实例现场

用法:
  .venv312/bin/python scripts/monitor_chrome_launch.py            # 前台常驻
  .venv312/bin/python scripts/monitor_chrome_launch.py --once     # 跑一次快照(测试)
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "logs" / "chrome_launch_watch.log"
STREAM_LOG = ROOT / "data" / "logs" / "chrome_stream.log"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def chrome_main_pids():
    try:
        r = subprocess.run(["pgrep", "-f", CHROME_BIN],
                           capture_output=True, text=True, timeout=5)
        return {int(x) for x in r.stdout.split() if x.strip().isdigit()}
    except Exception:
        return set()


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def snapshot(pid):
    L = [f"[检测] ===== Chrome 新实例 {time.strftime('%Y-%m-%d %H:%M:%S')} ====="]
    L.append(f"[检测] Chrome: {_run(['ps', '-p', str(pid), '-o', 'pid=,ppid=,lstart=,command=']) or '(ps 失败)'}")
    lsof = _run(["lsof", "-p", str(pid)], timeout=15)
    for ln in lsof.splitlines():
        if "chrome-bb" in ln:
            L.append("[检测] 配置目录: .chrome-bb ← 投注系统的 9222 Chrome"); break
        if "Application Support/Google/Chrome/Default" in ln:
            L.append("[检测] 配置目录: Default ← 普通 Chrome(非投注系统)"); break
    L.append(f"[检测] 9222 端口: {'监听' if _run(['lsof', '-iTCP:9222', '-sTCP:LISTEN']) else '无'}")
    bps = _run(["pgrep", "-fl", "token_check_push|renew_bb_login|launch_chrome_9222|remote-debugging-port"])
    L.append(f"[检测] 投注系统进程: {bps or '(无, 没在续期)'}")
    return "\n".join(L)


def start_stream():
    """后台跑 log stream, 实时监听 Chrome 启动事件(含发起者)。"""
    try:
        with open(STREAM_LOG, "a", encoding="utf-8") as f:
            subprocess.Popen(
                ["/usr/bin/log", "stream", "--predicate",
                 'eventMessage CONTAINS "Google Chrome"',
                 "--style", "compact"],
                stdout=f, stderr=f)
        return True
    except Exception:
        return False


def main():
    if "--once" in sys.argv:
        pids = chrome_main_pids()
        print(f"当前 Chrome 主进程: {pids or '无'}")
        for p in pids:
            print(snapshot(p))
        return

    LOG.parent.mkdir(parents=True, exist_ok=True)
    if start_stream():
        _log(f"[stream] ===== log stream 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    known = chrome_main_pids()
    _log(f"[检测] 监控启动, 已知 Chrome 主进程: {known or '无'}")
    print(f"Chrome 监控启动。日志: {LOG}\nstream 日志: {STREAM_LOG}\n(看到浏览器自动打开时, 看这两个文件)")
    while True:
        time.sleep(3)
        try:
            cur = chrome_main_pids()
            for pid in (cur - known):
                _log(snapshot(pid))
                print(snapshot(pid))
            known = cur
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[monitor] 异常: {type(e).__name__} {e}")


if __name__ == "__main__":
    main()
