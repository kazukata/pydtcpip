"""Exercise discovery over real local UDP sockets without contacting home devices."""

import asyncio
import errno
import socket
import sys
from typing import cast

import aiohttp
import pytest
from aiohttp import web

from pydtcpip.dlna import ssdp
from pydtcpip.dlna.client import DLNAClient
from pydtcpip.receiver import DubbingReceiver
from tests.fakes import FakeDevice, FakeRecorder, MemorySink, didl_container


class _FakeNic:
    """Minimal stand-in for an ifaddr address."""

    def __init__(self, ip: str) -> None:
        self.ip = ip


class _FakeAdapter:
    """Minimal stand-in for an ifaddr adapter."""

    def __init__(self, ips: list[str]) -> None:
        self.ips = [_FakeNic(ip) for ip in ips]


def test_ipv4_interfaces_excludes_addresses_that_cannot_reach_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave out loopback, unspecified, and automatic private addresses."""

    adapters = [
        _FakeAdapter(['192.0.2.10', '169.254.1.1']),
        _FakeAdapter(['127.0.0.1', '0.0.0.0']),
    ]
    monkeypatch.setattr(ssdp.ifaddr, 'get_adapters', lambda: adapters)
    # The helper is private, so reach it without importing it directly
    enumerate_interfaces = getattr(ssdp, '_ipv4_interfaces')
    assert enumerate_interfaces() == ['192.0.2.10']


def test_multicast_socket_reports_an_interface_that_rejects_multicast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report an unusable interface instead of aborting the whole discovery."""

    class _FailingSocket:
        """Socket that rejects every option, as some virtual adapters do."""

        def setsockopt(self, level: int, optname: int, value: object) -> None:
            raise OSError(10049, 'The requested address is not valid in its context')

        def close(self) -> None:
            pass

    def create_failing_socket(*args: object, **kwargs: object) -> _FailingSocket:
        return _FailingSocket()

    monkeypatch.setattr(ssdp.socket, 'socket', create_failing_socket)
    # The helper is private, so reach it without importing it directly
    create_multicast_socket = getattr(ssdp, '_multicast_socket')
    assert create_multicast_socket('192.0.2.10') is None


@pytest.mark.anyio
async def test_search_skips_an_interface_that_cannot_carry_multicast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep searching on the remaining interfaces when one cannot be configured."""

    create_multicast_socket = getattr(ssdp, '_multicast_socket')

    def create_for(address: str) -> socket.socket | None:
        if address == '127.0.0.2':
            return None
        return create_multicast_socket(address)

    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(('127.0.0.1', 0))
    responder.setblocking(False)
    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: ['127.0.0.1', '127.0.0.2'])
    monkeypatch.setattr(ssdp, '_multicast_socket', create_for)
    monkeypatch.setattr(ssdp, 'SSDP_ADDRESS', '127.0.0.1')
    monkeypatch.setattr(ssdp, 'SSDP_PORT', responder.getsockname()[1])

    async def respond() -> None:
        loop = asyncio.get_running_loop()
        for _ in range(2):
            _, sender = await loop.sock_recvfrom(responder, 8192)
            await loop.sock_sendto(
                responder,
                b'HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1/description.xml\r\n'
                b'USN: uuid:test\r\nST: upnp:rootdevice\r\n\r\n',
                sender,
            )

    try:
        async with asyncio.timeout(5):
            response_task = asyncio.create_task(respond())
            results = await ssdp.search(timeout=0.1)
            await response_task
        assert len(results) == 1
        assert results[0].location == 'http://127.0.0.1/description.xml'
    finally:
        responder.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    'interface',
    [
        pytest.param(
            None,
            marks=pytest.mark.skipif(sys.platform != 'linux', reason='Requires Linux loopback aliases without setup'),
        ),
        '127.0.0.1',
    ],
)
async def test_search_uses_each_address_and_deduplicates_responses(
    monkeypatch: pytest.MonkeyPatch, interface: str | None
) -> None:
    """Send from both local addresses by default, and honor an explicit interface."""

    # Supply isolated addresses instead of the machine's LAN; UDP transport remains real
    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: ['127.0.0.1', '127.0.0.2'])
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(('127.0.0.1', 0))
    responder.setblocking(False)
    monkeypatch.setattr(ssdp, 'SSDP_ADDRESS', '127.0.0.1')
    monkeypatch.setattr(ssdp, 'SSDP_PORT', responder.getsockname()[1])
    senders: set[str] = set()
    expected = {'127.0.0.1', '127.0.0.2'} if interface is None else {interface}

    async def respond() -> None:
        loop = asyncio.get_running_loop()
        for _ in range(len(expected) * 2):
            request, sender = await loop.sock_recvfrom(responder, 8192)
            assert request.startswith(b'M-SEARCH * HTTP/1.1\r\n')
            senders.add(sender[0])
            await loop.sock_sendto(
                responder,
                b'HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1/description.xml\r\n'
                b'USN: uuid:test\r\nST: upnp:rootdevice\r\n\r\n',
                sender,
            )

    try:
        async with asyncio.timeout(5):
            response_task = asyncio.create_task(respond())
            results = await ssdp.search(timeout=0.1, interface=interface)
            await response_task
        assert senders == expected
        assert len(results) == 1
        assert results[0].location == 'http://127.0.0.1/description.xml'
    finally:
        responder.close()


def test_join_multicast_group_reports_an_interface_that_cannot_join(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report an interface that cannot join the group instead of aborting the advertisement."""

    class _RejectingSocket:
        """Socket that refuses to join the multicast group."""

        def setsockopt(self, level: int, optname: int, value: object) -> None:
            raise OSError(10049, 'The requested address is not valid in its context')

    # The helper is private, so reach it without importing it directly
    join_multicast_group = getattr(ssdp, '_join_multicast_group')
    assert join_multicast_group(cast(socket.socket, _RejectingSocket()), '192.0.2.10') is False


@pytest.mark.anyio
async def test_advertiser_skips_an_interface_that_cannot_join_the_multicast_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep advertising on the remaining interfaces when one cannot join the multicast group."""

    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: ['127.0.0.1'])
    # Allocate a private discovery port; advertisements only leave through loopback
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
    monkeypatch.setattr(ssdp, 'SSDP_PORT', port)

    def reject_multicast_group(sock: socket.socket, interface: str) -> bool:
        return False

    monkeypatch.setattr(ssdp, '_join_multicast_group', reject_multicast_group)
    advertiser = ssdp.SSDPAdvertiser(
        interface='127.0.0.1',
        location='http://127.0.0.1:57000/description.xml',
        udn='uuid:local-test',
        targets=(ssdp.MEDIA_SERVER_DEVICE_TYPE,),
        allowed_senders=None,
        server='test',
    )
    try:
        await advertiser.start()
    finally:
        await advertiser.stop()


@pytest.mark.anyio
@pytest.mark.parametrize('interface', [None, '0.0.0.0'])
async def test_search_skips_unavailable_addresses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, interface: str | None
) -> None:
    """Discover through loopback even when an enumerated address cannot be bound."""

    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: ['192.0.2.123', '127.0.0.1'])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as responder:
        responder.bind(('127.0.0.1', 0))
        responder.setblocking(False)
        monkeypatch.setattr(ssdp, 'SSDP_ADDRESS', '127.0.0.1')
        monkeypatch.setattr(ssdp, 'SSDP_PORT', responder.getsockname()[1])

        async def respond() -> None:
            loop = asyncio.get_running_loop()
            for _ in range(2):
                _, sender = await loop.sock_recvfrom(responder, 8192)
                await loop.sock_sendto(
                    responder,
                    b'HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1/description.xml\r\n\r\n',
                    sender,
                )

        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            tasks.create_task(respond())
            results = await ssdp.search(timeout=0.1, interface=interface)
        assert len(results) == 1
        assert results[0].location == 'http://127.0.0.1/description.xml'
        assert '192.0.2.123' in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize('interface', [None, '0.0.0.0', '192.0.2.123'])
@pytest.mark.parametrize('addresses', [[], ['192.0.2.123']])
async def test_search_reports_unavailable_interfaces(
    monkeypatch: pytest.MonkeyPatch, interface: str | None, addresses: list[str]
) -> None:
    """Report failure when the explicit address or every automatic candidate is unavailable."""

    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: addresses)
    with pytest.raises(OSError) as error:
        await ssdp.search(timeout=0.1, interface=interface)
    assert error.value.errno == errno.EADDRNOTAVAIL


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != 'linux', reason='Exercises Linux multicast membership with loopback aliases')
@pytest.mark.parametrize(
    'addresses',
    [
        ['127.0.0.1', '127.0.0.2'],
        ['192.0.2.123', '127.0.0.1', '127.0.0.2'],
        ['127.0.0.1', '127.0.0.2', '192.0.2.123'],
    ],
)
async def test_advertiser_accepts_multiple_addresses_on_one_adapter(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    """Advertise on loopback aliases and skip unavailable automatic candidates."""

    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: addresses)
    # Allocate a private discovery port; advertisements only leave through loopback
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
    monkeypatch.setattr(ssdp, 'SSDP_PORT', port)
    advertiser = ssdp.SSDPAdvertiser(
        interface='0.0.0.0',
        location='http://0.0.0.0:57000/description.xml',
        udn='uuid:local-test',
        targets=(ssdp.MEDIA_SERVER_DEVICE_TYPE,),
        allowed_senders=None,
        server='test',
    )
    try:
        await advertiser.start()
        # Send a unicast search to the real listener to keep this test independent of multicast routing
        monkeypatch.setattr(ssdp, 'SSDP_ADDRESS', '127.0.0.1')
        responses = await ssdp.search(interface='127.0.0.1', timeout=0.1)
        assert len(responses) == 1
        assert responses[0].location == 'http://127.0.0.1:57000/description.xml'
    finally:
        await advertiser.stop()


@pytest.mark.anyio
@pytest.mark.parametrize('interface', ['0.0.0.0', '192.0.2.123'])
async def test_advertiser_reports_unavailable_interfaces(monkeypatch: pytest.MonkeyPatch, interface: str) -> None:
    """Fail and release the listener if no usable advertisement interface exists."""

    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: ['192.0.2.123'])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
    monkeypatch.setattr(ssdp, 'SSDP_PORT', port)
    advertiser = ssdp.SSDPAdvertiser(
        interface=interface,
        location='http://0.0.0.0:57000/description.xml',
        udn='uuid:local-test',
        targets=(ssdp.MEDIA_SERVER_DEVICE_TYPE,),
        allowed_senders=None,
        server='test',
    )
    try:
        with pytest.raises(OSError) as error:
            await advertiser.start()
        assert error.value.errno == errno.EADDRNOTAVAIL
        # A failed startup must release port 1900 so the caller can retry immediately
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(('', port))
    finally:
        await advertiser.stop()


@pytest.mark.anyio
async def test_receiver_releases_http_after_advertisement_failure(
    monkeypatch: pytest.MonkeyPatch, sink_device: FakeDevice
) -> None:
    """Release the HTTP port after discovery startup fails, then retry on the same receiver."""

    monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: list[str]())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        monkeypatch.setattr(ssdp, 'SSDP_PORT', port_socket.getsockname()[1])
    receiver = DubbingReceiver(sink_device.credentials(), lambda recording: MemorySink(), port=0)
    try:
        with pytest.raises(OSError, match='No IPv4 interface'):
            await receiver.start()
        # The real HTTP listener opened before SSDP failed must no longer accept connections
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            assert probe.connect_ex(('127.0.0.1', receiver.port)) != 0
        monkeypatch.setattr(ssdp, '_ipv4_interfaces', lambda: ['127.0.0.1'])
        await receiver.start()
        async with aiohttp.ClientSession() as session:
            async with session.get(f'http://127.0.0.1:{receiver.port}/description.xml') as response:
                assert response.status == 200
                assert 'pydtcpip' in await response.text()
    finally:
        await receiver.stop()


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != 'linux', reason='Exercises multicast on the Linux loopback interface')
@pytest.mark.parametrize('listen_only', [False, True])
async def test_discover_uses_media_server_announcements(monkeypatch: pytest.MonkeyPatch, listen_only: bool) -> None:
    """Find and browse a recorder that sends alive announcements without answering searches."""

    interface = '127.0.0.1'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_socket:
        port_socket.bind((interface, 0))
        port = port_socket.getsockname()[1]
    monkeypatch.setattr(ssdp, 'SSDP_PORT', port)

    async with (
        FakeRecorder({'0': [didl_container('recordings', '0', 'Recordings', 0)]}) as recorder,
        FakeRecorder({}, udn='uuid:other-service') as other,
        DLNAClient() as client,
        asyncio.timeout(5),
    ):

        async def announce() -> None:
            """Send availability notifications through loopback without receiving any search requests."""

            loop = asyncio.get_running_loop()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.setblocking(False)
                sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
                for _ in range(3):
                    await asyncio.sleep(0.04)
                    for target, subtype, location in (
                        ('upnp:rootdevice', 'ssdp:alive', other.location),
                        ('urn:schemas-upnp-org:device:MediaServer:1', 'ssdp:byebye', other.location),
                        ('urn:schemas-upnp-org:device:MediaServer:1', 'ssdp:alive', recorder.location),
                    ):
                        await loop.sock_sendto(
                            sender,
                            (
                                'NOTIFY * HTTP/1.1\r\n'
                                f'HOST: {ssdp.SSDP_ADDRESS}:{port}\r\n'
                                f'NT: {target}\r\nNTS: {subtype}\r\nLOCATION: {location}\r\n'
                                'CACHE-CONTROL: max-age=1800\r\n\r\n'
                            ).encode(),
                            (ssdp.SSDP_ADDRESS, port),
                        )

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(announce())
            if listen_only:
                servers = [server async for server in client.listen_for_servers(interface=interface, timeout=0.3)]
            else:
                servers = await client.discover(interface=interface, timeout=0.3)
        assert [server.description.udn for server in servers] == [recorder.udn]
        listing = await servers[0].browse('0')
        assert [item.title for item in listing.objects] == ['Recordings']


@pytest.mark.anyio
async def test_discover_lists_multihomed_server_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch two advertised HTTP endpoints for one device and show it only once."""

    async def description(request: web.Request) -> web.Response:
        return web.Response(
            text='<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
            '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
            '<friendlyName>Test recorder</friendlyName><manufacturer>Test</manufacturer>'
            '<modelName>Test</modelName><UDN>uuid:same-recorder</UDN></device></root>',
            content_type='text/xml',
        )

    application = web.Application()
    application.router.add_get('/{endpoint}', description)
    runner = web.AppRunner(application)
    await runner.setup()
    http_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_socket.bind(('127.0.0.1', 0))
    http_port = http_socket.getsockname()[1]
    await web.SockSite(runner, http_socket).start()
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(('127.0.0.1', 0))
    responder.setblocking(False)
    monkeypatch.setattr(ssdp, 'SSDP_ADDRESS', '127.0.0.1')
    monkeypatch.setattr(ssdp, 'SSDP_PORT', responder.getsockname()[1])

    async def respond() -> None:
        loop = asyncio.get_running_loop()
        for _ in range(2):
            _, sender = await loop.sock_recvfrom(responder, 8192)
            for endpoint in ('wired.xml', 'wireless.xml'):
                await loop.sock_sendto(
                    responder,
                    f'HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:{http_port}/{endpoint}\r\n'
                    'USN: uuid:same-recorder\r\nST: upnp:rootdevice\r\n\r\n'.encode(),
                    sender,
                )

    try:
        async with asyncio.timeout(5), DLNAClient() as client:
            response_task = asyncio.create_task(respond())
            servers = await client.discover(interface='127.0.0.1', timeout=0.1)
            await response_task
        assert len(servers) == 1
        assert servers[0].description.udn == 'uuid:same-recorder'
    finally:
        responder.close()
        await runner.cleanup()
