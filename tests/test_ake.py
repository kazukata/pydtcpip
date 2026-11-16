"""Exercise sink authentication against a fake source on 127.0.0.1."""

import asyncio

import pytest

from pydtcpip.dtcp.ake import AuthenticationResult, SRMUpdateCallback, authenticate, open_ake_connection
from pydtcpip.dtcp.srm import SRM
from pydtcpip.errors import DTCPError
from pydtcpip.events import EventCallback, TransferEvent
from tests.fakes import FakeAuthority, FakeDevice, FakeSourceAKE


async def _authenticate(
    source: FakeSourceAKE,
    sink_device: FakeDevice,
    srm: SRM,
    *,
    is_move: bool = False,
    on_srm_update: SRMUpdateCallback | None = None,
    on_event: EventCallback | None = None,
) -> AuthenticationResult:
    """
    Authenticate with a fake source and close the connection afterward.

    Args:
        source (FakeSourceAKE): Fake source accepting the connection
        sink_device (FakeDevice): Sink credentials
        srm (SRM): Sink's current SRM
        is_move (bool): Whether to request a Move exchange key
        on_srm_update (SRMUpdateCallback | None): Callback for a newer SRM
        on_event (EventCallback | None): Event callback

    Returns:
        AuthenticationResult: Exchange key, SRM and peer certificate
    """

    reader, writer = await open_ake_connection('127.0.0.1', source.port, timeout=5)
    try:
        return await authenticate(
            reader,
            writer,
            sink_device.credentials(),
            srm,
            is_move=is_move,
            transfer_id='test',
            host='127.0.0.1',
            port=source.port,
            on_srm_update=on_srm_update,
            on_event=on_event,
        )
    finally:
        writer.close()
        await writer.wait_closed()
        # Let the source process the final acceptance before its context closes active connections
        await asyncio.wait_for(source.disconnected.wait(), timeout=5)


@pytest.mark.anyio
async def test_authenticate_receives_exchange_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """A mismatch in signatures, ECDH, proximity MAC, or exchange-key XOR prevents key exchange."""

    async with FakeSourceAKE(authority, source_device) as source:
        events: list[TransferEvent] = []
        result = await _authenticate(source, sink_device, SRM(), on_event=events.append)

    assert result.exchange_key.value == source.exchange_key
    assert result.exchange_key.label == source.exchange_key_label
    assert result.peer_certificate == source_device.certificate
    assert source.log == ['challenge', 'rtt', 'exchange_key']
    assert source.errors == []
    assert [event.type for event in events] == [
        'authentication_started',
        'peer_certificate_verified',
        'proximity_check_responded',
        'authentication_completed',
    ]


@pytest.mark.anyio
async def test_authenticate_receives_move_exchange_key(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """A Move exchange key must not be recovered using the normal exchange-key XOR."""

    async with FakeSourceAKE(authority, source_device) as source:
        result = await _authenticate(source, sink_device, SRM(), is_move=True)

    assert source.move_exchange_key is not None
    assert result.exchange_key.value == source.move_exchange_key
    assert result.exchange_key.label == source.move_exchange_key_label
    assert source.log == ['mv_initiate', 'challenge', 'rtt', 'mv_exchange_key']
    assert source.errors == []


@pytest.mark.anyio
async def test_authenticate_receives_newer_srm_from_source(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """A newer source SRM must reach on_srm_update so revocation information is updated."""

    source_srm = authority.issue_srm(3)
    updates: list[SRM] = []
    async with FakeSourceAKE(authority, source_device, srm=source_srm) as source:
        result = await _authenticate(source, sink_device, SRM(), on_srm_update=updates.append)

    assert [srm.data for srm in updates] == [source_srm]
    assert result.srm.data == source_srm
    assert result.srm.version == 3
    assert source.log == ['challenge', 'rtt', 'exchange_key', 'srm_sent']
    assert source.errors == []


@pytest.mark.anyio
async def test_authenticate_sends_newer_local_srm_to_source(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Send a newer local SRM so the source's revocation information is updated."""

    local_srm = SRM.parse(authority.issue_srm(5))
    old_srm = authority.issue_srm(2)
    updates: list[SRM] = []
    async with FakeSourceAKE(authority, source_device, srm=old_srm) as source:
        events: list[TransferEvent] = []
        result = await _authenticate(
            source, sink_device, local_srm, on_srm_update=updates.append, on_event=events.append
        )

    assert source.received_srm == local_srm.data
    assert result.srm == local_srm
    assert updates == []
    assert 'srm_sent' in [event.type for event in events]
    assert source.log == ['challenge', 'rtt', 'exchange_key', 'srm_received']
    assert source.errors == []


@pytest.mark.anyio
async def test_authenticate_rejects_revoked_source(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Reject a revoked source certificate before receiving its exchange key."""

    srm = SRM.parse(authority.issue_srm(1, (0x1111111111, source_device.device_id)))
    async with FakeSourceAKE(authority, source_device) as source:
        with pytest.raises(DTCPError, match='revoked'):
            await _authenticate(source, sink_device, srm)

    assert 'exchange_key' not in source.log


@pytest.mark.anyio
async def test_authenticate_without_srm(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """A sink without an SRM authenticates with a source without sending an empty SRM."""

    updates: list[SRM] = []
    async with FakeSourceAKE(authority, source_device) as source:
        result = await _authenticate(source, sink_device, SRM(), on_srm_update=updates.append)

    assert result.exchange_key.value == source.exchange_key
    assert result.srm == SRM()
    assert updates == []
    assert source.received_srm is None
    assert source.errors == []
