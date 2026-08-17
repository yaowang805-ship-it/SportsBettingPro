"""Pin 代理池 — Shadowrocket 节点自动切换 + Pin 连通健康检查。

背景(见记忆 pinnacle-cloudflare-block): Pin 的 Cloudflare 会封 IP。根因是
"换节点不换 IP" —— 日本/台湾节点共用同一个 AWS 东京出口 IP, 换日↔台 IP 不变。
只有换**不同国家**(香港/美国/德国)才真正换 IP。

本脚本:
  1. 解析 Shadowrocket 的 ServerManager(binary plist) 拿节点列表(UUID/国家/名称)
  2. 读 DLWServerNotify.nosync 拿当前节点 UUID
  3. 写新 UUID 到 DLWServerNotify.nosync 切节点
  4. curl api.ipify.org 验证出口 IP 真变了
  5. Pin API 连通性自检

⚠️ 不自动接入流水线(用户要求"不影响现在网络环境")。等 Pin 再次封禁时手动跑:
    .venv312/bin/python -m src.scrapers.pin_proxy_pool --recover

用法:
  --list                列出所有节点(国家/名称/出口)
  --current             显示当前节点 + 当前出口 IP
  --switch <HK|US|DE|...>  切换到指定国家的节点(挑一个该国节点)
  --recover             检测封禁 → 自动换国家节点 → 自检, 直到 Pin 恢复
"""
import sys, os, json, time, argparse, plistlib, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

SHADOWROCKET_DIR = Path.home() / "Library" / "Group Containers" / "group.com.liguangming.Shadowrocket"
SERVER_MANAGER = SHADOWROCKET_DIR / "ServerManager"
NOTIFY_FILE = SHADOWROCKET_DIR / "DLWServerNotify.nosync"

# 换节点后等待 Shadowrocket 重连的秒数
SWITCH_WAIT = 8


def _unarchive(obj, objs, cache):
    """最小 NSKeyedArchiver 解档器。"""
    if isinstance(obj, plistlib.UID):
        u = obj.data
        if u in cache:
            return cache[u]
        cache[u] = None
        r = _unarchive(objs[u], objs, cache)
        cache[u] = r
        return r
    if isinstance(obj, dict):
        return {k: _unarchive(v, objs, cache)
                for k, v in obj.items() if k not in ("$class", "$classname", "$classes")}
    if isinstance(obj, list):
        return [_unarchive(x, objs, cache) for x in obj]
    return obj


def _str_of(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("NS.string", "NS.mutableString"):
            if k in v:
                return v[k]
    return ""


def load_nodes():
    """解析 ServerManager, 返回 [{uuid, title, flag, host}] (排除订阅节点 type=Subscribe)。"""
    if not SERVER_MANAGER.exists():
        return []
    with open(SERVER_MANAGER, "rb") as f:
        pl = plistlib.load(f)
    objs = pl["$objects"]
    root = _unarchive(objs[pl["$top"]["root"].data], objs, {})
    nodes = []
    for node in root.get("NS.objects", []):
        if not isinstance(node, dict):
            continue
        if _str_of(node.get("type")) == "Subscribe":
            continue
        uuid = _str_of(node.get("uuid"))
        if not uuid:
            continue
        nodes.append({
            "uuid": uuid,
            "title": _str_of(node.get("title")),
            "flag": _str_of(node.get("flag")),
            "host": _str_of(node.get("host")),
        })
    return nodes


def get_current_uuid():
    if NOTIFY_FILE.exists():
        return NOTIFY_FILE.read_text().strip()
    return ""


def switch_node(uuid):
    """写新 UUID 到 DLWServerNotify.nosync, 原子写入 + 等待 Shadowrocket 重连。"""
    tmp = NOTIFY_FILE.with_suffix(".tmp")
    tmp.write_text(uuid)
    os.replace(tmp, NOTIFY_FILE)
    print(f"  ⏳ 已切节点, 等待 {SWITCH_WAIT}s 让 Shadowrocket 重连...")
    time.sleep(SWITCH_WAIT)


def get_exit_ip():
    """查询当前出口 IP(失败返回空串)。"""
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "10", "https://api.ipify.org"],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def check_pin():
    """Pin 连通性自检。返回 (ok, detail)。"""
    try:
        from src.scrapers.pinnacle_api import api_get
        # bypass_pause=True: 自检必须真实请求, 不能被封禁暂停(自己设的)挡住
        data = api_get("/sports", bypass_pause=True)
        if data:
            return True, f"连通(返回 {len(data)} 项)"
        return False, "API 返回空"
    except Exception as e:
        return False, f"异常: {str(e)[:80]}"


def _flag_of(nodes, uuid):
    for n in nodes:
        if n["uuid"] == uuid:
            return n["flag"], n["title"]
    return "?", "?"


def do_recover(nodes, max_attempts=10):
    """检测封禁 → 换国家节点 → 自检, 直到 Pin 恢复。"""
    current = get_current_uuid()
    cur_flag, cur_title = _flag_of(nodes, current)
    ip0 = get_exit_ip()
    print(f"当前节点: {cur_title} ({cur_flag}) | 出口 IP: {ip0 or '未知'}")
    ok, detail = check_pin()
    if ok:
        print(f"✅ Pin 正常: {detail}, 无需切换")
        return True

    print(f"❌ Pin 异常: {detail}, 开始自动换节点...")

    # 优先换不同国家的节点(同国可能共用一个出口 IP)
    tried_uuids = {current}
    for _ in range(max_attempts):
        # 按国家分组, 优先选与当前不同国家的节点
        by_flag = {}
        for n in nodes:
            by_flag.setdefault(n["flag"], []).append(n)
        candidates = []
        for flag, lst in by_flag.items():
            if flag != cur_flag:
                candidates.extend(lst)
        if not candidates:
            candidates = nodes  # 兜底: 无不同国家节点, 用全部

        # 选一个没试过的节点
        pick = next((n for n in candidates if n["uuid"] not in tried_uuids), None)
        if pick is None:
            print("❌ 所有节点都试过了, Pin 仍不可用")
            return False
        tried_uuids.add(pick["uuid"])

        print(f"  → 切换 {pick['title']} ({pick['flag']}) {pick['host']}")
        switch_node(pick["uuid"])
        ip = get_exit_ip()
        changed = (ip != ip0) if ip0 else True
        print(f"     出口 IP: {ip or '未知'}{' (IP 已变 ✅)' if changed else ' (IP 未变 ⚠️)'}")
        ok, detail = check_pin()
        if ok:
            print(f"  ✅ Pin 恢复: {detail}")
            return True
        print(f"  ❌ 仍异常: {detail}")
        cur_flag = pick["flag"]
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--current", action="store_true")
    ap.add_argument("--switch", metavar="FLAG", help="切到指定国家节点, 如 HK/US/DE/TW/JP")
    ap.add_argument("--recover", action="store_true")
    args = ap.parse_args()

    nodes = load_nodes()
    if not nodes:
        print("❌ 解析不到节点(ServerManager 不存在或格式变了)")
        sys.exit(1)

    if args.list:
        for n in nodes:
            print(f"  {n['flag']:3s} | {n['title']:20s} | {n['host']:40s} | {n['uuid']}")
        return

    if args.current:
        cur = get_current_uuid()
        flag, title = _flag_of(nodes, cur)
        ip = get_exit_ip()
        print(f"当前节点: {title} ({flag}) | UUID: {cur} | 出口 IP: {ip or '未知'}")
        ok, detail = check_pin()
        print(f"Pin 连通: {'✅ ' + detail if ok else '❌ ' + detail}")
        return

    if args.switch:
        target = args.switch.upper()
        cands = [n for n in nodes if n["flag"] == target]
        if not cands:
            print(f"❌ 无 {target} 国家节点, 可选: {sorted({n['flag'] for n in nodes})}")
            sys.exit(1)
        pick = cands[0]
        print(f"切换 → {pick['title']} ({pick['flag']})")
        switch_node(pick["uuid"])
        ip = get_exit_ip()
        print(f"出口 IP: {ip or '未知'}")
        ok, detail = check_pin()
        print(f"Pin 连通: {'✅ ' + detail if ok else '❌ ' + detail}")
        return

    if args.recover:
        ok = do_recover(nodes)
        sys.exit(0 if ok else 1)

    ap.print_help()


if __name__ == "__main__":
    main()
