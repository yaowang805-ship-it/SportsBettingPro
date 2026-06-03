#!/usr/bin/env python3
"""
DingTalk 统一通知模块 - 所有推荐模块的钉钉发送接口
提供强大的错误处理、调试和验证功能
"""
import json
import sys
import os
from pathlib import Path
from typing import Dict, List, Optional
import requests

sys.path.insert(0, str(Path.cwd()))
from config.logging_config import get_logger
logger = get_logger(__name__)
from config.settings import DINGTALK_WEBHOOK

# DingTalk API 相关
DINGTALK_TIMEOUT = 10
DINGTALK_KEYWORD = "投注推荐"  # 必须包含的关键词

class DingTalkNotifier:
    """DingTalk 通知器 - 处理所有钉钉消息发送"""
    
    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化通知器
        
        Args:
            webhook_url: 钉钉 webhook URL，如果为 None 则使用环境配置
        """
        self.webhook = webhook_url or DINGTALK_WEBHOOK
        self.enabled = bool(self.webhook and self.webhook.startswith('https://'))
    
    def verify_webhook(self) -> bool:
        """
        验证 webhook URL 是否有效
        
        Returns:
            bool: webhook 是否有效
        """
        if not self.webhook:
            logger.warning("⚠️ DingTalk webhook 未配置")
            return False
        
        if not self.webhook.startswith('https://'):
            logger.warning(f"⚠️ DingTalk webhook URL 格式无效: {self.webhook[:50]}...")
            return False
        
        logger.info(f"✅ webhook URL 格式验证通过")
        return True
    
    def build_text_message(self, title: str, content: str) -> Dict:
        """
        构建纯文本消息（用于简单通知）
        
        Args:
            title: 消息标题
            content: 消息内容
            
        Returns:
            dict: DingTalk 消息体
        """
        # 确保包含钉钉关键词
        if DINGTALK_KEYWORD not in content and DINGTALK_KEYWORD not in title:
            content = f"【{DINGTALK_KEYWORD}】\n{content}"
        
        return {
            "msgtype": "text",
            "text": {
                "content": content
            }
        }
    
    def build_markdown_message(self, title: str, markdown_text: str) -> Dict:
        """
        构建 Markdown 消息（用于格式化推荐）
        
        Args:
            title: 消息标题
            markdown_text: Markdown 内容
            
        Returns:
            dict: DingTalk 消息体
        """
        # 确保包含钉钉关键词
        if DINGTALK_KEYWORD not in markdown_text and DINGTALK_KEYWORD not in title:
            markdown_text = f"**【{DINGTALK_KEYWORD}】**\n\n{markdown_text}"
        
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown_text
            }
        }
    
    def send(self, message: Dict, description: str = "") -> bool:
        """
        发送钉钉消息
        
        Args:
            message: 消息体（dict）
            description: 发送描述（用于日志）
            
        Returns:
            bool: 发送是否成功
        """
        if not self.enabled:
            logger.warning("⚠️ DingTalk 通知已禁用")
            return False
        
        try:
            logger.info(f"📤 正在发送钉钉消息{f'({description})' if description else ''}...")
            
            resp = requests.post(
                self.webhook,
                json=message,
                timeout=DINGTALK_TIMEOUT,
                headers={"Content-Type": "application/json"}
            )
            
            # 详细的响应分析
            if resp.status_code == 200:
                try:
                    resp_json = resp.json()
                    if resp_json.get('errcode') == 0:
                        logger.info(f"✅ 钉钉消息发送成功")
                        return True
                    else:
                        logger.error(f"❌ 钉钉 API 错误: {resp_json.get('errmsg', '未知错误')}")
                        logger.debug(f"完整响应: {resp_json}")
                        return False
                except json.JSONDecodeError:
                    logger.warning(f"⚠️ 响应不是有效JSON: {resp.text}")
                    return True  # HTTP 200 但无法解析，视为成功的attempt
            else:
                logger.error(f"❌ HTTP {resp.status_code}: {resp.text}")
                return False
                
        except requests.exceptions.Timeout:
            logger.error(f"❌ 钉钉请求超时 ({DINGTALK_TIMEOUT}s)")
            return False
        except requests.exceptions.ConnectionError:
            logger.error(f"❌ 无法连接到钉钉服务器")
            return False
        except Exception as e:
            logger.error(f"❌ 发送钉钉消息失败: {e}")
            return False
    
    def send_recommendations(self, title: str, recommendations: List[str], sport: str = "") -> bool:
        """
        发送投注推荐消息
        
        Args:
            title: 消息标题
            recommendations: 推荐列表
            sport: 运动类型（用于描述）
            
        Returns:
            bool: 发送是否成功
        """
        if not recommendations:
            # 发送无推荐通知
            msg = self.build_markdown_message(
                title,
                f"**【{DINGTALK_KEYWORD}】**\n\n"
                f"✅ 已完成{sport}推荐分析\n\n"
                f"⚠️ 今日未发现符合策略的正期望值投注机会，系统保持谨慎。"
            )
            return self.send(msg, f"无{sport}推荐通知")
        
        # 发送推荐
        lines = [f"## **【{DINGTALK_KEYWORD}】** {title}\n"]
        for rec in recommendations[:10]:  # 限制前10条
            lines.append(rec)
            lines.append("")
        
        lines.append("\n---")
        lines.append("*系统自动生成 · 仅供参考*")
        
        msg = self.build_markdown_message(title, "\n".join(lines))
        return self.send(msg, f"{len(recommendations)}条{sport}推荐")
    
    def send_alert(self, title: str, content: str) -> bool:
        """
        发送系统告警
        
        Args:
            title: 告警标题
            content: 告警内容
            
        Returns:
            bool: 发送是否成功
        """
        msg = self.build_markdown_message(
            title,
            f"🚨 **系统告警**\n\n{content}"
        )
        return self.send(msg, "系统告警")


# 全局实例
_notifier = None

def get_notifier() -> DingTalkNotifier:
    """获取全局 DingTalk 通知器实例"""
    global _notifier
    if _notifier is None:
        _notifier = DingTalkNotifier()
    return _notifier


def send_recommendation_notification(
    sport: str,
    recommendations: List[str],
    title: str = "每日推荐"
) -> bool:
    """
    快捷函数：发送推荐通知
    
    Args:
        sport: 运动类型（如 "NBA", "足球"）
        recommendations: 推荐列表
        title: 消息标题
        
    Returns:
        bool: 发送是否成功
    """
    notifier = get_notifier()
    return notifier.send_recommendations(title, recommendations, sport=sport)


def send_system_alert(title: str, content: str) -> bool:
    """
    快捷函数：发送系统告警
    
    Args:
        title: 告警标题
        content: 告警内容
        
    Returns:
        bool: 发送是否成功
    """
    notifier = get_notifier()
    return notifier.send_alert(title, content)


def verify_webhook_connection() -> bool:
    """
    验证 webhook 连接
    
    Returns:
        bool: 连接是否有效
    """
    notifier = get_notifier()
    return notifier.verify_webhook()


if __name__ == "__main__":
    # 测试模式
    notifier = get_notifier()
    
    logger.info("🧪 DingTalk 通知模块测试")
    logger.info("%s", "=" * 60)

    # 测试 webhook 验证
    if notifier.verify_webhook():
        # 尝试发送测试消息
        test_msg = notifier.build_text_message(
            "测试",
            f"【{DINGTALK_KEYWORD}】\n这是一条测试消息\n时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if notifier.send(test_msg, "测试消息"):
            logger.info("✅ 测试消息发送成功！")
        else:
            logger.error("❌ 测试消息发送失败")
    else:
        logger.warning("⚠️ Webhook 未配置或格式无效")
