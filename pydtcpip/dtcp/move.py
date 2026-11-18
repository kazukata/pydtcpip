"""Finalize, complete, cancel, and resume DTCP-IP Move transfers."""

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Literal, Self

from pydtcpip.dtcp.ake import (
    MV_CANCEL,
    MV_COMPLETE,
    MV_FINALIZE,
    ControlFrame,
    ExchangeKey,
    open_ake_connection,
)
from pydtcpip.dtcp.pcp import ProtectedPacket, decrypt_packet
from pydtcpip.errors import DTCPError, MoveCommitmentRejectedError
from pydtcpip.events import (
    EventCallback,
    MoveCancelled,
    MoveCancelRequested,
    MoveCompleted,
    MoveFinalized,
    MoveFinalizeRequested,
    dispatch_event,
)
from pydtcpip.models import MoveResumeState


_LOGGER = logging.getLogger(__name__)

# E-EMI identifying content being transferred by Move
MOVE_ENCRYPTION_MODE = 0x6

# Move states
## receiving: receiving data; finalization has not been requested and cancellation is possible
## finalizing: about to send or awaiting a response to finalization; retry rather than cancel
## finalized: finalize response verified; completion notification not yet sent
## completed: completion notification accepted
## cancelled: cancellation accepted
## rejected: source rejected finalization; retries cannot finalize
type MoveState = Literal['receiving', 'finalizing', 'finalized', 'completed', 'cancelled', 'rejected']

# Function opening an authenticated control connection for retrying finalization, completion, or cancellation
type MoveControlConnector = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class MoveTransaction:
    """
    Hold a Move's exchange key and finalization state.
    Once finalization is requested, the source may have consumed a copy; retry with the same nonce instead of cancelling.
    """

    def __init__(
        self,
        host: str,
        port: int,
        exchange_key: ExchangeKey,
        *,
        transfer_id: str,
        timeout: float,
        connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None,
        connect: MoveControlConnector | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        """
        Start a Move with the exchange key received during Move authentication.

        Args:
            host (str): Source authentication address
            port (int): Source authentication port
            exchange_key (ExchangeKey): Exchange key specific to this Move
            transfer_id (str): Transfer identifier for events
            timeout (float): Finalize, completion, and cancellation request timeout in seconds
            connection (tuple[asyncio.StreamReader, asyncio.StreamWriter] | None): Authentication connection, also used for finalization
            connect (MoveControlConnector | None): Reconnection function (None opens TCP without authentication)
            on_event (EventCallback | None): Event callback
        """

        self.host = host
        self.port = port
        self.exchange_key = exchange_key
        self.transfer_id = transfer_id
        self.state: MoveState = 'receiving'
        self.has_received_all = False
        self.written_bytes = 0
        self._nonce = secrets.token_bytes(8)
        # Whether a sent finalize request has an unknown outcome, including when restored from resume state
        ## If a retry is rejected, the source may already have accepted the earlier request
        self.is_finalize_result_unknown = False
        self._timeout = timeout
        self._connection = connection
        self._connect = connect
        self._on_event = on_event

    @classmethod
    def from_resume_state(
        cls,
        resume_state: MoveResumeState,
        *,
        transfer_id: str,
        timeout: float,
        connect: MoveControlConnector | None = None,
        on_event: EventCallback | None = None,
    ) -> Self:
        """
        Restore a Move from saved state to retry finalization or completion.

        Args:
            resume_state (MoveResumeState): State returned by resume_state()
            transfer_id (str): Transfer identifier for events
            timeout (float): Finalize and completion request timeout in seconds
            connect (MoveControlConnector | None): Connection function (None opens TCP without authentication)
            on_event (EventCallback | None): Event callback

        Returns:
            MoveTransaction: Fully received Move awaiting finalization or completion
        """

        transaction = cls(
            resume_state.host,
            resume_state.port,
            ExchangeKey(value=resume_state.exchange_key, label=resume_state.exchange_key_label),
            transfer_id=transfer_id,
            timeout=timeout,
            connect=connect,
            on_event=on_event,
        )
        transaction.state = resume_state.state
        transaction.has_received_all = True
        transaction.written_bytes = resume_state.written_bytes
        transaction._nonce = resume_state.nonce
        transaction.is_finalize_result_unknown = resume_state.state == 'finalizing'
        return transaction

    def decrypt(self, packet: ProtectedPacket) -> bytes:
        """
        Decrypt a protected packet received during a Move.

        Args:
            packet (ProtectedPacket): Protected packet read from the HTTP body

        Returns:
            bytes: Decrypted recording data
        """

        if self.state != 'receiving':
            raise DTCPError('This Move is no longer receiving data.')
        if packet.encryption_mode != MOVE_ENCRYPTION_MODE:
            raise DTCPError(f'The source did not use Move encryption mode (E-EMI: 0x{packet.encryption_mode:x}).')
        if packet.exchange_key_label != self.exchange_key.label:
            raise DTCPError('The source encrypted data with a different exchange key for this Move.')
        return decrypt_packet(packet, self.exchange_key.value)

    def resume_state(self) -> MoveResumeState:
        """
        Return the state needed to resume finalization.
        Persist this before requesting finalization so the same transfer can be resumed if the response is lost.

        Returns:
            MoveResumeState: Resume state containing the exchange key, nonce, and status
        """

        if self.has_received_all is False:
            raise DTCPError('Receive the complete recording before obtaining resume state.')
        if self.state in ('completed', 'cancelled', 'rejected'):
            raise DTCPError('Completed, cancelled, or rejected Moves have no resume state.')

        # Save the receiving state as ready to send a finalize request
        state: Literal['finalizing', 'finalized'] = 'finalized' if self.state == 'finalized' else 'finalizing'
        return MoveResumeState(
            host=self.host,
            port=self.port,
            exchange_key=self.exchange_key.value,
            exchange_key_label=self.exchange_key.label,
            nonce=self._nonce,
            state=state,
            written_bytes=self.written_bytes,
        )

    async def finalize(self) -> None:
        """Request finalization after persisting the recording and verify the response MAC."""

        if self.state not in ('receiving', 'finalizing'):
            raise DTCPError('The Move is not in a state that can be finalized.')
        if self.has_received_all is False:
            raise DTCPError('Receive the complete recording before finalizing the Move.')

        # Add the nonce to a value derived from the exchange key; use half the SHA-1 digest for each direction
        base = int.from_bytes(hashlib.sha1(self.exchange_key.value + self.exchange_key.value).digest(), 'big')
        digest = hashlib.sha1(((base + int.from_bytes(self._nonce, 'big')) % (1 << 160)).to_bytes(20, 'big')).digest()
        prefix = bytes((self.exchange_key.label,)) + self._nonce

        # Once finalization is requested, the source may have consumed a copy
        ## Disable cancellation and retry finalization with the same nonce if the response is lost
        self.state = 'finalizing'
        await dispatch_event(self._on_event, MoveFinalizeRequested(transfer_id=self.transfer_id))
        try:
            response = await self._exchange_with_reconnect(MV_FINALIZE, prefix + digest[:10])
        except _MoveCommandRejectedError as ex:
            # Do not retry a rejected finalize request (V1SE 10.4.3)
            ## If the first request is rejected, the source has not made the recording unavailable
            ## If a retry is rejected after an unknown outcome, the source may have accepted the earlier request
            ## DBR-Z620 discards resume state on disconnect after acceptance and rejects subsequent finalize requests
            self.state = 'rejected'
            if self.is_finalize_result_unknown is True:
                raise MoveCommitmentRejectedError(
                    'The source rejected the finalize retry. Its resume state is unavailable, '
                    'so it is unknown whether the source finalized the Move.',
                    may_be_committed=True,
                ) from ex
            raise MoveCommitmentRejectedError(
                f'The source rejected the Move finalize request (status: 0x{ex.status:x}).', may_be_committed=False
            ) from ex
        except BaseException:
            self.is_finalize_result_unknown = True
            raise
        if hmac.compare_digest(response, prefix + digest[10:]) is False:
            raise DTCPError('Could not verify the Move finalize response MAC.')
        self.state = 'finalized'
        await dispatch_event(self._on_event, MoveFinalized(transfer_id=self.transfer_id))

    async def complete(self) -> None:
        """Notify the source of completion after receiving a verifiable finalize response."""

        if self.state != 'finalized':
            raise DTCPError('Receive the Move finalize response before notifying completion.')
        try:
            await self._exchange_with_reconnect(MV_COMPLETE, bytes((self.exchange_key.label,)))
        except _MoveCommandRejectedError:
            # Sources should accept completion after deleting resume state (V1SE 10.4.3.1), but some reject it
            ## DBR-Z620 rejects completion after deleting resume state
            ## A verified finalize response means finalization succeeded, so treat the Move as complete
            _LOGGER.info(
                'The source rejected completion; treating the Move as complete because finalization was verified.'
            )
        self.state = 'completed'
        await dispatch_event(self._on_event, MoveCompleted(transfer_id=self.transfer_id))
        await self.close()

    async def cancel(self) -> None:
        """
        Cancel a Move before sending the finalize request.
        If no cancellation response arrives, raise an exception because the source's outcome is unknown.
        """

        if self.state != 'receiving':
            raise DTCPError('A Move cannot be cancelled after finalization starts. Resume it to finalize.')
        await dispatch_event(self._on_event, MoveCancelRequested(transfer_id=self.transfer_id))
        await self._exchange(MV_CANCEL, bytes((self.exchange_key.label,)), role=1)
        self.state = 'cancelled'
        await dispatch_event(self._on_event, MoveCancelled(transfer_id=self.transfer_id))
        await self.close()

    async def close(self) -> None:
        """Close the control connection; finalization can resume over a new connection."""

        if self._connection is not None:
            _, writer = self._connection
            self._connection = None
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                # Treat the close as complete even if the peer disconnected first
                pass

    async def _exchange_with_reconnect(self, command: int, payload: bytes, *, role: int = 0) -> bytes:
        """
        Exchange a finalization or completion request, reconnecting when the response is lost.
        These requests are idempotent for the same nonce, so a request whose response never
        arrived can be sent again on a fresh connection instead of abandoning the Move.
        A rejection is a definitive answer from the source and is never retried.

        Args:
            command (int): Move control command
            payload (bytes): Exchange key label and command-specific payload
            role (int): Role value for the command

        Returns:
            bytes: Accepted response payload
        """

        # Wait briefly between attempts to let the source finish closing the previous connection
        delays = (1.0, 4.0)
        attempt = 0
        while True:
            try:
                return await self._exchange(command, payload, role=role)
            except _MoveCommandRejectedError:
                raise
            except (OSError, TimeoutError, DTCPError):
                if self._connect is None or attempt >= len(delays):
                    raise
                _LOGGER.info(
                    f'No response to the Move control request (command: 0x{command:02x}); '
                    'reconnecting to send it again.'
                )
                await asyncio.sleep(delays[attempt])
                attempt += 1

    async def _exchange(self, command: int, payload: bytes, *, role: int = 0) -> bytes:
        """
        Send a control request and return the accepted response payload.
        Reconnect if needed, using connect when supplied.
        Build requests with the saved Move exchange key and nonce rather than obtaining a new Move key.

        Args:
            command (int): Move control command
            payload (bytes): Exchange key label and command-specific payload
            role (int): Role value for the command

        Returns:
            bytes: Accepted response payload
        """

        try:
            async with asyncio.timeout(self._timeout):
                if self._connection is not None and self._connection[0].at_eof() is True:
                    # The source already closed this connection (some sources close the
                    # authentication connection right after authentication completes), so
                    # sending the request on it would lose the request; reconnect first
                    await self.close()
                if self._connection is None:
                    if self._connect is not None:
                        self._connection = await self._connect()
                    else:
                        self._connection = await open_ake_connection(self.host, self.port, timeout=self._timeout)
                reader, writer = self._connection
                await ControlFrame.request(command, payload, procedure=0, exchange_key=0, role=role).send(writer)
                response = await ControlFrame.receive(reader, allow_rejected=True)
        except BaseException:
            # A failed connection may have mismatched responses; discard it and reconnect for the next request
            await self.close()
            raise
        if response.command != command:
            raise DTCPError(
                f'The source responded with a different command to the Move request (command: 0x{command:02x}).'
            )
        if response.is_accepted is False:
            raise _MoveCommandRejectedError(command, response.status)
        return response.payload


class _MoveCommandRejectedError(DTCPError):
    """The source rejected a Move control request; callers interpret the command-specific meaning."""

    def __init__(self, command: int, status: int) -> None:
        super().__init__(
            f'The source rejected a Move control request (command: 0x{command:02x}, status: 0x{status:x}).'
        )
        self.status = status
