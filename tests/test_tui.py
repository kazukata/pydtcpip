"""Drive the interactive interface against fake recorders and transfer sources."""

import asyncio
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual import events
from textual.coordinate import Coordinate
from textual.messages import InBandWindowResize
from textual.widgets import Button, Checkbox, DataTable, Input
from typer.testing import CliRunner

from pydtcpip.__main__ import app as cli_app
from pydtcpip.dlna.client import DLNAClient, MediaServer
from pydtcpip.dlna.description import parse_device_description
from pydtcpip.dlna.didl import parse_didl
from pydtcpip.dlna.profiles import PROFILES, select_profile
from pydtcpip.events import (
    AuthenticationStarted,
    MoveCancelled,
    MoveCancelRequested,
    MoveCompleted,
    MoveFinalized,
    MoveFinalizeRequested,
    MoveResumeRequired,
    SinkPersisted,
    TransferReceived,
    TransferStarted,
)
from pydtcpip.metadata import MetadataSidecar
from pydtcpip.models import Recording, RecordingMetadata
from pydtcpip.operations import recording_output_path, resume_state_path
from pydtcpip.store import DeviceStore
from pydtcpip.tui import display
from pydtcpip.tui.app import PyDTCPIPApp, Selection, TransferRunner
from pydtcpip.tui.screens import ChoiceScreen, ConfirmMoveScreen, ResourceScreen
from pydtcpip.tui.state import QueueItem
from tests.fakes import (
    FakeAuthority,
    FakeDevice,
    FakeMediaServer,
    FakeRecorder,
    FakeSourceAKE,
    didl_container,
    didl_recording,
    dtcp_resource,
)


_UDN = 'uuid:00000000-0000-4000-8000-000000000001'


def _recording(object_id: str, parent_id: str, title: str, url: str, *, copies: int | None = 10, **kwargs: str) -> str:
    """Build a Toshiba-style recording whose saved size is reported by the device."""

    fields = f'<regza:copycount>{copies}</regza:copycount>' if copies is not None else ''
    return didl_recording(
        object_id,
        parent_id,
        title,
        dtcp_resource(url, attributes='dlna:cleartextSize="40000" duration="0:30:00.000"'),
        fields=fields,
        **kwargs,
    )


def _transfer_id() -> str:
    return 'transfer'


def test_queue_states_follow_finalization_and_cancellation() -> None:
    """Completion, cancellation and resumption remain distinct, and finalization locks out cancellation."""

    item = QueueItem(title='Recording', output=Path('recording.m2ts'))
    item.start()
    item.apply(AuthenticationStarted(transfer_id=_transfer_id(), host='127.0.0.1', port=1, is_move=True))
    item.apply(TransferStarted(transfer_id=_transfer_id(), total_http_bytes=None, total_content_bytes=100))
    assert (item.status, item.is_committing) == ('receiving', False)
    item.apply(TransferReceived(transfer_id=_transfer_id(), http_bytes=110, content_bytes=100))
    item.apply(SinkPersisted(transfer_id=_transfer_id(), written_bytes=100))
    assert (item.status, item.is_committing) == ('persisted', False)
    item.apply(MoveFinalizeRequested(transfer_id=_transfer_id()))
    assert (item.status, item.is_committing) == ('finalizing', True)
    item.apply(MoveFinalized(transfer_id=_transfer_id()))
    item.apply(MoveCompleted(transfer_id=_transfer_id()))
    item.finish(None, has_resume_state=False)
    assert item.status == 'completed'
    assert item.progress_text() == '100 B written (100%)'

    cancelled = QueueItem(title='Recording', output=Path('recording.m2ts'))
    cancelled.start()
    cancelled.apply(TransferStarted(transfer_id=_transfer_id(), total_http_bytes=110, total_content_bytes=None))
    cancelled.apply(MoveCancelRequested(transfer_id=_transfer_id()))
    cancelled.finish(asyncio.CancelledError(), has_resume_state=False)
    assert cancelled.status == 'cancel_unconfirmed'
    acknowledged = QueueItem(title='Recording', output=Path('recording.m2ts'))
    acknowledged.start()
    acknowledged.apply(MoveCancelRequested(transfer_id=_transfer_id()))
    acknowledged.apply(MoveCancelled(transfer_id=_transfer_id()))
    acknowledged.finish(asyncio.CancelledError(), has_resume_state=False)
    assert acknowledged.status == 'cancelled'

    interrupted = QueueItem(title='Recording', output=Path('recording.m2ts'))
    interrupted.start()
    interrupted.apply(MoveFinalizeRequested(transfer_id=_transfer_id()))
    interrupted.apply(MoveResumeRequired(transfer_id=_transfer_id(), state='finalizing'))
    interrupted.finish(TimeoutError(), has_resume_state=True)
    assert interrupted.status == 'resume_required'

    # Resumption authenticates again but stays within finalization
    interrupted.is_resume = True
    interrupted.start()
    interrupted.apply(AuthenticationStarted(transfer_id=_transfer_id(), host='127.0.0.1', port=1, is_move=False))
    assert (interrupted.status, interrupted.is_committing) == ('resuming', True)


def test_output_paths_do_not_create_directories_or_collide() -> None:
    """Object IDs with path separators or shared prefixes produce distinct file names in the destination."""

    def parse(object_id: str) -> Recording:
        document = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            + didl_recording(object_id, '0', 'Same/title', dtcp_resource('http://192.0.2.1/media'))
            + '</DIDL-Lite>'
        )
        recording = parse_didl(document, next(profile for profile in PROFILES if profile.name == 'generic'))[0]
        assert isinstance(recording, Recording)
        return recording

    directory = Path('/recordings')
    recordings = [
        parse('0/video/all/pvr?id=1234567890123456789&group=1'),
        parse('RD_00010003_0000010300'),
        parse('RD_00010003_0000010301'),
    ]
    paths = [recording_output_path(directory, _UDN, recording) for recording in recordings]
    assert all(path.parent == directory for path in paths)
    assert len(set(paths)) == len(paths)
    assert all(path.name.startswith('Same_title-') for path in paths)


def test_tui_command_requires_a_terminal_and_an_existing_directory(tmp_path: Path) -> None:
    """Running without a command still shows usage, and the tui command explains what it needs."""

    result = CliRunner().invoke(cli_app, [])
    assert result.exit_code == 2
    assert 'Usage' in result.output
    result = CliRunner().invoke(cli_app, ['tui', str(tmp_path / 'missing')])
    assert result.exit_code == 1
    assert 'Output directory not found' in result.output
    result = CliRunner().invoke(cli_app, ['tui', str(tmp_path)])
    assert result.exit_code == 1
    assert 'requires a terminal' in result.output


@asynccontextmanager
async def _launch(
    recorder: FakeRecorder, directory: Path, data_dir: Path
) -> AsyncGenerator[tuple[PyDTCPIPApp, TransferRunner]]:
    """Run the interface against a fake recorder, returning only after any transfer settles."""

    async with AsyncExitStack() as stack:
        dlna = await stack.enter_async_context(DLNAClient())

        async def discover(
            *,
            timeout: float = 3.0,
            interface: str | None = None,
            port: int = 0,
            include_announcements: bool = True,
        ) -> list[MediaServer]:
            return [await dlna.get_server(recorder.location)]

        dlna.discover = discover  # type: ignore[method-assign]
        runner = TransferRunner(DeviceStore(data_dir), stack)
        app = PyDTCPIPApp(dlna=dlna, runner=runner, interface='127.0.0.1', search_port=0, directory=directory)
        try:
            yield app, runner
        finally:
            await runner.settle()


async def _wait(pilot_pause: Callable[..., object], condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Process events until a condition holds."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while condition() is False:
        assert loop.time() < deadline, 'Timed out waiting for the interface'
        await pilot_pause(0.02)  # pyright: ignore[reportGeneralTypeIssues]


def _table(app: PyDTCPIPApp) -> DataTable[object]:
    return app.query_one('#recordings', DataTable)


def _rows(app: PyDTCPIPApp) -> list[str]:
    table = _table(app)
    return [
        str(table.get_cell_at(Coordinate(index, table.get_column_index('title')))) for index in range(table.row_count)
    ]


def _highlight(app: PyDTCPIPApp, title: str) -> None:
    table = _table(app)
    table.move_cursor(row=_rows(app).index(title))


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch, sink_device: FakeDevice) -> None:
    """Provide test sink credentials through the environment."""

    monkeypatch.setenv('PYDTCPIP_CERTIFICATE', sink_device.certificate.hex())
    monkeypatch.setenv('PYDTCPIP_PRIVATE_KEY', sink_device.private_key.hex())
    monkeypatch.setattr('pydtcpip.tui.app._REQUERY_INTERVAL', 0.01)
    monkeypatch.setattr('pydtcpip.tui.app._REQUERY_ATTEMPTS', 2)


@pytest.mark.anyio
async def test_browsing_pages_folders_and_restores_the_position(tmp_path: Path) -> None:
    """Folders are listed page by page, entered and left, and failures do not close the interface."""

    many = [
        _recording(f'RD_{index:04d}', 'F1', f'長いタイトルの番組 第{index:03d}話', 'http://192.0.2.1/media')
        for index in range(95)
    ]
    folders = {
        '0': [didl_container('F1', '0', 'ドラマ', len(many)), didl_container('F2', '0', 'Broken', 1)],
        'F1': many,
    }
    async with FakeRecorder(folders, page_size=40) as recorder:
        recorder.failing_ids.add('F2')
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 2)
            await pilot.press('enter')
            _highlight(app, 'ドラマ/')
            await pilot.press('enter')
            await _wait(pilot.pause, lambda: _table(app).row_count == 95)
            assert [start for _, object_id, start in recorder.calls if object_id == 'F1'] == [0, 40, 80]
            _highlight(app, '長いタイトルの番組 第050話')
            await pilot.press('backspace')
            await _wait(pilot.pause, lambda: _table(app).row_count == 2)
            _highlight(app, 'Broken/')
            await pilot.press('enter')
            await pilot.pause(0.3)
            assert app.is_running is True
            await pilot.press('backspace')
            _highlight(app, 'ドラマ/')
            await pilot.press('enter')
            await _wait(pilot.pause, lambda: _table(app).row_count == 95)
            # Cached listing reopens at the previously highlighted recording
            assert _rows(app)[_table(app).cursor_row] == '長いタイトルの番組 第050話'


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_selected_recordings_move_in_sequence(
    authority: FakeAuthority, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Duplicate references queue once, warnings and conflicts appear, and each Move finalizes in order."""

    output_dir = tmp_path / 'out'
    output_dir.mkdir()
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, b'First recording ' * 2500) as first,
        FakeMediaServer(source, b'Final recording ' * 2500) as final,
        FakeMediaServer(source, b'Unknown recording ' * 2500) as unknown,
        FakeMediaServer(source, b'Existing recording ' * 2500) as existing,
    ):
        folders = {
            '0': [didl_container('ALL', '0', 'All', 4), didl_container('GENRE', '0', 'Genre', 1)],
            'ALL': [
                _recording('A1', 'ALL', 'First', first.url, reference_id='R1'),
                _recording('A2', 'ALL', 'Final', final.url, copies=1),
                _recording('A3', 'ALL', 'Unknown', unknown.url, copies=None),
                _recording('A4', 'ALL', 'Existing', existing.url),
            ],
            'GENRE': [_recording('G1', 'GENRE', 'First', first.url, reference_id='R1')],
        }
        async with FakeRecorder(folders, udn=_UDN) as recorder:
            async with _launch(recorder, output_dir, tmp_path / 'device') as (app, _):
                async with app.run_test(size=(200, 50)) as pilot:
                    await _wait(pilot.pause, lambda: _table(app).row_count == 2)
                    await pilot.press('enter')
                    _highlight(app, 'All/')
                    await pilot.press('enter')
                    await _wait(pilot.pause, lambda: _table(app).row_count == 4)
                    recordings = {
                        title: app._objects[f'A{index}']  # pyright: ignore[reportPrivateUsage]
                        for index, title in enumerate(['First', 'Final', 'Unknown', 'Existing'], start=1)
                    }
                    existing_recording = recordings['Existing']
                    assert isinstance(existing_recording, Recording)
                    existing_output = recording_output_path(output_dir, _UDN, existing_recording)
                    existing_output.write_bytes(b'kept')
                    await pilot.press('a')
                    assert len(app._selection) == 4  # pyright: ignore[reportPrivateUsage]
                    await pilot.press('backspace')
                    _highlight(app, 'Genre/')
                    await pilot.press('enter')
                    await _wait(pilot.pause, lambda: _table(app).row_count == 1)
                    # The genre listing refers to the selected recording, so it shows as selected
                    assert str(_table(app).get_row_at(0)[0]) == '\u25a0'
                    await pilot.press('m')
                    await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
                    warnings = str(app.screen.query_one('#confirm-warnings').render())
                    assert 'Only the final Move remains' in warnings
                    assert 'The remaining copy count is unavailable' in warnings
                    move = app.screen.query_one('#confirm-move', Button)
                    assert str(move.label) == 'Move 3 recordings'
                    move.press()
                    queue = app._queue  # pyright: ignore[reportPrivateUsage]
                    await _wait(
                        pilot.pause, lambda: len(queue) == 3 and all(item.status == 'completed' for item in queue)
                    )
                    await _wait(
                        pilot.pause,
                        lambda: all(
                            item.copies_after not in (None, 'Checking...') for item in queue if item.title == 'First'
                        ),
                    )
                    await pilot.press('q')
        assert source.errors == []
        assert source.log.count('finalize') == 3
        assert source.log.count('complete') == 3
        assert first.count('GET') == 1
        assert existing.count('GET') == 0
        assert existing_output.read_bytes() == b'kept'
        for item in queue:
            assert item.output.exists()
            assert item.output.with_suffix('.xml').exists()
            assert item.output.with_suffix('.metadata.json').exists()
            assert resume_state_path(item.output).exists() is False


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_finalization_survives_cancel_and_quit_requests(
    authority: FakeAuthority, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Every quit path and cancellation waits for a Move that has requested finalization."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, b'Finalizing recording ' * 2500) as media,
    ):
        source.finalize_gate = asyncio.Event()
        folders = {'0': [_recording('A1', '0', 'Finalizing', media.url)]}
        async with FakeRecorder(folders) as recorder:
            async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, runner):
                async with app.run_test(size=(200, 50)) as pilot:
                    await _wait(pilot.pause, lambda: _table(app).row_count == 1)
                    await pilot.press('enter')
                    await pilot.press('m')
                    await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
                    app.screen.query_one('#confirm-move', Button).press()
                    await _wait(pilot.pause, lambda: runner.active is not None and runner.active.is_committing)
                    item = runner.active
                    assert item is not None
                    for key in ('c', 'q', 'ctrl+q', 'ctrl+c'):
                        await pilot.press(key)
                    await app.action_quit()
                    await pilot.pause(0.2)
                    assert app.is_running is True
                    assert item.task is not None and item.task.cancelled() is False
                    source.finalize_gate.set()
                    await _wait(pilot.pause, lambda: app.is_running is False)
        assert item.status == 'completed'
        assert source.log[-2:] == ['finalize', 'complete']
        assert source.errors == []


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_failed_move_stops_the_queue_until_continued(
    authority: FakeAuthority, source_device: FakeDevice, tmp_path: Path
) -> None:
    """A failure before finalization pauses the queue, explains that no copy was consumed and can continue."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, b'Broken recording ' * 2500) as broken,
        FakeMediaServer(source, b'Next recording ' * 2500) as following,
    ):
        broken.http_errors['HEAD'] = (500, {})
        folders = {'0': [_recording('A1', '0', 'Broken', broken.url), _recording('A2', '0', 'Next', following.url)]}
        async with FakeRecorder(folders) as recorder:
            async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _):
                async with app.run_test(size=(200, 50)) as pilot:
                    await _wait(pilot.pause, lambda: _table(app).row_count == 2)
                    await pilot.press('enter')
                    _highlight(app, 'Broken')
                    await pilot.press('space')
                    _highlight(app, 'Next')
                    await pilot.press('space')
                    await pilot.press('m')
                    await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
                    app.screen.query_one('#confirm-move', Button).press()
                    await _wait(pilot.pause, lambda: isinstance(app.screen, ChoiceScreen))
                    queue = app._queue  # pyright: ignore[reportPrivateUsage]
                    assert [item.status for item in queue] == ['failed', 'waiting']
                    assert 'has not consumed a copy' in ''.join(
                        str(widget.render()) for widget in app.screen.query('.dialog-text')
                    )
                    assert not app.screen.query('#choice-stream')
                    app.screen.query_one('#choice-skip', Button).press()
                    await _wait(pilot.pause, lambda: queue[1].status == 'completed')
                    await pilot.press('q')
        assert source.log.count('finalize') == 1


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
@pytest.mark.parametrize('result', ['saved', 'truncated', 'quality_unknown'])
async def test_save_original_stream_after_move_refusal(
    authority: FakeAuthority, source_device: FakeDevice, tmp_path: Path, result: str
) -> None:
    """Offer the original stream after a refusal, and publish only a complete file."""

    content = b'Original recording bytes, across packets' * 1000
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, content, truncation='boundary' if result == 'truncated' else 'none') as media,
    ):
        source.reject_initiate = True
        resource = dtcp_resource(
            media.url,
            parameters='DLNA.ORG_CI=0' if result != 'quality_unknown' else 'DLNA.ORG_OP=10',
            attributes=f'dlna:cleartextSize="{len(content)}"',
        )
        folders = {'0': [didl_recording('A1', '0', 'Original', resource)]}
        async with FakeRecorder(folders, udn=_UDN) as recorder:
            async with (
                _launch(recorder, tmp_path, tmp_path / 'device') as (app, _),
                app.run_test(size=(160, 42)) as pilot,
            ):
                await _wait(pilot.pause, lambda: _table(app).row_count == 1)
                await pilot.press('enter', 'space', 'm')
                await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
                app.screen.query_one('#confirm-move', Button).press()
                await _wait(pilot.pause, lambda: isinstance(app.screen, ChoiceScreen))
                assert source.log == ['mv_initiate_rejected']
                assert media.count('GET') == 0
                if result == 'quality_unknown':
                    assert not app.screen.query('#choice-stream')
                    return

                button = app.screen.query_one('#choice-stream', Button)
                assert str(button.label) == 'Save in original quality'
                button.press()
                queue: DataTable[str] = app.query_one('#queue', DataTable)
                status = 'Saved via stream' if result == 'saved' else 'Failed'
                await _wait(pilot.pause, lambda: str(queue.get_row_at(0)[0]) == status)
                if result == 'saved':
                    (output,) = tmp_path.glob('*.m2ts')
                    assert output.read_bytes() == content
                else:
                    assert not list(tmp_path.glob('*.m2ts'))
                    assert not list(tmp_path.glob('.*.part'))
                    await _wait(pilot.pause, lambda: isinstance(app.screen, ChoiceScreen))
                    assert not app.screen.query('#choice-stream')

        assert media.count('GET') == 1
        assert all('BLKMove.dtcp.com' not in headers for _, headers in media.requests)
        assert not {'finalize', 'complete', 'cancel'}.intersection(source.log)
        assert source.errors == []


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
@pytest.mark.parametrize('repeat', [False, True])
async def test_stream_fallback_choice_applies_only_to_current_queue(
    authority: FakeAuthority, source_device: FakeDevice, tmp_path: Path, repeat: bool
) -> None:
    """Save the first recording, apply consent to the second refusal only if selected, then start a new queue."""

    content = b'Original recording ' * 2000
    async with FakeSourceAKE(authority, source_device) as source, FakeMediaServer(source, content) as media:
        source.reject_initiate = True
        resource = dtcp_resource(media.url)
        folders = {'0': [didl_recording(f'A{index}', '0', str(index), resource) for index in range(1, 4)]}
        async with (
            FakeRecorder(folders) as recorder,
            _launch(recorder, tmp_path, tmp_path / 'device') as (app, _),
            app.run_test(size=(100, 30)) as pilot,
        ):
            await _wait(pilot.pause, lambda: _table(app).row_count == 3)
            await pilot.press('enter')
            for title in ('1', '2'):
                _highlight(app, title)
                await pilot.press('space')
            await pilot.press('m')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
            app.screen.query_one('#confirm-move', Button).press()
            await _wait(
                pilot.pause, lambda: isinstance(app.screen, ChoiceScreen) and bool(app.screen.query('#repeat-choice'))
            )
            checkbox = app.screen.query_one('#repeat-choice', Checkbox)
            assert checkbox.value is False
            if repeat is True:
                assert await pilot.click('#repeat-choice') is True
            app.screen.query_one('#choice-stream', Button).press()
            if repeat is False:
                await _wait(
                    pilot.pause,
                    lambda: (
                        source.log.count('mv_initiate_rejected') == 2
                        and isinstance(app.screen, ChoiceScreen)
                        and bool(app.screen.query('#choice-stream'))
                    ),
                )
                assert media.count('GET') == 1
                app.screen.query_one('#choice-stream', Button).press()

            queue: DataTable[str] = app.query_one('#queue', DataTable)
            await _wait(pilot.pause, lambda: all(str(queue.get_row_at(i)[0]) == 'Saved via stream' for i in range(2)))
            await pilot.pause()
            assert len(list(tmp_path.glob('*.m2ts'))) == 2
            assert media.count('GET') == 2

            _table(app).focus()
            _highlight(app, '3')
            await pilot.press('space', 'm')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
            app.screen.query_one('#confirm-move', Button).press()
            await _wait(
                pilot.pause, lambda: isinstance(app.screen, ChoiceScreen) and bool(app.screen.query('#choice-stop'))
            )
            assert app.screen.query('#choice-stream')
            assert not app.screen.query('#repeat-choice')
            assert source.log.count('mv_initiate_rejected') == 3
            assert media.count('GET') == 2


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_mobile_only_recording_requires_an_explicit_resource(tmp_path: Path) -> None:
    """A recording without the original version is selectable only after choosing another resource."""

    mobile = dtcp_resource(
        'http://192.0.2.1/mobile',
        parameters='DLNA.ORG_PN=DTCP_AVC_TS_JP_AAC_T;DLNA.ORG_CI=1;DTCP.COM_FLAGS=C0000000',
        attributes='duration="0:30:00.000" resolution="1280x720"',
    )
    folders = {'0': [didl_recording('A1', '0', 'Mobile only', mobile)]}
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 1)
            await pilot.press('enter')
            await pilot.press('space')
            assert app._selection == {}  # pyright: ignore[reportPrivateUsage]
            await pilot.press('v')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ResourceScreen))
            app.screen.dismiss(1)
            await _wait(pilot.pause, lambda: len(app._selection) == 1)  # pyright: ignore[reportPrivateUsage]
            selection = next(iter(app._selection.values()))  # pyright: ignore[reportPrivateUsage]
            assert selection.resource_index == 1
            await pilot.press('m')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
            warnings = str(app.screen.query_one('#confirm-warnings').render())
            assert 'This is not the original recording' in warnings
            assert 'converted resource has not been verified' in warnings
            await pilot.press('escape')


@pytest.mark.anyio
async def test_selection_updates_only_rows_left_by_the_filter(tmp_path: Path) -> None:
    """Selecting all filtered recordings keeps the interface running and selects only the visible ones."""

    folders = {
        '0': [
            # Broadcast listings use full-width letters, while the filter is typed in half-width
            _recording('A1', '0', 'Ｎｅｗｓ at nine', 'http://192.0.2.1/1'),
            _recording('A2', '0', 'Drama', 'http://192.0.2.1/2'),
            _recording('A3', '0', 'News at ten', 'http://192.0.2.1/3'),
        ]
    }
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 3)
            await pilot.press('enter')
            await pilot.press('slash')
            await pilot.press(*'News')
            await pilot.press('enter')
            await _wait(pilot.pause, lambda: _table(app).row_count == 2)
            await pilot.press('a')
            await pilot.pause(0.1)
            assert app.is_running is True
            assert {selection.recording.title for selection in app._selection.values()} == {  # pyright: ignore[reportPrivateUsage]
                'Ｎｅｗｓ at nine',
                'News at ten',
            }


@pytest.mark.anyio
async def test_queued_recording_is_excluded_for_any_destination(tmp_path: Path) -> None:
    """Another reference to a queued recording is excluded even when a different destination is chosen."""

    def parse(object_id: str, folder: str) -> Recording:
        document = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            + didl_recording(object_id, folder, 'Same', dtcp_resource('http://192.0.2.1/media'), reference_id='R1')
            + '</DIDL-Lite>'
        )
        recording = parse_didl(document, next(profile for profile in PROFILES if profile.name == 'generic'))[0]
        assert isinstance(recording, Recording)
        return recording

    async with DLNAClient() as dlna, AsyncExitStack() as stack:
        app = PyDTCPIPApp(
            dlna=dlna,
            runner=TransferRunner(DeviceStore(tmp_path / 'device'), stack),
            interface=None,
            search_port=0,
            directory=tmp_path,
        )
        first = parse('ALL1', 'ALL')
        app._queue.append(  # pyright: ignore[reportPrivateUsage]
            QueueItem(title='Same', output=recording_output_path(tmp_path, _UDN, first), key=(_UDN, 'R1'))
        )
        other = tmp_path / 'other'
        other.mkdir()
        selection = [Selection(_UDN, parse('GENRE1', 'GENRE'))]
        queued = app._confirm_entries(selection, other)  # pyright: ignore[reportPrivateUsage]
        # An interrupted Move of the same recording must be resumed rather than moved again
        app._queue[0].status = 'resume_required'  # pyright: ignore[reportPrivateUsage]
        interrupted = app._confirm_entries(selection, other)  # pyright: ignore[reportPrivateUsage]
    assert queued[0].excluded == 'already queued'
    assert interrupted[0].excluded == 'previous Move needs resumption or checking'


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_forced_exit_during_finalization_completes_the_move(
    authority: FakeAuthority, source_device: FakeDevice, tmp_path: Path
) -> None:
    """Closing the application by any path, including an unexpected error, leaves finalization running to completion."""

    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, b'Interrupted interface ' * 2500) as media,
    ):
        source.finalize_gate = asyncio.Event()
        folders = {'0': [_recording('A1', '0', 'Finalizing', media.url)]}
        async with FakeRecorder(folders) as recorder:
            async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, runner):
                async with app.run_test(size=(200, 50)) as pilot:
                    await _wait(pilot.pause, lambda: _table(app).row_count == 1)
                    await pilot.press('enter')
                    await pilot.press('m')
                    await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
                    app.screen.query_one('#confirm-move', Button).press()
                    await _wait(pilot.pause, lambda: runner.active is not None and runner.active.is_committing)
                    item = runner.active
                    assert item is not None
                    # Bypass the quit guard, as an unhandled error in the interface would
                    app.exit()
                    await _wait(pilot.pause, lambda: app.is_running is False)
                assert item.task is not None and item.task.done() is False
                source.finalize_gate.set()
            # Leaving _launch waits for the transfer that was finalizing
            assert item.task.done() is True and item.task.cancelled() is False
        assert source.log[-2:] == ['finalize', 'complete']
        assert source.errors == []
    assert item.output.exists()
    assert resume_state_path(item.output).exists() is False


@pytest.mark.parametrize(
    ('fixture', 'row', 'details'),
    [
        (
            'toshiba/dbr-z620',
            ['1:00:08', 'AVC / 4.36025 Mbps / 60i', '2.0 GB', '9'],
            {'Copies remaining': '9', 'Move resource': '1: AVC / 4.36025 Mbps / 60i'},
        ),
        (
            'toshiba_regza_tv/43z570l',
            ['0:17:03', 'MPEG-2 / 12.1979 Mbps / 60i', '~1.6 GB', '-'],
            {'Device metadata (network_id)': '7fe6', 'Device metadata (service_id)': '430'},
        ),
        (
            # Every advertised resource is a conversion, so the stored recording must be chosen explicitly
            'panasonic/dmr-brx2060',
            ['0:30:03', 'No original', 'Unknown', '-'],
            {'Reported count (CopyCount)': '10 (includes final Move)'},
        ),
    ],
)
def test_device_fixtures_reach_the_table_and_details(fixture: str, row: list[str], details: dict[str, str]) -> None:
    """Family profiles supply the copy counts, qualities, sizes and device fields shown for real device listings."""

    directory = Path(__file__).parent / 'fixtures' / fixture
    description = parse_device_description(
        'http://192.0.2.1/description.xml', (directory / 'description.xml').read_text(encoding='utf-8')
    )
    recording = parse_didl((directory / 'recording.xml').read_text(encoding='utf-8'), select_profile(description))[0]
    assert isinstance(recording, Recording)
    resource = display.default_move_resource(recording, None)
    assert [
        display.format_duration(resource.duration if resource is not None else recording.duration),
        resource.quality_label if resource is not None else 'No original',
        display.format_size(display.resource_size(resource)),
        str(display.format_copy_count(recording, resource)),
    ] == row
    fields = dict(display.recording_fields(recording, resource))
    assert {name: fields.get(name) for name in details} == details


def test_explicit_panasonic_resource_uses_its_reported_copy_count() -> None:
    """Choosing the first DIGA resource applies the copy count and size the recorder reports for it."""

    directory = Path(__file__).parent / 'fixtures' / 'panasonic' / 'dmr-brx2060'
    description = parse_device_description(
        'http://192.0.2.1/description.xml', (directory / 'description.xml').read_text(encoding='utf-8')
    )
    recording = parse_didl((directory / 'recording.xml').read_text(encoding='utf-8'), select_profile(description))[0]
    assert isinstance(recording, Recording)
    resource = display.default_move_resource(recording, 1)
    assert resource is not None
    assert str(display.format_copy_count(recording, resource)) == '9'
    assert display.format_size(display.resource_size(resource)) == '661.9 MB'


@pytest.mark.anyio
async def test_mouse_opens_folders_sorts_columns_and_scrolls(tmp_path: Path) -> None:
    """Clicking highlights and then opens a row, clicking a heading sorts, and the wheel scrolls the table."""

    many = [_recording(f'B{index:03d}', 'F2', f'Episode {index:03d}', 'http://192.0.2.1/media') for index in range(80)]
    folders = {
        '0': [didl_container('F1', '0', 'Anime', 0), didl_container('F2', '0', 'Drama', len(many))],
        'F2': many,
    }
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 2)
            # Separate clicks are further apart than the double-click interval
            await pilot.click('#devices', offset=(2, 0))
            await pilot.pause(0.6)
            # The header occupies the first line, so the second folder is on the third line
            await pilot.click('#recordings', offset=(6, 2))
            await pilot.pause(0.6)
            assert _rows(app)[_table(app).cursor_row] == 'Drama/'
            # A single click on the highlighted row only moves the cursor, as in htop
            await pilot.click('#recordings', offset=(6, 2))
            await pilot.pause(0.6)
            assert _table(app).row_count == 2
            await pilot.click('#recordings', offset=(6, 2), times=2)
            await _wait(pilot.pause, lambda: _table(app).row_count == 80)
            await pilot.pause(0.6)
            assert app.screen.focused is _table(app)
            assert app.query_one('#browser-pane').has_class('active-pane')
            assert app.query_one('#devices-pane').has_class('active-pane') is False
            await pilot.click('#recordings', offset=(6, 0))
            await pilot.pause(0.1)
            assert _rows(app)[:2] == ['Episode 000', 'Episode 001']
            table = _table(app)
            start = table.cursor_row
            for _ in range(5):
                table.post_message(events.MouseScrollDown(table, 6, 5, 0, 1, 0, False, False, False))
            # As in htop, the wheel moves the highlighted row rather than leaving it out of view
            await _wait(pilot.pause, lambda: table.cursor_row == start + 5)


@pytest.mark.anyio
async def test_clicking_side_panes_keeps_keyboard_navigation(tmp_path: Path) -> None:
    """Clicking the details pane or log keeps the keys moving through the recording table."""

    folders = {
        '0': [_recording(f'A{index}', '0', f'Recording {index}', 'http://192.0.2.1/media') for index in range(3)]
    }
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 3)
            await pilot.press('enter')
            await pilot.press('l')
            for pane in ('#details-pane', '#log'):
                before = _table(app).cursor_row
                await pilot.click(pane, offset=(2, 2))
                await pilot.press('down')
                assert _table(app).cursor_row == before + 1, pane


@pytest.mark.skipif(sys.platform == 'win32', reason='The Linux driver requires termios')
def test_driver_keeps_mouse_coordinates_in_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminals offering in-band resize stay in cell mouse mode, since iTerm2 cannot be recognized over SSH."""

    from textual.drivers.linux_driver import LinuxDriver

    from pydtcpip.tui.driver import CellMouseDriver

    forwarded: list[object] = []

    def record(driver: LinuxDriver, message: object) -> None:
        forwarded.append(message)

    monkeypatch.setattr(LinuxDriver, 'process_message', record)
    driver = object.__new__(CellMouseDriver)
    driver.process_message(InBandWindowResize(True, False))
    assert forwarded == []


@pytest.mark.anyio
async def test_selection_keeps_the_cursor_and_double_click_toggles_once(tmp_path: Path) -> None:
    """Space and a double click toggle only the highlighted recording without moving the cursor."""

    folders = {
        '0': [_recording(f'A{index}', '0', f'Recording {index}', 'http://192.0.2.1/media') for index in range(4)]
    }
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 4)
            await pilot.press('enter')
            _highlight(app, 'Recording 1')
            row = _table(app).cursor_row
            await pilot.press('space')
            assert (_table(app).cursor_row, len(app._selection)) == (row, 1)  # pyright: ignore[reportPrivateUsage]
            await pilot.press('space')
            assert (_table(app).cursor_row, len(app._selection)) == (row, 0)  # pyright: ignore[reportPrivateUsage]
            # The header occupies the first line, so the row at index 2 is on the fourth line
            await pilot.pause(0.6)
            await pilot.click('#recordings', offset=(6, 3), times=2)
            await pilot.pause(0.2)
            selected = [selection.recording.title for selection in app._selection.values()]  # pyright: ignore[reportPrivateUsage]
            assert (_table(app).cursor_row, selected) == (2, [_rows(app)[2]])


@pytest.mark.anyio
async def test_queue_states_appear_in_the_recording_table(tmp_path: Path) -> None:
    """Recordings queued in this session show their transfer state beside the selection mark."""

    folders = {
        '0': [_recording(f'A{index}', '0', f'Recording {index}', 'http://192.0.2.1/media') for index in range(3)]
    }
    async with FakeRecorder(folders, udn=_UDN) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 3)
            await pilot.press('enter')
            for index, status in enumerate(['completed', 'waiting']):
                item = QueueItem(title=f'Recording {index}', output=tmp_path / f'{index}.m2ts', key=(_UDN, f'A{index}'))
                item.status = status  # type: ignore[assignment]
                app._queue.append(item)  # pyright: ignore[reportPrivateUsage]
            await pilot.pause(0.7)
            table = _table(app)
            column = table.get_column_index('mark')
            labels = {
                _rows(app)[row]: str(table.get_cell_at(Coordinate(row, column))) for row in range(table.row_count)
            }
            assert labels == {'Recording 0': 'D', 'Recording 1': 'Q', 'Recording 2': ''}


@pytest.mark.anyio
@pytest.mark.parametrize('key', ['slash', 'katakana_middle_dot', 'fullwidth_solidus'])
async def test_filter_opens_with_kana_mode_slash(tmp_path: Path, key: str) -> None:
    """The filter opens even when a Japanese input method sends a full-width character for the slash key."""

    folders = {'0': [_recording('A1', '0', 'Recording', 'http://192.0.2.1/media')]}
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 1)
            await pilot.press('enter', key)
            assert app.screen.focused is app.query_one('#filter')


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_titles_with_brackets_are_shown_verbatim(tmp_path: Path) -> None:
    """Titles from the recorder are not interpreted as markup in the table, path and dialogs."""

    title = 'AC/DC [/live] [b]Encore'
    folders = {
        '0': [didl_container('F1', '0', '[/folder]', 1)],
        'F1': [_recording('A1', 'F1', title, 'http://192.0.2.1/media')],
    }
    async with FakeRecorder(folders) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 1)
            await pilot.press('enter', 'enter')
            await _wait(
                pilot.pause, lambda: _table(app).row_count == 1 and '[/folder]' in str(app.query_one('#path').render())
            )
            assert title in str(_table(app).get_row_at(0)[1])
            await pilot.press('v')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ResourceScreen))
            assert title in str(app.screen.query_one('.dialog-title').render())
            await pilot.press('escape', 'm')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
            table = app.screen.query_one('#confirm-table', DataTable)
            assert title in str(table.get_row_at(0)[0])
            await pilot.press('escape')
            assert app.is_running is True


@pytest.mark.anyio
async def test_protected_recordings_are_not_moved(tmp_path: Path) -> None:
    """Recordings protected on the recorder cannot be selected and are excluded from the confirmation."""

    protected = didl_recording(
        'A1',
        '0',
        'Protected',
        dtcp_resource('http://192.0.2.1/protected'),
        fields='<upnp:writeStatus>PROTECTED</upnp:writeStatus>',
    )
    folders = {'0': [protected, _recording('A2', '0', 'Normal', 'http://192.0.2.1/normal')]}
    async with FakeRecorder(folders, udn=_UDN) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(200, 50)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 2)
            await pilot.press('enter')
            _highlight(app, 'Protected')
            await pilot.press('space')
            assert app._selection == {}  # pyright: ignore[reportPrivateUsage]
            await pilot.press('a')
            assert [selection.recording.title for selection in app._selection.values()] == ['Normal']  # pyright: ignore[reportPrivateUsage]
            recording = next(item for item in app._objects.values() if item.title == 'Protected')  # pyright: ignore[reportPrivateUsage]
            assert isinstance(recording, Recording)
            entries = app._confirm_entries([Selection(_UDN, recording)], tmp_path)  # pyright: ignore[reportPrivateUsage]
            assert entries[0].excluded == 'protected on the recorder'


@pytest.mark.anyio
async def test_interrupted_move_found_on_disk_blocks_moving_the_recording_again(tmp_path: Path) -> None:
    """An interrupted Move from an earlier session keeps its recording identity, even for another destination."""

    document = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        + didl_recording('ALL1', 'ALL', 'Interrupted', dtcp_resource('http://192.0.2.1/media'), reference_id='R1')
        + '</DIDL-Lite>'
    )
    recording = parse_didl(document, next(profile for profile in PROFILES if profile.name == 'generic'))[0]
    assert isinstance(recording, Recording)
    output = recording_output_path(tmp_path, _UDN, recording)
    MetadataSidecar(
        output,
        RecordingMetadata(
            recording=recording,
            profile='generic',
            profile_unverified=False,
            observed_at=datetime.now(UTC),
            device_location='http://192.0.2.1/description.xml',
            device_description_xml=(
                '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
                '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><friendlyName>Test</friendlyName>'
                f'<manufacturer>Test</manufacturer><modelName>Test</modelName><UDN>{_UDN}</UDN></device></root>'
            ),
        ),
    )
    resume_state_path(output).write_text('{}', encoding='utf-8')
    async with DLNAClient() as dlna, AsyncExitStack() as stack:
        app = PyDTCPIPApp(
            dlna=dlna,
            runner=TransferRunner(DeviceStore(tmp_path / 'device'), stack),
            interface=None,
            search_port=0,
            directory=tmp_path,
        )
        other = tmp_path / 'other'
        other.mkdir()
        async with app.run_test(size=(200, 50)):
            # Starting the interface scans the destination for interrupted Moves
            entries = app._confirm_entries([Selection(_UDN, recording)], other)  # pyright: ignore[reportPrivateUsage]
            assert entries[0].excluded == 'previous Move needs resumption or checking'
            # Stopping the queue before a queued resumption starts keeps it resumable instead of skipped
            item = app._queue[0]  # pyright: ignore[reportPrivateUsage]
            item.is_resume = True
            item.status = 'waiting'
            app._skip_waiting()  # pyright: ignore[reportPrivateUsage]
            assert item.status == 'resume_required'
            entries = app._confirm_entries([Selection(_UDN, recording)], other)  # pyright: ignore[reportPrivateUsage]
            assert entries[0].excluded == 'previous Move needs resumption or checking'


@pytest.mark.anyio
@pytest.mark.usefixtures('credentials')
async def test_editing_the_destination_updates_the_move_button(tmp_path: Path) -> None:
    """Typing a destination without existing files enables Move at once, and the dialog fits an 80-column terminal."""

    folders = {'0': [_recording('A1', '0', 'Existing', 'http://192.0.2.1/media')]}
    async with FakeRecorder(folders, udn=_UDN) as recorder:
        async with _launch(recorder, tmp_path, tmp_path / 'device') as (app, _), app.run_test(size=(80, 40)) as pilot:
            await _wait(pilot.pause, lambda: _table(app).row_count == 1)
            recording = next(iter(app._objects.values()))  # pyright: ignore[reportPrivateUsage]
            assert isinstance(recording, Recording)
            recording_output_path(tmp_path, _UDN, recording).write_bytes(b'kept')
            await pilot.press('enter', 'm')
            await _wait(pilot.pause, lambda: isinstance(app.screen, ConfirmMoveScreen))
            move = app.screen.query_one('#confirm-move', Button)
            assert move.disabled is True
            assert move.region.right <= app.size.width
            empty = tmp_path / 'empty'
            empty.mkdir()
            field = app.screen.query_one('#confirm-directory', Input)
            if sys.platform != 'win32':
                # A partially typed ~user cannot be expanded and must not close the interface
                ## Windows expands any ~user to a directory beside the current home directory instead
                field.value = '~pydtcpip-no-such-user'
                await pilot.pause(0.2)
                assert (app.is_running, move.disabled) == (True, True)
            # An empty destination would silently refer to the current directory
            field.value = '  '
            await pilot.pause(0.2)
            assert move.disabled is True
            field.value = str(empty)
            await pilot.pause(0.2)
            assert move.disabled is False


@pytest.mark.anyio
async def test_copy_count_listed_before_a_completed_move_is_treated_as_unknown(tmp_path: Path) -> None:
    """Moving the same recording again before the listing refreshes warns that the source may be deleted."""

    document = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" xmlns:regza="urn:schemas-toshiba-co-jp:regzadata-1-0/">'
        + _recording('A1', '0', 'Copies', 'http://192.0.2.1/media', copies=2)
        + '</DIDL-Lite>'
    )
    recording = parse_didl(document, next(profile for profile in PROFILES if profile.name == 'toshiba'))[0]
    assert isinstance(recording, Recording)
    assert recording.protection.remaining_copy_count.value == 1
    async with DLNAClient() as dlna, AsyncExitStack() as stack:
        app = PyDTCPIPApp(
            dlna=dlna,
            runner=TransferRunner(DeviceStore(tmp_path / 'device'), stack),
            interface=None,
            search_port=0,
            directory=tmp_path,
        )
        before = app._confirm_entries([Selection(_UDN, recording)], tmp_path)  # pyright: ignore[reportPrivateUsage]
        moved = QueueItem(title='Copies', output=tmp_path / 'first.m2ts', key=(_UDN, 'A1'))
        moved.finish(None, has_resume_state=False)
        app._queue.append(moved)  # pyright: ignore[reportPrivateUsage]
        other = tmp_path / 'other'
        other.mkdir()
        after = app._confirm_entries([Selection(_UDN, recording)], other)  # pyright: ignore[reportPrivateUsage]
        # A listing fetched after the Move can still carry the old count until a re-query sees it change
        relisted = parse_didl(document, next(profile for profile in PROFILES if profile.name == 'toshiba'))[0]
        assert isinstance(relisted, Recording)
        unconfirmed = app._confirm_entries([Selection(_UDN, relisted)], other)  # pyright: ignore[reportPrivateUsage]
        moved.copies_confirmed_at = datetime.now(UTC)
        confirmed = parse_didl(document, next(profile for profile in PROFILES if profile.name == 'toshiba'))[0]
        assert isinstance(confirmed, Recording)
        fresh = app._confirm_entries([Selection(_UDN, confirmed)], other)  # pyright: ignore[reportPrivateUsage]
    assert [label for label, _, _ in before[0].warnings] == []
    assert [label for label, _, _ in after[0].warnings] == ['Copies unknown']
    assert str(after[0].copies) == '?'
    assert [label for label, _, _ in unconfirmed[0].warnings] == ['Copies unknown']
    assert [label for label, _, _ in fresh[0].warnings] == []
