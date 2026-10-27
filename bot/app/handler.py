"""消息路由：白名单 / 触发词 / 限流 / 上下文 / 回复。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any

from .config import Settings
from .llm import LLMClient
from .memory import ConversationStore
from .onebot import OneBotClient
from .tools import TOOLS, PokemonTools

log = logging.getLogger(__name__)

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_AT_RE = re.compile(r"\[CQ:at,qq=([^\]]+)\]")
_RESET_WORDS = {"/reset", "/clear", "清空上下文", "重置对话"}


class RateLimiter:
    """单窗口（60s）计数限流。"""

    def __init__(self, per_minute: int) -> None:
        self.limit = max(per_minute, 1)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class BotHandler:
    def __init__(
        self,
        settings: Settings,
        client: OneBotClient,
        store: ConversationStore,
        llm: LLMClient,
        toolbox: PokemonTools | None = None,
    ) -> None:
        self.cfg = settings
        self.b = settings.behavior
        self.client = client
        self.store = store
        self.llm = llm
        self.toolbox = toolbox
        self.tools = TOOLS if toolbox else None

        self.base_prompt = settings.llm.system_prompt
        self.kb_prompt_suffix = (
            "\n\n你可以调用宝可梦资料库工具。凡涉及属性、种族值、特性、进化、招式、克制等事实，先查再答，不许编。"
            "条件筛选（属性组合、种族值门槛、会某招式等）必须用 search_pokemon 的结构化参数，"
            "只有记不清名字时才用 keyword 模糊搜。"
            "招式、特性、道具的具体效果用 get_move_info / get_ability_info / get_item_info 查，"
            "查不到就说不知道，别补印象里的说法。"
            "查到结果后只答被问到的那个点：让列名单就只给名字，别点评、别排名、别补种族值；"
            "问某个数值就只报数值。返回里没被问到的字段（传说身份、世代、英文名、身高体重、进化链等）一个字都别提。"
            "不要回复\"我查一下\"这类空话，必须直接给结果或说明查不到。"
            "只查和问题直接相关的数据，别顺手动用无关的工具。"
        )

        self._sem = asyncio.Semaphore(max(self.b.max_concurrency, 1))
        self._user_rl = RateLimiter(self.b.rate_limit.user_per_minute)
        self._group_rl = RateLimiter(self.b.rate_limit.group_per_minute)
        self._seen: dict[Any, float] = {}

    def _system_prompt(self) -> str:
        if self.toolbox and self.llm.tools_ok:
            return self.base_prompt + self.kb_prompt_suffix
        return self.base_prompt

    # ---------------- 入口 ----------------

    async def on_event(self, event: dict[str, Any]) -> None:
        if event.get("post_type") != "message":
            return

        self_id = str(event.get("self_id", ""))
        user_id = str(event.get("user_id", ""))
        if not user_id or user_id == self_id:
            return

        if self._is_duplicate(event.get("message_id")):
            return

        text, at_targets = self._extract(event)
        message_type = event.get("message_type")
        sender = event.get("sender") or {}
        nickname = str(sender.get("card") or sender.get("nickname") or user_id)

        if message_type == "private":
            session = f"private:{user_id}"
            if not self._private_allowed(user_id):
                return
            text = self._strip_prefix(text)
            if not text:
                return
            if not self._user_rl.allow(user_id):
                await self._say_private(user_id, "你发得太快啦，休息一下再聊～")
                return
            await self._respond(session, text, False, user_id, None, nickname)

        elif message_type == "group":
            group_id = str(event.get("group_id", ""))
            if not group_id or not self._group_allowed(group_id, user_id):
                return

            triggered = self_id in at_targets
            text, prefix_hit = self._strip_prefix_with_flag(text)
            triggered = triggered or prefix_hit

            if self.b.require_at_in_group and not triggered:
                return
            if not text or text == self_id:
                return
            if not self._group_rl.allow(group_id) or not self._user_rl.allow(user_id):
                await self._say_group(group_id, user_id, "这个群问得太快啦，等一分钟再试～")
                return

            session = f"group:{group_id}"
            await self._respond(session, text, True, user_id, group_id, nickname)

    # ---------------- 权限 / 清洗 ----------------

    def _private_allowed(self, user_id: str) -> bool:
        if not self.b.private_enabled:
            return False
        if user_id in self.b.user_blacklist:
            return False
        return not self.b.private_whitelist or user_id in self.b.private_whitelist

    def _group_allowed(self, group_id: str, user_id: str) -> bool:
        if not self.b.group_enabled:
            return False
        if user_id in self.b.user_blacklist:
            return False
        return not self.b.group_whitelist or group_id in self.b.group_whitelist

    def _extract(self, event: dict[str, Any]) -> tuple[str, list[str]]:
        """返回 (纯文本, 被 @ 的 qq 列表)，兼容 array 与 CQ 字符串两种上报格式。"""
        message = event.get("message")
        texts: list[str] = []
        ats: list[str] = []

        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                data = seg.get("data") or {}
                if seg_type == "text":
                    texts.append(str(data.get("text", "")))
                elif seg_type == "at":
                    ats.append(str(data.get("qq", "")))
        else:
            raw = str(message or "")
            ats = _AT_RE.findall(raw)
            texts.append(_CQ_RE.sub("", raw))

        return "".join(texts).strip(), [a for a in ats if a]

    def _strip_prefix_with_flag(self, text: str) -> tuple[str, bool]:
        for prefix in self.b.group_trigger_prefixes:
            if prefix and text.startswith(prefix):
                return text[len(prefix):].lstrip(" ：:，,"), True
        return text, False

    def _strip_prefix(self, text: str) -> str:
        stripped, _ = self._strip_prefix_with_flag(text)
        return stripped

    def _is_duplicate(self, message_id: Any) -> bool:
        if message_id is None:
            return False
        now = time.monotonic()
        for mid in [k for k, ts in self._seen.items() if now - ts > 300]:
            self._seen.pop(mid, None)
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False

    # ---------------- 核心 ----------------

    async def _respond(
        self,
        session: str,
        text: str,
        is_group: bool,
        user_id: str,
        group_id: str | None,
        nickname: str,
    ) -> None:
        if text.strip() in _RESET_WORDS:
            await self.store.clear(session)
            if is_group and group_id:
                await self._say_group(group_id, user_id, "上下文已清空，我们重新开始吧。")
            else:
                await self._say_private(user_id, "上下文已清空，我们重新开始吧。")
            return

        async with self._sem:
            started = time.monotonic()
            history = await self.store.history(session)
            user_content = f"[{nickname}]: {text}" if is_group else text
            messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
            messages.extend(history)
            messages.append({"role": "user", "content": user_content})

            answer = await self.llm.chat(
                messages,
                tools=self.tools,
                tool_executor=self.toolbox.dispatch if self.toolbox else None,
            )

            await self.store.add(session, "user", user_content)
            await self.store.add(session, "assistant", answer)

            for chunk in split_text(answer, self.b.max_reply_chars):
                if is_group and group_id:
                    await self._say_group(group_id, user_id, chunk)
                else:
                    await self._say_private(user_id, chunk)

            log.info(
                "session=%s user=%s cost=%.1fs in=%d out=%d",
                session, user_id, time.monotonic() - started, len(text), len(answer),
            )

    async def _say_private(self, user_id: str, text: str) -> None:
        try:
            await self.client.send_private_msg(user_id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("私聊发送失败 user=%s: %s", user_id, exc)

    async def _say_group(self, group_id: str, user_id: str, text: str) -> None:
        message = [
            {"type": "at", "data": {"qq": user_id}},
            {"type": "text", "data": {"text": " " + text}},
        ]
        try:
            await self.client.send_group_msg(group_id, message)
        except Exception as exc:  # noqa: BLE001
            log.warning("群消息发送失败 group=%s: %s", group_id, exc)


def split_text(text: str, limit: int) -> list[str]:
    """按行累积切分，超长单行硬切。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(limit, 50)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not buf:
            buf = line
        elif len(buf) + len(line) + 1 <= limit:
            buf = f"{buf}\n{line}"
        else:
            chunks.append(buf)
            buf = line
    if buf:
        chunks.append(buf)
    return chunks
