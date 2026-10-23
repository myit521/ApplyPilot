"""DeepSeek 模型适配器。

DeepSeek 提供 OpenAI 兼容的 chat/completions 接口。API key 通过
环境变量 DEEPSEEK_API_KEY 提供（docs/design.md 第 11 节：密钥不
写入仓库和数据库）。
"""

from __future__ import annotations

import os
import time
import uuid

import httpx

from .model_adapter import ModelError, RetryableModelError
from .model_usage import current_model_context, emit_model_event, usage_summary

# 单次响应内容的合理上限：超出即视为异常响应，避免把超长/失控内容
# 当作模型结果继续下游处理。
MAX_RESPONSE_CHARS = 32768


def _require_positive_int(value: object, name: str) -> int:
    """配置值必须是正整数（bool 不是 int）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelError(f"{name} 必须是正整数")
    return value


class DeepSeekAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
        max_output_tokens: int = 2048,
        max_input_chars: int = 64000,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ModelError("缺少 DEEPSEEK_API_KEY 环境变量")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_output_tokens = _require_positive_int(
            max_output_tokens, "max_output_tokens"
        )
        self.max_input_chars = _require_positive_int(max_input_chars, "max_input_chars")

    def complete(self, system: str, user: str) -> str:
        started = time.monotonic()
        event = {"provider": "deepseek", "call_id": str(uuid.uuid4()),
                 **current_model_context(), "request_started": False}
        usage = usage_summary(None)
        outcome = "error"
        try:
            self._check_input_size(system, user)
            event["request_started"] = True
            emit_model_event({**event, "event": "model_call_started"})
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
                        "max_tokens": self.max_output_tokens,
                    },
                    timeout=self.timeout,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                raise RetryableModelError("DeepSeek 请求暂时不可用") from e
            except httpx.HTTPError as e:
                raise ModelError("DeepSeek 请求失败") from e

            if resp.status_code != 200:
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    raise RetryableModelError(f"DeepSeek 返回 {resp.status_code}")
                raise ModelError(f"DeepSeek 返回 {resp.status_code}")
            try:
                payload = resp.json()
            except ValueError as e:
                raise ModelError("DeepSeek 返回了无法解析的响应") from e
            usage = usage_summary(payload)
            content = self._extract_content(payload)
            outcome = "success"
            return content
        except RetryableModelError:
            outcome = "retryable_error"
            raise
        except ModelError:
            if not event["request_started"]:
                outcome = "input_rejected"
            raise
        finally:
            emit_model_event({**event, **usage, "event": "model_call_finished",
                              "outcome": outcome,
                              "duration_ms": round((time.monotonic() - started) * 1000)})

    def _check_input_size(self, system: str, user: str) -> None:
        """在发起 HTTP 请求之前拒绝超长输入。"""
        total = len(system or "") + len(user or "")
        if total > self.max_input_chars:
            raise ModelError(
                f"输入长度 {total} 超过上限 {self.max_input_chars}，已跳过本次请求"
            )

    def _extract_content(self, payload: object) -> str:
        """校验响应信封；任何异常都转成不含响应体的 ModelError。"""
        try:
            choice = payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as e:
            raise ModelError("DeepSeek 返回了无法解析的响应") from e

        if finish_reason != "stop":
            raise ModelError("DeepSeek 响应未正常结束")
        if not isinstance(content, str) or not content.strip():
            raise ModelError("DeepSeek 响应缺少文本内容")
        if len(content) > MAX_RESPONSE_CHARS:
            raise ModelError("DeepSeek 响应内容超过长度上限")
        return content
