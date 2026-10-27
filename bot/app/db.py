"""PostgreSQL 连接池与核心表结构。"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id         BIGSERIAL PRIMARY KEY,
    session    TEXT        NOT NULL,
    role       TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages (session, id DESC);
CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages (created_at);
"""


async def create_pool(dsn: str, min_size: int = 1, max_size: int = 4) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=max(min_size, 1),
        max_size=max(max_size, min_size),
        command_timeout=30,
    )
    async with pool.acquire() as conn:
        await conn.execute(CORE_SCHEMA)
    log.info("PostgreSQL 连接成功")
    return pool
