"""Transfer queue entries and their states, derived from transfer events without depending on Textual."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydtcpip.errors import MoveCommitmentRejectedError, MoveInitiationRejectedError
from pydtcpip.events import (
    AuthenticationStarted,
    MoveCancelled,
    MoveCancelRequested,
    MoveCompleted,
    MoveFinalized,
    MoveFinalizeRejected,
    MoveFinalizeRequested,
    MoveResumeRequired,
    SinkPersisted,
    TransferEvent,
    TransferProgressed,
    TransferReceived,
    TransferStarted,
)
from pydtcpip.models import ProtectionValue, Recording, Resource
from pydtcpip.tui.display import format_bytes


type QueueStatus = Literal[
    'waiting',
    'metadata',
    'authenticating',
    'receiving',
    'received',
    'persisted',
    'finalizing',
    'finalized',
    'completed',
    'stream_saved',
    'stream_cancelled',
    'cancel_requested',
    'cancelled',
    'cancel_unconfirmed',
    'stopped',
    'resume_required',
    'resuming',
    'finalize_rejected',
    'initiation_rejected',
    'failed',
    'skipped',
]

STATUS_LABELS: dict[QueueStatus, str] = {
    'waiting': 'Waiting',
    'metadata': 'Fetching metadata',
    'authenticating': 'Authenticating',
    'receiving': 'Receiving',
    'received': 'Received',
    'persisted': 'Saved, finalizing next',
    'finalizing': 'Finalizing',
    'finalized': 'Finalized',
    'completed': 'Completed',
    'stream_saved': 'Saved via stream',
    'stream_cancelled': 'Stream cancelled',
    'cancel_requested': 'Cancellation requested',
    'cancelled': 'Cancelled by source',
    'cancel_unconfirmed': 'Stopped (cancellation unconfirmed)',
    'stopped': 'Stopped before transfer',
    'resume_required': 'Resume required',
    'resuming': 'Resuming finalization',
    'finalize_rejected': 'Finalization rejected',
    'initiation_rejected': 'Move refused',
    'failed': 'Failed',
    'skipped': 'Not started',
}


@dataclass
class QueueItem:
    """
    A recording queued for Move, including resumption or an original-quality stream chosen after a refusal.
    The committing flag is set once a finalize request may be sent and stays set until the transfer ends,
    so neither cancellation nor quitting can drop the control connection afterward.
    """

    title: str
    output: Path
    # Identity of the recording on its device: (device UDN, reference ID or object ID)
    key: tuple[str, str] | None = None
    recording: Recording | None = None
    resource: Resource | None = None
    stream_resource: Resource | None = None
    is_resume: bool = False
    status: QueueStatus = 'waiting'
    is_committing: bool = False
    is_cancelled_by_user: bool = False
    total_bytes: int | None = None
    # Whether total_bytes is the encrypted HTTP body size rather than the decrypted size
    is_total_http: bool = False
    http_bytes: int = 0
    content_bytes: int = 0
    written_bytes: int = 0
    message: str | None = None
    may_be_committed: bool | None = None
    copies_before: ProtectionValue[int] | None = None
    copies_after: str | None = None
    # When the Move completed, used to tell copy counts listed before it from fresh ones
    completed_at: datetime | None = None
    # When a re-query first observed the source count change after the Move
    copies_confirmed_at: datetime | None = None
    task: asyncio.Task[int] | None = field(default=None, repr=False)

    @property
    def is_active(self) -> bool:
        """Return whether the transfer has started and not yet ended."""

        return self.status not in (
            'waiting',
            'completed',
            'stream_saved',
            'stream_cancelled',
            'cancelled',
            'cancel_unconfirmed',
            'stopped',
            'resume_required',
            'finalize_rejected',
            'initiation_rejected',
            'failed',
            'skipped',
        )

    def start(self) -> None:
        """Mark the transfer as started; a resumed Move is committing from the beginning."""

        self.message = None
        if self.is_resume is True:
            self.status = 'resuming'
            self.is_committing = True
        else:
            self.status = 'authenticating' if self.stream_resource is not None else 'metadata'
            self.is_committing = False

    def apply(self, event: TransferEvent) -> None:
        """
        Update the state from a transfer event.

        Args:
            event (TransferEvent): Event delivered by the transfer
        """

        if isinstance(event, AuthenticationStarted):
            # Resumption authenticates a new control connection but remains within finalization
            if self.is_committing is False:
                self.status = 'authenticating'
        elif isinstance(event, TransferStarted):
            self.status = 'receiving'
            self.is_total_http = event.total_content_bytes is None
            self.total_bytes = (
                event.total_content_bytes if event.total_content_bytes is not None else event.total_http_bytes
            )
        elif isinstance(event, TransferProgressed):
            self.http_bytes = event.http_bytes
            self.content_bytes = event.content_bytes
            self.written_bytes = event.written_bytes
        elif isinstance(event, TransferReceived):
            self.http_bytes = event.http_bytes
            self.content_bytes = event.content_bytes
            self.status = 'received'
        elif isinstance(event, SinkPersisted):
            self.written_bytes = event.written_bytes
            self.status = 'persisted'
        elif isinstance(event, MoveFinalizeRequested):
            # The source may consume a copy from here, so cancellation and quitting must wait
            self.is_committing = True
            self.status = 'finalizing'
        elif isinstance(event, MoveFinalized):
            self.status = 'finalized'
        elif isinstance(event, MoveCompleted):
            self.status = 'completed'
        elif isinstance(event, MoveCancelRequested):
            self.status = 'cancel_requested'
        elif isinstance(event, MoveCancelled):
            self.status = 'cancelled'
        elif isinstance(event, MoveResumeRequired):
            self.status = 'resume_required'
        elif isinstance(event, MoveFinalizeRejected):
            self.status = 'finalize_rejected'
            self.may_be_committed = event.may_be_committed

    def finish(self, error: BaseException | None, *, has_resume_state: bool) -> None:
        """
        Settle the state after the transfer task ends.

        Args:
            error (BaseException | None): Exception that ended the transfer, or None on success
            has_resume_state (bool): Whether resume state remains beside the output
        """

        if error is None:
            if self.stream_resource is not None:
                self.status = 'stream_saved'
            else:
                self.status = 'completed'
                self.completed_at = datetime.now(UTC)
        elif isinstance(error, asyncio.CancelledError):
            if self.stream_resource is not None:
                self.status = 'stream_cancelled'
            elif self.status == 'cancel_requested':
                # The cancellation was sent but not acknowledged; finalization was never requested
                self.status = 'cancel_unconfirmed'
            elif self.status in ('waiting', 'metadata', 'authenticating'):
                self.status = 'stopped'
            elif self.status != 'cancelled':
                self.status = 'resume_required' if has_resume_state is True else 'cancel_unconfirmed'
        elif isinstance(error, MoveCommitmentRejectedError):
            self.status = 'finalize_rejected'
            self.may_be_committed = error.may_be_committed
            self.message = str(error)
        else:
            self.message = str(error) or type(error).__name__
            if has_resume_state is True:
                self.status = 'resume_required'
            elif isinstance(error, MoveInitiationRejectedError) and self.is_resume is False:
                self.status = 'initiation_rejected'
            elif self.status not in ('cancelled', 'finalize_rejected'):
                self.status = 'failed'

    def progress_text(self) -> str:
        """
        Describe transfer progress with written bytes and the expected total when known.

        Returns:
            str: Progress such as 1.2 GB written (20%)
        """

        if self.status in ('waiting', 'metadata', 'authenticating', 'skipped', 'stopped'):
            return ''
        written = self.written_bytes or self.content_bytes
        if self.total_bytes:
            # Compare received HTTP bytes with an HTTP total, since encryption makes the body larger than the file
            received = self.http_bytes if self.is_total_http is True else written
            percent = (
                100 if self.status in ('completed', 'stream_saved') else min(100, received * 100 // self.total_bytes)
            )
            return f'{format_bytes(written)} written ({percent}%)'
        return f'{format_bytes(written)} written'
