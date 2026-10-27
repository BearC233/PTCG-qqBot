"""入口：加载配置 -> 连接 PostgreSQL / OneBot -> 分发消息。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import asyncpg

from .config import load_settings
from .db import create_pool
from .handler import BotHandler
from .kb import Pokedex
from .llm import LLMClient
from .memory import ConversationStore
from .onebot import OneBotClient
from .tools import PokemonTools

log = logging.getLogger("bot")


async def _periodic_prune(store: ConversationStore, interval: int = 600) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            removed = await store.prune()
            if removed:
                log.info("清理过期上下文 %d 条", removed)
        except Exception:  # noqa: BLE001
            log.exception("清理上下文失败")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )

    settings = load_settings(os.environ.get("BOT_CONFIG", "config.yaml"))
    log.info("模型: %s @ %s", settings.llm.model, settings.llm.base_url)
    log.info("OneBot: %s", settings.onebot.ws_url)

    b = settings.behavior
    log.info(
        "策略: 私聊=%s 白名单=%s | 群聊=%s | 单用户限流 %d 次/分钟",
        "开" if b.private_enabled else "关",
        ",".join(b.private_whitelist) if b.private_whitelist else "全部",
        "开" if b.group_enabled else "关",
        b.rate_limit.user_per_minute,
    )

    pool: asyncpg.Pool | None = None
    if settings.database.dsn:
        pool = await create_pool(
            settings.database.dsn, settings.database.min_pool, settings.database.max_pool
        )
    else:
        log.warning("未配置 PostgreSQL DSN，上下文记忆将不可用")

    store = ConversationStore(
        pool=pool,  # type: ignore[arg-type]
        history_turns=settings.memory.history_turns,
        ttl_minutes=settings.memory.history_ttl_minutes,
        max_item_chars=settings.memory.max_item_chars,
    )
    llm = LLMClient(settings.llm)

    toolbox = None
    if settings.kb.enabled and pool is not None:
        toolbox = PokemonTools(Pokedex(pool, settings.kb.max_results))
        log.info("宝可梦知识库已启用")

    client = OneBotClient(
        ws_url=settings.onebot.ws_url,
        access_token=settings.onebot.access_token,
        max_backoff=settings.onebot.max_backoff,
    )
    handler = BotHandler(settings, client, store, llm, toolbox)
    client.handler = handler.on_event

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    client_task = asyncio.create_task(client.run(), name="onebot")
    prune_task = asyncio.create_task(_periodic_prune(store), name="prune")

    if await client.wait_connected(15):
        try:
            info = await client.get_login_info()
            log.info("QQ 已登录: %s (%s)", info.get("nickname"), info.get("user_id"))
        except Exception as exc:  # noqa: BLE001
            log.warning("获取登录信息失败: %s", exc)
    else:
        log.warning("15 秒内未连上 OneBot，请检查 NapCat 是否已登录且 WS 已开启")

    try:
        await stop.wait()
    finally:
        log.info("正在退出 ...")
        for task in (prune_task, client_task):
            task.cancel()
        await client.close()
        if pool is not None:
            await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
