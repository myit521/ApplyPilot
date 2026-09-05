"""向量嵌入提供者。

pgvector 召回依赖事实与查询的向量。DeepSeek 不提供嵌入接口，
MVP 使用本地小模型（bge-small-zh，512 维，与 schema 中
vector(512) 对应），符合设计文档第 11 节"后续可以增加本地模型
适配器"的方向。sentence-transformers 依赖较重，作为可选依赖
惰性导入；未安装时退化为仅全文检索。
"""

from __future__ import annotations

from typing import Protocol

EMBEDDING_DIM = 512
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float] | None:
        """返回文本向量；不可用时返回 None，检索退化为仅全文。"""
        ...


class NullEmbeddingProvider:
    def embed(self, text: str) -> list[float] | None:
        return None


class LocalEmbeddingProvider:
    """本地 sentence-transformers 嵌入，首次调用时加载模型。"""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._model = None

    def embed(self, text: str) -> list[float] | None:
        try:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name)
            vector = self._model.encode(text, normalize_embeddings=True)
            return [float(x) for x in vector]
        except ImportError:
            return None
