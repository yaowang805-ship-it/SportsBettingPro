#!/usr/bin/env python3
"""每小时检查代理(2026-11-24 用户要求): 优先恢复小火箭, 不能用则退出走直连。

流程:
1. 确保小火箭运行 + 开开关
2. 测 BB 域名(走隧道, trust_env=False 仍走 TUN fake-ip)
3. 能用 → 保持代理(首次恢复推一条钉钉「已恢复」, 之后不再重复推)
4. 不能用 → 退出小火箭走直连
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"
_RESTORED_FLAG = DATA / ".proxy_restored_notified"  # 恢复通知去重标记


def _bb_ok():
    import requests
    import urllib3
    urllib3.disable_warnings()
    tok = ""
    try:
        tok = (DATA / ".bb_token").read_text().strip()
    except Exception:
        pass
    s = requests.Session()
    s.trust_env = False  # 走 TUN(小火箭开着时劫持 fake-ip → 走代理节点)
    for dom in ("api.nsvip9.com", "api.x-vip8.com"):
        try:
            r = s.post(
                f"https://{dom}/v1/order/new/bet/list",
                json={"languageType": "CMN", "isSettled": False, "current": 1, "size": 1},
                headers={"Content-Type": "application/json", "Authorization": tok,
                         "User-Agent": "Mozilla/5.0"},
                timeout=10, verify=False,
            )
            if r.status_code == 200:
                return True
        except Exception:
            pass
    return False


def _notify_restored():
    """首次恢复推一条钉钉; 已有标记则不重复推。"""
    if _RESTORED_FLAG.exists():
        return
    try:
        from config.settings import send_dingtalk
        send_dingtalk("🟢 小火箭已恢复", "机场节点已恢复, 已切回代理, 系统恢复正常。", urgent=True)
        _RESTORED_FLAG.write_text(str(time.time()))
    except Exception:
        pass


def main():
    # 1. 打开小火箭 + 开开关
    subprocess.run(["open", "-a", "Shadowrocket"], capture_output=True)
    time.sleep(4)
    subprocess.run(["open", "-g", "shadowrocket://toggle?enabled=true"], capture_output=True)
    time.sleep(10)

    # 2. 测隧道
    if _bb_ok():
        print(f"[check_proxy] 小火箭已恢复, 保持代理 ({time.strftime('%m-%d %H:%M')})", flush=True)
        _notify_restored()
        return 0

    # 3. 不能用 → 关开关(断开隧道 NE) + 退出小火箭走直连
    # 关键: 单 pkill MacPacketTunnel 隧道会被系统重启, 必须先关开关断开 NE, 再 pkill 清残留
    subprocess.run(["open", "-g", "shadowrocket://toggle?enabled=false"], capture_output=True)
    time.sleep(4)
    subprocess.run(["pkill", "-f", "MacPacketTunnel"], capture_output=True)
    subprocess.run(["pkill", "-f", "/Applications/Shadowrocket.app"], capture_output=True)
    time.sleep(4)
    print(f"[check_proxy] 小火箭仍不可用, 已退出走直连 ({time.strftime('%m-%d %H:%M')})", flush=True)
    # 恢复失败则清标记, 下次真正恢复时能再推
    try:
        _RESTORED_FLAG.unlink()
    except FileNotFoundError:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
