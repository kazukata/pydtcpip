"""Convert DIDL-Lite from ContentDirectory Browse into folder and recording models."""

from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from pydtcpip.dlna.arib import ARIB_PUA_TO_UNICODE
from pydtcpip.dlna.profiles import Profile
from pydtcpip.dlna.protocol_info import parse_protocol_info
from pydtcpip.errors import DLNAError
from pydtcpip.models import (
    Container,
    MetadataValue,
    ProtectionValue,
    Recording,
    RecordingProtection,
    ReportedCopyCount,
    Resource,
    ResourceConversion,
)


DIDL_NAMESPACE = 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/'
UPNP_NAMESPACE = 'urn:schemas-upnp-org:metadata-1-0/upnp/'
DC_NAMESPACE = 'http://purl.org/dc/elements/1.1/'
ARIB_NAMESPACE = 'urn:schemas-arib-or-jp:elements-1-0/'

# DTCP.COM_FLAGS bits (from the most significant: Move and transaction-based Move support)
_DTCP_FLAG_MOVE = 1 << 31
_DTCP_FLAG_TRANSACTION_MOVE = 1 << 30


def parse_didl(raw_xml: str, profile: Profile) -> list[Container | Recording]:
    """
    Parse DIDL-Lite and return folders and recordings in document order.
    The profile interprets recorder-specific elements. Human-readable metadata is normalized from
    ARIB private-use codes to standard Unicode, including the XML retained for sidecar files.

    Args:
        raw_xml (str): DIDL-Lite from the Browse Result
        profile (Profile): Recorder family profile

    Returns:
        list[Container | Recording]: Folders and recordings
    """

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as ex:
        raise DLNAError('Could not parse the recording listing (DIDL-Lite) XML.') from ex

    source_items = {element: ET.tostring(element, encoding='unicode') for element in root}
    observed_at = datetime.now(UTC)
    # Normalize text at the metadata boundary so API values and saved XML agree
    # Resource URLs, identifiers, protection values, and unknown extensions retain their original values
    text_tags = {
        f'{{{DC_NAMESPACE}}}title',
        f'{{{DC_NAMESPACE}}}description',
        f'{{{UPNP_NAMESPACE}}}longDescription',
        f'{{{ARIB_NAMESPACE}}}longDescription',
        f'{{{UPNP_NAMESPACE}}}channelName',
        f'{{{UPNP_NAMESPACE}}}genre',
    }
    for element in root.iter():
        if element.tag in text_tags and element.text is not None:
            element.text = element.text.translate(ARIB_PUA_TO_UNICODE)

    read_remaining_copy_count = profile.resolve_remaining_copy_count()
    objects: list[Container | Recording] = []
    for element in root:
        # Folders contain their child count and class
        if element.tag == f'{{{DIDL_NAMESPACE}}}container':
            child_count = element.get('childCount')
            objects.append(
                Container(
                    id=element.get('id', ''),
                    parent_id=element.get('parentID', ''),
                    title=element.findtext(f'{{{DC_NAMESPACE}}}title') or '',
                    upnp_class=element.findtext(f'{{{UPNP_NAMESPACE}}}class') or '',
                    child_count=int(child_count) if child_count is not None and child_count.isdecimal() else None,
                    raw_xml=ET.tostring(element, encoding='unicode'),
                )
            )

        # Recordings contain program metadata, resources, and protection information
        elif element.tag == f'{{{DIDL_NAMESPACE}}}item':
            resources = tuple(
                _parse_resource(res, element, profile) for res in element.iterfind(f'{{{DIDL_NAMESPACE}}}res')
            )
            metadata: dict[str, list[MetadataValue]] = {
                'ratings': [],
                'video_components': [],
                'audio_components': [],
                'broadcast_metadata': [],
                'icons': [],
                'vendor_metadata': [],
            }
            for child in element:
                category: str | None = None
                if child.tag == f'{{{UPNP_NAMESPACE}}}rating':
                    category = 'ratings'
                elif child.tag == f'{{{UPNP_NAMESPACE}}}icon':
                    category = 'icons'
                elif child.tag == f'{{{ARIB_NAMESPACE}}}videoComponentType':
                    category = 'video_components'
                elif child.tag == f'{{{ARIB_NAMESPACE}}}audioComponentType':
                    category = 'audio_components'
                elif child.tag.startswith(f'{{{ARIB_NAMESPACE}}}'):
                    category = 'broadcast_metadata'
                elif (
                    child.tag.startswith((f'{{{DC_NAMESPACE}}}', f'{{{UPNP_NAMESPACE}}}', f'{{{DIDL_NAMESPACE}}}'))
                    is False
                ):
                    category = 'vendor_metadata'
                if category is not None:
                    metadata[category].append(
                        MetadataValue(
                            name=child.tag,
                            value=child.text or '',
                            attributes=dict(child.attrib),
                        )
                    )
            for name, value in element.attrib.items():
                if name.startswith('{'):
                    metadata['vendor_metadata'].append(MetadataValue(name=f'@{name}', value=value))
            counts = profile.read_copy_counts(element)
            missing_count = read_remaining_copy_count(element)
            if profile.resolve_copy_count_fields():
                missing_count = ProtectionValue[int](status='omitted', source='metadata')
            objects.append(
                Recording(
                    id=element.get('id', ''),
                    parent_id=element.get('parentID', ''),
                    reference_id=element.get('refID'),
                    title=element.findtext(f'{{{DC_NAMESPACE}}}title') or '',
                    upnp_class=element.findtext(f'{{{UPNP_NAMESPACE}}}class') or '',
                    description=element.findtext(f'{{{DC_NAMESPACE}}}description'),
                    # Recorders may split details across multiple UPnP or ARIB elements
                    long_description='\n'.join(
                        child.text
                        for child in element
                        if child.tag in (f'{{{UPNP_NAMESPACE}}}longDescription', f'{{{ARIB_NAMESPACE}}}longDescription')
                        and child.text
                    )
                    or None,
                    channel_name=element.findtext(f'{{{UPNP_NAMESPACE}}}channelName'),
                    channel_number=element.findtext(f'{{{UPNP_NAMESPACE}}}channelNr'),
                    genres=tuple(genre.text or '' for genre in element.iterfind(f'{{{UPNP_NAMESPACE}}}genre')),
                    date=element.findtext(f'{{{DC_NAMESPACE}}}date'),
                    scheduled_start_time=element.findtext(f'{{{UPNP_NAMESPACE}}}scheduledStartTime'),
                    scheduled_end_time=element.findtext(f'{{{UPNP_NAMESPACE}}}scheduledEndTime'),
                    write_status=element.findtext(f'{{{UPNP_NAMESPACE}}}writeStatus'),
                    ratings=tuple(metadata['ratings']),
                    video_components=tuple(metadata['video_components']),
                    audio_components=tuple(metadata['audio_components']),
                    broadcast_metadata=tuple(metadata['broadcast_metadata']),
                    icons=tuple(metadata['icons']),
                    vendor_metadata=tuple(metadata['vendor_metadata']),
                    chapter_resources=profile.read_chapter_resources(element),
                    duration=resources[0].attributes.get('duration') if len(resources) > 0 else None,
                    resources=resources,
                    protection=_read_protection(element, resources, missing_count, counts),
                    raw_xml=ET.tostring(element, encoding='unicode'),
                    source_xml=source_items[element],
                    observed_at=observed_at,
                )
            )

    return objects


def _parse_resource(element: ET.Element, item: ET.Element, profile: Profile) -> Resource:
    """
    Convert a res element to a resource model.

    Args:
        element (ET.Element): DIDL-Lite res element
        item (ET.Element): Parent item containing family-specific resource references
        profile (Profile): Device-family rules

    Returns:
        Resource: Resource model
    """

    protocol_info = parse_protocol_info(element.get('protocolInfo', ''))
    conversion = (
        profile.resource_conversion(item, element)
        if protocol_info.is_converted is None
        else ResourceConversion(
            is_converted=protocol_info.is_converted,
            source='protocol_info',
            field_name='DLNA.ORG_CI',
            raw_value=protocol_info.parameters['DLNA.ORG_CI'],
        )
    )
    return Resource(
        url=(element.text or '').strip(),
        protocol_info=protocol_info,
        attributes=dict(element.attrib),
        raw_xml=ET.tostring(element, encoding='unicode'),
        conversion=conversion,
    )


def _read_protection(
    item: ET.Element,
    resources: tuple[Resource, ...],
    remaining_copy_count: ProtectionValue[int],
    reported_copy_counts: tuple[ReportedCopyCount, ...] = (),
) -> RecordingProtection:
    """
    Build protection information from the original resource's DTCP.COM_FLAGS and the profile's copy count.

    Args:
        item (ET.Element): DIDL-Lite item element
        resources (tuple[Resource, ...]): Recording resources
        remaining_copy_count (ProtectionValue[int]): Remaining copy count read by the profile
        reported_copy_counts (tuple[ReportedCopyCount, ...]): Raw counts with their counting conventions

    Returns:
        RecordingProtection: Protection information available from the listing
    """

    # Determine Move support from the advertised original-format resource used for lossless transfer
    target: Resource | None = None
    for resource in resources:
        if resource.protocol_info.is_dtcp is True and resource.is_converted is False:
            target = resource
            break
        if target is None and resource.protocol_info.is_dtcp is True and resource.is_converted is None:
            target = resource

    # An encoded derivative may advertise a different count from the original recording
    # Keep every observation, but normalize only a recording-level count or the selected original's count
    for count in reported_copy_counts:
        if count.resource_url is None or (target is not None and count.resource_url == target.url):
            remaining_copy_count = count.remaining_copies
            break

    flags_value = target.protocol_info.parameters.get('DTCP.COM_FLAGS') if target is not None else None
    if flags_value is None:
        # Some devices support Move without advertising it, so omission does not imply lack of support
        is_move_supported = ProtectionValue[bool](status='omitted', source='protocol_info')
        is_transaction_move_supported = ProtectionValue[bool](status='omitted', source='protocol_info')
    else:
        try:
            flags = int(flags_value[:8], 16)
            is_move_supported = ProtectionValue[bool](
                status='known', value=flags & _DTCP_FLAG_MOVE != 0, source='protocol_info'
            )
            is_transaction_move_supported = ProtectionValue[bool](
                status='known',
                value=flags & _DTCP_FLAG_TRANSACTION_MOVE != 0,
                source='protocol_info',
            )
        except ValueError:
            is_move_supported = ProtectionValue[bool](status='unsupported', source='protocol_info')
            is_transaction_move_supported = ProtectionValue[bool](status='unsupported', source='protocol_info')

    return RecordingProtection(
        remaining_copy_count=remaining_copy_count,
        reported_copy_counts=reported_copy_counts,
        is_move_supported=is_move_supported,
        is_transaction_move_supported=is_transaction_move_supported,
    )
