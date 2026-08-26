from __future__ import annotations

import contextlib
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ShopConnection:
    def __init__(
        self,
        endpoint: str,
        *,
        transport_factory: Any = streamable_http_client,
        session_factory: Any = ClientSession,
    ) -> None:
        self._endpoint = endpoint
        self._transport_factory = transport_factory
        self._session_factory = session_factory
        self._stack: contextlib.AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> ClientSession:
        self._stack = contextlib.AsyncExitStack()
        try:
            read_stream, write_stream = await self._stack.enter_async_context(
                self._transport_factory(self._endpoint)
            )
            session = await self._stack.enter_async_context(
                self._session_factory(read_stream, write_stream)
            )
            await session.initialize()
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            raise
        self.session = session
        return session

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self.session = None
