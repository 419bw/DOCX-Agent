"""工作模式策略注册。"""

from .base import BaseMode
from .fill import FillMode
from .detail_edit import DetailEditMode


def get_strategy(mode: str) -> BaseMode:
    """根据 mode 字符串返回对应策略实例。"""
    if mode == "detail_edit":
        return DetailEditMode()
    return FillMode()
