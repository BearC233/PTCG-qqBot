"""大模型调用：任意 OpenAI 兼容接口（DeepSeek / 通义 / Kimi / OneAPI / vLLM ...）。

支持 function calling：模型可以主动调用宝可梦知识库工具。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from .config import LLMConfig

log = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


_MD_PATTERNS = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.M), ""),
]


def clean_reply(text: str) -> str:
    """去掉思考模式残留与 QQ 不渲染的 Markdown 标记。"""
    text = (text or "").strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    # 去掉行首列表符号，并把换行合并成单段（QQ 里像连珠炮更自然）
    text = re.sub(r"(?m)^\s*(?:\d+[.、)]|[-*•])\s*", "", text)
    text = re.sub(r"\n+\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.api_key)
        self._client = AsyncOpenAI(
            api_key=cfg.api_key or "sk-not-configured",
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            max_retries=0,
        )
        # 模型是否支持 function calling；运行期遇到不支持会自动置 False
        self.tools_ok = bool(cfg.tools)
        if not self.enabled:
            log.warning("未配置 LLM API Key，将返回占位回复（仅用于框架自测）")

    @staticmethod
    def _is_tool_unsupported(exc: Exception) -> bool:
        """判断报错是否因为模型不支持 tools / function calling。"""
        text = str(exc).lower()
        if any(k in text for k in ("tool", "function call", "function_call")):
            return True
        status = getattr(exc, "status_code", None)
        return status in (400, 422)

    async def _create(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.cfg.retries + 2):
            use_tools = bool(tools) and self.tools_ok
            try:
                kwargs: dict[str, Any] = {
                    "model": self.cfg.model,
                    "messages": messages,
                    "temperature": self.cfg.temperature,
                    "max_tokens": self.cfg.max_tokens,
                }
                if use_tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if self.cfg.extra_body:
                    kwargs["extra_body"] = self.cfg.extra_body
                return await self._client.chat.completions.create(**kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if use_tools and self._is_tool_unsupported(exc):
                    self.tools_ok = False
                    log.warning(
                        "模型 %s 不支持 function calling，自动降级为纯对话模式（知识库将不可用）: %s",
                        self.cfg.model, exc,
                    )
                    continue
                log.warning("LLM 调用失败(第 %d 次): %s", attempt + 1, exc)
                if attempt < self.cfg.retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(str(last_error)) if last_error else RuntimeError("LLM 调用失败")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_rounds: int = 4,
    ) -> str:
        if not self.enabled:
            return "（框架已跑通，但还没配置大模型 API Key，请在 /opt/qqbot/.env 填入 LLM_API_KEY 后重启 bot）"

        convo: list[dict[str, Any]] = list(messages)
        try:
            for _ in range(max_rounds):
                resp = await self._create(convo, tools)
                message = resp.choices[0].message
                tool_calls = getattr(message, "tool_calls", None)

                if not tool_calls or tool_executor is None:
                    content = clean_reply(message.content or "")
                    return content or "（模型没有返回内容）"

                convo.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments or "{}",
                                },
                            }
                            for call in tool_calls
                        ],
                    }
                )
                for call in tool_calls:
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    log.info("调用知识库工具 %s(%s)", call.function.name, args)
                    result = await tool_executor(call.function.name, args)
                    convo.append({"role": "tool", "tool_call_id": call.id, "content": result})

            return "（查得太深了，换个问法我再试试？）"
        except Exception as exc:  # noqa: BLE001
            log.error("LLM 最终失败: %s", exc)
            return "（我这边调用模型失败了，稍后再试试）"
