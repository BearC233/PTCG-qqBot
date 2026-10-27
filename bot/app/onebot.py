"""极简 OneBot v11 客户端：正向 WebSocket 连接 + API 调用。

只依赖 aiohttp，覆盖本项目需要的 send_* / get_login_info。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    def __init__(
        self,
        ws_url: str,
        access_token: str = "",
        handler: EventHandler | None = None,
        max_backoff: float = 30.0,
    ) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self.handler = handler
        self.max_backoff = max_backoff

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._connected = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ---------------- 生命周期 ----------------

    async def run(self) -> None:
        """阻塞运行：连不上就指数退避重连。"""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15)
        )
        backoff = 1.0
        try:
            while not self._closed:
                try:
                    await self._connect_once()
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("OneBot 连接断开/失败: %s", exc)
                finally:
                    self._connected.clear()
                    self._ws = None
                    self._fail_pending("connection closed")

                if self._closed:
                    break
                log.info("%.0f 秒后重连 OneBot ...", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
        finally:
            await self._session.close()

    async def _connect_once(self) -> None:
        assert self._session is not None
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        async with self._session.ws_connect(
            self.ws_url, headers=headers, heartbeat=30, max_msg_size=0
        ) as ws:
            self._ws = ws
            self._connected.set()
            log.info("已连接 OneBot: %s", self.ws_url)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning("WebSocket 错误: %s", ws.exception())
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()
        for task in list(self._tasks):
            task.cancel()

    # ---------------- 收包 ----------------

    def _dispatch(self, raw: str) -> None:
        import json

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("收到非 JSON 数据: %.200s", raw)
            return

        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(payload)
            return

        if "post_type" not in payload:
            log.debug("忽略未知包: %.200s", raw)
            return

        if self.handler is None:
            return

        task = asyncio.create_task(self._safe_handle(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_handle(self, event: dict[str, Any]) -> None:
        try:
            assert self.handler is not None
            await self.handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("处理事件失败: %s", event.get("post_type"))

    def _fail_pending(self, reason: str) -> None:
        for echo, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(OneBotError(reason))
            self._pending.pop(echo, None)

    # ---------------- 发 API ----------------

    async def call(self, action: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        ws = self._ws
        if ws is None or ws.closed:
            raise OneBotError("OneBot 尚未连接")

        echo = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send_json({"action": action, "params": params or {}, "echo": echo})
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"{action} 超时") from exc
        finally:
            self._pending.pop(echo, None)

        if resp.get("retcode") not in (0, None):
            raise OneBotError(f"{action} 失败: {resp.get('retcode')} {resp.get('message') or resp.get('wording')}")
        return resp.get("data")

    async def send_private_msg(self, user_id: int | str, message: Any) -> Any:
        return await self.call("send_private_msg", {"user_id": int(user_id), "message": message})

    async def send_group_msg(self, group_id: int | str, message: Any) -> Any:
        return await self.call("send_group_msg", {"group_id": int(group_id), "message": message})

    async def get_login_info(self) -> dict[str, Any]:
        return await self.call("get_login_info") or {}
