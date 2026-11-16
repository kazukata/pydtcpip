"""CLI for discovering devices and recordings, streaming, Move, incoming transfers, and response dumps."""

import asyncio
import json
import logging
import os
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import aiohttp
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, IntPrompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pydtcpip.client import DTCPClient
from pydtcpip.dlna.client import DLNAClient, MediaServer
from pydtcpip.dtcp.crypto import DeviceCredentials
from pydtcpip.dump import dump_media_server
from pydtcpip.errors import DLNAError, MoveCommitmentRejectedError, PyDTCPIPError
from pydtcpip.events import (
    AuthenticationCompleted,
    AuthenticationStarted,
    MoveCancelled,
    MoveCancelRequested,
    MoveCompleted,
    MoveFinalized,
    MoveFinalizeRejected,
    MoveFinalizeRequested,
    MoveResumeRequired,
    SRMReceived,
    TransferEvent,
    TransferProgressed,
    TransferProtectionObserved,
    TransferReceived,
    TransferStarted,
)
from pydtcpip.metadata import MetadataSidecar
from pydtcpip.models import (
    Container,
    MetadataQuery,
    MoveResumeState,
    ProtectionValue,
    Recording,
    RecordingMetadata,
    Resource,
    TimeRange,
)
from pydtcpip.operations import (
    ResourceSelectionError,
    collect_move_metadata,
    existing_move_outputs,
    is_broadcast,
    load_resume_sidecar,
    load_resume_state,
    query_copy_count,
    resume_state_path,
    run_move,
    run_resume,
    sanitize_title,
    select_resource,
)
from pydtcpip.operations import save_resume_state as save_move_resume_state
from pydtcpip.receiver import DubbingReceiver
from pydtcpip.sinks import FileSink
from pydtcpip.store import DeviceStore


app = typer.Typer(
    help='Receive recordings from devices on your home network using DLNA and DTCP-IP.', no_args_is_help=True
)

# Send display output and logs to stderr because some commands write media to stdout
console = Console(stderr=True, theme=Theme({'log.time': 'cyan'}))

_LOGGER = logging.getLogger('pydtcpip')

# Select the data directory at startup and keep credentials and SRMs together
_device_store = DeviceStore()

CredentialsOption = Annotated[
    Path | None,
    typer.Option(
        '--credentials',
        '-c',
        help='Credentials dotenv file (defaults to credentials.env in the data directory; environment takes precedence)',
    ),
]
ServerOption = Annotated[
    str | None, typer.Option('--server', '-s', help='Recorder device description URL (discover and select if omitted)')
]
VerboseOption = Annotated[bool, typer.Option('--verbose', '-v', help='Show detailed communication logs')]

# Shared discovery settings, received by app.callback() and used by each command
# Default UDP discovery response port and TCP HTTP listening port for incoming transfers
## TCP and UDP have separate port spaces, so the same number can be allowed for both in the firewall
_DEFAULT_PORT = 57000

_search_settings: dict[str, str | int | None] = {'interface': None, 'port': _DEFAULT_PORT}

# Interval in seconds and number of retries for checking the remaining copy count after a Move
## DBR-Z620 updated the count about 80 seconds after completion, so query for up to two minutes
_COPY_COUNT_REQUERY_INTERVAL = 10.0
_COPY_COUNT_REQUERY_ATTEMPTS = 12


class _CLIHandler(RichHandler):
    """Render log levels with a trailing colon."""

    def get_level_text(self, record: logging.LogRecord) -> Text:
        """
        Format the log level using Rich's level style.

        Args:
            record (logging.LogRecord): Log record to render

        Returns:
            Text: Styled log level with a trailing colon
        """

        level_text = Text()
        level_text.append(record.levelname, style=f'logging.level.{record.levelname.lower()}')
        level_text.append(':')
        level_text.pad_right(max(0, 8 - len(level_text)))
        return level_text


def _setup_logging(is_verbose: bool) -> None:
    """
    Configure CLI logging on standard error.

    Args:
        is_verbose (bool): Whether to enable detailed communication logs
    """

    handler = _CLIHandler(
        console=console, show_path=False, rich_tracebacks=False, log_time_format='[%Y/%m/%d %H:%M:%S]'
    )
    logging.basicConfig(level=logging.WARNING, format='%(message)s', handlers=[handler])
    _LOGGER.setLevel(logging.DEBUG if is_verbose is True else logging.INFO)


def _load_credentials(path: Path | None) -> DeviceCredentials:
    """
    Load credentials from a file or environment and verify the certificate and private key match.

    Args:
        path (Path | None): dotenv file (defaults to credentials.env in the data directory)

    Returns:
        DeviceCredentials: Verified credentials
    """

    try:
        return _device_store.load_credentials(path)
    except (ValueError, OSError, PyDTCPIPError) as ex:
        console.print(f'[red]Could not load credentials: {ex}[/red]')
        raise typer.Exit(1) from ex


def _dubbing_output_path(directory: Path, recording: Recording) -> Path:
    """
    Choose the incoming recording destination from the item ID prefix and title.

    Args:
        directory (Path): Destination directory
        recording (Recording): Incoming recording metadata

    Returns:
        Path: Destination for the recording
    """

    return directory / f'{recording.id[:8]}-{sanitize_title(recording.title)}.m2ts'


def _format_protection(value: ProtectionValue[int] | ProtectionValue[bool]) -> str:
    """
    Format a protection value with its acquisition status.

    Args:
        value (ProtectionValue[int] | ProtectionValue[bool]): Value and acquisition status

    Returns:
        str: Value or a description of its acquisition status
    """

    if value.status == 'known':
        if isinstance(value.value, bool):
            return 'Yes' if value.value is True else 'No'
        return str(value.value)
    if value.status == 'omitted':
        return 'Not reported by device'
    if value.status == 'not_observed':
        return 'Available after transfer starts'
    return 'Unsupported format'


async def _discover(dlna: DLNAClient, *, timeout: float = 3.0) -> list[MediaServer]:
    """
    Discover recorders using the configured interface and response port.

    Args:
        dlna (DLNAClient): Client used for discovery
        timeout (float): Time to wait for responses in seconds

    Returns:
        list[MediaServer]: Discovered recorders
    """

    interface = _search_settings['interface']
    port = _search_settings['port']
    return await dlna.discover(
        timeout=timeout,
        interface=interface if isinstance(interface, str) else None,
        port=port if isinstance(port, int) else 0,
    )


def _print_no_servers_found() -> None:
    """Show common causes and remedies when no recorders are found."""

    # Discovery responses arrive by unicast and are dropped by firewalls that deny incoming traffic by default
    port = _search_settings['port']
    console.print(
        '[red]No recorders found. Check that they are on the same network and that the firewall '
        'allows UDP discovery responses.[/red]\n'
        f'[yellow]Responses are received on UDP port {port}. Allow incoming traffic on this port, '
        'or use --search-port to select another allowed port. If you know the device description URL, '
        'you can pass it directly to a command with --server.[/yellow]'
    )


async def _select_server(dlna: DLNAClient, location: str | None) -> MediaServer:
    """
    Select a recorder by device description URL or from discovered devices.

    Args:
        dlna (DLNAClient): DLNA client
        location (str | None): Device description URL (discover and select if None)

    Returns:
        MediaServer: Selected recorder
    """

    if location is not None:
        return await dlna.get_server(location)

    with console.status('Searching for recorders...'):
        servers = await _discover(dlna)
    if len(servers) == 0:
        _print_no_servers_found()
        raise typer.Exit(1)
    if len(servers) == 1 or sys.stdin.isatty() is False:
        return servers[0]

    table = Table(title='Recorders')
    table.add_column('Number', justify='right')
    table.add_column('Name')
    table.add_column('Manufacturer')
    table.add_column('Model')
    table.add_column('Profile')
    for index, server in enumerate(servers, start=1):
        table.add_row(
            str(index),
            server.description.friendly_name,
            server.description.manufacturer,
            server.description.model_name,
            f'{server.profile.name} (unverified)' if server.profile.is_unverified else server.profile.name,
        )
    console.print(table)
    choice = IntPrompt.ask(
        'Recorder number', console=console, choices=[str(index) for index in range(1, len(servers) + 1)]
    )
    return servers[choice - 1]


async def _select_recording(server: MediaServer, object_id: str | None) -> Recording:
    """
    Select a recording by ID or by browsing folders.

    Args:
        server (MediaServer): Recorder
        object_id (str | None): Recording ID (browse folders if None)

    Returns:
        Recording: Selected recording
    """

    if object_id is not None:
        item = await server.get_metadata(object_id)
        if isinstance(item, Container):
            console.print(f'[red]ID {object_id} is not a recording.[/red]')
            raise typer.Exit(1)

        # Some families provide fields only in listings (e.g. network and service IDs on REGZA TVs)
        # Find the recording in its parent listing to include those fields in the saved program metadata
        ## Large folders can take tens of seconds to list, so skip this for other families
        if server.profile.resolve_has_listing_only_metadata(item) is False:
            return item
        try:
            with console.status('Fetching recording information...'):
                siblings = await server.browse_all(item.parent_id)
        except DLNAError:
            return item
        for sibling in siblings:
            if isinstance(sibling, Recording) and sibling.id == item.id:
                return sibling
        return item

    if sys.stdin.isatty() is False:
        console.print('[red]Specify a recording ID with --id when input is not a terminal.[/red]')
        raise typer.Exit(1)

    # Enter selected folders until a recording is selected
    container_id = '0'
    while True:
        with console.status('Fetching listing...'):
            objects = await server.browse_all(container_id)
        if len(objects) == 0:
            console.print('[yellow]This folder is empty.[/yellow]')
            container_id = '0'
            continue
        table = Table(title=f'{server.description.friendly_name} contents')
        table.add_column('Number', justify='right')
        table.add_column('Type')
        table.add_column('Name')
        table.add_column('Date')
        table.add_column('Copies remaining')
        for index, item in enumerate(objects, start=1):
            if isinstance(item, Container):
                table.add_row(str(index), 'Folder', item.title, '', '')
            else:
                table.add_row(
                    str(index),
                    'Channel' if is_broadcast(item) is True else 'Recording',
                    item.title,
                    item.date or '',
                    _format_recording_copy_count(item),
                )
        console.print(table)
        choice = IntPrompt.ask('Number', console=console, choices=[str(index) for index in range(1, len(objects) + 1)])
        selected = objects[choice - 1]
        if isinstance(selected, Recording):
            return selected
        container_id = selected.id


def _choose_resource(recording: Recording, *, for_move: bool = False, resource_index: int | None = None) -> Resource:
    """
    Select the stored recording by default, or an explicitly requested resource.

    Args:
        recording (Recording): Recording whose resources are inspected
        for_move (bool): Select the original-quality Move endpoint instead of the streaming endpoint
        resource_index (int | None): One-based resource number from info, or None for the default selection

    Returns:
        Resource: Selected DTCP-IP resource
    """

    try:
        resource = select_resource(recording, for_move=for_move, resource_index=resource_index)
    except ResourceSelectionError as ex:
        console.print(f'[red]{ex}[/red]')
        raise typer.Exit(1) from ex
    if resource.is_original_quality is None:
        console.print(
            '[yellow]The recorder does not identify whether the selected resource is the original recording.[/yellow]'
        )
    return resource


def _format_recording_copy_count(recording: Recording) -> str:
    """
    Show normalized copies or the original value when its counting convention is unknown.

    Args:
        recording (Recording): Recording to display

    Returns:
        str: Copy count with any unresolved counting convention
    """

    count = recording.protection.remaining_copy_count
    if count.status == 'known':
        return _format_protection(count)
    original = recording.original_move_resource
    for reported in recording.protection.reported_copy_counts:
        if reported.resource_url is not None and (original is None or reported.resource_url != original.url):
            continue
        if reported.is_unlimited is True:
            return 'Unlimited (device report)'
        if reported.value is not None:
            return f'Unknown (device reports {reported.raw_value})'
    return _format_protection(count)


def _print_recording(recording: Recording, device_declarations: dict[str, str] | None = None) -> None:
    """
    Display recording metadata and protection information.

    Args:
        recording (Recording): Recording to display
        device_declarations (dict[str, str] | None): Declarations from the UPnP device description
    """

    table = Table(title=recording.title, show_header=False)
    table.add_column('Field')
    table.add_column('Value')
    table.add_row('ID', recording.id)
    table.add_row('Date', recording.date or 'Not reported by device')
    table.add_row('Duration', recording.duration or 'Not reported by device')
    table.add_row('Channel', recording.channel_name or 'Not reported by device')
    if recording.description:
        table.add_row('Description', recording.description)
    if recording.long_description:
        table.add_row('Program details', recording.long_description)
    if recording.genres:
        table.add_row('Genres', ', '.join(recording.genres))
    if recording.scheduled_start_time is not None:
        table.add_row('Scheduled start', recording.scheduled_start_time)
    if recording.scheduled_end_time is not None:
        table.add_row('Scheduled end', recording.scheduled_end_time)
    if recording.channel_number is not None:
        table.add_row('Channel number', recording.channel_number)
    if recording.write_status is not None:
        table.add_row('Write status', recording.write_status)
    for label, values in (
        ('Parental rating', recording.ratings),
        ('Video components', recording.video_components),
        ('Audio components', recording.audio_components),
        ('Broadcast information', recording.broadcast_metadata),
        ('Device metadata', recording.vendor_metadata),
    ):
        for value in values:
            attributes = ', '.join(f'{name.rpartition("}")[2]}={text}' for name, text in value.attributes.items())
            text = value.value + (f' ({attributes})' if attributes else '')
            field = value.name.rpartition('}')[2]
            table.add_row(f'{label} ({field})', text)
    table.add_row('Copies remaining (recording metadata)', _format_recording_copy_count(recording))
    table.add_row(
        'Selected resource Move support (protocolInfo)', _format_protection(recording.protection.is_move_supported)
    )
    table.add_row('Transfer copy control', 'Available after transfer starts')
    for count in recording.protection.reported_copy_counts:
        convention = 'Final Move convention unknown'
        if count.is_unlimited is True:
            convention = 'Reported as unlimited'
        elif count.includes_final_move is not None:
            convention = 'Includes final Move' if count.includes_final_move else 'Excludes final Move'
        table.add_row(f'Reported count ({count.field_name.rpartition("}")[2]})', f'{count.raw_value} ({convention})')
    for index, resource in enumerate(recording.resources, start=1):
        quality = {True: 'Original recording', False: 'Converted or mobile version', None: 'Quality unknown'}[
            resource.is_original_quality
        ]
        table.add_row(f'Resource {index}', Text(resource.quality_label))
        table.add_row(f'Resource {index}: Recording version', quality)
        # Keep all advertised profile identifiers; different namespaces can describe the same URL
        for name, value in resource.protocol_info.parameters.items():
            if name.endswith('_PN'):
                table.add_row(f'Resource {index}: {name}', Text(value))
        uses: list[str] = []
        if resource == recording.original_resource:
            uses.append('Streaming')
        if resource == recording.original_move_resource:
            uses.append('Move')
        if uses:
            table.add_row(f'Resource {index}: Default selection', ', '.join(uses))
        if resource.variant is not None and resource.variant.is_move_only is True:
            table.add_row(f'Resource {index}: Transfer use', 'Move only')
        for label, value in (
            ('Duration', resource.duration),
            ('Audio channels', resource.audio_channels),
            ('Audio sample frequency (Hz)', resource.sample_frequency),
            ('Audio bits per sample', resource.bits_per_sample),
            ('Usage information', resource.attributes.get('usageInfo')),
            ('HTTP size (bytes)', resource.size),
            ('Content size (bytes)', resource.cleartext_size),
        ):
            if value is not None:
                table.add_row(f'Resource {index}: {label}', Text(str(value)))
    console.print(table)
    if device_declarations:
        capabilities = {name: value for name, value in device_declarations.items() if name.endswith('CAP')}
        if capabilities:
            table = Table(title='Device capabilities (UPnP device description)', show_header=False)
            table.add_column('Declaration')
            table.add_column('Value')
            for name, value in capabilities.items():
                table.add_row(Text(name.rpartition('}')[2]), Text(value))
            console.print(table)
            console.print('These device-wide declarations do not establish Move permission for this recording.')


def _print_additional_metadata(metadata: RecordingMetadata) -> None:
    """
    Display optional metadata and acquisition failures alongside recording details.

    Args:
        metadata (RecordingMetadata): Metadata collected for the selected recording
    """

    chapters = metadata.chapters
    if chapters.status == 'known':
        table = Table(title='Chapters (device behavior unverified)')
        table.add_column('Position (ms)')
        table.add_column('Timing')
        for chapter in chapters.chapters:
            timing = 'Estimated' if chapter.is_estimated else 'Device-provided'
            if chapter.position_ms is None:
                timing = f'Time unavailable; source position {chapter.source_position}'
            table.add_row(str(chapter.position_ms) if chapter.position_ms is not None else 'Unknown', timing)
        console.print(table)
    for query in metadata.queries:
        if query.status == 'failed':
            console.print(f'[yellow]Could not retrieve {query.name}: {query.error}[/yellow]')
        elif query.status == 'known' and query.data:
            table = Table(title=query.name.replace('_', ' ').title(), show_header=False)
            table.add_column('Field')
            table.add_column('Value')
            # Show selected recording fields; complete response documents remain in the JSON archive
            selected = query.data.get('recording')
            items = [selected] if isinstance(selected, dict) else query.data.get('items', [query.data])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    fields = item.get('fields')
                    if isinstance(fields, list):
                        for field in fields:
                            if isinstance(field, dict):
                                name = str(field.get('name', '')).rpartition('}')[2]
                                table.add_row(name, Text(str(field.get('value', ''))))
                    else:
                        for name, value in item.items():
                            text = (
                                json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
                            )
                            table.add_row(name, Text(text))
            console.print(table)


class ProgressReporter:
    """Report transfer events through a Rich progress bar and logs."""

    def __init__(self, progress: Progress, description: str, sidecar: MetadataSidecar | None = None) -> None:
        """
        Attach progress reporting and optional metadata persistence to a transfer.

        Args:
            progress (Progress): Progress display
            description (str): Label for the transfer
            sidecar (MetadataSidecar | None): Metadata archive associated with the transfer
        """

        self._progress = progress
        self._description = description
        self._sidecar = sidecar
        # Keep each transfer's task and byte counter separate in the long-running receiver
        self._tasks: dict[str, tuple[TaskID, bool]] = {}

    def __call__(self, event: TransferEvent) -> None:
        """
        Save relevant metadata and update the transfer display.

        Args:
            event (TransferEvent): Transfer or authentication event
        """

        if self._sidecar is not None:
            self._sidecar.on_event(event)
        if isinstance(event, AuthenticationStarted):
            _LOGGER.info(
                f'Starting mutual authentication: {event.host}:{event.port}'
                + (' (Move)' if event.is_move is True else '')
            )
        elif isinstance(event, AuthenticationCompleted):
            _LOGGER.info('Mutual authentication completed.')
        elif isinstance(event, SRMReceived):
            _LOGGER.info(f'Received a newer SRM from the source (version {event.version}).')
        elif isinstance(event, TransferStarted):
            # Prefer HTTP body size, then decrypted size from metadata, otherwise show indeterminate progress
            ## DBR-Z620 uses chunked encoding and does not report the HTTP body size
            is_content_progress = event.total_http_bytes is None and event.total_content_bytes is not None
            total = event.total_content_bytes if is_content_progress is True else event.total_http_bytes
            task = self._progress.add_task(self._description, total=total)
            self._tasks[event.transfer_id] = (task, is_content_progress)
        elif isinstance(event, TransferProtectionObserved):
            _LOGGER.info(
                f'Transfer copy control (E-EMI): 0x{event.encryption_mode.value:x}'
                if event.encryption_mode.value is not None
                else 'Could not obtain transfer copy control.'
            )
        elif isinstance(event, (TransferProgressed, TransferReceived)):
            entry = self._tasks.get(event.transfer_id)
            if entry is not None:
                task, is_content_progress = entry
                completed = event.content_bytes if is_content_progress is True else event.http_bytes
                self._progress.update(task, completed=completed)
                if isinstance(event, TransferReceived):
                    # Receiving all bytes is distinct from persisting and completing a Move
                    self._progress.update(task, total=completed, description='Received')
                    self._progress.stop_task(task)
        elif isinstance(event, MoveFinalizeRequested):
            _LOGGER.info('Sent the Move finalize request to the source.')
        elif isinstance(event, MoveFinalized):
            _LOGGER.info('The source accepted the Move finalization.')
        elif isinstance(event, MoveCompleted):
            _LOGGER.info('Move completed.')
        elif isinstance(event, MoveCancelled):
            _LOGGER.info('Move cancelled.')
        elif isinstance(event, MoveCancelRequested):
            _LOGGER.info('Sent the Move cancellation request to the source.')
        elif isinstance(event, MoveFinalizeRejected):
            _LOGGER.warning('The source rejected the Move finalize request.')
        elif isinstance(event, MoveResumeRequired):
            _LOGGER.warning(
                'No response was received to Move finalization. The source may have consumed a copy. '
                'The received data and resume state have been retained; once connectivity is restored, '
                'run `pydtcpip resume <output-file>` to finalize.'
            )

        if isinstance(
            event, (MoveCompleted, MoveCancelRequested, MoveCancelled, MoveFinalizeRejected, MoveResumeRequired)
        ):
            entry = self._tasks.pop(event.transfer_id, None)
            if entry is not None:
                self._progress.remove_task(entry[0])


def _new_progress() -> Progress:
    """
    Create the CLI transfer progress display.

    Returns:
        Progress: Progress display configured for standard error
    """

    return Progress(
        TextColumn('[bold]{task.description}'),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def _print_commitment_rejected(error: MoveCommitmentRejectedError, output: Path) -> None:
    """
    Explain what happened to received data when the source rejects finalization.

    Args:
        error (MoveCommitmentRejectedError): Rejection and whether the source may have finalized
        output (Path): Intended recording destination
    """

    console.print(f'[red]{error}[/red]')
    if error.may_be_committed is False:
        console.print('[yellow]The source did not finalize the Move, so the received data was deleted.[/yellow]')
        return
    console.print(
        f'[yellow]The received data remains at {FileSink(output).pending_path}. '
        'If the source recording has fewer copies remaining or has been deleted, the source finalized the Move. '
        'The resume state was deleted because retrying cannot finalize it.[/yellow]'
    )


def _execute(coroutine: Coroutine[object, object, None], *, resume_path: Path | None = None) -> None:
    """
    Run an asynchronous operation and display library errors before exiting.

    Args:
        coroutine (Coroutine[object, object, None]): Coroutine to run
        resume_path (Path | None): Resume state path, used in guidance when a finalize response is lost
    """

    try:
        asyncio.run(coroutine)
    # Display file I/O errors as descriptions, like library exceptions
    except (PyDTCPIPError, OSError) as ex:
        console.print(f'[red]{ex}[/red]')
        if resume_path is not None and resume_path.exists() is True:
            console.print(
                f'[yellow]Received data and resume state were retained. Once connectivity is restored, '
                f'run `pydtcpip resume` to finalize: {resume_path}[/yellow]'
            )
        raise typer.Exit(1) from ex


@app.callback()
def main_options(
    data_dir: Annotated[
        Path | None,
        typer.Option('--data-dir', help='Directory for credentials.env and srm.bin (defaults to the OS location)'),
    ] = None,
    interface: Annotated[
        str | None, typer.Option('--interface', help='Local IPv4 address of the network interface used for discovery')
    ] = None,
    search_port: Annotated[
        int,
        typer.Option(
            '--search-port',
            help='UDP port for discovery responses (allow incoming traffic in the firewall; 0 lets the OS choose)',
        ),
    ] = _DEFAULT_PORT,
) -> None:
    """Receive recordings from devices on your home network using DLNA and DTCP-IP."""

    global _device_store
    _device_store = DeviceStore(data_dir)
    _search_settings['interface'] = interface
    _search_settings['port'] = search_port


@app.command()
def discover(
    timeout: Annotated[float, typer.Option(help='Seconds to wait for discovery responses')] = 3.0,
    verbose: VerboseOption = False,
) -> None:
    """Find and list recorders (DLNA media servers) on your home network."""

    _setup_logging(verbose)

    async def main() -> None:
        async with DLNAClient() as dlna:
            with console.status('Searching for recorders...'):
                servers = await _discover(dlna, timeout=timeout)
        if len(servers) == 0:
            _print_no_servers_found()
            raise typer.Exit(1)
        table = Table(title='Recorders')
        table.add_column('Name')
        table.add_column('Manufacturer')
        table.add_column('Model')
        table.add_column('Profile')
        table.add_column('Device description URL')
        for server in servers:
            table.add_row(
                server.description.friendly_name,
                server.description.manufacturer,
                server.description.model_name,
                f'{server.profile.name} (unverified)' if server.profile.is_unverified else server.profile.name,
                server.description.location,
            )
        console.print(table)

    _execute(main())


@app.command()
def info(
    server: ServerOption = None,
    object_id: Annotated[str | None, typer.Option('--id', help='Recording ID')] = None,
    additional: Annotated[
        bool, typer.Option('--additional/--basic', help='Query optional chapters and recording details')
    ] = True,
    json_output: Annotated[
        bool, typer.Option('--json', help='Write complete metadata as JSON to standard output')
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """Show recording metadata, with optional additional chapter queries."""

    _setup_logging(verbose)

    async def main() -> None:
        async with DLNAClient() as dlna:
            media_server = await _select_server(dlna, server)
            recording = await _select_recording(media_server, object_id)
            with console.status('Fetching recording metadata...'):
                metadata = await media_server.collect_metadata(recording, additional=additional)
            if json_output is True:
                print(metadata.model_dump_json(indent=2))
            else:
                profile_label = media_server.profile.name
                if media_server.profile.is_unverified is True:
                    profile_label += ' (device behavior unverified)'
                console.print(f'Profile: {profile_label}')
                _print_recording(recording, metadata.device_declarations)
                _print_additional_metadata(metadata)

    _execute(main())


@app.command()
def stream(
    url: Annotated[str | None, typer.Argument(help='Resource URL (select a recorder and recording if omitted)')] = None,
    server: ServerOption = None,
    object_id: Annotated[str | None, typer.Option('--id', help='Recording ID')] = None,
    resource_index: Annotated[
        int | None, typer.Option('--resource', min=1, help='Resource number shown by info (default: stored recording)')
    ] = None,
    start_ms: Annotated[int | None, typer.Option(help='Start position in milliseconds')] = None,
    end_ms: Annotated[int | None, typer.Option(help='End position in milliseconds')] = None,
    credentials: CredentialsOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Stream media to an external player through standard output."""

    _setup_logging(verbose)
    if url is not None and resource_index is not None:
        raise typer.BadParameter('Use --resource with a recorder and recording, or supply a URL, separately.')
    device_credentials = _load_credentials(credentials)
    time_range: TimeRange | None = None
    if start_ms is not None or end_ms is not None:
        # Start at the beginning when only an end position is specified
        try:
            time_range = TimeRange(start_ms=start_ms or 0, end_ms=end_ms)
        except ValidationError as ex:
            console.print('[red]The time range must start at or after zero and end at or after the start.[/red]')
            raise typer.Exit(1) from ex

    async def main() -> None:
        media_server: MediaServer | None = None
        async with (
            DLNAClient() as dlna,
            DTCPClient(
                device_credentials, srm=_device_store.load_srm(), on_srm_update=_device_store.save_srm
            ) as client,
        ):
            target: Resource | str
            if url is not None:
                target = url
            else:
                media_server = await _select_server(dlna, server)
                target = _choose_resource(
                    await _select_recording(media_server, object_id), resource_index=resource_index
                )

            stream_file = sys.stdout.buffer

            async def on_data(chunk: bytes) -> None:
                await asyncio.to_thread(stream_file.write, chunk)

            is_output_closed = False
            written = 0
            try:
                with _new_progress() as progress:
                    written = await client.stream(
                        target, on_data, time_range=time_range, on_event=ProgressReporter(progress, 'Receiving')
                    )
            except BrokenPipeError:
                # Stop normally if the stdout reader (such as ffplay) exits first
                is_output_closed = True
                # Redirect stdout to the null device to avoid another exception during the final flush
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                os.close(devnull)
            except DLNAError as ex:
                if media_server is not None:
                    raise media_server.interpret_http_error(ex, str(target)) from ex
                raise
            finally:
                stream_file.flush()
            if is_output_closed is True:
                console.print('Stopped receiving because the output was closed.')
                return
            console.print(f'Received {written} bytes.')

    _execute(main())


@app.command()
def move(
    output: Annotated[Path, typer.Argument(help='Output file')],
    url: Annotated[
        str | None, typer.Option('--url', help='Resource URL (select a recorder and recording if omitted)')
    ] = None,
    server: ServerOption = None,
    object_id: Annotated[str | None, typer.Option('--id', help='Recording ID')] = None,
    resource_index: Annotated[
        int | None, typer.Option('--resource', min=1, help='Resource number shown by info (default: stored recording)')
    ] = None,
    is_confirmed: Annotated[
        bool, typer.Option('--yes', '-y', help='Skip confirmation before consuming a copy')
    ] = False,
    credentials: CredentialsOption = None,
    verbose: VerboseOption = False,
) -> None:
    """
    Save a complete recording with GET Move, consuming one copy on the source.
    If only the final Move remains or copying is prohibited, the source recording is deleted permanently.
    """

    _setup_logging(verbose)
    if url is not None and resource_index is not None:
        raise typer.BadParameter('Use --resource with a recorder and recording, or supply a URL, separately.')
    device_credentials = _load_credentials(credentials)
    resume_path = resume_state_path(output)
    if existing_move_outputs(output):
        console.print(
            f'[red]The output, metadata sidecar, or previous Move resume state already exists: {output}[/red]'
        )
        raise typer.Exit(1)

    async def main() -> None:
        media_server: MediaServer | None = None
        async with (
            DLNAClient() as dlna,
            DTCPClient(
                device_credentials, srm=_device_store.load_srm(), on_srm_update=_device_store.save_srm
            ) as client,
        ):
            target: Resource | str
            recording: Recording | None = None
            if url is not None:
                target = url
            else:
                media_server = await _select_server(dlna, server)
                recording = await _select_recording(media_server, object_id)
                if is_broadcast(recording) is True:
                    console.print(f'[red]{recording.title} is a live channel and cannot be moved.[/red]')
                    raise typer.Exit(1)
                target = _choose_resource(recording, for_move=True, resource_index=resource_index)

            # Show the recording and copy count with its acquisition status before asking for confirmation
            remaining = None
            if recording is not None and isinstance(target, Resource):
                _print_recording(recording, media_server.description.extensions if media_server is not None else None)
                console.print(f'Selected resource: {recording.resources.index(target) + 1}')
                remaining = recording.copy_count_for(target)
                console.print(f'Copies remaining for selected resource: {_format_protection(remaining)}')
            else:
                console.print(
                    f'Recording: {target} (a URL was supplied, so the title and copy count were not retrieved)'
                )
            if remaining is not None and remaining.status == 'known' and remaining.value is not None:
                # The count excludes the final Move, so zero means only that Move remains
                if remaining.value == 0:
                    console.print(
                        '[bold red]Only the final Move remains. Moving will delete the source recording.[/bold red]'
                    )
            else:
                console.print(
                    '[bold yellow]The remaining copy count is unavailable. '
                    'Moving will delete the source recording if only the final Move remains or copying is prohibited.[/bold yellow]'
                )
            if is_confirmed is False and sys.stdin.isatty() is True:
                if (
                    Confirm.ask(
                        'Move this recording and consume one copy on the source?', console=console, default=False
                    )
                    is False
                ):
                    raise typer.Exit(1)

            # Query and persist optional metadata before the source can delete the recording
            sidecar: MetadataSidecar | None = None
            if recording is not None and media_server is not None:
                with console.status('Fetching recording metadata...'):
                    metadata, sidecar = await collect_move_metadata(media_server, recording, output)
                _print_additional_metadata(metadata)

            try:
                with _new_progress() as progress:
                    written = await run_move(
                        client,
                        target,
                        output,
                        media_server=media_server,
                        sidecar=sidecar,
                        on_event=ProgressReporter(progress, 'Move'),
                    )
            except MoveCommitmentRejectedError as ex:
                _print_commitment_rejected(ex, output)
                raise typer.Exit(1) from ex
            console.print(f'Move completed: {output} ({written} bytes)')

            # Query the source again and show when its copy count reflects the Move
            if media_server is not None and recording is not None and remaining is not None:
                await _requery_copy_count(
                    media_server,
                    recording,
                    remaining,
                    resource_url=target.url if isinstance(target, Resource) else None,
                )

    _execute(main(), resume_path=resume_path)


async def _requery_copy_count(
    media_server: MediaServer,
    recording: Recording,
    before: ProtectionValue[int],
    *,
    resource_url: str | None = None,
) -> None:
    """
    Query and display the source copy count alongside its value before the Move.
    DBR-Z620 updates the count about a minute after completion, so poll until it changes.

    Args:
        media_server (MediaServer): Source recorder
        recording (Recording): Moved recording
        before (ProtectionValue[int]): Copy count obtained before the Move
        resource_url (str | None): Selected resource URL, or None to query the default Move resource
    """

    if before.status != 'known':
        return
    with console.status('Checking the remaining copy count on the source...'):
        for _ in range(_COPY_COUNT_REQUERY_ATTEMPTS):
            await asyncio.sleep(_COPY_COUNT_REQUERY_INTERVAL)
            try:
                after = await query_copy_count(media_server, recording, resource_url)
            except DLNAError:
                # A recording with no copies remaining is deleted from the source and cannot be queried
                console.print('Could not query the source recording; it may have been deleted.')
                return
            if after is None:
                return
            if (after.status, after.value) != (before.status, before.value):
                console.print(f'Source copies remaining: {_format_protection(before)} -> {_format_protection(after)}')
                return
    console.print(f'The source still reports {_format_protection(before)} copies remaining; its update may be delayed.')


@app.command()
def resume(
    output: Annotated[Path, typer.Argument(help='Move output file')],
    credentials: CredentialsOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Resume finalization of a Move whose response was lost, without receiving the recording again."""

    _setup_logging(verbose)
    device_credentials = _load_credentials(credentials)
    resume_path = resume_state_path(output)
    if resume_path.exists() is False:
        console.print(f'[red]Move resume state not found: {resume_path}[/red]')
        raise typer.Exit(1)
    try:
        resume_state = load_resume_state(output)
    except (ValidationError, OSError) as ex:
        console.print(f'[red]Could not load Move resume state: {resume_path} ({ex})[/red]')
        raise typer.Exit(1) from ex
    sidecar = load_resume_sidecar(output)

    async def main() -> None:
        async with DTCPClient(
            device_credentials, srm=_device_store.load_srm(), on_srm_update=_device_store.save_srm
        ) as client:
            try:
                with _new_progress() as progress:
                    written = await run_resume(
                        client, output, resume_state, sidecar=sidecar, on_event=ProgressReporter(progress, 'Move')
                    )
            except MoveCommitmentRejectedError as ex:
                _print_commitment_rejected(ex, output)
                raise typer.Exit(1) from ex
        console.print(f'Move completed: {output} ({written} bytes)')

    _execute(main(), resume_path=resume_path)


@app.command()
def receive(
    directory: Annotated[Path, typer.Argument(help='Directory for received recordings and program metadata')],
    interface: Annotated[
        str, typer.Option(help='Local IPv4 address to listen on (defaults to all interfaces)')
    ] = '0.0.0.0',
    source: Annotated[
        list[str] | None, typer.Option(help='Restrict transfers to these recorder IPv4 addresses (repeatable)')
    ] = None,
    port: Annotated[int, typer.Option(help='HTTP listening port (TCP)')] = _DEFAULT_PORT,
    name: Annotated[str, typer.Option(help='Name shown in the recorder destination list')] = 'pydtcpip',
    credentials: CredentialsOption = None,
    verbose: VerboseOption = False,
) -> None:
    """
    Listen as a DTCP-IP NAS-like destination and save recordings sent by a recorder.
    Transfers consume a source copy; if only the final Move remains or copying is prohibited, the source recording is deleted.
    """

    _setup_logging(verbose)
    device_credentials = _load_credentials(credentials)
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    sidecars: dict[str, MetadataSidecar] = {}

    def create_sink(recording: Recording) -> FileSink:
        """
        Save the incoming metadata and prepare a media file.

        Args:
            recording (Recording): Metadata supplied by the sender

        Returns:
            FileSink: Media destination
        """

        # Incoming transfers provide their original CreateObject metadata, but no discoverable source endpoint
        output = _dubbing_output_path(directory, recording)
        sink = FileSink(output)
        # Preserve metadata for completed media and for full data retained for finalization recovery
        if output.exists() is True or sink.pending_path.exists() is True:
            raise FileExistsError(f'Received recording data already exists beside {output}.')
        # The sender can retry the same registered upload after its incomplete media has been discarded
        sidecar = MetadataSidecar.resume(output)
        if sidecar is None:
            sidecar = MetadataSidecar(
                output,
                RecordingMetadata(
                    recording=recording,
                    profile='generic',
                    profile_unverified=False,
                    observed_at=datetime.now(UTC),
                    queries=(MetadataQuery(name='source_details', status='not_observed'),),
                ),
            )
        elif sidecar.metadata.recording.id != recording.id:
            raise FileExistsError(f'Metadata for a different recording already exists beside {output}.')
        sidecars[recording.id] = sidecar
        _print_recording(recording)
        console.print(f'Incoming transfer accepted: {recording.title} -> {output}')
        return sink

    def save_resume_state(recording: Recording, state: MoveResumeState | None) -> None:
        # Save resume state beside the output, as in move, so `pydtcpip resume <output>` can resume finalization
        save_move_resume_state(resume_state_path(_dubbing_output_path(directory, recording)), state)

    progress = _new_progress()
    reporter = ProgressReporter(progress, 'Receiving')

    def on_event(event: TransferEvent) -> None:
        """
        Append transfer metadata and update the receiver's progress display.

        Args:
            event (TransferEvent): Incoming transfer event
        """

        sidecar = sidecars.get(event.transfer_id)
        if sidecar is not None:
            sidecar.on_event(event)
            if isinstance(event, (MoveCompleted, MoveCancelled, MoveFinalizeRejected, MoveResumeRequired)):
                sidecars.pop(event.transfer_id)
        reporter(event)

    async def main() -> None:
        receiver = DubbingReceiver(
            device_credentials,
            create_sink,
            interface=interface,
            allowed_sources=frozenset(source) if source is not None else None,
            port=port,
            name=name,
            srm=_device_store.load_srm(),
            on_srm_update=_device_store.save_srm,
            on_resume_state=save_resume_state,
            on_event=on_event,
        )
        await receiver.start()
        console.print('Listening. Select this destination on the recorder. Press Ctrl+C to stop.')
        try:
            await asyncio.Event().wait()
        finally:
            await receiver.stop()

    try:
        with progress:
            _execute(main())
    except KeyboardInterrupt:
        console.print('Stopped listening.')


@app.command()
def dump(
    output_directory: Annotated[Path, typer.Argument(help='Output directory')],
    server: ServerOption = None,
    max_recordings: Annotated[int, typer.Option(help='Maximum number of recordings to dump')] = 5,
    should_mask: Annotated[
        bool,
        typer.Option(
            '--mask-private',
            help='Mask identifiers and program information, including UDNs, addresses, titles, and broadcast times',
        ),
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """
    Dump recorder responses (device description, service definitions, listings, and media HEAD responses).
    Use --mask-private before sharing the output to mask identifiers and program content.
    This requires no credentials and consumes no copies.
    """

    _setup_logging(verbose)

    async def main() -> None:
        async with aiohttp.ClientSession() as http_session:
            dlna = DLNAClient(http_session=http_session)
            media_server = await _select_server(dlna, server)
            files = await dump_media_server(
                media_server,
                output_directory,
                http_session=http_session,
                max_recordings=max_recordings,
                should_mask=should_mask,
            )
        for file in files:
            console.print(f'{file.path} ({file.description})')
        if should_mask is True:
            console.print('[yellow]Check the output for unmasked information before sharing it.[/yellow]')
        else:
            console.print(
                '[yellow]Device responses were saved without masking. Use --mask-private before sharing them.[/yellow]'
            )

    _execute(main())


if __name__ == '__main__':
    app()
