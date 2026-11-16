"""Read-only recording and storage metadata from Sony-compatible UPnP services."""

from base64 import b64encode
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict

from pydtcpip.dlna.description import ServiceInfo
from pydtcpip.dlna.extensions.base import MetadataExtension
from pydtcpip.errors import DLNAError, UnsupportedActionError
from pydtcpip.models import MetadataQuery, MetadataValue, Recording


_PVR_CONTROL = 'urn:schemas-s-bras-org:service:X_PvrControl:'
_SCHEDULED_RECORDING = 'urn:schemas-xsrs-org:service:X_ScheduledRecording:'


class XMLMetadataItem(BaseModel):
    """An item in an additional metadata response, with original field names and attributes."""

    model_config = ConfigDict(frozen=True)

    id: str | None
    fields: tuple[MetadataValue, ...]
    raw_xml: str

    def get_value(self, name: str) -> str | None:
        """
        Read the first matching field by its local or expanded XML name.

        Args:
            name (str): Field name, such as recordStartDate or startPTS

        Returns:
            str | None: Original text, or None when omitted
        """

        for field in self.fields:
            if field.name == name or field.name.rpartition('}')[2] == name:
                return field.value
        return None


class SonyMetadataResult(BaseModel):
    """Additional recording or storage metadata, separate from ContentDirectory observations."""

    model_config = ConfigDict(frozen=True)

    items: tuple[XMLMetadataItem, ...]
    arguments: dict[str, str]
    source: str
    observed_at: datetime
    raw_xml: str


class SonyExtension(MetadataExtension):
    """Optional title-list, actual-start and storage queries for advertised services."""

    async def _find_action(self, candidates: tuple[tuple[str, str], ...]) -> tuple[ServiceInfo, str]:
        """
        Select an available action by the service's advertised capabilities.

        Args:
            candidates (tuple[tuple[str, str], ...]): Service prefixes and action names in preference order

        Returns:
            tuple[ServiceInfo, str]: Service and action to invoke
        """

        for prefix, action in candidates:
            service = self.server.description.find_service(prefix)
            if service is not None:
                description = await self.server.get_service_description(service)
                if action in description.actions:
                    return service, action
        raise UnsupportedActionError('The device does not advertise the requested recording metadata action.')

    async def get_details(self, recording: Recording) -> tuple[MetadataQuery, ...]:
        """
        Retrieve the selected title's details, actual start metadata and storage information.

        Args:
            recording (Recording): Recording selected through ContentDirectory

        Returns:
            tuple[MetadataQuery, ...]: Independently reported results for available metadata queries
        """

        identifier = recording.reference_id or recording.id
        is_usb = identifier.startswith('USBV_')
        start = 0
        queries: list[MetadataQuery] = []
        title: XMLMetadataItem | None = None
        while title is None:
            result = await self.get_title_list(start=start, is_usb=is_usb)
            for item in result.items:
                if item.id == identifier:
                    title = item
                    break
                # Sony title-list IDs encode the numeric DIDL suffix as hexadecimal after a ten-character prefix
                if item.id is not None and identifier.startswith(('V_', 'USBV_')):
                    try:
                        suffix = int(item.id[10:], 16)
                    except ValueError:
                        continue
                    if identifier == f'{"USBV" if is_usb else "V"}_{suffix}':
                        title = item
                        break
            if title is not None:
                queries.append(
                    MetadataQuery(
                        name='recording_details',
                        status='known',
                        data={
                            'recording': title.model_dump(mode='json'),
                            'response': result.model_dump(mode='json'),
                        },
                    )
                )
                break
            try:
                returned = int(result.arguments['NumberReturned'])
                total = int(result.arguments['TotalMatches'])
            except (KeyError, ValueError) as ex:
                raise DLNAError('The additional recording list has invalid pagination metadata.') from ex
            start += returned
            if returned == 0 or start >= total:
                return (MetadataQuery(name='recording_details', status='omitted'),)
        if title.id is not None:
            try:
                detail = await self.get_title_detail(title.id)
                queries.append(
                    MetadataQuery(name='recording_description', status='known', data=detail.model_dump(mode='json'))
                )
            except UnsupportedActionError:
                queries.append(MetadataQuery(name='recording_description', status='unsupported'))
            except DLNAError as ex:
                queries.append(MetadataQuery(name='recording_description', status='failed', error=str(ex)))
            try:
                info = await self.get_title_info(title.id)
                queries.append(MetadataQuery(name='recording_start', status='known', data=info.model_dump(mode='json')))
            except UnsupportedActionError:
                queries.append(MetadataQuery(name='recording_start', status='unsupported'))
            except DLNAError as ex:
                queries.append(MetadataQuery(name='recording_start', status='failed', error=str(ex)))
        destination = title.get_value('recordDestinationID')
        if destination is not None:
            try:
                storage = await self.get_media_info(destination)
                queries.append(MetadataQuery(name='storage', status='known', data=storage.model_dump(mode='json')))
            except UnsupportedActionError:
                queries.append(MetadataQuery(name='storage', status='unsupported'))
            except DLNAError as ex:
                queries.append(MetadataQuery(name='storage', status='failed', error=str(ex)))
        return tuple(queries)

    async def _query(self, service: ServiceInfo, action: str, arguments: dict[str, str]) -> SonyMetadataResult:
        """
        Invoke a metadata action and parse the XML Result while retaining the full response.

        Args:
            service (ServiceInfo): Advertised service
            action (str): Read-only metadata action
            arguments (dict[str, str]): Ordered input arguments

        Returns:
            SonyMetadataResult: Items, original arguments and response XML
        """

        headers: dict[str, str] = {}
        if service.service_type.startswith(_SCHEDULED_RECORDING):
            client_name = 'pydtcpip'
            encoded_name = b64encode(client_name.encode('utf-8')).decode('ascii')
            headers['X-AV-Physical-Unit-Info'] = f'pa="{client_name}"; pl={encoded_name};'
        result = await self.server.call_action(service, action, arguments, extra_headers=headers)
        raw_result = result.arguments.get('Result')
        if raw_result is None:
            raise DLNAError(f'{action} returned no Result argument.')
        try:
            if action in ('X_GetTitleDetail', 'X_GetTitleInfo'):
                # Title fields may be embedded in Result or escaped as an XML fragment
                envelope = ET.fromstring(result.raw_xml)
                body = envelope.find('{http://schemas.xmlsoap.org/soap/envelope/}Body')
                assert body is not None  # Validated by call_action
                root = next(child for child in body[0] if child.tag.rpartition('}')[2] == 'Result')
                if len(root) == 0:
                    try:
                        root = ET.fromstring(raw_result)
                    except ET.ParseError:
                        root = ET.fromstring(f'<Result>{raw_result}</Result>')
            else:
                root = ET.fromstring(raw_result)
        except ET.ParseError as ex:
            raise DLNAError(f'Could not parse the {action} metadata XML.') from ex
        elements: list[ET.Element] = []
        for element in root.iter():
            if element.tag.rpartition('}')[2] == 'item':
                elements.append(element)
        # Detail and storage responses contain one document rather than a list of item elements
        if len(elements) == 0 and action not in ('X_GetTitleList', 'X_GetTitleList2'):
            elements.append(root)
        items: list[XMLMetadataItem] = []
        for element in elements:
            fields = tuple(
                MetadataValue(name=child.tag, value=child.text or '', attributes=dict(child.attrib))
                for child in element
            )
            items.append(
                XMLMetadataItem(
                    id=element.get('id'),
                    fields=fields,
                    raw_xml=ET.tostring(element, encoding='unicode'),
                )
            )
        return SonyMetadataResult(
            items=tuple(items),
            arguments=result.arguments,
            source=f'{service.control_url}#{action}',
            observed_at=datetime.now(UTC),
            raw_xml=result.raw_xml,
        )

    async def get_title_list(self, *, start: int = 0, count: int = 100, is_usb: bool = False) -> SonyMetadataResult:
        """
        Fetch recording details using the newest advertised title-list action.

        Args:
            start (int): Starting index
            count (int): Requested item count
            is_usb (bool): Query recordings on USB storage

        Returns:
            SonyMetadataResult: Recording details with pagination arguments
        """

        service, action = await self._find_action(
            (
                (_PVR_CONTROL, 'X_GetTitleList2'),
                (_SCHEDULED_RECORDING, 'X_GetTitleList'),
            )
        )
        return await self._query(
            service,
            action,
            {
                'SearchCriteria': 'recordDestinationID="USBHDD"' if is_usb is True else '',
                'Filter': '*',
                'StartingIndex': str(start),
                'RequestedCount': str(count),
                'SortCriteria': '-scheduledStartDateTime',
            },
        )

    async def get_title_detail(self, title_id: str) -> SonyMetadataResult:
        """
        Retrieve the program summary and detailed description sections separately from the title list.
        Original summary, detail0 through detail7 and size values remain independent fields.

        Args:
            title_id (str): Title ID returned by get_title_list, not a ContentDirectory object ID

        Returns:
            SonyMetadataResult: Description fields with their original representation
        """

        service, action = await self._find_action(((_PVR_CONTROL, 'X_GetTitleDetail'),))
        return await self._query(service, action, {'Id': title_id})

    async def get_title_info(self, title_id: str) -> SonyMetadataResult:
        """
        Retrieve actual recording start metadata, including recordStartDate and startPTS.

        Args:
            title_id (str): Title ID returned by get_title_list, not a ContentDirectory object ID

        Returns:
            SonyMetadataResult: Original start metadata without timezone or PTS conversion
        """

        service, action = await self._find_action(((_PVR_CONTROL, 'X_GetTitleInfo'),))
        return await self._query(service, action, {'TitleID': title_id})

    async def get_media_info(self, destination_id: str = 'HDD') -> SonyMetadataResult:
        """
        Retrieve storage information without changing the recording destination.

        Args:
            destination_id (str): Advertised recordDestinationID, such as HDD or USBHDD

        Returns:
            SonyMetadataResult: Storage fields with their original units and representation
        """

        service, action = await self._find_action(
            (
                (_PVR_CONTROL, 'X_GetMediaInfo'),
                (_SCHEDULED_RECORDING, 'X_GetMediaInfo'),
            )
        )
        return await self._query(service, action, {'recordDestinationID': destination_id})
