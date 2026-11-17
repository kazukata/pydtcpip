"""Discover DLNA media servers on a home network and browse recordings."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from urllib.parse import urljoin

import aiohttp
from pydantic import BaseModel, ConfigDict

from pydtcpip.dlna import ssdp
from pydtcpip.dlna.description import DeviceDescription, ServiceInfo, parse_device_description
from pydtcpip.dlna.didl import parse_didl
from pydtcpip.dlna.extensions import MetadataExtension, create_extension
from pydtcpip.dlna.profiles import Profile, select_profile
from pydtcpip.dlna.scpd import ServiceDescription, parse_service_description
from pydtcpip.dlna.soap import SOAPResult, call_action
from pydtcpip.errors import DLNAError, UnsupportedActionError
from pydtcpip.models import ChapterList, Container, MetadataQuery, MetadataResponse, Recording, RecordingMetadata


_LOGGER = logging.getLogger(__name__)

_CONTENT_DIRECTORY = 'urn:schemas-upnp-org:service:ContentDirectory:'

# Number of entries requested per Browse when fetching all pages
## Device limits vary and some return fewer than requested, so advance by the actual count
_BROWSE_PAGE_SIZE = 100


class BrowseResult(BaseModel):
    """Result of one Browse request."""

    model_config = ConfigDict(frozen=True)

    objects: tuple[Container | Recording, ...]
    number_returned: int
    total_matches: int
    update_id: int | None
    raw_didl: str


class MediaServer:
    """
    A discovered DLNA media server.
    A profile selected from its device description interprets family-specific behavior.
    """

    def __init__(self, description: DeviceDescription, *, http_session: aiohttp.ClientSession, timeout: float) -> None:
        """
        Configure the device description and HTTP session.

        Args:
            description (DeviceDescription): Media server device description
            http_session (aiohttp.ClientSession): HTTP session shared with DLNAClient
            timeout (float): Response timeout in seconds
        """

        self.description = description
        self.profile: Profile = select_profile(description)
        self._http_session = http_session
        self._timeout = timeout
        self.extension: MetadataExtension = create_extension(self)
        self._service_descriptions: dict[str, ServiceDescription] = {}
        self._metadata_responses: ContextVar[list[MetadataResponse] | None] = ContextVar(
            'metadata_responses', default=None
        )

    @property
    def content_directory(self) -> ServiceInfo:
        """Return the ContentDirectory service that provides recording listings."""

        service = self.description.find_service(_CONTENT_DIRECTORY)
        if service is None:
            raise DLNAError(f'{self.description.friendly_name} does not provide a ContentDirectory service.')
        return service

    async def browse(self, object_id: str = '0', *, start: int = 0, count: int = 0) -> BrowseResult:
        """
        Fetch a page of a folder's immediate children.

        Args:
            object_id (str): Folder ID (`0` for root)
            start (int): Starting index
            count (int): Number to fetch (0 uses the device limit)

        Returns:
            BrowseResult: Retrieved entries and total count
        """

        result = await self._call_content_directory(
            'Browse',
            {
                'ObjectID': object_id,
                'BrowseFlag': 'BrowseDirectChildren',
                'Filter': '*',
                'StartingIndex': str(start),
                'RequestedCount': str(count),
                'SortCriteria': '',
            },
        )
        return self._browse_result(result)

    async def browse_all(self, object_id: str = '0') -> list[Container | Recording]:
        """
        Fetch all pages of a folder's immediate children.

        Args:
            object_id (str): Folder ID (`0` for root)

        Returns:
            list[Container | Recording]: Folders and recordings
        """

        objects: list[Container | Recording] = []
        while True:
            result = await self.browse(object_id, start=len(objects), count=_BROWSE_PAGE_SIZE)
            objects.extend(result.objects)

            # Devices may return fewer entries than requested, so advance by the actual count
            if result.number_returned == 0 or len(objects) >= result.total_matches:
                break
        return objects

    async def get_metadata(self, object_id: str) -> Container | Recording:
        """
        Fetch one folder or recording by ID.

        Args:
            object_id (str): Folder or recording ID

        Returns:
            Container | Recording: Retrieved item
        """

        browse_result = await self.browse_metadata(object_id)
        if len(browse_result.objects) != 1:
            raise DLNAError(f'Could not retrieve the item with ID {object_id}.')
        return browse_result.objects[0]

    async def iter_recordings(self, object_id: str | None = None) -> AsyncIterator[Recording]:
        """
        Recursively enumerate video items, retaining references and device-provided metadata.
        Explicit object IDs override the family's dedicated recording containers.

        Args:
            object_id (str | None): Starting container, or the profile's recording roots

        Yields:
            Recording: Video item, including live broadcasts advertised by the device
        """

        pending = list(self.profile.resolve_recording_roots()) if object_id is None else [object_id]
        visited: set[str] = set()
        recordings: set[str] = set()
        while pending:
            container_id = pending.pop(0)
            if container_id in visited:
                continue
            visited.add(container_id)
            for item in await self.browse_all(container_id):
                if isinstance(item, Container):
                    pending.append(item.id)
                elif item.upnp_class.startswith('object.item.videoItem'):
                    # Reference IDs identify duplicates in genre and all-recordings folders
                    identity = item.reference_id or item.id
                    if identity not in recordings:
                        recordings.add(identity)
                        yield item

    async def get_chapters(self, recording: Recording) -> ChapterList:
        """
        Explicitly query optional chapter information through the family extension.

        Args:
            recording (Recording): Recording to query

        Returns:
            ChapterList: Device-provided chapter points with source responses
        """

        return await self.extension.get_chapters(recording)

    async def collect_metadata(self, recording: Recording, *, additional: bool = True) -> RecordingMetadata:
        """
        Collect metadata for display or archival before a recording leaves its source.
        Optional query failures are retained in the result so they cannot erase available metadata.

        Args:
            recording (Recording): Selected recording and its listing metadata
            additional (bool): Fetch chapters and family-specific details

        Returns:
            RecordingMetadata: Available metadata, source responses and explicit query results
        """

        queries: list[MetadataQuery] = []
        chapters = ChapterList(status='not_observed')
        # Capture responses before parsing and keep simultaneous collections on this connection separate
        responses: list[MetadataResponse] = []
        token = self._metadata_responses.set(responses)
        try:
            if additional is True:
                try:
                    chapters = await self.get_chapters(recording)
                    queries.append(MetadataQuery(name='chapters', status=chapters.status))
                except DLNAError as ex:
                    queries.append(MetadataQuery(name='chapters', status='failed', error=str(ex)))
                try:
                    queries.extend(await self.extension.get_details(recording))
                except UnsupportedActionError:
                    queries.append(MetadataQuery(name='recording_details', status='unsupported'))
                except DLNAError as ex:
                    queries.append(MetadataQuery(name='recording_details', status='failed', error=str(ex)))
            else:
                queries.extend(
                    (
                        MetadataQuery(name='chapters', status='not_observed'),
                        MetadataQuery(name='recording_details', status='not_observed'),
                    )
                )
        finally:
            self._metadata_responses.reset(token)
        return RecordingMetadata(
            recording=recording,
            profile=self.profile.name,
            profile_unverified=self.profile.is_unverified,
            observed_at=datetime.now(UTC),
            device_description_xml=self.description.raw_xml,
            device_location=self.description.location,
            device_declarations=self.description.extensions,
            chapters=chapters,
            queries=tuple(queries),
            service_descriptions={url: description.raw_xml for url, description in self._service_descriptions.items()},
            responses=tuple(responses),
        )

    async def fetch_metadata(self, url: str) -> MetadataResponse:
        """
        Fetch an optional metadata document using the shared HTTP session.

        Args:
            url (str): Absolute URL or reference relative to the device description

        Returns:
            MetadataResponse: Original body, response headers and observation time
        """

        url = urljoin(self.description.location, url)
        try:
            async with self._http_session.get(url, timeout=aiohttp.ClientTimeout(total=self._timeout)) as response:
                metadata_response = MetadataResponse(
                    url=str(response.url),
                    headers=dict(response.headers),
                    body=await response.read(),
                    observed_at=datetime.now(UTC),
                    status_code=response.status,
                )
                responses = self._metadata_responses.get()
                if responses is not None:
                    responses.append(metadata_response)
                if response.status >= 400:
                    raise DLNAError(f'Metadata request failed (HTTP {response.status}).', http_status=response.status)
                return metadata_response
        except DLNAError as ex:
            raise self.interpret_http_error(ex, url) from ex
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise DLNAError(f'Metadata request failed ({ex}).') from ex

    async def get_service_description(self, service: ServiceInfo, *, refresh: bool = False) -> ServiceDescription:
        """
        Query the service's advertised actions, caching successful responses in this connection.

        Args:
            service (ServiceInfo): Service to inspect
            refresh (bool): Fetch a new response even if a description is cached

        Returns:
            ServiceDescription: Actions, ordered arguments and original SCPD XML
        """

        if refresh is False and service.scpd_url in self._service_descriptions:
            return self._service_descriptions[service.scpd_url]
        response = await self.fetch_metadata(service.scpd_url)
        # Preserve the original XML text, including comments, rather than serializing a parsed tree
        try:
            if response.body.startswith((b'\xff\xfe', b'\xfe\xff')):
                raw_xml = response.body.decode('utf-16')
            else:
                declaration = re.match(rb'<\?xml\s+[^?]*\bencoding\s*=\s*[\'\"]([^\'\"]+)[\'\"]', response.body)
                encoding = declaration[1].decode('ascii') if declaration is not None else 'utf-8-sig'
                raw_xml = response.body.decode(encoding)
        except (UnicodeError, LookupError) as ex:
            raise DLNAError('Could not decode the service description XML.') from ex
        description = parse_service_description(service, raw_xml)
        self._service_descriptions[service.scpd_url] = description
        return description

    async def call_action(
        self,
        service: ServiceInfo,
        action: str,
        arguments: dict[str, str],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> SOAPResult:
        """
        Invoke an explicitly selected UPnP action through this server's connection.

        Args:
            service (ServiceInfo): Service with its advertised endpoint and namespace
            action (str): Action name
            arguments (dict[str, str]): Input arguments in service-defined order
            extra_headers (dict[str, str] | None): Optional extension-specific request headers

        Returns:
            SOAPResult: Output arguments and original response
        """

        responses = self._metadata_responses.get()
        try:
            return await call_action(
                self._http_session,
                service.control_url,
                service.service_type,
                action,
                arguments,
                timeout=self._timeout,
                reuse_connection=self.profile.allows_soap_connection_reuse(),
                extra_headers=extra_headers,
                on_response=responses.append if responses is not None else None,
            )
        except DLNAError as ex:
            raise self.interpret_http_error(ex, service.control_url) from ex

    async def browse_metadata(self, object_id: str) -> BrowseResult:
        """
        Fetch a BrowseMetadata response by ID.
        Use this to retain the response's DIDL-Lite exactly as returned by the device.

        Args:
            object_id (str): Folder or recording ID

        Returns:
            BrowseResult: Retrieved item and response DIDL-Lite
        """

        result = await self._call_content_directory(
            'Browse',
            {
                'ObjectID': object_id,
                'BrowseFlag': 'BrowseMetadata',
                'Filter': '*',
                'StartingIndex': '0',
                'RequestedCount': '0',
                'SortCriteria': '',
            },
        )
        return self._browse_result(result)

    def interpret_http_error(self, error: DLNAError, url: str) -> DLNAError:
        """
        Interpret a media or listing HTTP error according to the recorder family.

        Args:
            error (DLNAError): Exception containing an HTTP status
            url (str): Failed request URL

        Returns:
            DLNAError: Family-specific exception, or the original if no interpretation applies
        """

        if error.http_status is None:
            return error
        interpreted = self.profile.resolve_http_status()(error.http_status, url)
        if isinstance(interpreted, DLNAError):
            return type(interpreted)(
                str(interpreted), http_status=error.http_status, diagnostic_headers=error.diagnostic_headers
            )
        return error

    async def _call_content_directory(self, action: str, arguments: dict[str, str]) -> SOAPResult:
        """
        Invoke a ContentDirectory action and interpret device-specific HTTP errors.

        Args:
            action (str): Action name
            arguments (dict[str, str]): Input argument names and values

        Returns:
            SOAPResult: Action response and output arguments
        """

        return await self.call_action(self.content_directory, action, arguments)

    def _browse_result(self, result: SOAPResult) -> BrowseResult:
        """
        Convert a Browse response into parsed objects and pagination metadata.

        Args:
            result (SOAPResult): ContentDirectory Browse response

        Returns:
            BrowseResult: Parsed objects and original DIDL-Lite
        """

        raw_didl = result.arguments.get('Result', '')
        number_returned = result.arguments.get('NumberReturned', '0')
        total_matches = result.arguments.get('TotalMatches', '0')
        update_id = result.arguments.get('UpdateID')
        objects = parse_didl(raw_didl, self.profile) if raw_didl != '' else []
        return BrowseResult(
            objects=tuple(objects),
            number_returned=int(number_returned) if number_returned.isdecimal() else len(objects),
            total_matches=int(total_matches) if total_matches.isdecimal() else len(objects),
            update_id=int(update_id) if update_id is not None and update_id.isdecimal() else None,
            raw_didl=raw_didl,
        )


class DLNAClient:
    """Discover DLNA media servers on the home network and create MediaServer instances."""

    def __init__(self, *, timeout: float = 10.0, http_session: aiohttp.ClientSession | None = None) -> None:
        """
        Configure communication.

        Args:
            timeout (float): Device response timeout in seconds
            http_session (aiohttp.ClientSession | None): Shared HTTP session (created by this client if None)
        """

        self._timeout = timeout
        self._http_session = http_session
        self._owns_http_session = http_session is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session created by this client."""

        if self._owns_http_session is True and self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def discover(
        self,
        *,
        timeout: float = 3.0,
        interface: str | None = None,
        port: int = 0,
        include_announcements: bool = True,
    ) -> list[MediaServer]:
        """
        Discover media servers using SSDP and return those whose descriptions were retrieved.

        Args:
            timeout (float): Seconds to wait for discovery responses and announcements
            interface (str | None): Local IPv4 address for discovery (None searches every IPv4 network)
            port (int): UDP response port (0 lets the OS choose)
            include_announcements (bool): Also collect servers that announce themselves during discovery

        Returns:
            list[MediaServer]: Discovered media servers
        """

        announced: list[MediaServer] = []
        listener = None
        # An active search explicitly using UDP 1900 must keep that reply port
        if include_announcements and port != ssdp.SSDP_PORT:
            listener = asyncio.create_task(
                self.listen_for_servers(on_server=announced.append, timeout=timeout, interface=interface)
            )
        try:
            responses = await ssdp.search(timeout=timeout, interface=interface, port=port)
            searched = await asyncio.gather(*(self._get_discovered_server(response.location) for response in responses))
            if listener is not None and not any(searched) and not announced:
                await listener
        finally:
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        servers: list[MediaServer] = []
        seen_udns: set[str] = set()
        for server in [*searched, *announced]:
            if server is None:
                continue
            # A multi-homed server may advertise a different URL on each network
            ## Deduplicate after fetching so an unreachable URL cannot hide a reachable one
            udn = server.description.udn
            if udn and udn in seen_udns:
                continue
            seen_udns.add(udn)
            servers.append(server)
        return servers

    async def listen_for_servers(
        self,
        *,
        on_server: Callable[[MediaServer], None],
        timeout: float,
        interface: str | None = None,
    ) -> None:
        """
        Resolve announcements independently and report each reachable server as soon as it is available.
        Descriptions already being fetched can finish after the listening window closes.

        Args:
            on_server (Callable[[MediaServer], None]): Called for each resolved server
            timeout (float): Seconds to listen for announcements
            interface (str | None): Local IPv4 address (None listens on every IPv4 network)
        """

        seen_locations: set[str] = set()
        async with asyncio.TaskGroup() as tasks:

            async def resolve(location: str) -> None:
                """
                Report an announced server, or allow a later announcement to retry the location.

                Args:
                    location (str): Device description URL from the announcement
                """

                server = await self._get_discovered_server(location)
                if server is None:
                    seen_locations.remove(location)
                else:
                    on_server(server)

            def receive(announcement: ssdp.SSDPResponse) -> None:
                """
                Fetch the announcement's description once per location.

                Args:
                    announcement (ssdp.SSDPResponse): Media server availability announcement
                """

                if announcement.location not in seen_locations:
                    seen_locations.add(announcement.location)
                    tasks.create_task(resolve(announcement.location))

            async with ssdp.listen_for_advertisements(receive, interface=interface) as is_listening:
                if is_listening:
                    await asyncio.sleep(timeout)

    async def _get_discovered_server(self, location: str) -> MediaServer | None:
        """
        Fetch a discovered description without hiding other devices if this one is unreachable.

        Args:
            location (str): Discovered device description URL

        Returns:
            MediaServer | None: Reachable server, or None if its description could not be loaded
        """

        try:
            return await self.get_server(location)
        except DLNAError as ex:
            _LOGGER.warning(f'Could not fetch device description: {location}', exc_info=ex)
            return None

    async def get_server(self, location: str) -> MediaServer:
        """
        Create a media server from its device description URL.

        Args:
            location (str): Device description URL (SSDP LOCATION)

        Returns:
            MediaServer: Media server
        """

        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        try:
            async with self._http_session.get(location, timeout=aiohttp.ClientTimeout(total=self._timeout)) as response:
                if response.status >= 400:
                    raise DLNAError(
                        f'Could not fetch device description (HTTP {response.status}): {location}',
                        http_status=response.status,
                    )
                raw_xml = await response.text(encoding='utf-8', errors='replace')
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise DLNAError(f'Could not fetch device description ({ex}): {location}') from ex
        return MediaServer(
            parse_device_description(location, raw_xml), http_session=self._http_session, timeout=self._timeout
        )
