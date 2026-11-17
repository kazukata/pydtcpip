"""Terminal interface for browsing recorders, inspecting recordings and moving several recordings in sequence."""

import asyncio
import logging
import sys
import unicodedata
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.driver import Driver
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from pydtcpip.client import DTCPClient
from pydtcpip.dlna import ssdp
from pydtcpip.dlna.client import DLNAClient, MediaServer
from pydtcpip.dlna.description import parse_device_description
from pydtcpip.errors import PyDTCPIPError
from pydtcpip.events import TransferEvent
from pydtcpip.models import Container, ProtectionValue, Recording, RecordingMetadata, Resource
from pydtcpip.operations import (
    collect_move_metadata,
    existing_move_outputs,
    is_broadcast,
    is_protected,
    load_resume_sidecar,
    load_resume_state,
    query_copy_count,
    recording_output_path,
    resume_state_path,
    run_move,
    run_resume,
    save_stream,
)
from pydtcpip.store import DeviceStore
from pydtcpip.tui import display
from pydtcpip.tui.screens import (
    AddDeviceScreen,
    ButtonVariant,
    ChoiceScreen,
    ConfirmEntry,
    ConfirmMoveScreen,
    ResourceScreen,
)
from pydtcpip.tui.state import STATUS_LABELS, QueueItem


_LOGGER = logging.getLogger(__name__)

_RESUME_SUFFIX = '.pydtcpip-resume.json'
# Delay before fetching optional metadata for the highlighted recording, so scrolling does not query each row
_METADATA_DELAY = 0.4
# Interval in seconds and number of retries for checking copy counts after Moves (DBR-Z620 takes about 80 seconds)
_REQUERY_INTERVAL = 10.0
_REQUERY_ATTEMPTS = 12
_SORT_ORDERS = ('date', 'title', 'size', 'copies')
# Placeholder shown while a copy count is being checked again
_CHECKING = 'Checking...'
_FIXED_COLUMNS = {
    'mark': 1,
    'date': 16,
    'duration': 7,
    'quality': 18,
    'size': 9,
    'copies': 6,
    'channel': 12,
}
# Widths of the side panes; the recording table takes the rest
_DEVICES_WIDTH = 26
_DETAILS_WIDTH = 50

# Light blue accents on neutral dark backgrounds, instead of the default navy and blue
_THEME = Theme(
    name='pydtcpip',
    primary='#4FC3F7',
    secondary='#26A69A',
    accent='#80DEEA',
    warning='#FFB74D',
    error='#EF5350',
    success='#81C784',
    foreground='#E0E0E0',
    background='#141414',
    surface='#1C1C1C',
    panel='#262626',
    dark=True,
    # Keep focused button labels in their contrasting color instead of reversing them into a box
    variables={'button-focus-text-style': 'bold underline'},
)

type RecordingKey = tuple[str, str]


def _normalize_for_search(text: str) -> str:
    """
    Normalize text for case- and width-insensitive matching.

    Args:
        text (str): Text to normalize

    Returns:
        str: NFKC-normalized, case-folded text
    """

    return unicodedata.normalize('NFKC', text).casefold()


@dataclass
class Selection:
    """A recording selected for Move and the resource chosen for it."""

    udn: str
    recording: Recording
    # One-based resource chosen by the user, or None for the original recording
    resource_index: int | None = None


@dataclass
class DeviceView:
    """Browsing position on one recorder."""

    # Folder IDs and titles from the root to the open folder
    path: list[tuple[str, str]]
    # Highlighted row key in each folder, restored when returning to it
    cursors: dict[str, str] = field(default_factory=dict[str, str])


class _CursorTable(DataTable[Text | str]):
    """
    Data table that behaves like htop with the mouse: the wheel moves the cursor so the highlighted row stays in view,
    a click only moves the cursor, and a double click opens or selects the row like Enter.
    """

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        is_row = isinstance(meta.get('row'), int) and meta['row'] >= 0
        if is_row is False:
            # Textual runs the base handler after this one, which handles heading clicks
            return
        if event.chain >= 2:
            # The first click highlighted the row, so the double click acts like Enter once
            self.action_select_cursor()
            event.prevent_default()
            event.stop()
            return
        self.cursor_coordinate = Coordinate(meta['row'], max(0, meta.get('column', 0)))
        self._scroll_cursor_into_view(animate=True)
        # Textual also runs the base class handlers, which would treat this as a click on the highlighted row
        event.prevent_default()
        event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_down(event)
            return
        self.action_cursor_down()
        # Skip the base handler, which would scroll the view away from the cursor
        event.prevent_default()
        event.stop()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_up(event)
            return
        self.action_cursor_up()
        event.prevent_default()
        event.stop()


class TransferRunner:
    """
    Own the DTCP-IP client and the active transfer task outside the Textual application.
    Textual cancels its workers when it exits, including after an unexpected error, so transfers run as separate
    tasks that the launcher can wait for until finalization ends.
    """

    def __init__(self, store: DeviceStore, stack: AsyncExitStack) -> None:
        """
        Configure credential loading and the context that owns the client.

        Args:
            store (DeviceStore): Credential and SRM storage
            stack (AsyncExitStack): Context that closes the client after the application and transfers end
        """

        self.store = store
        self._stack = stack
        self._client: DTCPClient | None = None
        self.active: QueueItem | None = None

    async def client(self) -> DTCPClient:
        """
        Return the DTCP-IP client, loading credentials on first use.

        Returns:
            DTCPClient: Client shared by queued transfers
        """

        if self._client is None:
            credentials = self.store.load_credentials()
            self._client = await self._stack.enter_async_context(
                DTCPClient(credentials, srm=self.store.load_srm(), on_srm_update=self.store.save_srm)
            )
        return self._client

    async def settle(self) -> None:
        """Wait for a committing transfer to finish, or cancel one that has not requested finalization."""

        item = self.active
        if item is None or item.task is None or item.task.done() is True:
            return
        if item.is_committing is False:
            item.is_cancelled_by_user = True
            item.task.cancel()
        await asyncio.wait([item.task])


class _LogHandler(logging.Handler):
    """Write library logs to the application's log panel instead of the terminal."""

    def __init__(self, app: 'PyDTCPIPApp') -> None:
        super().__init__(level=logging.INFO)
        self._app = app
        self.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%H:%M:%S'))

    def emit(self, record: logging.LogRecord) -> None:
        self._app.write_log(self.format(record))


class PyDTCPIPApp(App[None]):
    """Browse recorders in three panes and move selected recordings one at a time."""

    TITLE = 'pydtcpip'
    CSS = """
    #main { height: 1fr; }
    #devices-pane { width: 26; border-right: solid $panel-lighten-2; }
    #browser-pane { width: 1fr; }
    #details-pane { width: 50; border-left: solid $panel-lighten-2; padding: 0 1; }
    .pane-title { width: 100%; background: $boost; padding: 0 1; text-style: bold; }
    .active-pane .pane-title { background: $primary; color: $background; }
    #devices { height: 1fr; border: none; }
    #recordings { height: 1fr; }
    #filter { display: none; }
    #queue { height: 9; display: none; border-top: solid $panel-lighten-2; }
    #log { height: 10; display: none; border-top: solid $panel-lighten-2; }
    #status { height: 1; background: $panel; padding: 0 1; }
    ModalScreen { align: center middle; }
    .dialog { width: 84; max-width: 100%; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    .dialog.wide { width: 150; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-text { margin-bottom: 1; }
    .dialog-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .dialog-buttons Button { margin-left: 2; }
    #confirm-table { height: auto; max-height: 14; }
    #resource-list { height: auto; max-height: 20; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('q', 'quit', 'Quit'),
        Binding('ctrl+c', 'quit', 'Quit', show=False, priority=True),
        Binding('space', 'toggle_selection', 'Select'),
        Binding('a', 'select_all', 'Select all'),
        Binding('backspace', 'parent', 'Up'),
        Binding('m', 'move', 'Move'),
        Binding('v', 'choose_resource', 'Quality'),
        Binding('slash', 'filter', 'Filter'),
        # Japanese input methods send these for the slash key in kana mode
        Binding('katakana_middle_dot,fullwidth_solidus', 'filter', 'Filter', show=False),
        Binding('s', 'sort', 'Sort'),
        Binding('c', 'cancel_transfer', 'Cancel'),
        Binding('i', 'toggle_raw', 'XML', show=False),
        Binding('r', 'reload', 'Reload', show=False),
        Binding('l', 'toggle_log', 'Log'),
        Binding('plus', 'add_device', 'Add recorder'),
        Binding('f5', 'discover', 'Rediscover', show=False),
    ]

    def __init__(
        self,
        *,
        dlna: DLNAClient,
        runner: TransferRunner,
        interface: str | None,
        search_port: int,
        directory: Path,
    ) -> None:
        """
        Configure device access, transfers and the default destination.

        Args:
            dlna (DLNAClient): Client for discovery and recording listings
            runner (TransferRunner): Owner of the DTCP-IP client and active transfer
            interface (str | None): Local IPv4 address used for discovery
            search_port (int): UDP port for discovery responses
            directory (Path): Default destination directory
        """

        driver_class: type[Driver] | None = None
        if sys.platform != 'win32':
            # The Windows driver has no pixel mouse mode, and the Linux driver cannot be imported there
            from pydtcpip.tui.driver import CellMouseDriver

            driver_class = CellMouseDriver
        super().__init__(driver_class=driver_class)
        self.register_theme(_THEME)
        self.theme = _THEME.name
        self._dlna = dlna
        self._runner = runner
        self._discovery_lock = asyncio.Lock()
        self._interface = interface
        self._search_port = search_port
        self._directory = directory
        self._servers: dict[str, MediaServer] = {}
        self._views: dict[str, DeviceView] = {}
        self._current_udn: str | None = None
        self._listings: dict[tuple[str, str], list[Container | Recording]] = {}
        self._loading: set[tuple[str, str]] = set()
        self._objects: dict[str, Container | Recording] = {}
        self._metadata: dict[tuple[str, str], RecordingMetadata] = {}
        self._metadata_errors: dict[tuple[str, str], str] = {}
        self._metadata_timer: Timer | None = None
        self._selection: dict[RecordingKey, Selection] = {}
        self._queue: list[QueueItem] = []
        self._queue_keys: dict[str, QueueItem] = {}
        # Latest queue entry for each recording, shown in the recording table
        self._transfers: dict[RecordingKey, QueueItem] = {}
        self._transfer_statuses: dict[RecordingKey, str] = {}
        self._is_queue_running = False
        self._is_exit_requested = False
        self._filter = ''
        self._sort_order = 'date'
        self._shows_raw = False
        self._title_width = 40
        self._log_handler = _LogHandler(self)

    def compose(self) -> ComposeResult:
        with Horizontal(id='main'):
            with Vertical(id='devices-pane'):
                yield Label('Recorders', classes='pane-title')
                yield OptionList(id='devices')
            with Vertical(id='browser-pane'):
                yield Label('Select a recorder', id='path', classes='pane-title')
                yield Input(placeholder='Filter titles in this folder', id='filter')
                yield _CursorTable(id='recordings', cursor_type='row', zebra_stripes=True)
            # The details pane and log scroll with the wheel but never take focus from the lists
            with VerticalScroll(id='details-pane', can_focus=False):
                yield Static(id='details')
        yield _CursorTable(id='queue', cursor_type='row')
        log = RichLog(id='log', wrap=True, max_lines=2000)
        log.can_focus = False
        yield log
        yield Static(id='status')
        yield Footer()

    def on_mount(self) -> None:
        # Keep library logs inside the interface; writing to the terminal would corrupt the screen
        root = logging.getLogger()
        root.addHandler(self._log_handler)
        logging.getLogger('pydtcpip').setLevel(logging.INFO)
        queue: DataTable[Text | str] = self.query_one('#queue', DataTable)
        queue.add_column('Status', width=34, key='status')
        queue.add_column('Title', width=44, key='title')
        queue.add_column('Progress', width=30, key='progress')
        queue.add_column('Copies on source', width=34, key='copies')
        queue.add_column('Saved as', key='output')
        self._setup_columns(self.size.width)
        self._scan_resume_states()
        self._update_status()
        self.set_interval(0.5, self._refresh_queue)
        self.watch(self.screen, 'focused', self._highlight_pane)
        self.query_one('#devices', OptionList).focus()
        self.discover()

    def _highlight_pane(self, focused: Widget | None) -> None:
        """
        Highlight the title of the pane that receives keys.

        Args:
            focused (Widget | None): Newly focused widget
        """

        for pane in self.query('#devices-pane, #browser-pane'):
            pane.set_class(focused is not None and pane in focused.ancestors_with_self, 'active-pane')

    def on_unmount(self) -> None:
        logging.getLogger().removeHandler(self._log_handler)

    def outputs_requiring_resume(self) -> list[Path]:
        """
        List queued Moves whose resume state remains, including one that ended while the application closed.

        Returns:
            list[Path]: Outputs to resume with `pydtcpip resume`
        """

        return [item.output for item in self._queue if resume_state_path(item.output).exists()]

    def write_log(self, message: str) -> None:
        """
        Append a line to the log panel.

        Args:
            message (str): Formatted log record
        """

        if self.is_running is False:
            return
        self.query_one('#log', RichLog).write(Text(message))

    # Devices

    @work(group='discovery', exclusive=True, exit_on_error=False)
    async def discover(self) -> None:
        """Start discovery once the previous worker has released its sockets."""

        async with self._discovery_lock:
            await self._discover_recorders()

    async def _discover_recorders(self) -> None:
        """List search results first, then add recorders that announce themselves later."""

        self.notify('Searching for recorders...')
        announced: list[MediaServer] = []
        is_initial_search_done = False

        def on_server(server: MediaServer) -> None:
            """
            Keep active-search results first in the list even if the announcement arrived earlier.

            Args:
                server (MediaServer): Server whose device description was retrieved
            """

            if is_initial_search_done:
                self._add_server(server)
            else:
                announced.append(server)

        # Allow a little more than one 30-second announcement interval in the background
        async with asyncio.TaskGroup() as tasks:
            listener = None
            if self._search_port != ssdp.SSDP_PORT:
                listener = tasks.create_task(
                    self._dlna.listen_for_servers(on_server=on_server, timeout=35.0, interface=self._interface)
                )
            try:
                servers = await self._dlna.discover(
                    timeout=3.0, interface=self._interface, port=self._search_port, include_announcements=False
                )
            except (OSError, PyDTCPIPError) as ex:
                if listener is not None:
                    listener.cancel()
                _LOGGER.error(f'Discovery failed: {ex}')
                self.notify(f'Discovery failed: {ex}', severity='error', markup=False)
                return
            for server in [*servers, *announced]:
                self._add_server(server)
            is_initial_search_done = True
            if len(self._servers) == 0:
                self.notify('Listening for recorder announcements. Press + to enter a device description URL.')
            # If the user chose UDP 1900 for replies, listen once the active search has released it
            if listener is None:
                await self._dlna.listen_for_servers(on_server=on_server, timeout=35.0, interface=self._interface)
        if len(self._servers) == 0:
            self.notify(
                f'No recorders found. Allow incoming UDP traffic on port {self._search_port}, '
                'or press + to enter a device description URL.',
                severity='warning',
                timeout=15,
            )

    def _add_server(self, server: MediaServer) -> None:
        """
        Add a recorder to the device list, ignoring one that is already listed.

        Args:
            server (MediaServer): Discovered or entered recorder
        """

        udn = server.description.udn
        if udn in self._servers:
            return
        self._servers[udn] = server
        label = Text(server.description.friendly_name, style='bold')
        label.append(f'\n{server.description.model_name}  {server.profile.name}', style='dim')
        if server.profile.is_unverified is True:
            label.append(' (unverified)', style='yellow')
        devices = self.query_one('#devices', OptionList)
        devices.add_option(Option(label, id=udn))
        if self._current_udn is None:
            devices.highlighted = 0
            self._open_device(udn)

    def action_discover(self) -> None:
        """Discover recorders again."""

        self.discover()

    def action_add_device(self) -> None:
        """Ask for a device description URL and add that recorder."""

        def add(url: str | None) -> None:
            if url is not None:
                self._connect_device(url)

        self.push_screen(AddDeviceScreen(), add)

    @work(group='connect', exit_on_error=False)
    async def _connect_device(self, url: str) -> None:
        """
        Connect to a recorder by its device description URL.

        Args:
            url (str): Device description URL
        """

        try:
            server = await self._dlna.get_server(url)
        except (OSError, TimeoutError, PyDTCPIPError) as ex:
            _LOGGER.error(f'Could not connect to {url}: {ex}')
            self.notify(f'Could not connect to {url}: {ex}', severity='error', markup=False)
            return
        self._add_server(server)
        self.notify(f'Added {server.description.friendly_name}.', markup=False)

    @on(OptionList.OptionHighlighted, '#devices')
    def _device_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        server = self._servers.get(event.option.id or '')
        if server is not None and self.focused is event.option_list:
            self._show_fields(server.description.friendly_name, display.device_sections(server))

    @on(OptionList.OptionSelected, '#devices')
    def _device_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._open_device(event.option.id)
            self.query_one('#recordings', DataTable).focus()

    def _open_device(self, udn: str) -> None:
        """
        Show the open folder of a recorder, starting at its root.

        Args:
            udn (str): Recorder UDN
        """

        self._current_udn = udn
        server = self._servers[udn]
        self._views.setdefault(udn, DeviceView([('0', server.description.friendly_name)]))
        self._show_folder()

    # Folders and recordings

    def _location(self) -> tuple[str, str] | None:
        """
        Return the open recorder and folder.

        Returns:
            tuple[str, str] | None: Recorder UDN and folder ID, or None when no recorder is open
        """

        if self._current_udn is None:
            return None
        return self._current_udn, self._views[self._current_udn].path[-1][0]

    def _show_folder(self) -> None:
        """Show the open folder from the cache or start retrieving it."""

        location = self._location()
        if location is None:
            return
        listing = self._listings.get(location)
        self._objects = {}
        self._rebuild_table(listing or [], restores_position=True)
        if listing is None and location not in self._loading:
            self._browse(*location)

    @work(group='listing', exclusive=True, exit_on_error=False)
    async def _browse(self, udn: str, container_id: str) -> None:
        """
        Retrieve a folder page by page and show entries as they arrive.

        Args:
            udn (str): Recorder UDN
            container_id (str): Folder ID
        """

        server = self._servers[udn]
        location = (udn, container_id)
        objects: list[Container | Recording] = []
        self._loading.add(location)
        try:
            while True:
                result = await server.browse(container_id, start=len(objects), count=100)
                objects.extend(result.objects)
                if self._location() == location:
                    self._rebuild_table(objects, total=result.total_matches)
                # Devices may return fewer entries than requested, so advance by the actual count
                if result.number_returned == 0 or len(objects) >= result.total_matches:
                    break
        except (OSError, TimeoutError, PyDTCPIPError) as ex:
            _LOGGER.error(f'Could not list folder {container_id} on {server.description.friendly_name}: {ex}')
            self.notify(f'Could not list this folder: {ex}', severity='error', markup=False)
            return
        finally:
            self._loading.discard(location)
        self._listings[location] = objects
        if self._location() == location:
            self._rebuild_table(objects)

    def _setup_columns(self, width: int) -> None:
        """
        Create the recording table columns, giving the title the remaining width.

        Args:
            width (int): Terminal width in cells
        """

        table: DataTable[Text | str] = self.query_one('#recordings', DataTable)
        # Subtract the side panes, their borders and the table's scroll bar
        available = width - _DEVICES_WIDTH - _DETAILS_WIDTH - 4
        self._title_width = max(20, available - sum(_FIXED_COLUMNS.values()) - 2 * (len(_FIXED_COLUMNS) + 1))
        table.clear(columns=True)
        table.add_column('', width=_FIXED_COLUMNS['mark'], key='mark')
        table.add_column('Title', width=self._title_width, key='title')
        table.add_column('Date', width=_FIXED_COLUMNS['date'], key='date')
        table.add_column('Length', width=_FIXED_COLUMNS['duration'], key='duration')
        table.add_column('Quality', width=_FIXED_COLUMNS['quality'], key='quality')
        table.add_column('Size', width=_FIXED_COLUMNS['size'], key='size')
        table.add_column('Copies', width=_FIXED_COLUMNS['copies'], key='copies')
        table.add_column('Channel', width=_FIXED_COLUMNS['channel'], key='channel')

    def on_resize(self, event: events.Resize) -> None:
        # Use the new size carried by the event
        self._setup_columns(event.size.width)
        location = self._location()
        if location is not None:
            self._rebuild_table(self._listings.get(location, list(self._objects.values())))

    def _selection_for(self, recording: Recording) -> Selection | None:
        """
        Return the selection of a recording, matching references to the same recording in other folders.

        Args:
            recording (Recording): Listed recording

        Returns:
            Selection | None: Selection, or None when the recording is not selected
        """

        if self._current_udn is None:
            return None
        return self._selection.get((self._current_udn, recording.reference_id or recording.id))

    def _move_resource(self, recording: Recording) -> Resource | None:
        """
        Return the resource a Move of a listed recording would transfer.

        Args:
            recording (Recording): Listed recording

        Returns:
            Resource | None: Selected or default resource
        """

        selection = self._selection_for(recording)
        return display.default_move_resource(recording, selection.resource_index if selection is not None else None)

    def _matches_filter(self, title: str) -> bool:
        """
        Match a title against the filter, treating full-width and half-width characters alike.

        Args:
            title (str): Recording or folder title

        Returns:
            bool: True when the filter is empty or occurs in the title
        """

        # Broadcast titles often use full-width letters and digits, which users type in half-width
        return _normalize_for_search(self._filter) in _normalize_for_search(title)

    def _sort_key(self, item: Container | Recording) -> tuple[int, float | str]:
        """
        Order folders first, then recordings by the chosen order.

        Args:
            item (Container | Recording): Listed entry

        Returns:
            tuple[int, float | str]: Sort key
        """

        if isinstance(item, Container):
            return 0, item.title.casefold()
        if self._sort_order == 'title':
            return 1, item.title.casefold()
        if self._sort_order == 'size':
            size = display.resource_size(self._move_resource(item)).value
            return 1, -(size if size is not None else -1)
        if self._sort_order == 'copies':
            count = display.copy_count(item, self._move_resource(item))
            return 1, count.value if count.status == 'known' and count.value is not None else 1_000_000
        # Newest first; ISO 8601 dates sort chronologically as text
        return 1, ''.join(chr(0x10FFFF - ord(character)) for character in (item.date or ''))

    def _transfer_label(self, recording: Recording) -> Text:
        """
        Describe the queue state of a recording queued during this session.

        Args:
            recording (Recording): Listed recording

        Returns:
            Text: One-letter state, or empty text when the recording has not been queued
        """

        if self._current_udn is None:
            return Text('')
        item = self._transfers.get((self._current_udn, recording.reference_id or recording.id))
        if item is None:
            return Text('')
        if item.status == 'waiting':
            return Text('Q', style='cyan')
        if item.is_active is True:
            return Text('M', style='bold yellow')
        if item.status in ('completed', 'stream_saved'):
            return Text('D', style='green')
        if item.status in ('resume_required', 'finalize_rejected'):
            return Text('R' if item.status == 'resume_required' else 'C', style='bold red')
        if item.status in ('failed', 'initiation_rejected'):
            return Text('F', style='red')
        return Text('S', style='dim')

    def _row(self, item: Container | Recording) -> list[Text | str]:
        """
        Build the cells of a table row.

        Args:
            item (Container | Recording): Listed entry

        Returns:
            list[Text | str]: Cells in column order
        """

        if isinstance(item, Container):
            count = f'{item.child_count} items' if item.child_count is not None else ''
            return ['', Text(f'{item.title}/', style='bold cyan'), '', '', count, '', '', '']
        resource = self._move_resource(item)
        selection = self._selection_for(item)
        # Queued recordings leave the selection, so the column shows the transfer state unless selected again
        mark = self._transfer_label(item)
        if selection is not None:
            mark = Text('\u25a0', style='bold yellow' if selection.resource_index is not None else 'bold green')
        quality = Text('No original', style='yellow') if resource is None else Text(resource.quality_label)
        if resource is not None and resource.is_original_quality is not True:
            quality.stylize('yellow')
        title = Text(item.title, style='dim' if is_broadcast(item) else '')
        return [
            mark,
            title,
            display.format_date(item.date),
            display.format_duration(resource.duration if resource is not None else item.duration),
            quality,
            display.format_size(display.resource_size(resource)),
            display.format_copy_count(item, resource),
            Text(item.channel_name or ''),
        ]

    def _rebuild_table(
        self, objects: list[Container | Recording], *, total: int | None = None, restores_position: bool = False
    ) -> None:
        """
        Show folder entries after filtering and sorting, keeping the highlighted entry.

        Args:
            objects (list[Container | Recording]): Entries retrieved so far
            total (int | None): Total entries reported while retrieving, or None when complete
            restores_position (bool): Highlight the entry last highlighted in this folder instead of the current row
        """

        table: DataTable[Text | str] = self.query_one('#recordings', DataTable)
        location = self._location()
        if location is None:
            return
        view = self._views[location[0]]
        # The table still shows the previous folder when switching, so use the position saved for this folder
        highlighted = view.cursors.get(location[1]) if restores_position is True else None
        highlighted = highlighted or self._highlighted_key() or view.cursors.get(location[1])
        self._objects = {}
        for item in objects:
            self._objects.setdefault(item.id, item)
        visible = [item for item in self._objects.values() if self._matches_filter(item.title)]
        visible.sort(key=self._sort_key)
        table.clear()
        for item in visible:
            table.add_row(*self._row(item), key=item.id)
        if highlighted is not None and highlighted in {item.id for item in visible}:
            table.move_cursor(row=table.get_row_index(highlighted), scroll=True)
        path = ' / '.join(title for _, title in view.path)
        state = f'  loading {len(objects)} of {total}...' if total is not None and len(objects) < total else ''
        shown = f'  {len(visible)} of {len(self._objects)}' if self._filter else f'  {len(self._objects)} entries'
        self.query_one('#path', Label).update(Text(f'{path}{shown}{state}'))
        self._update_status()

    def _highlighted_key(self) -> str | None:
        """
        Return the key of the highlighted row in the recording table.

        Returns:
            str | None: Object ID, or None when the table is empty
        """

        table: DataTable[Text | str] = self.query_one('#recordings', DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def _highlighted(self) -> Container | Recording | None:
        """
        Return the highlighted entry.

        Returns:
            Container | Recording | None: Entry, or None when the table is empty
        """

        key = self._highlighted_key()
        return self._objects.get(key) if key is not None else None

    @on(DataTable.RowHighlighted, '#recordings')
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        location = self._location()
        item = self._objects.get(event.row_key.value or '')
        if location is None or item is None:
            return
        self._views[location[0]].cursors[location[1]] = item.id
        self._show_details(item)
        if self._metadata_timer is not None:
            self._metadata_timer.stop()
        if isinstance(item, Recording) and (location[0], item.id) not in self._metadata:
            udn = location[0]
            self._metadata_timer = self.set_timer(_METADATA_DELAY, lambda: self._fetch_metadata(udn, item))

    @on(DataTable.RowSelected, '#recordings')
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        item = self._objects.get(event.row_key.value or '')
        location = self._location()
        if isinstance(item, Container) and location is not None:
            self._views[location[0]].path.append((item.id, item.title))
            self._show_folder()
        elif isinstance(item, Recording):
            self.action_toggle_selection()

    def action_parent(self) -> None:
        """Return to the parent folder."""

        location = self._location()
        if location is None:
            return
        view = self._views[location[0]]
        if len(view.path) > 1:
            view.path.pop()
            self._show_folder()

    def action_reload(self) -> None:
        """Retrieve the open folder again and discard cached metadata for this recorder."""

        location = self._location()
        if location is None:
            return
        self._listings.pop(location, None)
        self._metadata = {key: value for key, value in self._metadata.items() if key[0] != location[0]}
        self._metadata_errors.clear()
        self._show_folder()

    def action_filter(self) -> None:
        """Show the title filter."""

        field = self.query_one('#filter', Input)
        field.display = True
        field.focus()

    @on(Input.Changed, '#filter')
    def _filter_changed(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        location = self._location()
        if location is not None:
            self._rebuild_table(self._listings.get(location, list(self._objects.values())))

    @on(Input.Submitted, '#filter')
    def _filter_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == '':
            event.input.display = False
        self.query_one('#recordings', DataTable).focus()

    def action_sort(self) -> None:
        """Switch to the next sort order."""

        self._sort_by(_SORT_ORDERS[(_SORT_ORDERS.index(self._sort_order) + 1) % len(_SORT_ORDERS)])

    @on(DataTable.HeaderSelected, '#recordings')
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        # Clicking a column heading sorts by that column, as in htop
        if event.column_key.value in _SORT_ORDERS:
            self._sort_by(event.column_key.value)

    def _sort_by(self, order: str) -> None:
        """
        Sort the open folder by a column.

        Args:
            order (str): One of date, title, size or copies
        """

        self._sort_order = order
        self.notify(f'Sorted by {order}.')
        location = self._location()
        if location is not None:
            self._rebuild_table(self._listings.get(location, list(self._objects.values())))

    # Details

    @work(group='metadata', exclusive=True, exit_on_error=False)
    async def _fetch_metadata(self, udn: str, recording: Recording) -> None:
        """
        Collect optional metadata for the highlighted recording and cache it.

        Args:
            udn (str): Recorder UDN
            recording (Recording): Highlighted recording
        """

        key = (udn, recording.id)
        try:
            self._metadata[key] = await self._servers[udn].collect_metadata(recording)
        except (OSError, TimeoutError, PyDTCPIPError) as ex:
            self._metadata_errors[key] = str(ex)
        highlighted = self._highlighted()
        if self._current_udn == udn and highlighted is not None and highlighted.id == recording.id:
            self._show_details(highlighted)

    def action_toggle_raw(self) -> None:
        """Show or hide the original XML in the details pane."""

        self._shows_raw = not self._shows_raw
        item = self._highlighted()
        if item is not None:
            self._show_details(item)

    def _show_details(self, item: Container | Recording) -> None:
        """
        Show a folder or recording in the details pane.

        Args:
            item (Container | Recording): Highlighted entry
        """

        if isinstance(item, Container):
            fields = [('Title', item.title), ('ID', item.id)]
            if item.child_count is not None:
                fields.append(('Entries', str(item.child_count)))
            self._show_fields(item.title, [('Folder', fields)])
            return
        resource = self._move_resource(item)
        sections: list[tuple[str, list[tuple[str, str]]]] = [
            ('Recording', display.recording_fields(item, resource)),
            ('Qualities (press v to choose)', display.resource_fields(item, resource)),
        ]
        key = (self._current_udn or '', item.id)
        if key in self._metadata:
            sections.append(('Additional metadata', display.metadata_fields(self._metadata[key])))
        elif key in self._metadata_errors:
            sections.append(('Additional metadata', [('Error', self._metadata_errors[key])]))
        else:
            sections.append(('Additional metadata', [('Status', 'Loading...')]))
        extra: list[RenderableType] = []
        if self._shows_raw is True:
            extra = [Text('\nOriginal XML', style='bold underline'), Text(item.raw_xml, style='dim')]
        self._show_fields(item.title, sections, extra)

    def _show_fields(
        self,
        title: str,
        sections: list[tuple[str, list[tuple[str, str]]]],
        extra: list[RenderableType] | None = None,
    ) -> None:
        """
        Render titled sections of fields in the details pane.

        Args:
            title (str): Heading
            sections (list[tuple[str, list[tuple[str, str]]]]): Section names and their fields
            extra (list[RenderableType] | None): Additional content after the sections
        """

        parts: list[RenderableType] = [Text(title, style='bold')]
        for name, fields in sections:
            grid = Table.grid(padding=(0, 1))
            # Wrap long field names within a narrow column instead of truncating them
            grid.add_column(style='dim', max_width=16, overflow='fold')
            grid.add_column(overflow='fold')
            for label, value in fields:
                grid.add_row(label, Text(value))
            parts.extend([Text(f'\n{name}', style='bold underline'), grid])
        parts.extend(extra or [])
        self.query_one('#details', Static).update(Group(*parts))

    # Selection

    def action_toggle_selection(self) -> None:
        """Select or deselect the highlighted recording."""

        item = self._highlighted()
        if not isinstance(item, Recording) or self._current_udn is None:
            return
        udn = self._current_udn
        key = (udn, item.reference_id or item.id)
        if key in self._selection:
            del self._selection[key]
        else:
            if is_broadcast(item) is True:
                self.notify('Live channels cannot be moved.', severity='warning')
                return
            if is_protected(item) is True:
                self.notify(
                    'This recording is protected on the recorder. Release the protection on the recorder to move it.',
                    severity='warning',
                )
                return
            if display.default_move_resource(item, None) is None:
                self.notify(
                    'This recording has no original-quality resource. Press v to choose another version.',
                    severity='warning',
                )
                return
            if self._clear_other_device(udn, lambda: self._select(key, Selection(udn, item))) is True:
                return
            self._selection[key] = Selection(udn, item)
        self._refresh_row(item)
        self._update_status()

    def _select(self, key: RecordingKey, selection: Selection) -> None:
        """
        Add a selection and refresh the table.

        Args:
            key (RecordingKey): Recorder UDN and recording identity
            selection (Selection): Selected recording and resource
        """

        self._selection[key] = selection
        self._refresh_all_rows()

    def _clear_other_device(self, udn: str, then: Callable[[], None]) -> bool:
        """
        Ask before replacing selections from another recorder, since a queue moves from one recorder at a time.

        Args:
            udn (str): Recorder of the new selection
            then (Callable[[], None]): Action run after the other selections are cleared

        Returns:
            bool: True when the question was shown and selection continues after the answer
        """

        if all(selection.udn == udn for selection in self._selection.values()):
            return False

        def answer(choice: str | None) -> None:
            if choice == 'clear':
                self._selection.clear()
                then()
                self._update_status()

        self.push_screen(
            ChoiceScreen(
                'Replace the selection?',
                [
                    (
                        'Recordings selected on another recorder will be deselected. Moves run on one recorder at a time.',
                        '',
                    )
                ],
                [('clear', 'Deselect them', 'warning'), ('back', 'Back', 'primary')],
                default='back',
            ),
            answer,
        )
        return True

    def action_select_all(self) -> None:
        """Select every movable recording in the open folder, or deselect them if all are selected."""

        if self._current_udn is None:
            return
        udn = self._current_udn
        recordings = [
            item
            for item in self._objects.values()
            if isinstance(item, Recording)
            and is_broadcast(item) is False
            and is_protected(item) is False
            and display.default_move_resource(item, None) is not None
            and self._matches_filter(item.title)
        ]
        keys = {(udn, item.reference_id or item.id): item for item in recordings}
        if keys and all(key in self._selection for key in keys):
            for key in keys:
                del self._selection[key]
        else:

            def select() -> None:
                for key, item in keys.items():
                    self._selection.setdefault(key, Selection(udn, item))
                self._refresh_all_rows()

            if self._clear_other_device(udn, select) is True:
                return
            select()
        self._refresh_all_rows()

    def action_choose_resource(self) -> None:
        """Choose which advertised resource to move for the highlighted recording."""

        item = self._highlighted()
        if not isinstance(item, Recording) or self._current_udn is None or is_broadcast(item) is True:
            return
        udn = self._current_udn
        options = [
            (
                index,
                f'{index}: {resource.quality_label}  [{display.resource_version(resource)}]  '
                f'{display.format_size(display.resource_size(resource))}',
                resource.protocol_info.is_dtcp is True,
            )
            for index, resource in enumerate(item.resources, start=1)
        ]

        def chosen(index: int | None) -> None:
            if index is None:
                return
            key = (udn, item.reference_id or item.id)
            if index == 0 and display.default_move_resource(item, None) is None:
                self._selection.pop(key, None)
                self.notify('This recording has no original-quality resource.', severity='warning')
            else:
                selection = Selection(udn, item, None if index == 0 else index)
                if self._clear_other_device(udn, lambda: self._select(key, selection)) is True:
                    return
                self._selection[key] = selection
            self._refresh_all_rows()
            self._show_details(item)

        self.push_screen(ResourceScreen(item.title, options), chosen)

    def _refresh_row(self, item: Recording) -> None:
        """
        Update the cells of one recording row.

        Args:
            item (Recording): Listed recording
        """

        table: DataTable[Text | str] = self.query_one('#recordings', DataTable)
        # Entries hidden by the filter have no row to update
        if item.id not in table.rows:
            return
        for column, value in zip(
            ('mark', 'title', 'date', 'duration', 'quality', 'size', 'copies', 'channel'),
            self._row(item),
            strict=True,
        ):
            table.update_cell(item.id, column, value)

    def _refresh_all_rows(self) -> None:
        """Update every row, including other references to the same recording."""

        for item in self._objects.values():
            if isinstance(item, Recording):
                self._refresh_row(item)
        self._update_status()

    def _update_status(self) -> None:
        """Show the selection size, destination and transfer state."""

        if self.is_mounted is False:
            return
        sizes = [
            display.resource_size(display.default_move_resource(selection.recording, selection.resource_index))
            for selection in self._selection.values()
        ]
        total = sum(size.value or 0 for size in sizes)
        prefix = '~' if any(size.kind == 'estimated' for size in sizes) else ''
        suffix = ' or more' if any(size.value is None for size in sizes) else ''
        selected = f'{len(self._selection)} selected'
        if self._selection:
            selected += f' ({prefix}{display.format_bytes(total)}{suffix})'
        active = self._runner.active
        transfer = 'idle'
        if active is not None:
            transfer = f'{STATUS_LABELS[active.status]}: {active.title} {active.progress_text()}'
        waiting = sum(1 for item in self._queue if item.status == 'waiting')
        if waiting:
            transfer += f', {waiting} waiting'
        self.query_one('#status', Static).update(
            Text(f'{selected}  |  Destination: {self._directory}  |  Sort: {self._sort_order}  |  Transfer: {transfer}')
        )

    # Moves

    def action_move(self) -> None:
        """Confirm and queue the selected recordings, or the highlighted one when nothing is selected."""

        selections = list(self._selection.values())
        if not selections:
            item = self._highlighted()
            if (
                isinstance(item, Recording)
                and self._current_udn is not None
                and is_broadcast(item) is False
                and display.default_move_resource(item, None) is not None
            ):
                selections = [Selection(self._current_udn, item)]
        if not selections:
            self.notify('Select recordings with Space first.', severity='warning')
            return
        try:
            # Report missing credentials before confirmation; the client itself is created when a Move starts
            self._runner.store.load_credentials()
        except (ValueError, OSError, PyDTCPIPError) as ex:
            self.push_screen(
                ChoiceScreen(
                    'Credentials required',
                    [(f'Could not load credentials: {ex}', 'red'), (self._credentials_hint(), '')],
                    [('ok', 'OK', 'primary')],
                )
            )
            return
        server = self._servers[selections[0].udn]
        notes: list[tuple[str, str]] = []
        if server.profile.is_unverified is True:
            notes.append(
                (
                    f'The {server.profile.name} profile used for {server.description.friendly_name} is unverified on '
                    'hardware. Check the copy counts shown here against the recorder.',
                    'yellow',
                )
            )

        def confirmed(directory: Path | None) -> None:
            if directory is not None:
                self._enqueue(selections, directory)

        self.push_screen(
            ConfirmMoveScreen(lambda directory: self._confirm_entries(selections, directory), self._directory, notes),
            confirmed,
        )

    def _credentials_hint(self) -> str:
        """
        Describe where credentials are read from.

        Returns:
            str: Credential locations
        """

        location = self._runner.store.data_dir / 'credentials.env'
        return f'Place credentials in {location}, or set PYDTCPIP_CERTIFICATE and PYDTCPIP_PRIVATE_KEY.'

    def _is_count_stale(self, key: RecordingKey, recording: Recording) -> bool:
        """
        Return whether a Move completed after a recording was listed, so its listed copy count no longer applies.

        Args:
            key (RecordingKey): Device UDN and recording identifier
            recording (Recording): Listed recording

        Returns:
            bool: True when the listed copy count predates a completed Move of the recording
        """

        # The source can report the pre-Move count for some time, so a count is trusted only once a re-query saw it
        ## change, and only when listed after that
        moved = [item for item in self._queue if item.key == key and item.completed_at is not None]
        if not moved:
            return False
        confirmed_at = moved[-1].copies_confirmed_at
        return confirmed_at is None or recording.observed_at is None or recording.observed_at < confirmed_at

    def _confirm_entries(self, selections: list[Selection], directory: Path) -> list[ConfirmEntry]:
        """
        Build confirmation entries with warnings, destinations and exclusions.

        Args:
            selections (list[Selection]): Recordings to move
            directory (Path): Destination directory

        Returns:
            list[ConfirmEntry]: Entries in selection order
        """

        # Identify recordings by device and recording, since another destination gives a different path
        ## An interrupted Move must be resumed; moving the recording again would consume another copy
        blocked: dict[RecordingKey, str] = {}
        for item in self._queue:
            if item.key is None:
                continue
            if item.status == 'resume_required' or (item.status == 'finalize_rejected' and item.may_be_committed):
                blocked[item.key] = 'previous Move needs resumption or checking'
            elif item.status == 'waiting' or item.is_active is True:
                blocked[item.key] = 'already queued'
        outputs: set[Path] = set()
        entries: list[ConfirmEntry] = []
        for selection in selections:
            recording = selection.recording
            resource = display.default_move_resource(recording, selection.resource_index)
            output = recording_output_path(directory, selection.udn, recording)
            # Short labels appear in the table and each explanation once below it, so many recordings still fit
            warnings: list[tuple[str, str, str]] = []
            count = display.copy_count(recording, resource)
            is_stale = self._is_count_stale((selection.udn, recording.reference_id or recording.id), recording)
            if is_stale is True:
                count = ProtectionValue[int](status='not_observed', source='metadata')
            if count.status == 'known' and count.value == 0:
                warnings.append(
                    ('Final Move', 'Only the final Move remains. Moving will delete the source recording.', 'bold red')
                )
            elif count.status != 'known':
                warnings.append(
                    (
                        'Copies unknown',
                        'The remaining copy count is unavailable. Moving will delete the source recording if only '
                        'the final Move remains or copying is prohibited.',
                        'yellow',
                    )
                )
            if resource is not None and resource.is_original_quality is False:
                warnings.append(
                    (
                        'Not original',
                        'This is not the original recording. It may have lower quality, and moving it still '
                        'consumes a copy of the source recording.',
                        'yellow',
                    )
                )
            elif resource is not None and resource.is_original_quality is None:
                warnings.append(
                    (
                        'Original unconfirmed',
                        'The recorder does not identify whether this is the original recording.',
                        'yellow',
                    )
                )
            if resource is not None and resource.is_converted is True:
                warnings.append(
                    ('Converted', 'Moving a converted resource has not been verified on real devices.', 'yellow')
                )
            excluded: str | None = None
            if resource is None:
                excluded = 'no resource to move'
            elif is_protected(recording) is True:
                excluded = 'protected on the recorder'
            elif (selection.udn, recording.reference_id or recording.id) in blocked:
                excluded = blocked[(selection.udn, recording.reference_id or recording.id)]
            elif output in outputs:
                excluded = 'same destination as another recording'
            elif existing_move_outputs(output):
                excluded = 'files already exist'
            outputs.add(output)
            entries.append(
                ConfirmEntry(
                    title=recording.title,
                    quality=resource.quality_label if resource is not None else '-',
                    size=display.format_size(display.resource_size(resource)),
                    copies=Text('?', style='yellow')
                    if is_stale is True
                    else display.format_copy_count(recording, resource),
                    output_name=output.name,
                    warnings=warnings,
                    excluded=excluded,
                )
            )
        return entries

    def _enqueue(self, selections: list[Selection], directory: Path) -> None:
        """
        Queue confirmed recordings and start the queue.

        Args:
            selections (list[Selection]): Confirmed recordings
            directory (Path): Destination directory
        """

        self._directory = directory
        entries = self._confirm_entries(selections, directory)
        for selection, entry in zip(selections, entries, strict=True):
            if entry.excluded is not None:
                continue
            recording = selection.recording
            self._queue.append(
                QueueItem(
                    title=recording.title,
                    output=directory / entry.output_name,
                    key=(selection.udn, recording.reference_id or recording.id),
                    recording=recording,
                    resource=display.default_move_resource(recording, selection.resource_index),
                )
            )
            self._selection.pop((selection.udn, recording.reference_id or recording.id), None)
        self._scan_resume_states()
        self._refresh_all_rows()
        self._refresh_queue()
        self._start_queue()

    def _start_queue(self) -> None:
        """Start processing waiting entries unless the queue is already running."""

        if self._is_queue_running is False and any(item.status == 'waiting' for item in self._queue):
            self._is_queue_running = True
            self._run_queue()

    def _scan_resume_states(self) -> None:
        """List interrupted Moves found in the destination directory so they can be resumed."""

        try:
            paths = sorted(self._directory.glob(f'*{_RESUME_SUFFIX}'))
        except OSError:
            return
        known = {item.output for item in self._queue}
        for path in paths:
            output = path.with_name(path.name.removesuffix(_RESUME_SUFFIX))
            if output in known:
                continue
            title, key = output.name, None
            # The metadata archive identifies the recording, so it cannot be moved again under another name
            sidecar = load_resume_sidecar(output)
            if sidecar is not None and sidecar.metadata.device_description_xml is not None:
                recording = sidecar.metadata.recording
                try:
                    description = parse_device_description(
                        sidecar.metadata.device_location or '', sidecar.metadata.device_description_xml
                    )
                except PyDTCPIPError:
                    description = None
                if description is not None:
                    title, key = recording.title, (description.udn, recording.reference_id or recording.id)
            self._queue.append(
                QueueItem(
                    title=title,
                    output=output,
                    key=key,
                    is_resume=True,
                    status='resume_required',
                    message='Interrupted Move found in the destination; press Enter to resume',
                )
            )
        self._refresh_queue()

    @on(DataTable.RowSelected, '#queue')
    def _queue_selected(self, event: DataTable.RowSelected) -> None:
        item = self._queue_keys.get(event.row_key.value or '')
        if item is None or item.status != 'resume_required':
            return
        item.is_resume = True
        item.status = 'waiting'
        self._refresh_queue()
        self._start_queue()

    @work(group='queue', exit_on_error=False)
    async def _run_queue(self) -> None:
        """Move waiting entries one at a time, keeping fallback choices for this queue only."""

        stream_on_refusal: set[str] = set()
        try:
            while self._is_exit_requested is False:
                item = next((item for item in self._queue if item.status == 'waiting'), None)
                if item is None:
                    break
                await self._run_item(item)
                if item.status in ('completed', 'stream_saved') or self._exit_requested() is True:
                    continue
                if item.is_cancelled_by_user is True:
                    self._skip_waiting()
                    break
                if await self._decide_after_failure(item, stream_on_refusal) is False:
                    self._skip_waiting()
                    break
        finally:
            self._is_queue_running = False
            self._refresh_queue()
        if self._is_exit_requested is True:
            self.exit()
            return
        self._requery_copy_counts()

    async def _run_item(self, item: QueueItem) -> None:
        """
        Transfer one entry in a task owned outside Textual and settle its state.

        Args:
            item (QueueItem): Entry to transfer
        """

        try:
            client = await self._runner.client()
        except (ValueError, OSError, PyDTCPIPError) as ex:
            # An interrupted Move still needs resumption; only a new Move fails without consuming a copy
            item.status = 'resume_required' if item.is_resume is True else 'failed'
            item.message = f'Could not load credentials: {ex}'
            return
        item.start()
        self._runner.active = item
        self._refresh_queue()
        task = asyncio.get_running_loop().create_task(self._transfer(client, item))
        item.task = task
        try:
            # Shield the transfer so cancelling this worker cannot cancel a committing Move
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() is False:
                raise
        except BaseException:
            # The task's outcome is read below
            pass
        error: BaseException | None = asyncio.CancelledError() if task.cancelled() is True else task.exception()
        item.finish(error, has_resume_state=resume_state_path(item.output).exists())
        self._runner.active = None
        if item.status == 'completed':
            _LOGGER.info(f'Move completed: {item.output}')
        elif item.status == 'stream_saved':
            _LOGGER.info(f'Saved via stream: {item.output}')
        else:
            _LOGGER.warning(
                f'{STATUS_LABELS[item.status]}: {item.title}' + (f' ({item.message})' if item.message else '')
            )
        self._refresh_queue()

    async def _transfer(self, client: DTCPClient, item: QueueItem) -> int:
        """
        Save metadata and receive a recording, or resume finalization of an interrupted Move.

        Args:
            client (DTCPClient): DTCP-IP client
            item (QueueItem): Entry to transfer

        Returns:
            int: Number of recording bytes written
        """

        def on_event(event: TransferEvent) -> None:
            # Only update the state here; an exception from the interface would interrupt finalization
            ## The queue table is refreshed by a timer, which stops if the interface closes
            item.apply(event)

        if item.is_resume is True:
            resume_state = load_resume_state(item.output)
            sidecar = load_resume_sidecar(item.output)
            return await run_resume(client, item.output, resume_state, sidecar=sidecar, on_event=on_event)
        if item.stream_resource is not None:
            return await save_stream(
                client, item.stream_resource, item.output, sidecar=load_resume_sidecar(item.output), on_event=on_event
            )
        if item.key is None or item.recording is None or item.resource is None:
            raise ValueError('The queued recording has no resource to move.')
        server = self._servers[item.key[0]]
        # Query and persist optional metadata before the source can delete the recording
        metadata, sidecar = await collect_move_metadata(server, item.recording, item.output)
        self._metadata[(item.key[0], item.recording.id)] = metadata
        item.copies_before = item.recording.copy_count_for(item.resource)
        if self._is_count_stale(item.key, item.recording) is True:
            # The listing predates an earlier Move of this recording, so its count cannot show this Move's change
            item.copies_before = ProtectionValue[int](status='not_observed', source='metadata')
        item.status = 'authenticating'
        return await run_move(
            client, item.resource, item.output, media_server=server, sidecar=sidecar, on_event=on_event
        )

    async def _decide_after_failure(self, item: QueueItem, stream_on_refusal: set[str]) -> bool:
        """
        Explain an unsuccessful Move and ask whether to continue, resuming first when finalization was interrupted.

        Args:
            item (QueueItem): Entry that did not complete
            stream_on_refusal (set[str]): Device UDNs whose refused Moves can be saved via stream in this queue

        Returns:
            bool: True to continue with the next entry
        """

        while True:
            detail = [(item.message, 'dim')] if item.message else []
            title = f'{STATUS_LABELS[item.status]}: {item.title}'
            udn = item.key[0] if item.key is not None else None
            stream = (
                item.recording.original_resource
                if item.status == 'initiation_rejected' and item.recording is not None
                else None
            )
            can_save_stream = stream is not None and stream.is_original_quality is True
            repeat_label: str | None = None
            if item.status == 'resume_required':
                body = [
                    (
                        'The source may have consumed a copy. The received data and resume state were kept beside '
                        'the output. Resuming finalizes the Move without receiving the recording again.',
                        'yellow',
                    ),
                    *detail,
                ]
                choices: list[tuple[str, str, ButtonVariant]] = [
                    ('resume', 'Resume now', 'primary'),
                    ('skip', 'Continue with the next recording', 'default'),
                    ('stop', 'Stop the queue', 'error'),
                ]
            elif item.status == 'finalize_rejected':
                if item.may_be_committed is True:
                    text = (
                        f'The received data remains beside {item.output}. If the source recording has fewer copies or '
                        'has been deleted, the source finalized the Move.'
                    )
                else:
                    text = 'The source did not finalize the Move and kept its copy. The received data was deleted.'
                body = [(text, 'yellow'), *detail]
                choices = [('skip', 'Continue with the next recording', 'primary'), ('stop', 'Stop the queue', 'error')]
            elif can_save_stream is True:
                title = 'The source refused to start dubbing'
                body = [
                    (item.title, 'bold'),
                    (
                        'Save it via stream in its original quality. The recording and copy count stay unchanged '
                        'on the source.',
                        '',
                    ),
                ]
                choices = [
                    ('stream', 'Save in original quality', 'primary'),
                    ('skip', 'Skip recording', 'default'),
                    ('stop', 'Stop the queue', 'error'),
                ]
                if udn is not None and any(
                    entry.status == 'waiting'
                    and entry.is_resume is False
                    and entry.key is not None
                    and entry.key[0] == udn
                    for entry in self._queue
                ):
                    repeat_label = 'Stream later recordings this device refuses in this queue'
            else:
                text = (
                    'Streaming leaves the source recording and copy count unchanged.'
                    if item.stream_resource is not None
                    else 'The source may have consumed a copy.'
                    if item.is_committing is True
                    else 'Finalization was not requested, so the source has not consumed a copy.'
                )
                body = [(text, 'yellow'), *detail]
                choices = [('skip', 'Continue with the next recording', 'primary'), ('stop', 'Stop the queue', 'error')]
            if can_save_stream is True and udn in stream_on_refusal:
                choice = 'stream'
            else:
                screen = ChoiceScreen(title, body, choices, repeat_label=repeat_label)
                choice = await self.push_screen_wait(screen)
                if choice == 'stream' and screen.repeat_choice is True and udn is not None:
                    stream_on_refusal.add(udn)
            if choice == 'stream' and stream is not None:
                item.stream_resource = stream
                item.copies_before = None
                await self._run_item(item)
                if item.status == 'stream_saved' or self._exit_requested() is True:
                    return True
                if item.is_cancelled_by_user is True:
                    return False
                continue
            if choice == 'resume':
                item.is_resume = True
                await self._run_item(item)
                if item.status == 'completed' or self._is_exit_requested is True:
                    return True
                continue
            return choice == 'skip'

    def _exit_requested(self) -> bool:
        """
        Read whether quitting was requested, which can change while a transfer is awaited.

        Returns:
            bool: True when the application should exit once the active transfer ends
        """

        return self._is_exit_requested

    def _skip_waiting(self) -> None:
        """Mark entries that have not started as not started when the queue stops, keeping resumptions resumable."""

        for item in self._queue:
            if item.status == 'waiting':
                # An interrupted Move stays resumable; moving the recording again would consume another copy
                item.status = 'resume_required' if item.is_resume is True else 'skipped'
        self._refresh_queue()

    def action_cancel_transfer(self) -> None:
        """Cancel the active Move if it has not requested finalization."""

        item = self._runner.active
        if item is None:
            self.notify('No transfer is in progress.')
            return
        if item.is_committing is True:
            self.notify('Finalization is in progress and cannot be cancelled.', severity='warning')
            return

        def answer(choice: str | None) -> None:
            if choice != 'cancel' or self._runner.active is not item or item.task is None:
                return
            # The transfer may have advanced while the dialog was open
            if item.is_committing is True:
                self.notify('The Move entered finalization, so it was not cancelled.', severity='warning')
                return
            item.is_cancelled_by_user = True
            item.task.cancel()

        self.push_screen(
            ChoiceScreen(
                f'Cancel the transfer of {item.title}?',
                [
                    (
                        'Cancelling leaves the recording and copy count unchanged.'
                        if item.stream_resource is not None
                        else 'Finalization has not been requested, so cancelling keeps the copy on the source.',
                        '',
                    )
                ],
                [('cancel', 'Cancel the transfer', 'error'), ('back', 'Keep transferring', 'primary')],
                default='back',
            ),
            answer,
        )

    async def action_quit(self) -> None:
        """Quit, cancelling a Move that has not requested finalization or waiting for one that has."""

        item = self._runner.active
        if item is None:
            self.exit()
            return
        if item.is_committing is True:
            self._is_exit_requested = True
            self.notify('Waiting for the Move to finish finalizing before quitting.', severity='warning')
            return

        def answer(choice: str | None) -> None:
            if choice != 'quit':
                return
            self._is_exit_requested = True
            active = self._runner.active
            if active is None or active.task is None:
                self.exit()
                return
            # The transfer may have advanced while the dialog was open
            if active.is_committing is True:
                self.notify('Waiting for the Move to finish finalizing before quitting.', severity='warning')
                return
            active.is_cancelled_by_user = True
            active.task.cancel()

        self.push_screen(
            ChoiceScreen(
                'Quit while transferring?',
                [
                    (
                        f'{item.title} is being saved via stream. Cancelling keeps the recording on the source.'
                        if item.stream_resource is not None
                        else f'{item.title} is being transferred. Finalization has not been requested, so cancelling '
                        'keeps the copy on the source.',
                        '',
                    ),
                ],
                [('quit', 'Cancel the transfer and quit', 'error'), ('back', 'Back', 'primary')],
                default='back',
            ),
            answer,
        )

    @work(group='requery', exclusive=True, exit_on_error=False)
    async def _requery_copy_counts(self) -> None:
        """Check source copy counts after the queue empties, without querying while a transfer runs."""

        pending = [
            item
            for item in self._queue
            if item.status == 'completed'
            and item.copies_before is not None
            and item.copies_before.status == 'known'
            # A previous check cancelled by a newer one leaves its placeholder
            and item.copies_after in (None, _CHECKING)
            and item.key is not None
            and item.recording is not None
        ]
        udns = {item.key[0] for item in pending if item.key is not None}
        for item in pending:
            item.copies_after = _CHECKING
        self._refresh_queue()
        for _ in range(_REQUERY_ATTEMPTS):
            if not pending or self._is_queue_running is True:
                break
            await asyncio.sleep(_REQUERY_INTERVAL)
            for item in list(pending):
                if item.key is None or item.recording is None or item.copies_before is None:
                    continue
                before = display.describe_protection(item.copies_before)
                try:
                    after = await query_copy_count(
                        self._servers[item.key[0]],
                        item.recording,
                        item.resource.url if item.resource is not None else None,
                    )
                except (OSError, TimeoutError, PyDTCPIPError):
                    # A recording with no copies remaining is deleted from the source and cannot be queried
                    item.copies_after = 'Not found on source (it may have been deleted)'
                    pending.remove(item)
                    continue
                if after is None:
                    item.copies_after = '-'
                    pending.remove(item)
                elif (after.status, after.value) != (item.copies_before.status, item.copies_before.value):
                    item.copies_after = f'{before} -> {display.describe_protection(after)}'
                    item.copies_confirmed_at = datetime.now(UTC)
                    pending.remove(item)
            self._refresh_queue()
        for item in pending:
            if self._is_queue_running is True:
                # Another queue started on the device; check again after it ends
                item.copies_after = None
            elif item.copies_before is not None:
                item.copies_after = f'Still {display.describe_protection(item.copies_before)} (may update later)'
        # Listings cached before the Moves show old copy counts or deleted recordings
        self._listings = {key: value for key, value in self._listings.items() if key[0] not in udns}
        if self._current_udn in udns:
            self._show_folder()
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        """Update the queue table and status line."""

        # The interval keeps firing while the application closes, after its widgets are removed
        if self.is_mounted is False or self.is_running is False:
            return
        table: DataTable[Text | str] = self.query_one('#queue', DataTable)
        table.display = len(self._queue) > 0
        for index, item in enumerate(self._queue):
            key = str(index)
            status = Text(STATUS_LABELS[item.status])
            if item.status in ('completed', 'stream_saved'):
                status.stylize('green')
            elif item.status in (
                'resume_required',
                'finalize_rejected',
                'initiation_rejected',
                'failed',
                'cancel_unconfirmed',
            ):
                status.stylize('bold red' if item.status == 'resume_required' else 'red')
            elif item.is_committing is True and item.is_active is True:
                status.stylize('bold yellow')
            copies = item.copies_after or (
                display.describe_protection(item.copies_before) if item.copies_before is not None else ''
            )
            cells: list[Text | str] = [
                status,
                Text(item.title),
                item.progress_text(),
                copies,
                Text(item.message or item.output.name),
            ]
            if key not in self._queue_keys:
                self._queue_keys[key] = item
                table.add_row(*cells, key=key)
                continue
            for column, value in zip(('status', 'title', 'progress', 'copies', 'output'), cells, strict=True):
                table.update_cell(key, column, value)
        # Show queue states in the recording table, refreshing rows only when a state changes
        self._transfers = {item.key: item for item in self._queue if item.key is not None}
        statuses = {key: item.status for key, item in self._transfers.items()}
        if statuses != self._transfer_statuses:
            self._transfer_statuses = statuses
            self._refresh_all_rows()
        self._update_status()

    def action_toggle_log(self) -> None:
        """Show or hide the log panel."""

        log = self.query_one('#log', RichLog)
        log.display = not log.display
