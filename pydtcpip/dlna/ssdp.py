"""Discover UPnP devices and advertise an incoming transfer destination over SSDP."""

import asyncio
import errno
import logging
import socket
import sys
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from ipaddress import IPv4Address
from urllib.parse import urlsplit

import ifaddr
from pydantic import BaseModel, ConfigDict


_LOGGER = logging.getLogger(__name__)

SSDP_ADDRESS = '239.255.255.250'
SSDP_PORT = 1900
MEDIA_SERVER_DEVICE_TYPE = 'urn:schemas-upnp-org:device:MediaServer:1'
_LINUX_IP_MULTICAST_ALL = 49


def _ipv4_interfaces() -> list[str]:
    """
    Enumerate local addresses for multicast discovery and advertisements.

    Returns:
        list[str]: Non-loopback IPv4 addresses on configured adapters
    """

    return sorted(
        {
            address.ip
            for adapter in ifaddr.get_adapters()
            for address in adapter.ips
            if isinstance(address.ip, str)
            and IPv4Address(address.ip).is_loopback is False
            and IPv4Address(address.ip).is_unspecified is False
            and IPv4Address(address.ip).is_link_local is False
        }
    )


def _multicast_socket(address: str) -> socket.socket | None:
    """
    Create a UDP socket that sends multicast traffic through one local address.

    Args:
        address (str): Local IPv4 address of the interface to use

    Returns:
        socket.socket | None: Configured socket, or None when the interface cannot carry multicast
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        # Some virtual adapters and automatic private addresses reject the multicast interface option
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
    except OSError as ex:
        sock.close()
        _LOGGER.warning(f'Skipping the network interface {address} because it cannot carry multicast ({ex}).')
        return None
    return sock


def _join_multicast_group(sock: socket.socket, interface: str) -> bool:
    """
    Join the SSDP multicast group through one local address.

    Args:
        sock (socket.socket): Socket to join the group on
        interface (str): Local IPv4 address of the interface to use

    Returns:
        bool: True when the group was joined, False when the interface cannot join it
    """

    try:
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(SSDP_ADDRESS) + socket.inet_aton(interface),
        )
    except OSError as ex:
        # Multiple addresses on one NIC share a multicast membership on Linux
        if ex.errno == errno.EADDRINUSE:
            return True
        # An unusable interface must not prevent receiving multicast on the remaining ones
        _LOGGER.warning(
            f'Skipping the network interface {interface} because it cannot join the multicast group ({ex}).'
        )
        return False
    return True


class SSDPResponse(BaseModel):
    """Description location and identifiers from an SSDP response or advertisement."""

    model_config = ConfigDict(frozen=True)

    location: str
    usn: str
    search_target: str
    server: str | None
    sender_address: str
    headers: dict[str, str]


def parse_ssdp_message(data: bytes) -> tuple[str, dict[str, str]]:
    """
    Split an SSDP message into its start line and headers with uppercase names.

    Args:
        data (bytes): Received UDP datagram

    Returns:
        tuple[str, dict[str, str]]: Start line and headers
    """

    lines = data.decode('iso-8859-1').split('\r\n')
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(':')
        if separator == '':
            continue
        headers[name.strip().upper()] = value.strip()
    return lines[0], headers


class _SearchProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_response: Callable[[SSDPResponse], None], *, advertisement_target: str | None = None) -> None:
        """
        Set the handler for responses or advertisements on this socket.

        Args:
            on_response (Callable[[SSDPResponse], None]): Handler for each matching datagram
            advertisement_target (str | None): Notification type to listen for (None accepts search responses)
        """

        self._on_response = on_response
        self._advertisement_target = advertisement_target

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        start_line, headers = parse_ssdp_message(data)
        location = headers.get('LOCATION')
        if not location:
            return
        if self._advertisement_target is None:
            if start_line.startswith('HTTP/1.1 200') is False:
                return
            target = headers.get('ST', '')
        else:
            if start_line != 'NOTIFY * HTTP/1.1' or headers.get('NTS') != 'ssdp:alive':
                return
            target = headers.get('NT', '')
            if target != self._advertisement_target:
                return
        self._on_response(
            SSDPResponse(
                location=location,
                usn=headers.get('USN', ''),
                search_target=target,
                server=headers.get('SERVER'),
                sender_address=str(addr[0]),
                headers=headers,
            )
        )


async def search(
    search_target: str = MEDIA_SERVER_DEVICE_TYPE,
    *,
    timeout: float = 3.0,
    interface: str | None = None,
    port: int = 0,
) -> list[SSDPResponse]:
    """
    Send M-SEARCH and return responding devices, deduplicated by description URL.

    Args:
        search_target (str): Device or service type (defaults to DLNA media servers)
        timeout (float): Seconds to wait for responses
        interface (str | None): Local IPv4 address for M-SEARCH (None searches every IPv4 network)
        port (int): UDP response port (0 lets the OS choose; specify a port allowed by the firewall if needed)

    Returns:
        list[SSDPResponse]: Responding devices
    """

    responses: dict[str, SSDPResponse] = {}

    def on_response(response: SSDPResponse) -> None:
        responses.setdefault(response.location, response)

    # Responses arrive by unicast at the port that sent M-SEARCH
    ## Connection tracking cannot associate them with the multicast request, so restrictive firewalls drop them
    interfaces = _ipv4_interfaces() if interface in (None, '0.0.0.0') else [interface]
    transports: list[asyncio.DatagramTransport] = []
    unavailable_error: OSError | None = None
    try:
        for address in interfaces:
            sock = _multicast_socket(address)
            if sock is None:
                continue
            try:
                try:
                    sock.bind((address, port))
                except OSError as ex:
                    # Preserve discovery when the requested response port is already in use
                    if port == 0 or ex.errno != errno.EADDRINUSE:
                        raise
                    _LOGGER.warning(
                        f'Discovery port {port} is in use on {address}; '
                        'using an OS-assigned port (the firewall may drop responses).'
                    )
                    sock.bind((address, 0))
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _SearchProtocol(on_response), sock=sock
                )
            except OSError as ex:
                sock.close()
                # Disconnected adapters can retain addresses; search through the remaining local networks
                if interface in (None, '0.0.0.0') and ex.errno == errno.EADDRNOTAVAIL:
                    _LOGGER.warning(f'Cannot use discovery interface {address}: {ex}')
                    unavailable_error = ex
                    continue
                raise
            except BaseException:
                sock.close()
                raise
            transports.append(transport)
        # Surface the address error when automatic discovery has no usable network
        if len(transports) == 0:
            if unavailable_error is not None:
                raise unavailable_error
            raise OSError(errno.EADDRNOTAVAIL, 'No IPv4 interface is available for SSDP discovery.')
        # Devices respond at any time within MX seconds, so send twice with a delay to reduce missed responses
        mx = max(1, min(5, int(timeout) - 1))
        message = (
            'M-SEARCH * HTTP/1.1\r\n'
            f'HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n'
            'MAN: "ssdp:discover"\r\n'
            f'MX: {mx}\r\n'
            f'ST: {search_target}\r\n'
            '\r\n'
        ).encode()
        for transport in transports:
            transport.sendto(message, (SSDP_ADDRESS, SSDP_PORT))
        await asyncio.sleep(min(0.5, timeout / 4))
        for transport in transports:
            transport.sendto(message, (SSDP_ADDRESS, SSDP_PORT))
        await asyncio.sleep(timeout)
    finally:
        for transport in transports:
            transport.close()
    return list(responses.values())


@asynccontextmanager
async def listen_for_advertisements(
    on_advertisement: Callable[[SSDPResponse], None],
    *,
    interface: str | None = None,
) -> AsyncGenerator[bool]:
    """
    Observe availability announcements for DLNA media servers while the context is open.

    Args:
        on_advertisement (Callable[[SSDPResponse], None]): Handler for matching announcements
        interface (str | None): Local IPv4 address (None listens on every IPv4 network)

    Yields:
        bool: Whether the announcement socket is ready
    """

    interfaces = _ipv4_interfaces() if interface in (None, '0.0.0.0') else [interface]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    transport: asyncio.DatagramTransport | None = None
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, 'SO_REUSEPORT') is True:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            if sys.platform == 'linux':
                # Keep --interface scoped to this socket's memberships when other UPnP software is running
                sock.setsockopt(socket.IPPROTO_IP, _LINUX_IP_MULTICAST_ALL, 0)
            sock.bind(('', SSDP_PORT))
            joined_interfaces = [address for address in interfaces if _join_multicast_group(sock, address)]
            if joined_interfaces:
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: _SearchProtocol(on_advertisement, advertisement_target=MEDIA_SERVER_DEVICE_TYPE),
                    sock=sock,
                )
        except OSError as ex:
            _LOGGER.debug(f'Cannot listen for SSDP announcements ({ex}).')
        yield transport is not None
    finally:
        if transport is None:
            sock.close()
        else:
            transport.close()


class _AdvertiserProtocol(asyncio.DatagramProtocol):
    def __init__(self, advertiser: 'SSDPAdvertiser') -> None:
        self._advertiser = advertiser

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        self._advertiser.handle_search(data, (str(addr[0]), int(addr[1])))


class SSDPAdvertiser:
    """
    Advertise an incoming transfer destination and respond to M-SEARCH on the home network.
    Optionally restrict discovery responses to allowed transfer sources.
    """

    def __init__(
        self,
        *,
        interface: str,
        location: str,
        udn: str,
        targets: tuple[str, ...],
        allowed_senders: frozenset[str] | None,
        server: str,
        interval: float = 30.0,
        max_age: int = 120,
    ) -> None:
        """
        Configure the advertisement.

        Args:
            interface (str): Local IPv4 address (0.0.0.0 advertises on every configured IPv4 network)
            location (str): Device description URL
            udn (str): Device UDN (`uuid:...`)
            targets (tuple[str, ...]): Device and service types to advertise
            allowed_senders (frozenset[str] | None): Permitted discovery senders (None accepts any sender)
            server (str): SERVER header value
            interval (float): Advertisement interval in seconds
            max_age (int): Advertisement lifetime in seconds
        """

        self.interface = interface
        self.location = location
        self.udn = udn
        self.targets = ('upnp:rootdevice', udn, *targets)
        self.allowed_senders = allowed_senders
        self.server = server
        self.interval = interval
        self.max_age = max_age
        self._transport: asyncio.DatagramTransport | None = None
        self._announcers: list[tuple[str, asyncio.DatagramTransport]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Open a discovery socket and start periodic advertisements."""

        interfaces = [self.interface]
        if self.interface == '0.0.0.0':
            # Enumerate addresses on each NIC; hostname resolution does not enumerate all connected networks
            interfaces = _ipv4_interfaces()

        # One receive socket joins every selected network; unicast replies follow the route to the requester
        # M-SEARCH arrives on multicast port 1900; bind so other UPnP software can share it
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        unavailable_error: OSError | None = None
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, 'SO_REUSEPORT') is True:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(('', SSDP_PORT))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface))
            except OSError as ex:
                # An unusable interface must not prevent advertising on the remaining ones
                _LOGGER.warning(f'Cannot select the network interface {self.interface} for multicast ({ex}).')

            # Prepare a sender before joining its network so stale adapter addresses can be skipped
            ## Separate send sockets keep the source address and advertised URL on the same NIC
            for interface in interfaces:
                sender = _multicast_socket(interface)
                if sender is None:
                    continue
                try:
                    sender.bind((interface, 0))
                    _join_multicast_group(sock, interface)
                    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                        asyncio.DatagramProtocol, sock=sender
                    )
                except OSError as ex:
                    sender.close()
                    # Keep automatic advertisements active on the remaining usable networks
                    if self.interface == '0.0.0.0' and ex.errno == errno.EADDRNOTAVAIL:
                        _LOGGER.warning(f'Cannot use advertisement interface {interface}: {ex}')
                        unavailable_error = ex
                        continue
                    raise
                except BaseException:
                    sender.close()
                    raise
                self._announcers.append((interface, transport))

            # Report failure instead of starting a receiver that cannot advertise on any network
            if len(self._announcers) == 0:
                if unavailable_error is not None:
                    raise unavailable_error
                raise OSError(errno.EADDRNOTAVAIL, 'No IPv4 interface is available for SSDP advertisements.')
            self._transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _AdvertiserProtocol(self), sock=sock
            )
        except BaseException:
            sock.close()
            await self.stop()
            raise
        self._task = asyncio.create_task(self._advertise_periodically())

    async def stop(self) -> None:
        """Stop periodic advertisements, send ssdp:byebye, and close the socket."""

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for _, transport in self._announcers:
            for target in self.targets:
                message = (
                    'NOTIFY * HTTP/1.1\r\n'
                    f'HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n'
                    f'NT: {target}\r\n'
                    'NTS: ssdp:byebye\r\n'
                    f'USN: {self._usn(target)}\r\n'
                    '\r\n'
                )
                transport.sendto(message.encode(), (SSDP_ADDRESS, SSDP_PORT))
            transport.close()
        self._announcers.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def handle_search(self, data: bytes, sender: tuple[str, int]) -> None:
        """
        Respond to M-SEARCH from an allowed sender.

        Args:
            data (bytes): Received UDP datagram
            sender (tuple[str, int]): Sender address and port
        """

        start_line, headers = parse_ssdp_message(data)
        if start_line.startswith('M-SEARCH ') is False:
            return
        if self.allowed_senders is not None and sender[0] not in self.allowed_senders:
            return
        interface = self.interface
        if interface == '0.0.0.0':
            # UDP connect selects a local route without sending a packet or needing an Internet connection
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(sender)
                interface = str(route.getsockname()[0])
        requested = headers.get('ST', '')
        for target in self.targets:
            if requested not in ('ssdp:all', target):
                continue
            message = (
                'HTTP/1.1 200 OK\r\n'
                f'CACHE-CONTROL: max-age={self.max_age}\r\n'
                'EXT:\r\n'
                f'LOCATION: {self._location_for(interface)}\r\n'
                f'SERVER: {self.server}\r\n'
                f'ST: {target}\r\n'
                f'USN: {self._usn(target)}\r\n'
                '\r\n'
            )
            if self._transport is not None:
                self._transport.sendto(message.encode(), sender)
            _LOGGER.info(f'Responded to SSDP discovery: {sender[0]} ({target})')

    async def _advertise_periodically(self) -> None:
        """Send periodic ssdp:alive advertisements for all targets."""

        while True:
            started_at = time.monotonic()
            for interface, transport in self._announcers:
                for target in self.targets:
                    message = (
                        'NOTIFY * HTTP/1.1\r\n'
                        f'HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n'
                        f'CACHE-CONTROL: max-age={self.max_age}\r\n'
                        f'LOCATION: {self._location_for(interface)}\r\n'
                        f'NT: {target}\r\n'
                        'NTS: ssdp:alive\r\n'
                        f'SERVER: {self.server}\r\n'
                        f'USN: {self._usn(target)}\r\n'
                        '\r\n'
                    )
                    transport.sendto(message.encode(), (SSDP_ADDRESS, SSDP_PORT))
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - started_at)))

    def _location_for(self, interface: str) -> str:
        """
        Advertise the concrete address of the selected network rather than a wildcard address.

        Args:
            interface (str): Local IPv4 address reachable by the recipient

        Returns:
            str: Device description URL for this network
        """

        if self.interface != '0.0.0.0':
            return self.location
        location = urlsplit(self.location)
        return location._replace(netloc=f'{interface}:{location.port}').geturl()

    def _usn(self, target: str) -> str:
        """
        Construct the USN for an advertised target.

        Args:
            target (str): Device or service target

        Returns:
            str: UDN alone for the device or UDN and target for other advertisements
        """

        # Use the UDN alone for its own advertisement, otherwise use `UDN::target`
        if target == self.udn:
            return self.udn
        return f'{self.udn}::{target}'
