"""DNS 劫持守卫 — 自动检测 Shadowrocket 劫持并修复。

每次管线启动时运行，检测关键域名是否被劫持。
如果被劫持 → 自动通过 DNS over HTTPS 获取真实 IP → 更新 /etc/hosts 和 Python socket 补丁。
"""
import json
import os
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

STATE_FILE = DATA_DIR / "dns_guard_state.json"

# 需要保护的关键域名
PROTECTED_DOMAINS = {
    "guest.api.arcadia.pinnacle.com": "Pinnacle API",
    "pinnacle.com": "Pinnacle 网站",
    "oapi.dingtalk.com": "钉钉推送",
}

# Shadowrocket 劫持特征：IP 在 198.18.0.x 范围内
HIJACK_PREFIX = "198.18.0."


def _resolve_via_doh(hostname: str) -> list:
    """通过 DNS over HTTPS 获取真实 IP 列表。"""
    try:
        url = f"https://dns.google/resolve?name={hostname}&type=A"
        req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        return ips if ips else []
    except Exception as e:
        logger.warning(f"DNS over HTTPS 查询 {hostname} 失败: {e}")
        return []


def _resolve_local(hostname: str) -> str:
    """本地 DNS 解析结果。"""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return ""


def _is_hijacked(ip: str) -> bool:
    """判断 IP 是否被劫持。"""
    return ip.startswith(HIJACK_PREFIX)


def check_and_fix() -> dict:
    """检测所有关键域名的 DNS 劫持状态，必要时修复。

    Returns:
        dict: {hostname: {status, local_ip, real_ips, fixed}}
    """
    results = {}
    hijacked_any = False

    for hostname, label in PROTECTED_DOMAINS.items():
        local_ip = _resolve_local(hostname)
        hijacked = _is_hijacked(local_ip)
        result = {"label": label, "local_ip": local_ip, "hijacked": hijacked, "real_ips": [], "fixed": False}

        if hijacked:
            hijacked_any = True
            real_ips = _resolve_via_doh(hostname)
            result["real_ips"] = real_ips

            if real_ips:
                # Fix /etc/hosts
                _fix_hosts_file(hostname, real_ips[0])
                result["fixed"] = True
                logger.warning(f"🔧 {label} ({hostname}): {local_ip} → {real_ips[0]}")
            else:
                logger.warning(f"⚠️ {label} ({hostname}): 被劫持但无法获取真实IP")
        else:
            logger.info(f"✅ {label} ({hostname}): {local_ip}")

        results[hostname] = result

    # Save state
    state = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "hijacked_any": hijacked_any,
        "results": {h: {"label": r["label"], "hijacked": r["hijacked"], "fixed": r["fixed"],
                         "local_ip": r["local_ip"], "real_ips": r["real_ips"]}
                    for h, r in results.items()},
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    return results


def _fix_hosts_file(hostname: str, real_ip: str):
    """更新 /etc/hosts 绕过劫持。

    如果已有旧条目（可能是过期的假IP），先删除再添加。
    """
    hosts_path = "/etc/hosts"

    try:
        with open(hosts_path) as f:
            lines = f.readlines()
    except PermissionError:
        logger.warning("无权限修改 /etc/hosts, 跳过")
        return

    # Remove old entries for this hostname
    new_lines = []
    for line in lines:
        if hostname in line and HIJACK_PREFIX not in line:
            # Keep non-hijacked entries (previous fixes)
            pass  # Will replace below
        if hostname not in line:
            new_lines.append(line)

    # Add new entry
    new_lines.append(f"{real_ip} {hostname}\n")

    try:
        # Write to a temp file first, then move (need sudo for actual /etc/hosts)
        tmp_path = "/tmp/dns_guard_hosts"
        with open(tmp_path, "w") as f:
            f.writelines(new_lines)

        result = subprocess.run(
            ["sudo", "cp", tmp_path, hosts_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info(f"  ✅ /etc/hosts 已更新: {hostname} → {real_ip}")
        else:
            logger.warning(f"  ⚠️ /etc/hosts 更新失败: {result.stderr}")
    except Exception as e:
        logger.warning(f"  ⚠️ /etc/hosts 更新异常: {e}")


def get_dns_status() -> str:
    """返回当前 DNS 状态摘要，供健康检查或推送使用。"""
    results = check_and_fix()
    hijacked = [r["label"] for r in results.values() if r["hijacked"]]
    if hijacked:
        return f"⚠️ DNS劫持: {', '.join(hijacked)}"
    return "✅ DNS正常"


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()

    print("=== DNS 劫持检测 ===")
    results = check_and_fix()
    for hostname, r in results.items():
        status = "🔴 被劫持" if r["hijacked"] else "🟢 正常"
        fixed = " → ✅ 已修复" if r["fixed"] else ""
        print(f"  {status} {r['label']}: {r['local_ip']}{fixed}")
    print(f"\n状态保存到 {STATE_FILE}")
