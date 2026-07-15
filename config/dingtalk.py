"""钉钉机器人推送工具 — DNS 绕过直连

Shadowrocket VPN DNS 劫持 oapi.dingtalk.com → 198.18.0.35（假 IP），
通过硬编码真实 IP + SNI（Server Name Indication）绕过。
"""
import json
import socket
import ssl

from config.logging_config import get_logger
from config.settings import DINGTALK_WEBHOOK

logger = get_logger(__name__)

_TIMEOUT = 15

# oapi.dingtalk.com 真实 IP（DNS 查询 8.8.8.8 获取，备用防止 VPN 劫持）
_REAL_IP = "161.117.107.66"


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


def send_dingtalk(
    content: str,
    msgtype: str = "text",
    title: str = "",
) -> bool:
    """发送钉钉消息（DNS 绕过直连）。

    用真实 IP 建立 TCP 连接，SSL SNI 设置为 oapi.dingtalk.com，
    绕过 Shadowrocket VPN 的 DNS 劫持。

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

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT)
    try:
        sock.connect((_REAL_IP, 443))

        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(sock, server_hostname=host)

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
        # 找到 body 部分
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
            sock.close()
        except Exception:
            pass
