"""Exercise DubbingReceiver against a fake television on 127.0.0.1."""

import asyncio
import errno
import os
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

import aiohttp
import pytest
from aiohttp import web
from rich.progress import Progress

import pydtcpip.__main__ as cli
from pydtcpip.__main__ import ProgressReporter
from pydtcpip.dlna.ssdp import SSDPAdvertiser
from pydtcpip.events import EventCallback, MoveCompleted, TransferEvent, TransferReceived
from pydtcpip.metadata import MetadataSidecar
from pydtcpip.models import MoveResumeState, Recording, RecordingMetadata
from pydtcpip.receiver import DubbingReceiver, DubbingResumeStateCallback
from pydtcpip.sinks import FileSink
from pydtcpip.store import DeviceStore
from tests.fakes import FakeAuthority, FakeDevice, FakeSourceAKE, FakeTelevision, Framing, MemorySink


# Recording split into four packets, with the last plaintext length not divisible by the AES block size
_CONTENT = os.urandom(4000 * 3 + 1234)


@pytest.mark.anyio
@pytest.mark.parametrize('is_interrupted', [False, True])
async def test_cli_receiver_preserves_retries_and_finalization(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    is_interrupted: bool,
) -> None:
    """Complete CLI uploads after interruption and a metadata-only write failure.

    Args:
        authority (FakeAuthority): Test certificate authority
        sink_device (FakeDevice): Receiver credentials
        source_device (FakeDevice): Sender credentials
        tmp_path (Path): Isolated output directory
        monkeypatch (pytest.MonkeyPatch): CLI setup and disk failure injection
        caplog (pytest.LogCaptureFixture): Captured metadata warnings
        is_interrupted (bool): Retry the same upload after truncating its first body
    """

    receivers: list[DubbingReceiver] = []
    tasks: list[asyncio.Task[None]] = []
    started = asyncio.Event()
    upload_finished = asyncio.Event()
    replace = os.replace
    failed_updates: list[Path] = []

    class ObservedReceiver(DubbingReceiver):
        """Expose receiver startup and completed upload cleanup to the test sender."""

        async def start(self) -> None:
            """Expose the CLI listener to the test sender after startup."""

            await super().start()
            receivers.append(self)
            started.set()

        async def _handle_upload(self, request: web.Request) -> web.StreamResponse:
            """Notify the test after cleanup releases the receiver's upload lock.

            Args:
                request (web.Request): Incoming recording upload

            Returns:
                web.StreamResponse: Original upload response
            """

            try:
                return await super()._handle_upload(request)
            finally:
                upload_finished.set()

    def execute(coroutine: Coroutine[None, None, None]) -> None:
        """Run the CLI listener in the test's event loop.

        Args:
            coroutine (Coroutine[None, None, None]): CLI receive loop
        """

        tasks.append(asyncio.create_task(coroutine))

    def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        """Simulate a failed JSON update while media and resume files remain writable.

        Args:
            src (str | os.PathLike[str]): Completed temporary file
            dst (str | os.PathLike[str]): Destination of the replacement
        """

        if str(dst).endswith('.metadata.json'):
            archive = RecordingMetadata.model_validate_json(Path(src).read_bytes())
            if archive.transfer_events[-1]['type'] == 'move_finalized':
                failed_updates.append(Path(dst))
                raise OSError(errno.ENOSPC, 'Metadata volume is full')
        replace(src, dst)

    monkeypatch.setattr(cli, '_execute', execute)
    monkeypatch.setenv('PYDTCPIP_CERTIFICATE', sink_device.certificate.hex())
    monkeypatch.setenv('PYDTCPIP_PRIVATE_KEY', sink_device.private_key.hex())
    monkeypatch.setattr(cli, '_device_store', DeviceStore(tmp_path / 'device'))
    monkeypatch.setattr(cli, 'DubbingReceiver', ObservedReceiver)
    monkeypatch.setattr('pydtcpip.metadata.os.replace', replace_file)
    cli.receive(tmp_path, interface='127.0.0.1', source=['127.0.0.1'], port=0)
    try:
        await asyncio.wait_for(started.wait(), 5)
        receiver = receivers[0]
        async with FakeSourceAKE(authority, source_device) as source:
            television = FakeTelevision(source, _CONTENT)
            status, import_uri = await television.create_object(receiver.base_url, title='Retry example')
            assert status == 200 and import_uri is not None
            if is_interrupted is True:
                assert await television.upload(import_uri, truncation='boundary') is None
                await asyncio.wait_for(source.cancelled.wait(), 5)
                await asyncio.wait_for(source.disconnected.wait(), 5)
                # The control connection closes before asynchronous partial-file cleanup finishes
                await asyncio.wait_for(upload_finished.wait(), 5)
                assert list(tmp_path.glob('*.m2ts')) == []
            upload_finished.clear()
            assert await television.upload(import_uri) == 200
            await asyncio.wait_for(source.disconnected.wait(), 5)
            await asyncio.wait_for(upload_finished.wait(), 5)
            assert source.log[-2:] == ['finalize', 'complete']
            assert source.errors == []
            (archive_path,) = tmp_path.glob('*.metadata.json')
            completed_archive = archive_path.read_bytes()
            upload_finished.clear()
            assert await television.upload(import_uri) == 500
            await asyncio.wait_for(source.disconnected.wait(), 5)
            await asyncio.wait_for(upload_finished.wait(), 5)
            assert archive_path.read_bytes() == completed_archive
    finally:
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]

    (output,) = tmp_path.glob('*.m2ts')
    assert output.read_bytes() == _CONTENT
    archive = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
    events = [event['type'] for event in archive.transfer_events]
    assert ('move_cancelled' in events) is is_interrupted
    assert events[-2:] == ['move_finalized', 'move_completed']
    assert failed_updates == [output.with_suffix('.metadata.json')]
    assert 'Could not update recording metadata' in caplog.text
    assert list(tmp_path.glob('*.pydtcpip-resume.json')) == []


@pytest.mark.anyio
async def test_incoming_metadata_preserves_sender_xml_and_transfer_events(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
) -> None:
    """Archive sender-supplied IDs and text before the receiver rewrites upload metadata."""

    output = tmp_path / 'incoming.m2ts'
    sidecars: dict[str, MetadataSidecar] = {}
    completed = asyncio.Event()

    def create_sink(recording: Recording) -> FileSink:
        sidecars[recording.id] = MetadataSidecar(
            output,
            RecordingMetadata(
                recording=recording,
                profile='generic',
                profile_unverified=False,
                observed_at=datetime.now(UTC),
            ),
        )
        return FileSink(output)

    def on_event(event: TransferEvent) -> None:
        sidecar = sidecars.get(event.transfer_id)
        if sidecar is not None:
            sidecar.on_event(event)
        if isinstance(event, MoveCompleted):
            completed.set()

    receiver = DubbingReceiver(sink_device.credentials(), create_sink, interface='127.0.0.1', port=0, on_event=on_event)
    await receiver.start()
    try:
        async with FakeSourceAKE(authority, source_device) as source:
            television = FakeTelevision(source, _CONTENT)
            status, import_uri = await television.create_object(receiver.base_url, title='Sample\ue0fe')
            assert status == 200 and import_uri is not None
            assert await television.upload(import_uri, framing='chunked') == 200
            await asyncio.wait_for(completed.wait(), timeout=5)
            assert source.errors == []
    finally:
        await receiver.stop()
    metadata = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
    assert metadata.recording.title == 'Sample🈑'
    assert metadata.recording.source_xml is not None
    original_item = ET.fromstring(metadata.recording.source_xml)[0]
    assert original_item.get('id') == ''
    assert '\ue0fe' in metadata.recording.source_xml
    assert metadata.recording.id != ''
    assert metadata.transfer_events[-1]['type'] == 'move_completed'
    assert output.read_bytes() == _CONTENT


@pytest.mark.anyio
@pytest.mark.parametrize('framing', ['length', 'chunked'])
async def test_progress_finishes_for_three_incoming_recordings(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    framing: Framing,
) -> None:
    """Keep the listener running after three transfers without leaving receiving bars behind."""

    progress = Progress(auto_refresh=False, disable=True)
    reporter = ProgressReporter(progress, 'Receiving')
    completed = asyncio.Event()
    received_counts: list[int] = []

    def on_event(event: TransferEvent) -> None:
        reporter(event)
        if isinstance(event, TransferReceived):
            task = progress.tasks[0]
            assert task.finished is True
            assert task.stop_time is not None
            assert task.description == 'Received'
            received_counts.append(event.content_bytes)
        elif isinstance(event, MoveCompleted):
            assert progress.tasks == []
            completed.set()

    sink = MemorySink()
    async with _run_receiver(sink_device, sink, on_event=on_event) as receiver:
        for _ in range(3):
            completed.clear()
            async with FakeSourceAKE(authority, source_device) as source:
                television = FakeTelevision(source, _CONTENT)
                status, import_uri = await television.create_object(receiver.base_url)
                assert status == 200
                assert import_uri is not None
                assert await television.upload(import_uri, framing=framing) == 200
                await asyncio.wait_for(completed.wait(), timeout=5)
                assert source.errors == []
        assert progress.tasks == []
    assert received_counts == [len(_CONTENT)] * 3


@pytest.fixture(autouse=True)
def _disable_ssdp(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Disable SSDP advertisements so tests do not send multicast traffic to the LAN.

    Args:
        monkeypatch (pytest.MonkeyPatch): Fixture used to replace advertiser methods
    """

    async def do_nothing(self: SSDPAdvertiser) -> None:
        pass

    monkeypatch.setattr(SSDPAdvertiser, 'start', do_nothing)
    monkeypatch.setattr(SSDPAdvertiser, 'stop', do_nothing)


@asynccontextmanager
async def _run_receiver(
    sink_device: FakeDevice,
    sink: MemorySink,
    *,
    allowed_sources: frozenset[str] | None = frozenset(('127.0.0.1',)),
    interface: str = '127.0.0.1',
    on_event: EventCallback | None = None,
    on_resume_state: DubbingResumeStateCallback | None = None,
) -> AsyncGenerator[DubbingReceiver]:
    """
    Run the receiver on an OS-assigned localhost port and wait for active transfers before stopping.

    Args:
        sink_device (FakeDevice): Credentials for the receiver
        sink (MemorySink): Destination for received data
        allowed_sources (frozenset[str] | None): Permitted source addresses
        interface (str): Local listener address
        on_event (EventCallback | None): Event callback
        on_resume_state (DubbingResumeStateCallback | None): Resume-state callback

    Yields:
        DubbingReceiver: Running receiver
    """

    recordings: list[Recording] = []

    def create_sink(recording: Recording) -> MemorySink:
        recordings.append(recording)
        return sink

    receiver = DubbingReceiver(
        sink_device.credentials(),
        create_sink,
        interface=interface,
        allowed_sources=allowed_sources,
        port=0,
        timeout=5,
        on_event=on_event,
        on_resume_state=on_resume_state,
    )
    await receiver.start()
    try:
        yield receiver
    finally:
        await receiver.stop()


@pytest.mark.anyio
@pytest.mark.parametrize('framing', ['length', 'chunked'])
async def test_dubbing_is_finalized_and_completed(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    framing: Framing,
) -> None:
    """Authenticate before 100 Continue and finalize after the final HTTP response."""

    sink = MemorySink()
    async with FakeSourceAKE(authority, source_device) as source:
        television = FakeTelevision(source, _CONTENT)
        async with _run_receiver(sink_device, sink) as receiver:
            status, import_uri = await television.create_object(receiver.base_url)
            assert status == 200
            assert import_uri is not None
            assert await television.upload(import_uri, framing=framing) == 200
        # stop() waits for active finalization and completion

    assert bytes(sink.data) == _CONTENT
    assert sink.calls == ['persist', 'commit']
    assert source.log == ['mv_initiate', 'challenge', 'rtt', 'mv_exchange_key', 'http_response', 'finalize', 'complete']
    assert source.errors == []


@pytest.mark.anyio
async def test_wildcard_receiver_returns_reachable_upload_url(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
) -> None:
    """An unrestricted wildcard listener returns the address used by the recorder and completes a transfer."""

    sink = MemorySink()
    async with FakeSourceAKE(authority, source_device) as source:
        television = FakeTelevision(source, _CONTENT)
        async with _run_receiver(sink_device, sink, interface='0.0.0.0', allowed_sources=None) as receiver:
            status, import_uri = await television.create_object(f'http://127.0.0.1:{receiver.port}')
            assert status == 200
            assert import_uri is not None
            assert import_uri.startswith(f'http://127.0.0.1:{receiver.port}/upload/')
            assert await television.upload(import_uri, framing='length') == 200
    assert bytes(sink.data) == _CONTENT
    assert sink.calls == ['persist', 'commit']
    assert source.errors == []


@pytest.mark.anyio
@pytest.mark.parametrize('framing', ['length', 'chunked'])
async def test_truncated_dubbing_is_cancelled(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    framing: Framing,
) -> None:
    """A body truncated at a packet boundary must not be finalized as a complete recording."""

    sink = MemorySink()
    async with FakeSourceAKE(authority, source_device) as source:
        television = FakeTelevision(source, _CONTENT)
        async with _run_receiver(sink_device, sink) as receiver:
            _, import_uri = await television.create_object(receiver.base_url)
            assert import_uri is not None
            assert await television.upload(import_uri, framing=framing, truncation='boundary') is None

    assert sink.calls == ['discard']
    assert 'cancel' in source.log
    assert source.finalize_nonces == []


@pytest.mark.anyio
async def test_dubbing_resume_state_identifies_recording(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
) -> None:
    """Resume state must identify the recording so a transfer can be resumed after a lost response."""

    sink = MemorySink()
    resume_states: list[tuple[str, MoveResumeState | None]] = []

    def save_resume_state(recording: Recording, state: MoveResumeState | None) -> None:
        resume_states.append((recording.title, state))

    async with FakeSourceAKE(authority, source_device) as source:
        television = FakeTelevision(source, _CONTENT)
        async with _run_receiver(sink_device, sink, on_resume_state=save_resume_state) as receiver:
            _, import_uri = await television.create_object(receiver.base_url, title='再開の確認')
            assert import_uri is not None
            assert await television.upload(import_uri) == 200

    assert [title for title, _ in resume_states] == ['再開の確認'] * 3
    assert [state.state if state is not None else None for _, state in resume_states] == [
        'finalizing',
        'finalized',
        None,
    ]


@pytest.mark.anyio
async def test_callback_error_during_authentication_releases_upload_lock(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
) -> None:
    """An unexpected authentication callback error must release the lock for later transfers and shutdown."""

    sink = MemorySink()
    failures = ['authentication_started']

    def on_event(event: TransferEvent) -> None:
        if event.type in failures:
            failures.remove(event.type)
            raise ValueError('User callback failed')

    async with FakeSourceAKE(authority, source_device) as source:
        television = FakeTelevision(source, _CONTENT)
        async with _run_receiver(sink_device, sink, on_event=on_event) as receiver:
            _, import_uri = await television.create_object(receiver.base_url)
            assert import_uri is not None
            assert await television.upload(import_uri) == 500

            # The next transfer succeeds only if the lock was released
            _, import_uri = await television.create_object(receiver.base_url)
            assert import_uri is not None
            assert await television.upload(import_uri) == 200

    assert bytes(sink.data) == _CONTENT
    assert sink.calls == ['persist', 'commit']
    assert source.log[-2:] == ['finalize', 'complete']


@pytest.mark.anyio
async def test_description_declares_namespace_on_each_extension(sink_device: FakeDevice) -> None:
    """REGZA 43Z570L requires extension namespaces on individual elements to list the destination."""

    async with (
        _run_receiver(sink_device, MemorySink()) as receiver,
        aiohttp.ClientSession() as session,
        session.get(f'{receiver.base_url}/description.xml') as response,
    ):
        assert response.status == 200
        document = minidom.parseString(await response.read())

    # Each capability element declares its own namespace; the root declares neither extension namespace
    root = document.documentElement
    assert root is not None
    for name, prefix, namespace, value in (
        ('X_DLNACAP', 'dlna', 'urn:schemas-dlna-org:device-1-0', 'av-upload,dtcp-move'),
        ('X_move_support', 'move', 'urn:schemas-toshiba-co-jp:movemetadata-1-0/', 'MPEG_TTS_CP,PN_FLAG=C0000000'),
    ):
        element = document.getElementsByTagName(f'{prefix}:{name}')[0]
        assert element.getAttribute(f'xmlns:{prefix}') == namespace
        assert element.firstChild is not None
        assert element.firstChild.nodeValue == value
        assert root.hasAttribute(f'xmlns:{prefix}') is False


@pytest.mark.anyio
async def test_create_object_from_unknown_source_is_forbidden(
    authority: FakeAuthority, sink_device: FakeDevice, source_device: FakeDevice
) -> None:
    """Reject CreateObject from unauthorized devices so they cannot start transfers to the destination."""

    sink = MemorySink()
    async with FakeSourceAKE(authority, source_device) as source:
        television = FakeTelevision(source, _CONTENT)
        # Exclude the television at 127.0.0.1 from the allowed documentation address
        async with _run_receiver(sink_device, sink, allowed_sources=frozenset(('192.0.2.1',))) as receiver:
            status, import_uri = await television.create_object(receiver.base_url)

    assert status == 403
    assert import_uri is None
    assert source.log == []
    assert sink.calls == []
