"""Exercise streaming exchange key reuse against a fake recorder on 127.0.0.1."""

import asyncio
import os
import secrets

import pytest

from pydtcpip.client import DTCPClient, _ExchangeKeyStore
from pydtcpip.dtcp.ake import ExchangeKey
from pydtcpip.errors import DLNAError
from tests.fakes import FakeAuthority, FakeDevice, FakeMediaServer, FakeSourceAKE


# Synthetic MPEG-TS: 188-byte packets starting with the sync byte, split across several protected packets
_TS_CONTENT = b''.join(b'\x47' + os.urandom(187) for _ in range(100))


def _authentication_count(source: FakeSourceAKE) -> int:
    """
    Count authentications the fake source accepted.

    Args:
        source (FakeSourceAKE): Fake source

    Returns:
        int: Number of accepted CHALLENGE requests
    """

    return source.log.count('challenge')


async def _stream(client: DTCPClient, url: str) -> bytes:
    """
    Stream a recording and return the decrypted content.

    Args:
        client (DTCPClient): Client under test
        url (str): Recording URL

    Returns:
        bytes: Decrypted content
    """

    received = bytearray()

    async def on_data(chunk: bytes) -> None:
        received.extend(chunk)

    await client.stream(url, on_data)
    return bytes(received)


@pytest.mark.anyio
async def test_stream_reuses_cached_exchange_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Authenticate once and reuse the exchange key for later streams from the same source."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        for _ in range(3):
            assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 1
    assert source.errors == []


@pytest.mark.anyio
async def test_stream_without_cache_authenticates_every_time(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Keep authenticating for every stream when exchange key reuse is off."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5) as client,
    ):
        for _ in range(2):
            assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 2


@pytest.mark.anyio
async def test_authenticate_in_advance_skips_authentication_when_streaming(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Save the exchange key from authenticate() and use it for the first stream."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        await client.authenticate(server.url)
        assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 1


@pytest.mark.anyio
async def test_stream_reauthenticates_when_source_rotates_label(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Replace a cached key whose label no longer matches, as after the source restarts with a new key."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        source.exchange_key = secrets.token_bytes(12)
        source.exchange_key_label = (source.exchange_key_label + 1) % 256
        assert await _stream(client, server.url) == _TS_CONTENT
        assert await _stream(client, server.url) == _TS_CONTENT
        store = client._exchange_keys  # pyright: ignore[reportPrivateUsage]
        assert store is not None
        cached = store.get('127.0.0.1', source.port)

    assert _authentication_count(source) == 2
    assert cached is not None and cached.label == source.exchange_key_label


@pytest.mark.anyio
async def test_stream_reauthenticates_when_key_changes_with_same_label(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Detect a replaced key with an unchanged label from the decrypted content and authenticate again."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        source.exchange_key = secrets.token_bytes(12)
        assert await _stream(client, server.url) == _TS_CONTENT
        assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 2


@pytest.mark.anyio
async def test_stream_with_cached_key_accepts_content_other_than_mpeg_ts(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Deliver non-TS content unchanged after confirming the cached key is still current."""

    content = os.urandom(4000 * 3 + 1234)
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, content) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        assert await _stream(client, server.url) == content
        assert await _stream(client, server.url) == content

    assert _authentication_count(source) == 2


@pytest.mark.anyio
async def test_stream_reauthenticates_when_source_refuses_cached_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Authenticate again and retry once when the source refuses a request made with a cached key."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        server.http_errors['GET'] = (403, {})
        store = client._exchange_keys  # pyright: ignore[reportPrivateUsage]
        assert store is not None
        original_set = store.set

        def set_and_accept(host: str, port: int, key: ExchangeKey) -> None:
            server.http_errors.pop('GET', None)
            original_set(host, port, key)

        store.set = set_and_accept
        assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 2
    assert [method for method, _ in server.requests].count('GET') == 3


@pytest.mark.anyio
async def test_stream_does_not_retry_busy_source_with_cached_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Report 503 from a busy source without authenticating again."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        server.http_errors['GET'] = (503, {})
        with pytest.raises(DLNAError):
            await _stream(client, server.url)

    assert _authentication_count(source) == 1


@pytest.mark.anyio
async def test_store_expires_keys_after_idle_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expire a key after the idle lifetime without decryption, restarting the period on each use."""

    now = 1000.0
    monkeypatch.setattr('pydtcpip.client.time.monotonic', lambda: now)
    store = _ExchangeKeyStore(60)
    key = ExchangeKey(value=bytes(12), label=1)
    store.set('Example.Local', 8080, key)
    assert store.get('example.local', 8080) == key
    now = 1050.0
    store.touch('example.local', 8080, key)
    now = 1100.0
    assert store.get('example.local', 8080) == key
    now = 1110.0
    assert store.get('example.local', 8080) is None
    store.clear()


@pytest.mark.anyio
async def test_store_timer_expires_unused_keys() -> None:
    """Remove a key from memory when its idle lifetime passes even if the client is not used again."""

    store = _ExchangeKeyStore(0.05)
    store.set('127.0.0.1', 8080, ExchangeKey(value=bytes(12), label=1))
    await asyncio.sleep(0.2)
    assert store._keys == {}  # pyright: ignore[reportPrivateUsage]
    assert store._timer is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.anyio
async def test_expire_exchange_keys_forces_authentication(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Authenticate again after the application expires held keys, as when the network is lost."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        client.expire_exchange_keys()
        assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 2


@pytest.mark.anyio
async def test_close_expires_exchange_keys(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Expire held keys when the client closes."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
    ):
        client = DTCPClient(sink_device.credentials(), timeout=5, reuse_exchange_keys=True)
        assert await _stream(client, server.url) == _TS_CONTENT
        await client.close()
        store = client._exchange_keys  # pyright: ignore[reportPrivateUsage]
        assert store is not None and store.get('127.0.0.1', source.port) is None
