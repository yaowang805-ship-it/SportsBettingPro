"""Pipeline 异常告警 — 扫描链失败时通过钉钉通知。"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# 必须用 config.settings 入口: config.dingtalk 不注入机器人关键词, 消息会被钉钉以
# errcode 310000 静默拒收。本模块目前无调用者, 但若将来接线, 用错入口=接了等于没接。
from config.settings import send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)


def alert(step: str, error: Exception, context: str = ""):
    """发送 Pipeline 环节失败告警到钉钉。

    Args:
        step: 环节名称（如 "BB API 提取" / "Pinnacle 对比" / "推送"）
        error: 异常对象
        context: 额外上下文信息
    """
    tb = "".join(traceback.format_exception_only(type(error), error)).strip()
    msg = (
        f"⚠️ Pipeline 环节异常\n\n"
        f"环节: {step}\n"
        f"错误: {tb}\n"
    )
    if context:
        msg += f"上下文: {context}\n"
    msg += f"\n请检查后重试。"

    logger.warning("Pipeline 告警: [%s] %s", step, tb)
    try:
        if not send_dingtalk("Pipeline 告警", msg, urgent=True):
            logger.error("Pipeline 告警未送达(钉钉返回失败)")
    except Exception as e:
        logger.error("发送 Pipeline 告警失败: %s", e)


def alert_if_failed(step: str, success: bool, context: str = ""):
    """条件告警：success=False 时发送。"""
    if not success:
        alert(step, Exception("环节返回失败状态"), context)
