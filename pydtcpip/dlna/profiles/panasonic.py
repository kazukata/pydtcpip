"""Panasonic DIGA and VIERA metadata."""

from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree as ET

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import ChapterField, CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC
from pydtcpip.models import Recording, ResourceConversion


def _matches(description: DeviceDescription) -> bool:
    """
    Match Panasonic media servers using their manufacturer declaration.

    Args:
        description (DeviceDescription): Device description to inspect

    Returns:
        bool: Whether the manufacturer is Panasonic
    """

    model = description.model_name.upper()
    is_stb = 'CATV DIGITAL STB' in model or model in ('C01AS3', 'STW2000', 'IS1050')
    return 'PANASONIC' in description.manufacturer.upper() and is_stb is False


def _read_resource_conversion(item: ET.Element, resource: ET.Element) -> ResourceConversion | None:
    """
    Identify converted resources whose protocolInfo omits DLNA.ORG_CI.

    Args:
        item (ET.Element): Recording with Panasonic HD and VGA references
        resource (ET.Element): Resource to classify

    Returns:
        ResourceConversion | None: Conversion evidence, or None when the format remains unknown
    """

    url = (resource.text or '').strip()
    if '1' in parse_qs(urlsplit(url).query).get('CI', []):
        return ResourceConversion(is_converted=True, source='resource_url', field_name='CI', raw_value='1')
    # HD/VGA references can identify derivative resources even when their URLs have no conversion query
    for sibling in item.findall('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res'):
        for name in ('HDContentUri', 'VgaContentUri'):
            field_name = f'{{urn:schemas-panasonic-com:pxn}}{name}'
            reference = sibling.get(field_name)
            if reference is not None and reference.strip() and urljoin(url, reference.strip()) == url:
                return ResourceConversion(
                    is_converted=True, source='metadata', field_name=field_name, raw_value=reference
                )
    return None


def _needs_listing_metadata(recording: Recording) -> bool:
    """
    Request listing metadata only when the selected resource has no usable copy count.

    Args:
        recording (Recording): Recording returned by BrowseMetadata

    Returns:
        bool: Whether the parent listing may supply a missing copy count
    """

    resource = recording.original_resource
    return not any(
        (count.value is not None or count.is_unlimited is True)
        and (count.resource_url is None or (resource is not None and count.resource_url == resource.url))
        for count in recording.protection.reported_copy_counts
    )


PANASONIC = Profile(
    name='panasonic',
    parent=GENERIC,
    matches=_matches,
    is_unverified=True,
    # Some models omit CopyCount from BrowseMetadata but include it in parent listings
    has_listing_only_metadata=True,
    needs_listing_metadata=_needs_listing_metadata,
    # Finite counts include the final Move; CF and -1 denote unrestricted copies
    copy_count_fields=(
        CopyCountField(
            '{urn:schemas-panasonic-com:pxn}CopyCount',
            on_resource=True,
            includes_final_move=True,
            unlimited_values=('cf', '-1'),
        ),
    ),
    chapter_fields=(ChapterField('{urn:schemas-panasonic-com:pxn}ChapterList', 'panasonic', on_resource=True),),
    read_resource_conversion=_read_resource_conversion,
)
