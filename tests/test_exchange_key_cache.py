"""Exercise streaming exchange key reuse against a fake recorder on 127.0.0.1."""

import os
import secrets

import pytest

from pydtcpip.client import DTCPClient, InMemoryExchangeKeyCache
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

    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
    ):
        for _ in range(3):
            assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 1
    assert source.errors == []


@pytest.mark.anyio
async def test_stream_without_cache_authenticates_every_time(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Keep authenticating for every stream when no exchange key cache is configured."""

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

    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
    ):
        key = await client.authenticate(server.url)
        assert key.label == source.exchange_key_label
        assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 1


@pytest.mark.anyio
async def test_stream_reauthenticates_when_source_rotates_label(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Replace a cached key whose label no longer matches, as after the source restarts with a new key."""

    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        source.exchange_key = secrets.token_bytes(12)
        source.exchange_key_label = (source.exchange_key_label + 1) % 256
        assert await _stream(client, server.url) == _TS_CONTENT
        assert await _stream(client, server.url) == _TS_CONTENT
        cached = cache.get('127.0.0.1', source.port)

    assert _authentication_count(source) == 2
    assert cached is not None and cached.label == source.exchange_key_label


@pytest.mark.anyio
async def test_stream_reauthenticates_when_key_changes_with_same_label(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Detect a replaced key with an unchanged label from the decrypted content and authenticate again."""

    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
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
    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, content) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
    ):
        assert await _stream(client, server.url) == content
        assert await _stream(client, server.url) == content

    assert _authentication_count(source) == 2


@pytest.mark.anyio
async def test_stream_reauthenticates_when_source_refuses_cached_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Authenticate again and retry once when the source refuses a request made with a cached key."""

    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        server.http_errors['GET'] = (403, {})
        original_set = cache.set

        def set_and_accept(host: str, port: int, key: ExchangeKey) -> None:
            server.http_errors.pop('GET', None)
            original_set(host, port, key)

        cache.set = set_and_accept
        assert await _stream(client, server.url) == _TS_CONTENT

    assert _authentication_count(source) == 2
    assert [method for method, _ in server.requests].count('GET') == 3


@pytest.mark.anyio
async def test_stream_does_not_retry_busy_source_with_cached_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Report 503 from a busy source without authenticating again."""

    cache = InMemoryExchangeKeyCache()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, _TS_CONTENT) as server,
        DTCPClient(sink_device.credentials(), timeout=5, exchange_key_cache=cache) as client,
    ):
        assert await _stream(client, server.url) == _TS_CONTENT
        server.http_errors['GET'] = (503, {})
        with pytest.raises(DLNAError):
            await _stream(client, server.url)

    assert _authentication_count(source) == 1


def test_in_memory_cache_expires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop keys older than max_age and match hosts case-insensitively."""

    now = 1000.0
    monkeypatch.setattr('pydtcpip.client.time.monotonic', lambda: now)
    cache = InMemoryExchangeKeyCache(max_age=60)
    key = ExchangeKey(value=bytes(12), label=1)
    cache.set('Example.Local', 8080, key)
    assert cache.get('example.local', 8080) == key
    now = 1061.0
    assert cache.get('example.local', 8080) is None
