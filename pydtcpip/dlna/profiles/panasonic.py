"""Panasonic DIGA and VIERA metadata."""

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import ChapterField, CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC


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


PANASONIC = Profile(
    name='panasonic',
    parent=GENERIC,
    matches=_matches,
    is_unverified=True,
    # CopyCount is available in parent listings but omitted from BrowseMetadata
    has_listing_only_metadata=True,
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
)
