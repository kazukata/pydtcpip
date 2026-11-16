"""Perform DTCP-IP Full Authentication over TCP as a sink."""

import asyncio
import hashlib
import logging
import secrets
import socket
from collections.abc import Awaitable, Callable
from typing import Self, cast

from ecdsa import ECDH, BadSignatureError, MalformedPointError, SigningKey, VerifyingKey
from pydantic import BaseModel, ConfigDict, Field

from pydtcpip.dtcp.crypto import DTCP_CURVE, DeviceCredentials, device_id_of, verify_certificate
from pydtcpip.dtcp.srm import SRM
from pydtcpip.errors import DTCPError
from pydtcpip.events import (
    AuthenticationCompleted,
    AuthenticationStarted,
    EventCallback,
    PeerCertificateVerified,
    ProximityCheckResponded,
    SRMReceived,
    SRMSent,
    dispatch_event,
)


_LOGGER = logging.getLogger(__name__)

# Control frame commands (AKE subfunctions)
CHALLENGE = 0x01
RESPONSE = 0x02
EXCHANGE_KEY = 0x03
SRM_UPDATE = 0x04
RTT_SETUP = 0x11
RTT_TEST = 0x12
RTT_READY = 0x91
RTT_VERIFY = 0x92
MV_INITIATE = 0xA0
MV_EXCHANGE_KEY = 0x21
MV_FINALIZE = 0x22
MV_COMPLETE = 0x23
MV_CANCEL = 0x28

# Control frame ctype / response values
CONTROL = 0x0
ACCEPTED = 0x9

_FRAME_HEADER_SIZE = 11

# IP TTL for authentication TCP connections
## DTCP-IP limits TTL to 3 for home-network proximity
## Some recorders disconnect after accepting CHALLENGE when TTL is higher
AKE_IP_TTL = 3

# Callback for a newly received SRM (synchronous or awaitable)
type SRMUpdateCallback = Callable[[SRM], Awaitable[None] | None]


class ControlFrame(BaseModel):
    """A DTCP-IP control frame shared by authentication and Move control."""

    model_config = ConfigDict(frozen=True)

    # 11-byte header: type, length, ctype/response, category, command, procedure, key type, role, label, status
    header: bytes
    payload: bytes = b''

    @property
    def ctype_response(self) -> int:
        """Return the four-bit ctype for a request or response value for a response."""

        return self.header[3] & 0x0F

    @property
    def command(self) -> int:
        """Return the command (AKE subfunction)."""

        return self.header[5]

    @property
    def status(self) -> int:
        """Return the four-bit response status."""

        return self.header[10] & 0x0F

    @property
    def is_accepted(self) -> bool:
        """Return whether this is an accepted response."""

        return self.ctype_response == ACCEPTED and self.status == 0

    @classmethod
    def request(
        cls,
        command: int,
        payload: bytes = b'',
        *,
        procedure: int = 0x04,
        exchange_key: int = 0x08,
        role: int = 0x01,
        label: int = 0,
    ) -> Self:
        """
        Create a request control frame.

        Args:
            command (int): Command
            payload (bytes): Command-specific payload
            procedure (int): AKE procedure (0x04 for Full Authentication, 0 for Move control)
            exchange_key (int): Requested key type (0x08 for AES-128, 0 for no key)
            role (int): Command role value, dependent on the subfunction
            label (int): AKE label

        Returns:
            ControlFrame: Request control frame
        """

        # Set the two-byte length from the payload when sending
        return cls(
            header=bytes((0x01, 0, 0, CONTROL, 0x00, command, procedure, exchange_key, role, label, 0x0F)),
            payload=payload,
        )

    def accepted(self, payload: bytes = b'') -> Self:
        """
        Create an accepted response to this request.

        Args:
            payload (bytes): Accepted response payload

        Returns:
            ControlFrame: Request header with ctype set to ACCEPTED and status set to 0
        """

        header = bytearray(self.header)
        header[3] = (header[3] & 0xF0) | ACCEPTED
        header[10] &= 0xF0
        return type(self)(header=bytes(header), payload=payload)

    async def send(self, writer: asyncio.StreamWriter) -> None:
        """
        Send the control frame.

        Args:
            writer (asyncio.StreamWriter): Authentication TCP connection
        """

        header = bytearray(self.header)
        header[1:3] = (len(self.payload) + _FRAME_HEADER_SIZE - 3).to_bytes(2, 'big')
        writer.write(bytes(header) + self.payload)
        await writer.drain()
        _LOGGER.debug(
            f'Sent DTCP control frame: command: 0x{self.command:02x}, ctype: 0x{self.ctype_response:x}, bytes: {len(self.payload)}'
        )

    @classmethod
    async def receive(cls, reader: asyncio.StreamReader, *, allow_rejected: bool = False) -> Self:
        """
        Receive one control frame.
        Raise DTCPError for a rejected response unless allow_rejected is True.

        Args:
            reader (asyncio.StreamReader): Authentication TCP connection
            allow_rejected (bool): Whether to return rejected responses

        Returns:
            ControlFrame: Received control frame
        """

        try:
            prefix = await reader.readexactly(3)
            length = int.from_bytes(prefix[1:3], 'big')
            if prefix[0] != 0x01 or length < _FRAME_HEADER_SIZE - 3:
                raise DTCPError('Invalid DTCP control frame header.')
            rest = await reader.readexactly(length)
        except asyncio.IncompleteReadError as ex:
            raise DTCPError('The peer closed the TCP connection during DTCP-IP control exchange.') from ex

        data = prefix + rest
        frame = cls(header=data[:_FRAME_HEADER_SIZE], payload=data[_FRAME_HEADER_SIZE:])
        _LOGGER.debug(
            f'Received DTCP control frame: command: 0x{frame.command:02x}, response: 0x{frame.ctype_response:x}, status: 0x{frame.status:x}, bytes: {len(frame.payload)}'
        )

        # Anything other than a CONTROL request or accepted response indicates rejection
        is_rejected = frame.ctype_response != CONTROL and frame.is_accepted is False
        if is_rejected is True and allow_rejected is False:
            raise DTCPError(
                f'The peer rejected the request (command: 0x{frame.command:02x}, '
                f'response: 0x{frame.ctype_response:x}, status: 0x{frame.status:x}).'
            )
        return frame


class ExchangeKey(BaseModel):
    """An exchange key received during authentication and its label."""

    model_config = ConfigDict(frozen=True)

    value: bytes = Field(repr=False)
    label: int


class AuthenticationResult(BaseModel):
    """Mutual authentication result."""

    model_config = ConfigDict(frozen=True)

    exchange_key: ExchangeKey
    # Current SRM, including any update received during authentication
    srm: SRM
    peer_certificate: bytes


async def open_ake_connection(
    host: str, port: int, *, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    Open a TCP connection for authentication with IP TTL limited to 3.

    Args:
        host (str): Source authentication address
        port (int): Source authentication port
        timeout (float): Connection timeout in seconds

    Returns:
        tuple[asyncio.StreamReader, asyncio.StreamWriter]: Connected reader and writer
    """

    # Create the socket first because asyncio.open_connection() cannot set TTL before connecting
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, AKE_IP_TTL)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        async with asyncio.timeout(timeout):
            await asyncio.get_running_loop().sock_connect(sock, (host, port))
            return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise


async def authenticate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    credentials: DeviceCredentials,
    srm: SRM,
    *,
    is_move: bool,
    transfer_id: str,
    host: str,
    port: int,
    on_srm_update: SRMUpdateCallback | None = None,
    on_event: EventCallback | None = None,
) -> AuthenticationResult:
    """
    Authenticate as a sink, verify the source signature and revocation status, and receive an exchange key.
    The source drives the proximity check (RTT); the sink responds to its measurement requests.

    Args:
        reader (asyncio.StreamReader): Reader from open_ake_connection()
        writer (asyncio.StreamWriter): Writer for the same connection
        credentials (DeviceCredentials): Sink device certificate and private key
        srm (SRM): Current SRM (an empty version 0 SRM if unavailable)
        is_move (bool): Whether to request a Move exchange key
        transfer_id (str): Transfer identifier for events
        host (str): Authentication address for events
        port (int): Authentication port for events
        on_srm_update (SRMUpdateCallback | None): Callback for a newer SRM
        on_event (EventCallback | None): Event callback

    Returns:
        AuthenticationResult: Exchange key and updated SRM
    """

    await dispatch_event(
        on_event, AuthenticationStarted(transfer_id=transfer_id, host=host, port=port, is_move=is_move)
    )

    # Start the Move transaction before normal authentication
    if is_move is True:
        await ControlFrame.request(MV_INITIATE, procedure=0, exchange_key=0, role=0).send(writer)
        initiated = await ControlFrame.receive(reader, allow_rejected=True)
        if initiated.command != MV_INITIATE or initiated.is_accepted is False:
            # REGZA 43Z570L refused every Move initiation with status 0x1 during a scheduled recording, while it still
            ## authenticated streaming
            raise DTCPError(
                f'The source refused to start a Move of this recording (response: 0x{initiated.ctype_response:x}, '
                f'status: 0x{initiated.status:x}). This happens, for example, while the source is recording or '
                'busy with another transfer; try again later.'
            )

    # Send the device certificate and nonce, then receive the source CHALLENGE
    nonce = secrets.token_bytes(16)
    ephemeral_key = SigningKey.generate(curve=DTCP_CURVE, hashfunc=hashlib.sha1)
    signing_key = SigningKey.from_string(credentials.private_key, curve=DTCP_CURVE, hashfunc=hashlib.sha1)
    await ControlFrame.request(CHALLENGE, nonce + credentials.certificate).send(writer)
    frame = await ControlFrame.receive(reader)
    if frame.command != CHALLENGE or frame.is_accepted is False:
        raise DTCPError('The source did not accept CHALLENGE.')

    # Verify the source certificate and check that it is not revoked by the SRM
    frame = await ControlFrame.receive(reader)
    if frame.command != CHALLENGE or frame.ctype_response != CONTROL or len(frame.payload) != 104:
        raise DTCPError('Invalid CHALLENGE from the source.')
    peer_nonce, peer_certificate = frame.payload[:16], frame.payload[16:]
    peer_verifying_key = verify_certificate(peer_certificate)
    srm.check_device(peer_certificate)
    await frame.accepted().send(writer)
    await dispatch_event(
        on_event, PeerCertificateVerified(transfer_id=transfer_id, device_id=device_id_of(peer_certificate))
    )

    # Verify the source RESPONSE signature and derive the authentication key from the ECDH shared point
    frame = await ControlFrame.receive(reader)
    if frame.command != RESPONSE or frame.ctype_response != CONTROL or len(frame.payload) != 83:
        raise DTCPError('Invalid RESPONSE from the source.')
    try:
        peer_verifying_key.verify(frame.payload[43:], nonce + frame.payload[:43], hashfunc=hashlib.sha1)
    except BadSignatureError as ex:
        raise DTCPError('Could not verify the source RESPONSE signature.') from ex
    peer_srm_version = int.from_bytes(frame.payload[40:42], 'big')
    peer_srm_generation = frame.payload[42] & 0x0F
    peer_max_srm_generation = peer_certificate[1] >> 4
    if peer_srm_generation > peer_max_srm_generation:
        raise DTCPError('The source reported an SRM generation beyond the limit in its certificate.')
    agreement = ECDH(curve=DTCP_CURVE, private_key=ephemeral_key)
    try:
        agreement.load_received_public_key_bytes(frame.payload[:40])
    except MalformedPointError as ex:
        raise DTCPError('The source key exchange public key is invalid.') from ex
    # Use the low 96 bits of the ECDH shared point's x coordinate as the authentication key
    auth_key = agreement.generate_sharedsecret_bytes()[-12:]
    await frame.accepted().send(writer)

    # Sign and send the source nonce, our ephemeral public key, and SRM version and generation
    ## ecdsa lacks type annotations, so explicitly type the generated public key
    ephemeral_public_key = cast(VerifyingKey, ephemeral_key.get_verifying_key())
    response_data: bytes = ephemeral_public_key.to_string() + srm.version.to_bytes(2, 'big') + bytes((srm.generation,))
    signature: bytes = signing_key.sign_deterministic(peer_nonce + response_data, hashfunc=hashlib.sha1)
    await ControlFrame.request(RESPONSE, response_data + signature).send(writer)
    frame = await ControlFrame.receive(reader)
    if frame.command != RESPONSE or frame.is_accepted is False:
        raise DTCPError('The source did not accept RESPONSE.')

    # Receive the source's newer SRM after the exchange key
    ## A device limited to generation 0 does not request generation 1 of the same version
    should_receive_srm = peer_srm_version > srm.version or (
        credentials.max_srm_generation > 0 and peer_srm_version == srm.version and peer_srm_generation > srm.generation
    )

    # The source drives proximity checks, key exchange, and SRM updates; respond to each request in turn
    exchange_key: ExchangeKey | None = None
    rtt_mac_base = int.from_bytes(hashlib.sha1(auth_key + auth_key).digest(), 'big')
    expected_rtt_test: bytes | None = None
    rtt_test_response = b''
    rtt_verify_response = b''
    received_rtt_test: bytes | None = None
    while exchange_key is None or should_receive_srm is True:
        frame = await ControlFrame.receive(reader)

        # After the sink's RTT_READY is accepted, wait for the next request
        if frame.ctype_response != CONTROL:
            if frame.command == RTT_READY:
                continue
            raise DTCPError(f'Unexpected response during authentication (command: 0x{frame.command:02x}).')

        # Accept the source's proximity check and report that the sink is ready
        if frame.command == RTT_READY:
            await frame.accepted().send(writer)
            await ControlFrame.request(RTT_READY).send(writer)

        # Precompute measurement and verification MACs from the source's counter
        ## The source limits the measurement round trip to 7 ms, so do not compute them after the request
        elif frame.command == RTT_SETUP:
            if len(frame.payload) != 2:
                raise DTCPError('Invalid proximity check counter length.')
            counter = int.from_bytes(frame.payload, 'big')
            digest = hashlib.sha1(((rtt_mac_base + counter) % (1 << 160)).to_bytes(20, 'big')).digest()
            expected_rtt_test, rtt_test_response = digest[:10], digest[10:]
            rtt_verify_response = hashlib.sha1(
                ((rtt_mac_base + counter + 1) % (1 << 160)).to_bytes(20, 'big')
            ).digest()[:10]
            await frame.accepted(frame.payload).send(writer)

        # Respond immediately to the measurement request with the precomputed MAC
        elif frame.command == RTT_TEST:
            if expected_rtt_test is None or len(frame.payload) != 10:
                raise DTCPError('Received a proximity measurement request before setup or with an invalid length.')
            received_rtt_test = frame.payload
            await frame.accepted(rtt_test_response).send(writer)

        # Verify the measurement request MAC before responding to the verification request
        elif frame.command == RTT_VERIFY:
            if expected_rtt_test is None or received_rtt_test != expected_rtt_test:
                raise DTCPError('Could not verify the proximity measurement.')
            await frame.accepted(rtt_verify_response).send(writer)
            await dispatch_event(on_event, ProximityCheckResponded(transfer_id=transfer_id))

        # The exchange key is XORed with the authentication key; Move uses a separately derived mask
        elif frame.command == (MV_EXCHANGE_KEY if is_move is True else EXCHANGE_KEY):
            if len(frame.payload) != 14:
                raise DTCPError('Invalid exchange key length.')
            key_mask = auth_key
            if is_move is True:
                key_mask = hashlib.sha1(auth_key + auth_key).digest()[8:]
            exchange_key = ExchangeKey(
                value=bytes(a ^ b for a, b in zip(key_mask, frame.payload[2:], strict=True)),
                label=frame.payload[0],
            )
            await frame.accepted().send(writer)

            # Send a newer local SRM to update the source
            is_local_srm_newer = srm.version > peer_srm_version or (
                srm.version == peer_srm_version
                and peer_srm_generation < peer_max_srm_generation
                and srm.generation > peer_srm_generation
            )
            if is_local_srm_newer is True:
                # Send only the first part to a peer limited to generation 0, retaining the signed header
                send_length = len(srm.data)
                if srm.generation > peer_max_srm_generation:
                    send_length = 4 + int.from_bytes(srm.data[4:6], 'big')
                # Do not request an exchange key for SRM updates; the source rejects a specified key type
                await ControlFrame.request(SRM_UPDATE, srm.data[:send_length], exchange_key=0).send(writer)
                accepted = await ControlFrame.receive(reader)
                if accepted.command != SRM_UPDATE or accepted.is_accepted is False:
                    raise DTCPError('The source did not accept the SRM update.')
                await dispatch_event(on_event, SRMSent(transfer_id=transfer_id, version=srm.version))

        # Verify the received SRM signature and pass newer versions to the caller
        elif frame.command == SRM_UPDATE:
            received_srm = SRM.parse(frame.payload)
            if received_srm.is_newer_than(srm) is True:
                srm = received_srm
                # Retain the new SRM for future authentication even if it revokes the sink or source
                if on_srm_update is not None:
                    result = on_srm_update(srm)
                    if result is not None:
                        await result
                await dispatch_event(
                    on_event, SRMReceived(transfer_id=transfer_id, version=srm.version, generation=srm.generation)
                )
            srm.check_device(peer_certificate)
            srm.check_device(credentials.certificate)
            await frame.accepted().send(writer)
            should_receive_srm = False

        else:
            raise DTCPError(f'Received an unsupported authentication command (0x{frame.command:02x}).')

    await dispatch_event(
        on_event, AuthenticationCompleted(transfer_id=transfer_id, exchange_key_label=exchange_key.label)
    )
    return AuthenticationResult(exchange_key=exchange_key, srm=srm, peer_certificate=peer_certificate)
