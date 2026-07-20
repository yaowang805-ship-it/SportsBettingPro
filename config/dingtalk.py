"""钉钉机器人推送工具 — DNS 绕过直连

Shadowrocket VPN DNS 劫持 oapi.dingtalk.com → 198.18.0.35（假 IP），
通过硬编码真实 IP + SNI（Server Name Indication）绕过。
备用机制：硬编码 IP 失败时通过 8.8.8.8 直接 DNS 查询获取新 IP。
"""
import json
import socket
import ssl
import struct
import random

from config.logging_config import get_logger
from config.settings import DINGTALK_WEBHOOK

logger = get_logger(__name__)

_TIMEOUT = 15

# oapi.dingtalk.com 真实 IP（DNS 查询 8.8.8.8 获取，备用防止 VPN 劫持）
_REAL_IP = "161.117.107.66"
_REAL_HOST = "oapi.dingtalk.com"


def _resolve_via_dns():
    """直接向 8.8.8.8 发送 DNS A 记录查询，绕过本机 VPN DNS 劫持。

    Returns:
        str | None: 解析到的 IP，失败返回 None
    """
    tid = random.randint(0, 0xFFFF)
    # 标准 DNS 查询：A 记录 (type=1), IN class
    query = struct.pack(">H", tid)  # Transaction ID
    query += struct.pack(">H", 0x0100)  # Flags: recursion desired
    query += struct.pack(">H", 1)  # Questions: 1
    query += struct.pack(">HHH", 0, 0, 0)  # Answer/Authority/Additional: 0
    # Encode oapi.dingtalk.com as DNS labels
    for part in _REAL_HOST.split("."):
        query += struct.pack("B", len(part)) + part.encode("ascii")
    query += struct.pack("B", 0)  # Root label
    query += struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(query, ("8.8.8.8", 53))
        resp, _ = sock.recvfrom(1024)
        sock.close()

        # 校验 Transaction ID
        if len(resp) < 12:
            return None
        (rid,) = struct.unpack(">H", resp[:2])
        if rid != tid:
            return None
        # 跳过 header + question section
        pos = 12
        while pos < len(resp):
            if resp[pos] == 0:
                pos += 1
                break
            pos += 1 + resp[pos]
        pos += 4  # QTYPE + QCLASS
        # 读取 answer（第一个 A 记录）
        while pos + 12 <= len(resp):
            rtype = struct.unpack(">H", resp[pos:pos+2])[0]
            rclass = struct.unpack(">H", resp[pos+2:pos+4])[0]
            rdlength = struct.unpack(">H", resp[pos+8:pos+10])[0]
            rdata = resp[pos+10:pos+10+rdlength]
            if rtype == 1 and rclass == 1 and rdlength == 4:
                return ".".join(str(b) for b in rdata)
            pos += 10 + rdlength
        return None
    except Exception:
        return None


def _resolve_host(webhook: str):
    """从 webhook URL 解析 host 和 path。"""
    rest = webhook
    if rest.startswith("https://"):
        rest = rest[len("https://"):]
    elif rest.startswith("http://"):
        rest = rest[len("http://"):]
    host = rest.split("/", 1)[0]
    path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
    return host, path


def _connect_with_fallback(hostname: str):
    """尝试用硬编码 IP 建立 SSL 连接，失败则尝试 DNS 解析备用 IP。

    Returns:
        ssl.SSLSocket | None: 连接成功返回 SSL socket
    """
    # 尝试1: 硬编码 IP
    # 尝试2: 8.8.8.8 DNS 查询
    # 尝试3: 系统 DNS（走 Shadowrocket，通常可用）
    ips = [_REAL_IP, None, None]
    for attempt in range(3):
        if attempt == 1:
            dns_ip = _resolve_via_dns()
            if not dns_ip:
                logger.warning("  DNS(8.8.8.8) 解析失败")
                continue
            ip = dns_ip
            logger.info(f"  使用 DNS(8.8.8.8) IP: {ip}")
        elif attempt == 2:
            try:
                addrs = socket.getaddrinfo(hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
                ip = addrs[0][4][0]
                logger.info(f"  使用系统DNS IP: {ip}")
            except Exception:
                logger.warning("  系统DNS解析失败")
                continue
        else:
            ip = ips[0]

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_TIMEOUT)
        try:
            sock.connect((ip, 443))
            ctx = ssl.create_default_context()
            ssock = ctx.wrap_socket(sock, server_hostname=hostname)
            labels = ["硬编码", "DNS(8.8.8.8)", "系统DNS"]
            logger.info(f"  钉钉连接成功 ({labels[attempt]} IP: {ip})")
            return ssock
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            if attempt == 0:
                logger.warning(f"  硬编码 IP {_REAL_IP} 连接失败")
            continue

    return None


def send_dingtalk(
    content: str,
    msgtype: str = "text",
    title: str = "",
) -> bool:
    """发送钉钉消息（DNS 绕过直连）。

    用真实 IP 建立 TCP 连接，SSL SNI 设置为 oapi.dingtalk.com，
    绕过 Shadowrocket VPN 的 DNS 劫持。硬编码 IP 失败时自动
    通过 8.8.8.8 DNS 查询获取新 IP 重试。

    Args:
        content: 消息正文
        msgtype: "text" 或 "markdown"
        title: markdown 模式下的标题

    Returns:
        True=成功, False=失败
    """
    if not DINGTALK_WEBHOOK:
        logger.warning("DINGTALK_WEBHOOK 未配置")
        return False

    if msgtype == "markdown":
        payload = {"msgtype": "markdown", "markdown": {"title": title or "消息", "text": content}}
    else:
        payload = {"msgtype": "text", "text": {"content": content}}

    body = json.dumps(payload)
    host, path = _resolve_host(DINGTALK_WEBHOOK)

    ssock = _connect_with_fallback(host)
    if ssock is None:
        logger.warning("  钉钉推送失败: 无法连接")
        return False

    try:
        req = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        ssock.sendall(req.encode())

        resp_data = b""
        while True:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            resp_data += chunk

        resp_text = resp_data.decode(errors="replace")
        if "\r\n\r\n" in resp_text:
            http_body = resp_text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in resp_text:
            http_body = resp_text.split("\n\n", 1)[1]
        else:
            http_body = resp_text

        result = json.loads(http_body)
        if result.get("errcode") == 0:
            logger.info("  钉钉推送成功")
            return True
        elif result.get("errcode") == 310000:
            logger.warning('  钉钉推送失败: 关键词不匹配（需包含"投注推荐"）')
            return False
        else:
            logger.warning("  钉钉推送失败: %s", result.get("errmsg", resp_text[:200]))
            return False

    except socket.timeout:
        logger.warning("  钉钉推送超时")
        return False
    except Exception as e:
        logger.warning("  钉钉推送异常: %s", e)
        return False
    finally:
        try:
            ssock.close()
        except Exception:
            pass
