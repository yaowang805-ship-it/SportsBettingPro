#!/usr/bin/env python3
"""自愈网络(2026-11-11 用户要求): 自动重启 Shadowrocket 隧道, 不行就彻底重启 app。

用户要的步骤:
1. 确保 Shadowrocket 在运行
2. 重启隧道(kill MacPacketTunnel, 等价于「关闭开关 → 再打开开关」刷新网络)
3. 检测网络(连 BB 域名)
4. 不行 → 彻底关闭 Shadowrocket(kill 主进程)再打开
5. 再检测

5 分钟冷却(文件锁), 避免多进程/频繁触发时重复自愈。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
LOCK_FILE = DATA / ".network_self_heal.lock"
COOLDOWN = 5 * 60  # 5 分钟冷却


def _bb_domain():
    try:
        dom = (DATA / ".bb_domain").read_text().strip().rstrip("/")
        if dom.startswith("http"):
            return dom
    except Exception:
        pass
    return "https://api.x-vip8.com"


def _network_ok():
    """检测 BB 域名是否连通(HTTPS 探活, 能握手即算网络通, 不依赖 token 有效性)。"""
    import requests
    import urllib3
    urllib3.disable_warnings()
    s = requests.Session()
    s.trust_env = False
    try:
        s.post(f'{_bb_domain()}/v1/order/new/bet/list',
               json={'languageType': 'CMN', 'isSettled': False, 'current': 1, 'size': 1},
               headers={'Content-Type': 'application/json', 'Authorization': 'x',
                        'User-Agent': 'Mozilla/5.0'},
               timeout=8, verify=False)
        return True  # 能连上(哪怕 token 无效, 说明网络通)
    except Exception:
        return False


def _get_nodes():
    """解析 Shadowrocket 的 ServerManager(bplist/NSKeyedArchiver), 提取节点名列表。

    节点配置存在 Group Containers/group.com.liguangming.Shadowrocket/ServerManager。
    返回 [(title, host), ...], 过滤掉订阅条目(title 为 None)。
    """
    import os
    import plistlib
    p = os.path.expanduser(
        '~/Library/Group Containers/group.com.liguangming.Shadowrocket/ServerManager')
    if not os.path.exists(p):
        return []
    try:
        d = plistlib.load(open(p, 'rb'))
    except Exception:
        return []
    objs = d.get('$objects', [])

    def _resolve(o, depth=0):
        if depth > 50:
            return None
        if isinstance(o, plistlib.UID):
            return _resolve(objs[o.data], depth + 1)
        if isinstance(o, list):
            return [_resolve(x, depth) for x in o]
        if isinstance(o, dict):
            return {k: _resolve(v, depth) for k, v in o.items()}
        return o

    nodes = []
    for o in objs:
        if not isinstance(o, dict):
            continue
        cls = o.get('$class')
        cn = None
        if isinstance(cls, plistlib.UID):
            cob = objs[cls.data]
            if isinstance(cob, dict):
                cn = cob.get('$classname')
        if cn == 'DLWServer':
            title = _resolve(o.get('title'))
            host = _resolve(o.get('host'))
            if title and isinstance(title, str):
                nodes.append((title, host if isinstance(host, str) else ''))
    return nodes


def _switch_node():
    """从节点列表随机选一个切换(shadowrocket://select?s=节点名)。返回切换的节点名或 None。"""
    import random
    import subprocess
    import urllib.parse
    nodes = _get_nodes()
    if not nodes:
        return None
    title = random.choice(nodes)[0]
    enc = urllib.parse.quote(title)
    subprocess.run(["open", "-g", f"shadowrocket://select?s={enc}"], capture_output=True)
    return title


def main():
    # 冷却(文件锁): COOLDOWN 秒内不自愈第二次, 防多进程/频繁触发重复重启 Shadowrocket
    now = time.time()
    try:
        last = float(LOCK_FILE.read_text().strip() or 0)
        if now - last < COOLDOWN:
            print(f"自愈冷却中(距上次 {int(now - last)}s), 跳过")
            return 2
    except Exception:
        pass
    LOCK_FILE.write_text(str(now))

    # 1. 确保 Shadowrocket 在运行
    subprocess.run(["open", "-a", "Shadowrocket"], capture_output=True)
    time.sleep(3)

    # 2. 重启隧道(关开关 → 再开开关的等价操作: kill 隧道扩展, Shadowrocket 会自动重连)
    print("自愈: 重启 Shadowrocket 隧道...", flush=True)
    subprocess.run(["pkill", "-f", "MacPacketTunnel"], capture_output=True)
    time.sleep(6)

    # 3. 检测网络
    if _network_ok():
        print("自愈: 隧道重启后网络恢复 ✅", flush=True)
        return 0

    # 4. 彻底重启 Shadowrocket(kill 主进程 → open app)
    print("自愈: 隧道重启无效, 彻底重启 Shadowrocket...", flush=True)
    subprocess.run(["pkill", "-f", "/Applications/Shadowrocket.app"], capture_output=True)
    time.sleep(4)
    subprocess.run(["open", "/Applications/Shadowrocket.app"], capture_output=True)
    time.sleep(10)

    # 5. 再检测
    if _network_ok():
        print("自愈: 彻底重启后网络恢复 ✅", flush=True)
        return 0

    # 6. 换节点(节点失效时重启隧道/app 都无效, 循环尝试多个节点直到网络通, 最多 6 个)
    import random
    import urllib.parse
    nodes = _get_nodes()
    random.shuffle(nodes)
    for _i, (_title, _host) in enumerate(nodes[:6]):
        _enc = urllib.parse.quote(_title)
        subprocess.run(["open", "-g", f"shadowrocket://select?s={_enc}"], capture_output=True)
        print(f"自愈: 换节点 {_i + 1}/{min(6, len(nodes))} -> {_title}, 等 8s...", flush=True)
        time.sleep(8)
        if _network_ok():
            print(f"自愈: 换节点 {_title} 后网络恢复 ✅", flush=True)
            return 0

    print("自愈: 失败, 网络仍不通 ❌", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
