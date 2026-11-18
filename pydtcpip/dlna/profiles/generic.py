"""Root profile defining standard DLNA behavior."""

from xml.etree import ElementTree as ET

from pydtcpip.dlna.profiles.base import Profile
from pydtcpip.errors import PyDTCPIPError
from pydtcpip.models import ProtectionValue


def _read_remaining_copy_count(item: ET.Element) -> ProtectionValue[int]:
    """
    Report that standard DLNA does not define a copy count.

    Args:
        item (ET.Element): Recording's DIDL-Lite item

    Returns:
        ProtectionValue[int]: Unsupported metadata value
    """

    # DLNA has no standard copy count element, and vendor-specific elements vary by device
    return ProtectionValue[int](status='unsupported', source='metadata')


def _interpret_http_status(status: int, url: str, upnp_error_code: int | None) -> PyDTCPIPError | None:
    """
    Leave HTTP errors without a device-specific interpretation.

    Args:
        status (int): HTTP status code
        url (str): URL of the failed request
        upnp_error_code (int | None): UPnP error code from a SOAP fault, if present

    Returns:
        PyDTCPIPError | None: Always None for the generic profile
    """

    # Standard behavior does not assign device-specific meanings to HTTP errors
    return None


GENERIC = Profile(
    name='generic',
    parent=None,
    read_remaining_copy_count=_read_remaining_copy_count,
    interpret_http_status=_interpret_http_status,
    # Standard DLNA returns the same fields in BrowseMetadata and BrowseDirectChildren
    has_listing_only_metadata=False,
)
