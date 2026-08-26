import asyncio

import pytest

from emptor.client import ShopConnection


class _FakeSession:
    def __init__(self, read_stream, write_stream):
        self.read_stream = read_stream
        self.write_stream = write_stream
        self.initialized = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def initialize(self):
        self.initialized = True


class _FakeTransport:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __aenter__(self):
        return ("fake-read-stream", "fake-write-stream")

    async def __aexit__(self, *exc_info):
        return None


async def test_shop_connection_initializes_session():
    connection = ShopConnection(
        "http://example.test",
        transport_factory=_FakeTransport,
        session_factory=_FakeSession,
    )
    async with connection as session:
        assert session.initialized is True
        assert session.read_stream == "fake-read-stream"
        assert session.write_stream == "fake-write-stream"
    assert connection.session is None


async def test_shop_connection_passes_endpoint_to_transport():
    seen = {}

    class _RecordingTransport(_FakeTransport):
        def __init__(self, endpoint):
            seen["endpoint"] = endpoint
            super().__init__(endpoint)

    connection = ShopConnection(
        "http://shop.example.test:8000",
        transport_factory=_RecordingTransport,
        session_factory=_FakeSession,
    )
    async with connection:
        pass

    assert seen["endpoint"] == "http://shop.example.test:8000"


async def test_shop_connection_cleans_up_on_initialize_failure():
    closed = {"transport": False, "session": False}

    class _TrackedTransport(_FakeTransport):
        async def __aexit__(self, *exc_info):
            closed["transport"] = True
            return await super().__aexit__(*exc_info)

    class _FailingSession(_FakeSession):
        async def initialize(self):
            raise RuntimeError("handshake failed")

        async def __aexit__(self, *exc_info):
            closed["session"] = True
            return await super().__aexit__(*exc_info)

    connection = ShopConnection(
        "http://example.test",
        transport_factory=_TrackedTransport,
        session_factory=_FailingSession,
    )

    with pytest.raises(RuntimeError):
        async with connection:
            pass

    assert closed["transport"] is True
    assert closed["session"] is True
    assert connection.session is None


async def test_shop_connection_fails_fast_on_unreachable_endpoint():
    # CLI-05: uses the real (default) transport_factory against an address
    # with nothing listening, so failure is a fast connection-refused rather
    # than a slow DNS/network timeout. Wrapped in wait_for as the actual
    # proof of "bounded time, no infinite hang" - if ShopConnection ever
    # regressed into hanging here, this test would time out and fail instead
    # of hanging the whole suite.
    connection = ShopConnection("http://127.0.0.1:1/mcp")

    async def attempt():
        async with connection:
            pass

    with pytest.raises(Exception):
        await asyncio.wait_for(attempt(), timeout=15)
