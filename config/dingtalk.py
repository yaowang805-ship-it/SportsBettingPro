"""钉钉机器人推送工具 — 绕过本地 DNS 拦截

用法:
    from config.dingtalk import send_dingtalk
    send_dingtalk("投注推荐 消息内容")
    send_dingtalk("投注推荐 消息内容", msgtype="markdown", title="标题")
"""
import json

import requests

from config.logging_config import get_logger
from config.settings import DINGTALK_WEBHOOK

logger = get_logger(__name__)

_TIMEOUT = 15


def send_dingtalk(
    content: str,
    msgtype: str = "text",
    title: str = "",
) -> bool:
    """发送钉钉消息。

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

    headers = {"Content-Type": "application/json"}

    # 用 requests 发送（trust_env=False 避免 Python 3.14 环境问题）
    sess = requests.Session()
    sess.trust_env = False

    try:
        resp = sess.post(DINGTALK_WEBHOOK, json=payload, headers=headers, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("  钉钉推送异常: HTTP Error %s", resp.status_code)
            return False
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("  钉钉推送成功")
            return True
        elif result.get("errcode") == 310000:
            logger.warning('  钉钉推送失败: 关键词不匹配（需包含"投注推荐"）')
            return False
        else:
            logger.warning("  钉钉推送失败: %s", result.get("errmsg", resp.text))
            return False
    except requests.exceptions.Timeout:
        logger.warning("  钉钉推送超时")
        return False
    except requests.exceptions.ConnectionError:
        logger.warning("  钉钉推送连接失败")
        return False
    except Exception as e:
        logger.warning("  钉钉推送异常: %s", e)
        return False
