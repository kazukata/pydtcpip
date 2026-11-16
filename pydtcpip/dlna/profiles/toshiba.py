"""Profile for Toshiba recorders and REGZA TVs."""

from xml.etree import ElementTree as ET

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC
from pydtcpip.models import ProtectionValue, Resource


# Namespaces for regza:copycount, which reports a recording's remaining copies
## Recorders declare a trailing `/` and REGZA TVs omit it, so check both
_REGZA_METADATA_NAMESPACES = (
    'urn:schemas-toshiba-co-jp:regzadata-1-0/',
    'urn:schemas-toshiba-co-jp:regzadata-1-0',
)


def _matches(description: DeviceDescription) -> bool:
    """
    Match Toshiba recorder and television manufacturer names.

    Args:
        description (DeviceDescription): Device description to inspect

    Returns:
        bool: True when the manufacturer contains TOSHIBA
    """

    # Recorders report `TOSHIBA` and REGZA TVs report `TOSHIBA TVS REGZA` as the manufacturer
    manufacturer = description.manufacturer.upper()
    return 'TOSHIBA' in manufacturer or 'TVS REGZA' in manufacturer


def _read_remaining_copy_count(item: ET.Element) -> ProtectionValue[int]:
    """
    Read regza:copycount, excluding the final Move from the result.

    Args:
        item (ET.Element): Recording's DIDL-Lite item

    Returns:
        ProtectionValue[int]: Copy count and its acquisition status
    """

    # regza:copycount contains the remaining count as a decimal, including the final Move
    ## An unused Dubbing 10 recording has a count of 10, while DBR-Z620 and REGZA display nine copies
    ## Subtract one to match the TV display, which excludes the final Move
    ## REGZA TVs omit this element for some recordings
    value: str | None = None
    for namespace in _REGZA_METADATA_NAMESPACES:
        value = item.findtext(f'{{{namespace}}}copycount')
        if value is not None:
            break
    if value is None:
        return ProtectionValue[int](status='omitted', source='metadata')
    # Zero has not been observed and its meaning is unknown, so treat it as unsupported
    if value.strip().isdecimal() is False or int(value.strip()) == 0:
        return ProtectionValue[int](status='unsupported', source='metadata')
    return ProtectionValue[int](status='known', value=int(value.strip()) - 1, source='metadata')


def _read_resource_details(resource: Resource) -> Resource:
    """
    Read the frame rate advertised in either REGZA metadata namespace.

    Args:
        resource (Resource): Resource with its original attributes

    Returns:
        Resource: Resource carrying the reported scan rate without changing its i/p notation
    """

    for namespace in _REGZA_METADATA_NAMESPACES:
        frame_rate = resource.attributes.get(f'{{{namespace}}}framerate')
        if frame_rate is not None:
            return resource.model_copy(update={'frame_rate': frame_rate})
    return resource


TOSHIBA = Profile(
    name='toshiba',
    parent=GENERIC,
    matches=_matches,
    read_remaining_copy_count=_read_remaining_copy_count,
    copy_count_fields=tuple(
        CopyCountField(f'{{{namespace}}}copycount', includes_final_move=True)
        for namespace in _REGZA_METADATA_NAMESPACES
    ),
    extension='toshiba',
    read_resource_details=_read_resource_details,
)
