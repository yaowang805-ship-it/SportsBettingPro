"""钉钉机器人推送工具 — 绕过本地 DNS 拦截（Shadowrocket 网络扩展）

用法:
    from config.dingtalk import send_dingtalk
    send_dingtalk("投注推荐 消息内容")
    send_dingtalk("投注推荐 消息内容", msgtype="markdown", title="标题")
"""
import json
import ssl
import urllib.request
import urllib.error

from config.logging_config import get_logger
from config.settings import DINGTALK_WEBHOOK

logger = get_logger(__name__)

# Shadowrocket 网络扩展将 DNS 劫持到 198.18.x.x，需手动解析
_DINGTALK_IP = "47.246.137.199"
_TIMEOUT = 10


def send_dingtalk(
    content: str,
    msgtype: str = "text",
    title: str = "",
) -> bool:
    """发送钉钉消息，绕过 DNS 劫持直连 IP。

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

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 用真实 IP 替换域名，通过 Host header 让服务器正确识别
    url = DINGTALK_WEBHOOK.replace("oapi.dingtalk.com", _DINGTALK_IP)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Host": "oapi.dingtalk.com",
        },
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx)
        body = resp.read().decode()
        result = json.loads(body)
        if result.get("errcode") == 0:
            logger.info("  钉钉推送成功")
            return True
        elif result.get("errcode") == 310000:
            logger.warning('  钉钉推送失败: 关键词不匹配（需包含"投注推荐"）')
            return False
        else:
            logger.warning("  钉钉推送失败: %s", result.get("errmsg", body))
            return False
    except Exception as e:
        logger.warning("  钉钉推送异常: %s", e)
        return False
