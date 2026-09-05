"""DeepSeek 模型适配器。

DeepSeek 提供 OpenAI 兼容的 chat/completions 接口。API key 通过
环境变量 DEEPSEEK_API_KEY 提供（docs/design.md 第 11 节：密钥不
写入仓库和数据库）。
"""

from __future__ import annotations

import os

import httpx

from .model_adapter import ModelError


class DeepSeekAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ModelError("缺少 DEEPSEEK_API_KEY 环境变量")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise ModelError(f"DeepSeek 请求失败: {e}") from e

        if resp.status_code != 200:
            raise ModelError(f"DeepSeek 返回 {resp.status_code}: {resp.text[:200]}")
        return resp.json()["choices"][0]["message"]["content"]
