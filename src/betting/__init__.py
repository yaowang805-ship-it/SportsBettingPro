"""SportsBettingPro Betting Execution API.

已迁移至 src.betting.bb_virtual_bet 和 src.report.bb_ev_push。

此模块保留仅用于向后兼容。
"""
from typing import Optional

from config.logging_config import get_logger
from src.betting.base import BaseExecutor

logger = get_logger(__name__)


def get_executor(platform: Optional[str] = None) -> BaseExecutor:
    """已废弃 — 自动执行已被钉钉推送+手动投注取代。"""
    logger.warning("get_executor() 已废弃，请改用 bb_virtual_bet 虚拟投注")
    raise NotImplementedError("自动执行已移除，使用 bb_virtual_bet 替代")
