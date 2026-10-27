"""会话上下文存储：PostgreSQL，按会话保留最近 N 条 + TTL。"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


class ConversationStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        history_turns: int = 8,
        ttl_minutes: int = 120,
        max_item_chars: int = 1000,
    ) -> None:
        self.pool = pool
        self.history_turns = max(history_turns, 0)
        self.ttl_minutes = max(ttl_minutes, 1)
        self.max_item_chars = max(max_item_chars, 50)

    async def add(self, session: str, role: str, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        if len(content) > self.max_item_chars:
            content = content[: self.max_item_chars] + "…"
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chat_messages (session, role, content) VALUES ($1, $2, $3)",
                session,
                role,
                content,
            )

    async def history(self, session: str) -> list[dict[str, Any]]:
        if self.history_turns <= 0:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content FROM (
                    SELECT id, role, content
                    FROM chat_messages
                    WHERE session = $1
                      AND created_at >= now() - ($2 || ' minutes')::interval
                    ORDER BY id DESC
                    LIMIT $3
                ) t ORDER BY id ASC
                """,
                session,
                str(self.ttl_minutes),
                self.history_turns,
            )
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def clear(self, session: str) -> int:
        async with self.pool.acquire() as conn:
            status = await conn.execute("DELETE FROM chat_messages WHERE session = $1", session)
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def prune(self) -> int:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM chat_messages WHERE created_at < now() - ($1 || ' minutes')::interval",
                str(self.ttl_minutes),
            )
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0
