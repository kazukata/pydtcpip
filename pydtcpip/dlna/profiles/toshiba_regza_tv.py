"""Toshiba REGZA TV profile, derived from the Toshiba profile."""

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import Profile
from pydtcpip.dlna.profiles.toshiba import TOSHIBA


def _matches(description: DeviceDescription) -> bool:
    """
    Match REGZA televisions by the manufacturer and model description.

    Args:
        description (DeviceDescription): Device description to inspect

    Returns:
        bool: True when REGZA identifies the manufacturer and the description does not identify a recorder
    """

    # Newer recorders can report TOSHIBA TVS REGZA as well, with TOSHIBA RECORDER in the description
    return (
        'REGZA' in description.manufacturer.upper() and 'RECORDER' not in (description.model_description or '').upper()
    )


TOSHIBA_REGZA_TV = Profile(
    name='toshiba_regza_tv',
    parent=TOSHIBA,
    matches=_matches,
    # REGZA 43Z570L includes broadcast network and service IDs (regza:network_id and regza:service_id)
    # only in BrowseDirectChildren responses, not in BrowseMetadata responses
    has_listing_only_metadata=True,
)
