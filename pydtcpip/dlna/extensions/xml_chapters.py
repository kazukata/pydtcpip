"""Chapter documents advertised through metadata URLs and ContentDirectory actions."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from xml.etree import ElementTree as ET

from pydtcpip.dlna.extensions.base import MetadataExtension
from pydtcpip.dlna.extensions.sony import SonyExtension
from pydtcpip.errors import DLNAError
from pydtcpip.models import Chapter, ChapterList, Container, MetadataQuery, Recording


type ChapterFormat = Literal['sony', 'panasonic', 'sharp', 'toshiba', 'shared_tags']


def parse_chapters(raw_xml: bytes | str, format: ChapterFormat) -> tuple[Chapter, ...]:
    """
    Parse device-provided points without adding synthetic start or end boundaries.

    Args:
        raw_xml (bytes | str): Chapter document
        format (ChapterFormat): SOAP format or URL reference hint; recognizable URL documents take precedence

    Returns:
        tuple[Chapter, ...]: Chapter points in document order
    """

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as ex:
        raise DLNAError('Could not parse the chapter document XML.') from ex
    # A server may retain another recorder's chapter reference while serving a different XML format
    # SOAP actions have their own fixed response structures
    if format in ('sony', 'panasonic', 'sharp'):
        if root.find('{urn:schemas-sony-com:av}content_chapter_info') is not None:
            format = 'sony'
        elif root.find('chapterList') is not None:
            format = 'panasonic'
        elif root.tag == 'chapterList' or root.find('chapter') is not None:
            format = 'sharp'
    namespace = root.tag.partition('}')[0] + '}' if root.tag.startswith('{') else ''
    container: ET.Element | None = root
    point_name: str | None = None
    if format == 'sony':
        namespace = '{urn:schemas-sony-com:av}'
        container = root.find(f'{namespace}content_chapter_info')
        path = f'{namespace}chapter'
        point_name = f'{namespace}chapter_point'
    elif format == 'panasonic':
        container = root.find('chapterList')
        path = 'item'
        point_name = 'timeCode'
    elif format == 'shared_tags':
        container = root.find(f'{namespace}item/{namespace}tagList')
        path = f'{namespace}tag'
        point_name = f'{namespace}posTime'
    else:
        path = f'{namespace}Chapter' if format == 'toshiba' else 'chapter'
    if container is None:
        raise DLNAError(f'The {format} chapter document is missing its chapter container.')
    chapters: list[Chapter] = []
    for point in container.iterfind(path):
        if point_name is not None:
            child = point.find(point_name)
            if child is None:
                raise DLNAError(f'The {format} chapter entry is missing its time code.')
            point = child
        value = (point.text or '').strip()
        try:
            if format == 'sony':
                milliseconds = Decimal(value) * 1000
            else:
                # Time codes may include an ISO calendar date; the chapter position uses the time component
                value = value.rsplit('T', 1)[-1]
                hours, minutes, seconds = value.split(':')
                milliseconds = (int(hours) * 3600 + int(minutes) * 60 + Decimal(seconds)) * 1000
            if milliseconds.is_finite() is False or milliseconds < 0:
                raise ValueError('Invalid chapter position')
            chapters.append(Chapter(position_ms=int(milliseconds)))
        except (ValueError, InvalidOperation) as ex:
            raise DLNAError(f'The {format} chapter document contains an invalid time code.') from ex
    return tuple(chapters)


class SOAPChapterExtension(MetadataExtension):
    """Chapter retrieval through an advertised ContentDirectory action."""

    action: str
    format: ChapterFormat

    async def get_chapters(self, recording: Recording) -> ChapterList:
        """
        Fetch chapter metadata using the original DIDL object ID.

        Args:
            recording (Recording): Recording to query

        Returns:
            ChapterList: Retrieved points, or unsupported if the action is not advertised
        """

        if recording.chapter_resources:
            return await super().get_chapters(recording)
        service = self.server.content_directory
        description = await self.server.get_service_description(service)
        if self.action not in description.actions:
            return ChapterList(status='unsupported', observed_at=description.observed_at)
        arguments = {'ObjectID': recording.id}
        if self.format == 'shared_tags':
            arguments.update({'TagIDs': '', 'Filter': '', 'Type': ''})
        result = await self.server.call_action(service, self.action, arguments)
        raw_xml = result.arguments.get('Result')
        if raw_xml is None:
            raise DLNAError(f'{self.action} returned no Result argument.')
        return ChapterList(
            status='known',
            chapters=parse_chapters(raw_xml, self.format),
            sources=(f'{service.control_url}#{self.action}',),
            raw_responses=(result.raw_xml.encode('utf-8'),),
            observed_at=datetime.now(UTC),
        )


class ToshibaExtension(SOAPChapterExtension):
    """Toshiba's chapter-list action and USB drive names from recording folders."""

    action = 'X_Toshiba_GetChapterList'
    format: ChapterFormat = 'toshiba'

    async def get_details(self, recording: Recording) -> tuple[MetadataQuery, ...]:
        """
        Read a USB drive name from the recording's parent folders.

        Args:
            recording (Recording): Recording whose folder hierarchy identifies its USB drive

        Returns:
            tuple[MetadataQuery, ...]: Storage name and source folder, or an omitted result
        """

        parent_id = recording.parent_id
        visited = {'0', '-1', ''}
        while parent_id not in visited:
            visited.add(parent_id)
            parent = await self.server.get_metadata(parent_id)
            if isinstance(parent, Container) is False:
                break
            # REGZA Blu-ray recorders advertise USB folders as USB1:<drive name>
            match = re.fullmatch(r'(?:usb|ｕｓｂ)[0-9０-９]+[:：](.+)', parent.title, flags=re.IGNORECASE)
            if match is not None:
                return (
                    MetadataQuery(
                        name='storage',
                        status='known',
                        data={'name': match[1], 'container_id': parent.id, 'container_title': parent.title},
                    ),
                )
            parent_id = parent.parent_id
        return (MetadataQuery(name='storage', status='omitted'),)


class SharedTagsExtension(SOAPChapterExtension):
    """REC-ON and compatible set-top box chapter tags."""

    action = 'X_TShrGetTags'
    format: ChapterFormat = 'shared_tags'

    async def get_details(self, recording: Recording) -> tuple[MetadataQuery, ...]:
        """
        Query recording details when the box advertises a compatible title-list service.

        Args:
            recording (Recording): Recording selected through ContentDirectory

        Returns:
            tuple[MetadataQuery, ...]: Title-list observations from the advertised service
        """

        return await SonyExtension(self.server).get_details(recording)
