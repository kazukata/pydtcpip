"""Shared profile for nasne devices sold by Sony and Buffalo."""

from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC
from pydtcpip.errors import ClientNotAllowedError, PyDTCPIPError
from pydtcpip.models import ProtectionValue, Resource, ResourceVariant


# Namespace of av:COGCount, which reports a recording's remaining copies
_SONY_AV_NAMESPACE = 'urn:schemas-sony-com:av'


def _matches(description: DeviceDescription) -> bool:
    """
    Match the model names reported by Sony and Buffalo nasne devices.

    Args:
        description (DeviceDescription): Device description to inspect

    Returns:
        bool: True when the model name identifies a nasne
    """

    # Both generations share the nasne metadata format but advertise different model names
    return description.model_name.lower() in ('nasne', 'buffalo-nasne', 'buffalo-nasne2')


def _read_remaining_copy_count(item: ET.Element) -> ProtectionValue[int]:
    """
    Read av:COGCount, excluding the final Move from the result.

    Args:
        item (ET.Element): Recording's DIDL-Lite item

    Returns:
        ProtectionValue[int]: Copy count and its acquisition status
    """

    # av:COGCount includes the final Move: Dubbing 10 permits nine copies followed by one Move
    ## Normalize to the common field so a reported count of one triggers the source-deletion warning
    value = item.findtext(f'{{{_SONY_AV_NAMESPACE}}}COGCount')
    if value is None:
        return ProtectionValue[int](status='omitted', source='metadata')
    # Zero has no established meaning here; keep it distinct from one remaining Move
    if value.strip().isdecimal() is False or int(value.strip()) == 0:
        return ProtectionValue[int](status='unsupported', source='metadata')
    return ProtectionValue[int](status='known', value=int(value.strip()) - 1, source='metadata')


def _read_resource_variant(resource: Resource) -> ResourceVariant | None:
    """
    Distinguish the recorded DR or three-times format from the separate mobile version.

    Args:
        resource (Resource): Resource with the recorder's advertised URL

    Returns:
        ResourceVariant | None: Recording version and transfer use, or None for an unrecognized quality value
    """

    parameters = parse_qs(urlsplit(resource.url).query, keep_blank_values=True)
    quality = parameters.get('quality', [''])[0]
    if quality not in ('dr', 'x3', 'mobile'):
        return None
    return ResourceVariant(
        kind='mobile' if quality == 'mobile' else 'main',
        is_move_only='cp' in parameters,
    )


def _interpret_http_status(status: int, url: str, upnp_error_code: int | None) -> PyDTCPIPError | None:
    """
    Interpret nasne's client authorization refusal.

    Args:
        status (int): HTTP status code
        url (str): URL of the failed request
        upnp_error_code (int | None): UPnP error code from a SOAP fault, if present

    Returns:
        PyDTCPIPError | None: Client authorization error for 403, otherwise None
    """

    # nasne returns 403 for listing and media requests from unauthorized media server clients
    if status == 403:
        return ClientNotAllowedError(
            'nasne has not authorized this PC as a client. '
            'Allow this PC under Media Server Settings > Client Device Management in nasne HOME, then try again.',
            http_status=status,
        )
    return None


NASNE = Profile(
    name='nasne',
    parent=GENERIC,
    matches=_matches,
    read_remaining_copy_count=_read_remaining_copy_count,
    interpret_http_status=_interpret_http_status,
    copy_count_fields=(CopyCountField(f'{{{_SONY_AV_NAMESPACE}}}COGCount', includes_final_move=True),),
    extension='nasne',
    read_resource_variant=_read_resource_variant,
)
