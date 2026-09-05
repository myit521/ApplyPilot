"""模型适配器协议。

对应 docs/design.md 第 5 节"模型适配器"。工作流各节点只依赖
该协议，具体提供商（OpenAI、DeepSeek、本地模型等）在部署时注入，
便于测试替换和后续增加本地模型适配器。
"""

from __future__ import annotations

from typing import Protocol


class ModelError(Exception):
    """模型调用失败（超时、限流、服务异常等），节点可据此重试。"""


class ModelAdapter(Protocol):
    def complete(self, system: str, user: str) -> str:
        """调用模型并返回原始文本输出。"""
        ...
