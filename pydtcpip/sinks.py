"""Sinks for received recording data."""

import asyncio
import os
from pathlib import Path
from typing import BinaryIO, Protocol, Self


class Sink(Protocol):
    """
    A destination for decrypted recording data received through Move or an incoming transfer.
    The library sends the finalize request only after persist() returns, so persist() must finish
    persisting all received data before returning.
    """

    async def write(self, chunk: bytes) -> None:
        """Write a chunk of decrypted recording data."""
        ...

    async def persist(self) -> None:
        """Return only after all written data has been persisted."""
        ...

    async def commit(self) -> None:
        """Make the saved data available after the Move completes."""
        ...

    async def discard(self) -> None:
        """Discard partial data when a Move is cancelled before the finalize request."""
        ...


class FileSink:
    """
    A sink that saves recording data to a file.
    Data is written to `.<filename>.part` beside the destination and moved into place when committed.
    """

    def __init__(self, path: Path) -> None:
        """
        Set the destination file.

        Args:
            path (Path): Recording destination (existing files are not overwritten)
        """

        self.path = path
        self.pending_path = path.with_name(f'.{path.name}.part')
        self._file: BinaryIO | None = None
        # Track ownership so discard() does not delete another transfer's `.part` file
        self._has_created_pending_file = False

    @classmethod
    def resume(cls, path: Path) -> Self:
        """
        Create a sink for an existing `.part` file to resume finalization.

        Args:
            path (Path): Destination specified for the original Move

        Returns:
            FileSink: Sink whose commit() moves the received data to the destination
        """

        sink = cls(path)
        if sink.pending_path.exists() is False:
            raise FileNotFoundError(f'Received recording data not found: {sink.pending_path}')
        return sink

    async def write(self, chunk: bytes) -> None:
        """
        Write received data to `.part`.

        Args:
            chunk (bytes): Chunk of decrypted recording data
        """

        # Create `.part` on the first write, without overwriting the destination or existing partial data
        if self._file is None:
            if self.path.exists() is True:
                raise FileExistsError(f'The destination file already exists: {self.path}')
            self._file = self.pending_path.open('xb')
            self._has_created_pending_file = True
        await asyncio.to_thread(self._file.write, chunk)

    async def persist(self) -> None:
        """
        Flush and fsync `.part` before returning.
        No writes follow persist(), so close the file rather than leaving it open while awaiting finalization recovery.
        """

        if self._file is None:
            return
        file = self._file

        def flush_and_sync() -> None:
            file.flush()
            os.fsync(file.fileno())

        await asyncio.to_thread(flush_and_sync)
        self._close()

    async def commit(self) -> None:
        """
        Move `.part` to the destination without overwriting an existing file.
        Report the received data's location on failure so it can be recovered.
        """

        self._close()

        def move() -> None:
            # Create the destination exclusively before replacing it, avoiding overwriting another process's file
            ## os.link() fails on filesystems without hard link support, such as exFAT, FAT, and some SMB shares
            with self.path.open('xb'):
                pass
            try:
                os.replace(self.pending_path, self.path)
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise

        try:
            await asyncio.to_thread(move)
        except OSError as ex:
            raise OSError(
                f'The received data could not be moved to the destination ({ex}). '
                f'The received data remains at {self.pending_path}.'
            ) from ex

    async def discard(self) -> None:
        """Delete the `.part` file created by this sink when cancelled before finalization."""

        self._close()
        if self._has_created_pending_file is True:
            await asyncio.to_thread(self.pending_path.unlink, missing_ok=True)
            self._has_created_pending_file = False

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
