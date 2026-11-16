"""Callback events for authentication, transfer, persistence, and finalization."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from pydtcpip.models import MediaInfo, ProtectionValue


def _now() -> datetime:
    """
    Obtain the local time for an event.

    Returns:
        datetime: Time with the local timezone
    """

    return datetime.now().astimezone()


class _BaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Identifier shared by events for the same stream, Move, or incoming transfer
    transfer_id: str
    occurred_at: datetime = Field(default_factory=_now)


class MediaInfoReceived(_BaseEvent):
    """The media HTTP response supplied the DTCP-IP authentication endpoint and transfer information."""

    type: Literal['media_info_received'] = 'media_info_received'
    media_info: MediaInfo
    method: Literal['HEAD', 'GET'] = 'HEAD'


class AuthenticationStarted(_BaseEvent):
    """DTCP-IP mutual authentication with the source has started."""

    type: Literal['authentication_started'] = 'authentication_started'
    host: str
    port: int
    is_move: bool


class PeerCertificateVerified(_BaseEvent):
    """The source certificate signature is valid and the certificate is not revoked by the SRM."""

    type: Literal['peer_certificate_verified'] = 'peer_certificate_verified'
    device_id: int


class SRMReceived(_BaseEvent):
    """A newer SRM was received from the source and passed to on_srm_update."""

    type: Literal['srm_received'] = 'srm_received'
    version: int
    generation: int


class SRMSent(_BaseEvent):
    """A newer local SRM was sent to the source."""

    type: Literal['srm_sent'] = 'srm_sent'
    version: int


class ProximityCheckResponded(_BaseEvent):
    """A response was sent to the source's proximity check; the source measures the round-trip time."""

    type: Literal['proximity_check_responded'] = 'proximity_check_responded'


class AuthenticationCompleted(_BaseEvent):
    """Mutual authentication completed and the exchange key was received."""

    type: Literal['authentication_completed'] = 'authentication_completed'
    exchange_key_label: int


class TransferStarted(_BaseEvent):
    """Media transfer started, with separate totals for the HTTP body and decrypted content."""

    type: Literal['transfer_started'] = 'transfer_started'
    # HTTP body size in bytes (None if Content-Length is absent)
    total_http_bytes: int | None
    # Decrypted content size reported by the recorder (None if dlna:cleartextSize is absent)
    total_content_bytes: int | None


class TransferProtectionObserved(_BaseEvent):
    """Copy control information was read from the first protected packet header."""

    type: Literal['transfer_protection_observed'] = 'transfer_protection_observed'
    encryption_mode: ProtectionValue[int]
    # Usage rules embedded in the PCP random field (PCP-UR) are not supported
    usage_rules: ProtectionValue[bytes]


class TransferProgressed(_BaseEvent):
    """Media transfer progress."""

    type: Literal['transfer_progressed'] = 'transfer_progressed'
    # HTTP body bytes received, including protected packet headers and encryption padding
    http_bytes: int
    # Decrypted content bytes
    content_bytes: int
    # Bytes written to the sink (or processed by on_data for streaming)
    written_bytes: int


class TransferReceived(_BaseEvent):
    """The complete HTTP body was received."""

    type: Literal['transfer_received'] = 'transfer_received'
    http_bytes: int
    content_bytes: int


class SinkPersisted(_BaseEvent):
    """The sink finished persisting the received data."""

    type: Literal['sink_persisted'] = 'sink_persisted'
    written_bytes: int


class MoveFinalizeRequested(_BaseEvent):
    """A Move finalize request was sent; the source may now have consumed a copy."""

    type: Literal['move_finalize_requested'] = 'move_finalize_requested'


class MoveFinalized(_BaseEvent):
    """A verifiable Move finalize response was received from the source."""

    type: Literal['move_finalized'] = 'move_finalized'


class MoveCompleted(_BaseEvent):
    """The source acknowledged the Move completion notification."""

    type: Literal['move_completed'] = 'move_completed'


class MoveCancelRequested(_BaseEvent):
    """A Move cancellation request was sent before finalization."""

    type: Literal['move_cancel_requested'] = 'move_cancel_requested'


class MoveCancelled(_BaseEvent):
    """The source accepted the Move cancellation."""

    type: Literal['move_cancelled'] = 'move_cancelled'


class MoveResumeRequired(_BaseEvent):
    """
    No response was received to the finalize request or completion notification.
    The source may have consumed a copy, so retain the complete data and resume state to resume the same transfer.
    """

    type: Literal['move_resume_required'] = 'move_resume_required'
    state: Literal['finalizing', 'finalized']


class MoveFinalizeRejected(_BaseEvent):
    """
    The source rejected a Move finalize request, which will not be retried.
    If may_be_committed is True, retain the received data because an earlier request may have succeeded.
    """

    type: Literal['move_finalize_rejected'] = 'move_finalize_rejected'
    # Whether the source may have finalized the Move (if False, received data has been discarded)
    may_be_committed: bool


type TransferEvent = Annotated[
    MediaInfoReceived
    | AuthenticationStarted
    | PeerCertificateVerified
    | SRMReceived
    | SRMSent
    | ProximityCheckResponded
    | AuthenticationCompleted
    | TransferStarted
    | TransferProtectionObserved
    | TransferProgressed
    | TransferReceived
    | SinkPersisted
    | MoveFinalizeRequested
    | MoveFinalized
    | MoveCompleted
    | MoveCancelRequested
    | MoveCancelled
    | MoveResumeRequired
    | MoveFinalizeRejected,
    Field(discriminator='type'),
]

# Event callback (synchronous or awaitable)
type EventCallback = Callable[[TransferEvent], Awaitable[None] | None]


async def dispatch_event(callback: EventCallback | None, event: TransferEvent) -> None:
    """
    Pass an event to the callback and await its result if it is awaitable.

    Args:
        callback (EventCallback | None): User callback (None does nothing)
        event (TransferEvent): Event to dispatch
    """

    if callback is None:
        return
    result = callback(event)
    if result is not None:
        await result
