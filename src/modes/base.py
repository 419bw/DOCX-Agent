"""工作模式策略基类。"""

from abc import ABC, abstractmethod


class BaseMode(ABC):
    """Agent 工作模式的策略接口。

    每个策略实现完整的 run() 异步生成器循环，
    通过 agent 参数调用共享基础设施（LLM 调用、工具 dispatch 等）。
    """

    @abstractmethod
    async def run(self, agent):
        """主循环。async generator，yield 事件 dict 给 server.py 转发前端。"""
        ...
