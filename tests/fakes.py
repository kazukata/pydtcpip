"""
Fake DTCP-IP sources for testing the pydtcpip sink.
A fake authority signs certificates and SRMs; source authentication, recorder HTTP, and a sending TV run on 127.0.0.1.
Source procedures and cryptography are implemented independently from the sink according to DTCP-IP.
Only content key derivation uses the sink's derive_content_key(), checked separately against known values.
"""

import asyncio
import hashlib
import secrets
import socket
from dataclasses import dataclass
from html import escape
from types import TracebackType
from typing import Literal, Self, cast
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from ecdsa import BadSignatureError, SigningKey, VerifyingKey

from pydtcpip.dtcp.crypto import DTCP_CURVE, DeviceCredentials, derive_content_key


# Source control frame commands, defined independently of sink constants
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

# Service type served by FakeRecorder
_CONTENT_DIRECTORY = 'urn:schemas-upnp-org:service:ContentDirectory:1'

# Control frame ctype / response
_CONTROL = 0x0
_ACCEPTED = 0x9
_REJECTED = 0xA
# Rejection status: Any other error
_ANY_OTHER_ERROR = 0x7
_FRAME_HEADER_SIZE = 11

# Source exchange key labels for streaming and Move, used by the sink for identification
EXCHANGE_KEY_LABEL = 0x31
MOVE_EXCHANGE_KEY_LABEL = 0x5A

# Protected packet E-EMI (0x4 for streaming, 0x6 for Move)
STREAM_ENCRYPTION_MODE = 0x4
MOVE_ENCRYPTION_MODE = 0x6

# HTTP body write size
## Differ from the packet size to verify that the sink reads packets independently of HTTP chunk boundaries
_HTTP_WRITE_SIZE = 1500

# Fake recorder contentFeatures, including the background transfer flag
_CONTENT_FEATURES = 'DLNA.ORG_PN=DTCP_MPEG_TS_JP_T;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=0ED00000000000000000000000000000'

_SOAP_NAMESPACE = 'http://schemas.xmlsoap.org/soap/envelope/'
_DIDL_NAMESPACE = 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/'
_DTCP_METADATA_NAMESPACE = 'urn:schemas-dtcp-com:metadata-1-0/'

# Body framing: fixed Content-Length or chunked
type Framing = Literal['length', 'chunked']

# Response framing also supports close-delimited bodies without Content-Length or chunked encoding
type ResponseFraming = Literal['length', 'chunked', 'close']

# Injected body truncation
## none: send the complete body
## boundary: stop before the final protected packet
## middle: stop within the final protected packet
type Truncation = Literal['none', 'boundary', 'middle']


class FakeSourceError(Exception):
    """The fake source could not validate a value received from the sink."""


@dataclass(frozen=True)
class FakeDevice:
    """A device certificate and private key issued by the fake authority."""

    certificate: bytes
    private_key: bytes

    @property
    def device_id(self) -> int:
        """Return the certificate's 5-byte device ID for SRM revocation checks."""

        return int.from_bytes(self.certificate[3:8], 'big')

    def credentials(self) -> DeviceCredentials:
        """Return credentials for the sink."""

        return DeviceCredentials(certificate=self.certificate, private_key=self.private_key)


@dataclass(frozen=True)
class FakeAuthority:
    """
    Test authority signing device certificates and SRMs in place of DTLA.
    Replacing the sink's DTLA public key with this key allows it to verify the generated signatures.
    """

    signing_key: SigningKey

    @classmethod
    def generate(cls) -> Self:
        """Create an authority with a new DTCP elliptic curve key."""

        return cls(SigningKey.generate(curve=DTCP_CURVE, hashfunc=hashlib.sha1))

    @property
    def public_key(self) -> bytes:
        """Return the 40-byte public key used in place of the sink's DTLA key."""

        return cast(VerifyingKey, self.signing_key.get_verifying_key()).to_string()

    def sign(self, data: bytes) -> bytes:
        """Sign data using ECDSA with SHA-1 and return two concatenated 20-byte integers."""

        return self.signing_key.sign_deterministic(data, hashfunc=hashlib.sha1)

    def verify_certificate(self, certificate: bytes) -> VerifyingKey:
        """Verify a device certificate and return its public key."""

        if len(certificate) != 88 or certificate[0] != 1:
            raise FakeSourceError('Invalid sink device certificate format.')
        verifier = VerifyingKey.from_string(self.public_key, curve=DTCP_CURVE, hashfunc=hashlib.sha1)
        try:
            verifier.verify(certificate[48:], certificate[:48], hashfunc=hashlib.sha1)
        except BadSignatureError as ex:
            raise FakeSourceError('Could not verify the sink device certificate signature.') from ex
        return VerifyingKey.from_string(certificate[8:48], curve=DTCP_CURVE, hashfunc=hashlib.sha1)

    def issue_device(self, device_id: int, *, max_srm_generation: int = 1) -> FakeDevice:
        """
        Issue an 88-byte device certificate and a 20-byte private key.
        The certificate contains type, maximum SRM generation, device ID, public key, and a signature over the first 48 bytes.
        """

        private_key = SigningKey.generate(curve=DTCP_CURVE, hashfunc=hashlib.sha1)
        public_key = cast(VerifyingKey, private_key.get_verifying_key()).to_string()
        body = bytes((1, max_srm_generation << 4, 0)) + device_id.to_bytes(5, 'big') + public_key
        return FakeDevice(certificate=body + self.sign(body), private_key=private_key.to_string())

    def issue_srm(self, version: int, revoked_device_ids: tuple[int, ...] = ()) -> bytes:
        """
        Issue an SRM containing only the first generation.
        Its header is followed by length, revocation list, and a signature over the preceding bytes.
        """

        header = bytes((0, 0)) + version.to_bytes(2, 'big')

        # Revocation entries use a type-0 count byte followed by 5-byte device IDs
        entries = b''
        if len(revoked_device_ids) > 0:
            entries = bytes((len(revoked_device_ids),))
            for device_id in revoked_device_ids:
                entries += device_id.to_bytes(5, 'big')

        # Length includes its own two bytes and the 40-byte signature
        length = 2 + len(entries) + 40
        signed = header + length.to_bytes(2, 'big') + entries
        return signed + self.sign(signed)


@dataclass(frozen=True)
class _Frame:
    """A control frame sent or received by the fake source."""

    header: bytes
    payload: bytes

    @property
    def ctype(self) -> int:
        return self.header[3] & 0x0F

    @property
    def command(self) -> int:
        return self.header[5]


class FakeSourceAKE:
    """
    TCP server on 127.0.0.1 that handles authentication and Move control as a DTCP-IP source.
    After CHALLENGE it exchanges certificates, verifies signatures, checks proximity, and exchanges keys and SRMs.
    For Move it accepts MV_INITIATE, issues a dedicated key, and handles finalize, complete, and cancel requests.
    Move keys persist across connections because finalization may resume on a new connection.
    """

    def __init__(self, authority: FakeAuthority, device: FakeDevice, *, srm: bytes = b'') -> None:
        """
        Configure source credentials and SRM.
        authority verifies sink certificates; an empty srm represents a source without an SRM at version 0.
        """

        self.authority = authority
        self.device = device
        self.srm = srm
        self.port = 0
        self.exchange_key = secrets.token_bytes(12)
        self.exchange_key_label = EXCHANGE_KEY_LABEL
        self.move_exchange_key: bytes | None = None
        self.move_exchange_key_label = MOVE_EXCHANGE_KEY_LABEL

        # Record accepted and sent requests in order
        self.log: list[str] = []
        self.received_srm: bytes | None = None
        self.finalize_nonces: list[bytes] = []
        self.errors: list[str] = []
        self.completed = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.disconnected = asyncio.Event()

        # Number of accepted finalize requests whose responses are dropped
        self.drop_finalize_responses = 0
        # Whether to drop the cancellation response
        self.drop_cancel_response = False
        # Number of accepted completion notifications whose responses are dropped
        self.drop_complete_responses = 0
        # Whether to reject finalize and completion with Any other error
        ## DBR-Z620 discards resume state on disconnect after finalization and rejects later requests
        self.reject_finalize = False
        self.reject_complete = False
        # Whether to reject the Move before authenticating the client
        self.reject_initiate = False
        # If set, wait for this event before finalization to represent receipt of the final HTTP response
        self.finalize_gate: asyncio.Event | None = None

        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle_connection, '127.0.0.1', 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            # Close connections still open from the sink before waiting for server shutdown
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None

    @property
    def srm_version(self) -> int:
        """Return the SRM version (0 if no SRM is held)."""

        if len(self.srm) < 4:
            return 0
        return int.from_bytes(self.srm[2:4], 'big')

    @property
    def srm_generation(self) -> int:
        """Return the SRM generation (always 0 because issue_srm() issues only the first part)."""

        return 0

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Respond to sink requests in order on one control connection.

        Args:
            reader (asyncio.StreamReader): Control connection reader
            writer (asyncio.StreamWriter): Control connection writer
        """

        self.disconnected.clear()
        is_move = False
        try:
            while True:
                frame = await self._receive(reader)

                # A close at a frame boundary indicates the sink is finished
                if frame is None:
                    return
                if frame.ctype != _CONTROL:
                    raise FakeSourceError(
                        f'Received a response while waiting for a request (command: 0x{frame.command:02x}).'
                    )

                # Move initiation signals that authentication should issue a Move-specific exchange key
                if frame.command == MV_INITIATE:
                    if self.reject_initiate is True:
                        self.log.append('mv_initiate_rejected')
                        await self._respond(writer, frame, _REJECTED)
                        continue
                    is_move = True
                    self.log.append('mv_initiate')
                    await self._respond(writer, frame, _ACCEPTED)

                # Authentication begins with the sink's CHALLENGE
                elif frame.command == CHALLENGE:
                    await self._authenticate(reader, writer, frame, is_move=is_move)

                # A newer sink SRM arrives after the exchange key
                elif frame.command == SRM_UPDATE:
                    self.received_srm = frame.payload
                    self.log.append('srm_received')
                    await self._respond(writer, frame, _ACCEPTED)

                elif frame.command == MV_FINALIZE:
                    is_responded = await self._finalize(writer, frame)
                    if is_responded is False:
                        return

                # Completion carries the finalized Move's exchange key label
                elif frame.command == MV_COMPLETE:
                    if frame.payload != bytes((self.move_exchange_key_label,)):
                        await self._respond(writer, frame, _REJECTED)
                        raise FakeSourceError('Completion exchange key label does not match.')
                    if self.reject_complete is True:
                        self.log.append('complete_rejected')
                        await self._respond(writer, frame, _REJECTED, status=_ANY_OTHER_ERROR)
                        continue
                    self.log.append('complete')
                    self.completed.set()
                    if self.drop_complete_responses > 0:
                        self.drop_complete_responses -= 1
                        return
                    await self._respond(writer, frame, _ACCEPTED)

                # Cancellation responses can be dropped to inject a failure
                elif frame.command == MV_CANCEL:
                    self.log.append('cancel')
                    self.cancelled.set()
                    if self.drop_cancel_response is True:
                        return
                    await self._respond(writer, frame, _ACCEPTED)

                else:
                    raise FakeSourceError(f'Received unsupported command (0x{frame.command:02x}).')

        except (FakeSourceError, asyncio.IncompleteReadError, ConnectionError) as ex:
            # Record sink errors for tests, including cases where the sink cancels authentication
            self.errors.append(f'{type(ex).__name__}: {ex}')
        finally:
            writer.close()
            self.disconnected.set()

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, challenge: _Frame, *, is_move: bool
    ) -> None:
        """
        Authenticate from the sink's CHALLENGE through sending the exchange key and SRM.

        Args:
            reader (asyncio.StreamReader): Control connection reader
            writer (asyncio.StreamWriter): Control connection writer
            challenge (_Frame): Sink's CHALLENGE request
            is_move (bool): Whether to issue a Move exchange key
        """

        # The sink CHALLENGE contains a 16-byte nonce and 88-byte certificate
        if len(challenge.payload) != 104:
            raise FakeSourceError('Invalid sink CHALLENGE length.')
        sink_nonce, sink_certificate = challenge.payload[:16], challenge.payload[16:]
        sink_verifying_key = self.authority.verify_certificate(sink_certificate)
        self.log.append('challenge')
        await self._respond(writer, challenge, _ACCEPTED)

        # Send the source nonce and certificate in CHALLENGE
        source_nonce = secrets.token_bytes(16)
        await self._send_request(writer, CHALLENGE, source_nonce + self.device.certificate)
        await self._expect_accepted(reader, CHALLENGE)

        # Sign the sink nonce, ephemeral public key, and SRM version and generation; send them in RESPONSE
        ephemeral_key = SigningKey.generate(curve=DTCP_CURVE, hashfunc=hashlib.sha1)
        ephemeral_public_key = cast(VerifyingKey, ephemeral_key.get_verifying_key()).to_string()
        response_data = ephemeral_public_key + self.srm_version.to_bytes(2, 'big') + bytes((self.srm_generation,))
        signing_key = SigningKey.from_string(self.device.private_key, curve=DTCP_CURVE, hashfunc=hashlib.sha1)
        signature = signing_key.sign_deterministic(sink_nonce + response_data, hashfunc=hashlib.sha1)
        await self._send_request(writer, RESPONSE, response_data + signature)
        await self._expect_accepted(reader, RESPONSE)

        # Verify the sink RESPONSE signature using the source nonce as a prefix
        frame = await self._expect_request(reader, RESPONSE)
        if len(frame.payload) != 83:
            raise FakeSourceError('Invalid sink RESPONSE length.')
        try:
            sink_verifying_key.verify(frame.payload[43:], source_nonce + frame.payload[:43], hashfunc=hashlib.sha1)
        except BadSignatureError as ex:
            raise FakeSourceError('Could not verify the sink RESPONSE signature.') from ex
        sink_srm_version = int.from_bytes(frame.payload[40:42], 'big')

        # Use the low 96 bits of the shared point's x coordinate as the authentication key
        ## ecdsa lacks type annotations, so check the extracted point and secret before use
        sink_public_key = VerifyingKey.from_string(frame.payload[:40], curve=DTCP_CURVE).pubkey
        ephemeral_secret = ephemeral_key.privkey
        if sink_public_key is None or ephemeral_secret is None:
            raise FakeSourceError('Could not read the key exchange keys.')
        shared_point = sink_public_key.point * ephemeral_secret.secret_multiplier
        auth_key = int(shared_point.x()).to_bytes(20, 'big')[-12:]
        await self._respond(writer, frame, _ACCEPTED)

        # Start the proximity check and receive the sink's readiness notification
        await self._send_request(writer, RTT_READY, b'')
        await self._expect_accepted(reader, RTT_READY)
        ready = await self._expect_request(reader, RTT_READY)
        await self._respond(writer, ready, _ACCEPTED)

        # Derive measurement and verification MACs from the authentication key and counter
        rtt_base = int.from_bytes(hashlib.sha1(auth_key + auth_key).digest(), 'big')
        counter = secrets.randbelow(0x10000)
        test_digest = hashlib.sha1(((rtt_base + counter) % (1 << 160)).to_bytes(20, 'big')).digest()
        verify_digest = hashlib.sha1(((rtt_base + counter + 1) % (1 << 160)).to_bytes(20, 'big')).digest()

        # Send setup, measurement, and verification in order and validate the returned MACs
        await self._send_request(writer, RTT_SETUP, counter.to_bytes(2, 'big'))
        await self._expect_accepted(reader, RTT_SETUP)
        await self._send_request(writer, RTT_TEST, test_digest[:10])
        accepted = await self._expect_accepted(reader, RTT_TEST)
        if accepted.payload != test_digest[10:]:
            raise FakeSourceError('Sink proximity measurement response MAC does not match.')
        await self._send_request(writer, RTT_VERIFY, b'')
        accepted = await self._expect_accepted(reader, RTT_VERIFY)
        if accepted.payload != verify_digest[:10]:
            raise FakeSourceError('Sink proximity verification response MAC does not match.')
        self.log.append('rtt')

        # XOR the exchange key with the authentication key, or a derived mask for Move
        if is_move is True:
            self.move_exchange_key = secrets.token_bytes(12)
            command, key, label = MV_EXCHANGE_KEY, self.move_exchange_key, self.move_exchange_key_label
            key_mask = hashlib.sha1(auth_key + auth_key).digest()[8:]
            log_name = 'mv_exchange_key'
        else:
            command, key, label = EXCHANGE_KEY, self.exchange_key, self.exchange_key_label
            key_mask = auth_key
            log_name = 'exchange_key'
        masked_key = bytes(a ^ b for a, b in zip(key, key_mask, strict=True))
        await self._send_request(writer, command, bytes((label, 0)) + masked_key)
        await self._expect_accepted(reader, command)
        self.log.append(log_name)

        # Send a newer source SRM after the exchange key
        if self.srm_version > sink_srm_version:
            await self._send_request(writer, SRM_UPDATE, self.srm, exchange_key=0)
            await self._expect_accepted(reader, SRM_UPDATE)
            self.log.append('srm_sent')

    async def _finalize(self, writer: asyncio.StreamWriter, frame: _Frame) -> bool:
        """
        Verify the Move finalize request MAC and respond.
        Return False when injecting a dropped response so the caller closes the connection.

        Args:
            writer (asyncio.StreamWriter): Control connection writer
            frame (_Frame): Finalize request

        Returns:
            bool: Whether the connection can continue after handling the request
        """

        if self.move_exchange_key is None:
            raise FakeSourceError('Received a finalize request before issuing a Move exchange key.')

        # For incoming transfers, process finalization after receiving the final HTTP response
        if self.finalize_gate is not None:
            try:
                await asyncio.wait_for(self.finalize_gate.wait(), 5)
            except TimeoutError as ex:
                raise FakeSourceError('Received a finalize request before the final HTTP response.') from ex

        # Finalization payload: exchange key label, 8-byte nonce, and 10-byte MAC
        payload = frame.payload
        if len(payload) != 19 or payload[0] != self.move_exchange_key_label:
            await self._respond(writer, frame, _REJECTED)
            raise FakeSourceError('Invalid finalize request length or exchange key label.')
        nonce = payload[1:9]
        if self.reject_finalize is True:
            self.log.append('finalize_rejected')
            await self._respond(writer, frame, _REJECTED, status=_ANY_OTHER_ERROR)
            return True

        # The SHA-1 digest derived from the exchange key and nonce supplies the request and response MACs
        base = int.from_bytes(hashlib.sha1(self.move_exchange_key + self.move_exchange_key).digest(), 'big')
        digest = hashlib.sha1(((base + int.from_bytes(nonce, 'big')) % (1 << 160)).to_bytes(20, 'big')).digest()
        if payload[9:] != digest[:10]:
            await self._respond(writer, frame, _REJECTED)
            raise FakeSourceError('Finalize request MAC does not match.')

        # The source consumes a copy when it accepts finalization
        self.finalize_nonces.append(nonce)
        self.log.append('finalize')
        if self.drop_finalize_responses > 0:
            self.drop_finalize_responses -= 1
            return False
        await self._respond(writer, frame, _ACCEPTED, payload[:9] + digest[10:])
        return True

    async def _receive(self, reader: asyncio.StreamReader) -> _Frame | None:
        """
        Receive a control frame.

        Args:
            reader (asyncio.StreamReader): Control connection reader

        Returns:
            _Frame | None: Received frame, or None if the connection closed at a frame boundary
        """

        try:
            prefix = await reader.readexactly(3)
        except asyncio.IncompleteReadError as ex:
            if len(ex.partial) == 0:
                return None
            raise
        if prefix[0] != 0x01:
            raise FakeSourceError('Invalid control frame type.')
        rest = await reader.readexactly(int.from_bytes(prefix[1:3], 'big'))
        data = prefix + rest
        return _Frame(header=data[:_FRAME_HEADER_SIZE], payload=data[_FRAME_HEADER_SIZE:])

    async def _expect_request(self, reader: asyncio.StreamReader, command: int) -> _Frame:
        """
        Receive a request for the specified command.

        Args:
            reader (asyncio.StreamReader): Control connection reader
            command (int): Expected command

        Returns:
            _Frame: Matching request frame
        """

        frame = await self._receive(reader)
        if frame is None or frame.ctype != _CONTROL or frame.command != command:
            raise FakeSourceError(f'Did not receive the sink request (command: 0x{command:02x}).')
        return frame

    async def _expect_accepted(self, reader: asyncio.StreamReader, command: int) -> _Frame:
        """
        Receive an accepted response for the specified command.

        Args:
            reader (asyncio.StreamReader): Control connection reader
            command (int): Expected command

        Returns:
            _Frame: Matching accepted response
        """

        frame = await self._receive(reader)
        if frame is None or frame.ctype != _ACCEPTED or frame.header[10] & 0x0F != 0 or frame.command != command:
            raise FakeSourceError(f'Did not receive the sink acceptance (command: 0x{command:02x}).')
        return frame

    async def _send_request(
        self, writer: asyncio.StreamWriter, command: int, payload: bytes, *, exchange_key: int = 0x08
    ) -> None:
        """
        Send a Full Authentication request.

        Args:
            writer (asyncio.StreamWriter): Control connection writer
            command (int): Command to send
            payload (bytes): Command payload
            exchange_key (int): Requested exchange key type
        """

        header = bytes((0x01, 0, 0, _CONTROL, 0x00, command, 0x04, exchange_key, 0x00, 0x00, 0x0F))
        await self._write(writer, header, payload)

    async def _respond(
        self, writer: asyncio.StreamWriter, request: _Frame, ctype: int, payload: bytes = b'', *, status: int = 0x1
    ) -> None:
        """
        Respond using the request header with a new ctype and status.

        Args:
            writer (asyncio.StreamWriter): Control connection writer
            request (_Frame): Request whose header is reused
            ctype (int): Response type
            payload (bytes): Response payload
            status (int): Rejection status (acceptance always uses 0)
        """

        header = bytearray(request.header)
        header[3] = (header[3] & 0xF0) | ctype
        header[10] &= 0xF0
        if ctype != _ACCEPTED:
            header[10] |= status
        await self._write(writer, bytes(header), payload)

    async def _write(self, writer: asyncio.StreamWriter, header: bytes, payload: bytes) -> None:
        """
        Send a control frame with length excluding the three-byte type and length prefix.

        Args:
            writer (asyncio.StreamWriter): Control connection writer
            header (bytes): Frame header
            payload (bytes): Frame payload
        """

        data = bytearray(header + payload)
        data[1:3] = (len(data) - 3).to_bytes(2, 'big')
        writer.write(bytes(data))
        await writer.drain()


def encrypt_packets(
    content: bytes, exchange_key: bytes, label: int, encryption_mode: int, packet_size: int
) -> list[bytes]:
    """
    Encrypt plaintext in packet_size chunks into protected packets (PCP).
    Each has a 14-byte header and AES-CBC ciphertext zero-padded to a multiple of 16 bytes.
    """

    packets: list[bytes] = []
    for start in range(0, len(content), packet_size):
        chunk = content[start : start + packet_size]
        nonce = secrets.token_bytes(8)
        content_key, iv = derive_content_key(exchange_key, encryption_mode, nonce)
        padded = chunk + bytes(-len(chunk) % 16)
        encryptor = Cipher(algorithms.AES128(content_key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        # The high four bits identify packet type and cipher: 0 for PCP and AES-128
        header = bytes((encryption_mode & 0x0F, label)) + nonce + len(chunk).to_bytes(4, 'big')
        packets.append(header + ciphertext)
    return packets


def encrypted_length(content_length: int, packet_size: int) -> int:
    """Return the total protected packet size produced by encrypt_packets() for the given plaintext size."""

    total = 0
    for start in range(0, content_length, packet_size):
        chunk_length = min(packet_size, content_length - start)
        total += 14 + chunk_length + (-chunk_length % 16)
    return total


def _truncate(packets: list[bytes], truncation: Truncation) -> bytes:
    """
    Truncate the outgoing body according to the injected failure.

    Args:
        packets (list[bytes]): Encrypted packets making up the body
        truncation (Truncation): Location at which to truncate

    Returns:
        bytes: Complete or truncated body
    """

    body = b''.join(packets)
    if truncation == 'boundary':
        return body[: len(body) - len(packets[-1])]
    if truncation == 'middle':
        return body[: len(body) - len(packets[-1]) // 2]
    return body


class FakeMediaServer:
    """
    Fake recorder HTTP server serving one DTCP-IP recording at /recording.
    HEAD and GET advertise the authentication endpoint; GET sends encrypted protected packets.
    GET with BLKMove.dtcp.com uses the Move-specific exchange key and E-EMI.
    """

    def __init__(
        self,
        ake: FakeSourceAKE,
        content: bytes,
        *,
        framing: ResponseFraming = 'length',
        truncation: Truncation = 'none',
        packet_size: int = 4000,
    ) -> None:
        """
        Configure plaintext content, body framing, injected truncation, and packet size.
        Encryption uses the exchange key issued by ake.
        """

        self.ake = ake
        self.content = content
        self.framing: ResponseFraming = framing
        self.truncation: Truncation = truncation
        self.packet_size = packet_size
        self.port = 0
        # Record received methods and headers in order
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.http_errors: dict[str, tuple[int, dict[str, str]]] = {}
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> Self:
        application = web.Application()
        application.router.add_get('/recording', self._handle)
        self._runner = web.AppRunner(application)
        await self._runner.setup()

        # Create the socket first to obtain the OS-assigned port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        self.port = int(sock.getsockname()[1])
        await web.SockSite(self._runner, sock).start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def url(self) -> str:
        """Return the recording URL."""

        return f'http://127.0.0.1:{self.port}/recording'

    def count(self, method: str) -> int:
        """Return the number of requests received for a method."""

        count = 0
        for request_method, _ in self.requests:
            if request_method == method:
                count += 1
        return count

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """
        Handle HEAD and GET with the authentication endpoint and encrypted body.

        Args:
            request (web.Request): Media request

        Returns:
            web.StreamResponse: Media response or an error for an invalid Move key label
        """

        self.requests.append((request.method, dict(request.headers)))
        if request.method in self.http_errors:
            status, error_headers = self.http_errors[request.method]
            return web.Response(status=status, headers=error_headers)
        headers = {
            'Content-Type': f'application/x-dtcp1;DTCP1HOST=127.0.0.1;DTCP1PORT={self.ake.port};CONTENTFORMAT="video/mpeg"',
            'contentFeatures.dlna.org': _CONTENT_FEATURES,
        }

        # Report the returned range and total duration when a time range was requested
        time_range = request.headers.get('TimeSeekRange.dlna.org')
        if time_range is not None:
            headers['TimeSeekRange.dlna.org'] = f'{time_range}/120.000'

        # Accept a Move GET only when its key label matches the one issued during Move authentication
        move_label = request.headers.get('BLKMove.dtcp.com')
        if move_label is None:
            key, label, encryption_mode = self.ake.exchange_key, self.ake.exchange_key_label, STREAM_ENCRYPTION_MODE
        else:
            if self.ake.move_exchange_key is None or int(move_label, 16) != self.ake.move_exchange_key_label:
                return web.Response(status=400)
            key, label, encryption_mode = (
                self.ake.move_exchange_key,
                self.ake.move_exchange_key_label,
                MOVE_ENCRYPTION_MODE,
            )
        packets = encrypt_packets(self.content, key, label, encryption_mode, self.packet_size)

        # Report the complete Content-Length even when injecting truncation
        response = web.StreamResponse(headers=headers)
        if self.framing == 'length':
            response.content_length = len(b''.join(packets))
        elif self.framing == 'chunked':
            response.enable_chunked_encoding()
        else:
            # Disable aiohttp's automatic chunked framing so connection close delimits the body
            response._length_check = False  # pyright: ignore[reportPrivateUsage]
            response.force_close()
        await response.prepare(request)
        if request.method == 'HEAD':
            return response

        body = _truncate(packets, self.truncation)
        for start in range(0, len(body), _HTTP_WRITE_SIZE):
            await response.write(body[start : start + _HTTP_WRITE_SIZE])

        # For injected truncation close TCP without sending the remainder or chunk terminator
        if self.truncation != 'none':
            if request.transport is not None:
                request.transport.close()
            return response
        await response.write_eof()
        return response


class FakeTelevision:
    """
    Fake television acting as a transfer source.
    Obtain importUri with CreateObject and POST the recording with `Expect: 100-continue`.
    Verify authentication precedes 100 Continue and its BLKMove.dtcp.com header carries the Move key label.
    Write POST directly over TCP to inspect the interim response and inject body truncation.
    """

    def __init__(self, ake: FakeSourceAKE, content: bytes, *, packet_size: int = 4000) -> None:
        """
        Configure recording plaintext and protected packet size.
        ake handles authentication, Move key issuance, and finalize and completion responses.
        """

        self.ake = ake
        self.content = content
        self.packet_size = packet_size
        # The source processes finalization after receiving the final HTTP response
        self.ake.finalize_gate = asyncio.Event()

    async def create_object(self, base_url: str, *, title: str = 'テスト用の録画') -> tuple[int, str | None]:
        """
        Send CreateObject and return the HTTP status and importUri (None on failure).
        """

        # One protected TS recording specifying transaction-based Move (dtcp:uploadInfo=C0000000)
        didl = ET.Element(f'{{{_DIDL_NAMESPACE}}}DIDL-Lite')
        item = ET.SubElement(didl, f'{{{_DIDL_NAMESPACE}}}item', {'id': '', 'parentID': '0', 'restricted': '0'})
        ET.SubElement(item, '{http://purl.org/dc/elements/1.1/}title').text = title
        ET.SubElement(item, '{urn:schemas-upnp-org:metadata-1-0/upnp/}class').text = 'object.item.videoItem'
        ET.SubElement(
            item,
            f'{{{_DIDL_NAMESPACE}}}res',
            {
                'protocolInfo': 'http-get:*:application/x-dtcp1:DLNA.ORG_PN=DTCP_MPEG_TS_JP_T',
                f'{{{_DTCP_METADATA_NAMESPACE}}}uploadInfo': 'C0000000',
            },
        )
        envelope = ET.Element(f'{{{_SOAP_NAMESPACE}}}Envelope')
        body = ET.SubElement(envelope, f'{{{_SOAP_NAMESPACE}}}Body')
        action = ET.SubElement(body, '{urn:schemas-upnp-org:service:ContentDirectory:1}CreateObject')
        ET.SubElement(action, 'ContainerID').text = '0'
        ET.SubElement(action, 'Elements').text = ET.tostring(didl, encoding='unicode')

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f'{base_url}/control/ContentDirectory',
                data=ET.tostring(envelope),
                headers={
                    'Content-Type': 'text/xml; charset="utf-8"',
                    'SOAPAction': '"urn:schemas-upnp-org:service:ContentDirectory:1#CreateObject"',
                },
            ) as response,
        ):
            data = await response.read()
            if response.status != 200:
                return response.status, None

        # Extract the body destination from the DIDL-Lite res in the response Result
        result = ET.fromstring(data).findtext('.//Result') or ''
        resource = ET.fromstring(result).find(f'.//{{{_DIDL_NAMESPACE}}}res')
        if resource is None:
            raise FakeSourceError('The CreateObject response has no res element.')
        return response.status, resource.get('importUri')

    async def upload(
        self, import_uri: str, *, framing: Framing = 'length', truncation: Truncation = 'none'
    ) -> int | None:
        """
        POST the recording to importUri and return the final HTTP status.
        Return a non-100 interim status immediately, or None without waiting when injecting truncation.
        """

        parsed = urlsplit(import_uri)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        try:
            # Advertise the television's authentication endpoint in Content-Type parameters
            length_header = 'Transfer-Encoding: chunked'
            if framing == 'length':
                length_header = f'Content-Length: {encrypted_length(len(self.content), self.packet_size)}'
            writer.write(
                (
                    f'POST {parsed.path} HTTP/1.1\r\n'
                    f'Host: {parsed.netloc}\r\n'
                    f'Content-Type: application/x-dtcp1;DTCP1HOST=127.0.0.1;DTCP1PORT={self.ake.port}\r\n'
                    f'{length_header}\r\n'
                    'Expect: 100-continue\r\n'
                    'Connection: close\r\n'
                    '\r\n'
                ).encode()
            )
            await writer.drain()

            # The sink must authenticate the Move before returning 100 Continue
            status, headers = await _read_response_head(reader)
            if status != 100:
                return status
            if self.ake.move_exchange_key is None:
                raise FakeSourceError('Received 100 Continue before Move authentication completed.')
            label = headers.get('blkmove.dtcp.com')
            if label is None or int(label, 16) != self.ake.move_exchange_key_label:
                raise FakeSourceError(
                    f'The 100 Continue BLKMove.dtcp.com label does not match the Move exchange key ({label}).'
                )

            # Send the body encrypted with the Move exchange key and E-EMI
            packets = encrypt_packets(
                self.content,
                self.ake.move_exchange_key,
                self.ake.move_exchange_key_label,
                MOVE_ENCRYPTION_MODE,
                self.packet_size,
            )
            body = _truncate(packets, truncation)
            if framing == 'length':
                writer.write(body)
            else:
                for start in range(0, len(body), _HTTP_WRITE_SIZE):
                    piece = body[start : start + _HTTP_WRITE_SIZE]
                    writer.write(f'{len(piece):X}\r\n'.encode() + piece + b'\r\n')
                if truncation == 'none':
                    writer.write(b'0\r\n\r\n')
            await writer.drain()

            # Inject truncation by closing TCP without the remaining body or chunk terminator
            if truncation != 'none':
                return None

            # Process finalization as the source after receiving the final HTTP response
            status, _ = await _read_response_head(reader)
            self.ake.log.append('http_response')
            if self.ake.finalize_gate is not None:
                self.ake.finalize_gate.set()
            return status
        finally:
            writer.close()


async def _read_response_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    """
    Read an HTTP response status line and headers.

    Args:
        reader (asyncio.StreamReader): HTTP connection reader

    Returns:
        tuple[int, dict[str, str]]: Status code and headers with lowercase names
    """

    status_line = await reader.readline()
    parts = status_line.decode('latin-1').split(' ', 2)
    if len(parts) < 2 or parts[1].isdecimal() is False:
        raise FakeSourceError(f'Invalid HTTP response status line: {status_line!r}')
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode('latin-1').rstrip('\r\n')
        if line == '':
            break
        name, _, value = line.partition(':')
        headers[name.strip().lower()] = value.strip()
    return int(parts[1]), headers


class MemorySink:
    """In-memory recording sink that records the order of sink calls."""

    def __init__(self) -> None:
        self.data = bytearray()
        # Order of persist, commit, and discard calls
        self.calls: list[str] = []

    async def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def persist(self) -> None:
        self.calls.append('persist')

    async def commit(self) -> None:
        self.calls.append('commit')

    async def discard(self) -> None:
        self.calls.append('discard')


_DIDL_HEADER = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" '
    'xmlns:regza="urn:schemas-toshiba-co-jp:regzadata-1-0/">'
)


def didl_container(object_id: str, parent_id: str, title: str, child_count: int) -> str:
    """
    Build a DIDL-Lite container element.

    Args:
        object_id (str): Folder ID
        parent_id (str): Parent folder ID
        title (str): Folder title
        child_count (int): Number of children

    Returns:
        str: Container element
    """

    return (
        f'<container id="{escape(object_id)}" parentID="{escape(parent_id)}" childCount="{child_count}">'
        f'<dc:title>{escape(title)}</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>'
    )


def didl_recording(
    object_id: str, parent_id: str, title: str, resources: str, *, fields: str = '', reference_id: str | None = None
) -> str:
    """
    Build a DIDL-Lite video item element.

    Args:
        object_id (str): Recording ID
        parent_id (str): Parent folder ID
        title (str): Recording title
        resources (str): res elements
        fields (str): Additional metadata elements
        reference_id (str | None): ID of the referenced recording for duplicate listings

    Returns:
        str: Item element
    """

    reference = f' refID="{escape(reference_id)}"' if reference_id is not None else ''
    return (
        f'<item id="{escape(object_id)}" parentID="{escape(parent_id)}"{reference}><dc:title>{escape(title)}</dc:title>'
        f'<upnp:class>object.item.videoItem</upnp:class>{fields}{resources}</item>'
    )


def dtcp_resource(url: str, *, parameters: str = 'DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000', attributes: str = '') -> str:
    """
    Build a DTCP-IP res element.

    Args:
        url (str): Resource URL
        parameters (str): protocolInfo fourth-field parameters
        attributes (str): Additional res attributes

    Returns:
        str: res element
    """

    return f'<res protocolInfo="http-get:*:application/x-dtcp1:{parameters}" {attributes}>{escape(url)}</res>'


class FakeRecorder:
    """
    Fake recorder serving a device description and ContentDirectory listings by object ID.
    Browse honors StartingIndex and RequestedCount so that clients must request every page.
    """

    def __init__(
        self,
        folders: dict[str, list[str]],
        *,
        manufacturer: str = 'TOSHIBA',
        model: str = 'DBR-TEST',
        friendly_name: str = 'Test recorder',
        udn: str = 'uuid:00000000-0000-4000-8000-000000000001',
        page_size: int = 100,
    ) -> None:
        """
        Configure listings and device identity.

        Args:
            folders (dict[str, list[str]]): DIDL-Lite child elements for each folder ID
            manufacturer (str): Manufacturer used for profile selection
            model (str): Model name
            friendly_name (str): Name shown to users
            udn (str): Unique device name
            page_size (int): Maximum entries returned per Browse request
        """

        self.folders = folders
        self.manufacturer = manufacturer
        self.model = model
        self.friendly_name = friendly_name
        self.udn = udn
        self.page_size = page_size
        # BrowseMetadata responses by object ID; other IDs are found in folder listings
        self.metadata: dict[str, str] = {}
        # Object IDs whose Browse requests fail with HTTP 500
        self.failing_ids: set[str] = set()
        # Received Browse requests as (BrowseFlag, ObjectID, StartingIndex)
        self.calls: list[tuple[str, str, int]] = []
        self.port = 0
        self._runner: web.AppRunner | None = None

    @property
    def location(self) -> str:
        """Return the device description URL."""

        return f'http://127.0.0.1:{self.port}/description.xml'

    async def __aenter__(self) -> Self:
        application = web.Application()
        application.router.add_route('*', '/{path:.*}', self._handle)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        self.port = int(sock.getsockname()[1])
        await web.SockSite(self._runner, sock).start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _find(self, object_id: str) -> str | None:
        """
        Find the DIDL-Lite element of an object in the configured listings.

        Args:
            object_id (str): Object ID

        Returns:
            str | None: Element, or None when not listed
        """

        if object_id in self.metadata:
            return self.metadata[object_id]
        needle = f'id="{escape(object_id)}"'
        for children in self.folders.values():
            for child in children:
                if needle in child:
                    return child
        return None

    async def _handle(self, request: web.Request) -> web.Response:
        """
        Serve the device description, service description and ContentDirectory actions.

        Args:
            request (web.Request): HTTP request

        Returns:
            web.Response: Response document
        """

        if request.path == '/description.xml':
            return web.Response(
                text=f"""<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
                <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
                <friendlyName>{escape(self.friendly_name)}</friendlyName><manufacturer>{escape(self.manufacturer)}</manufacturer>
                <modelName>{escape(self.model)}</modelName><UDN>{escape(self.udn)}</UDN>
                <serviceList><service><serviceType>{_CONTENT_DIRECTORY}</serviceType>
                <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId><controlURL>/control</controlURL>
                <eventSubURL>/event</eventSubURL><SCPDURL>/scpd.xml</SCPDURL></service></serviceList>
                </device></root>""",
                content_type='text/xml',
            )
        if request.path == '/scpd.xml':
            return web.Response(
                text='<scpd xmlns="urn:schemas-upnp-org:service-1-0"><actionList>'
                '<action><name>Browse</name></action></actionList></scpd>',
                content_type='text/xml',
            )
        envelope = ET.fromstring(await request.read())
        body = envelope.find('{http://schemas.xmlsoap.org/soap/envelope/}Body')
        if body is None or len(body) == 0:
            return web.Response(status=400)
        action = body[0]
        arguments = {child.tag: child.text or '' for child in action}
        object_id = arguments.get('ObjectID', '0')
        flag = arguments.get('BrowseFlag', '')
        start = int(arguments.get('StartingIndex', '0') or '0')
        self.calls.append((flag, object_id, start))
        if object_id in self.failing_ids:
            return web.Response(status=500)
        if flag == 'BrowseMetadata':
            element = self._find(object_id)
            if element is None:
                return web.Response(status=500)
            elements, total = [element], 1
        else:
            children = self.folders.get(object_id, [])
            count = int(arguments.get('RequestedCount', '0') or '0') or self.page_size
            elements = children[start : start + min(count, self.page_size)]
            total = len(children)
        result = _DIDL_HEADER + ''.join(elements) + '</DIDL-Lite>'
        return web.Response(
            text=f'''<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
            <u:BrowseResponse xmlns:u="{_CONTENT_DIRECTORY}"><Result>{escape(result)}</Result>
            <NumberReturned>{len(elements)}</NumberReturned><TotalMatches>{total}</TotalMatches>
            <UpdateID>1</UpdateID></u:BrowseResponse></s:Body></s:Envelope>''',
            content_type='text/xml',
        )
