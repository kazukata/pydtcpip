"""Sony PC TV Plus media server compatibility."""

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC


def _matches(description: DeviceDescription) -> bool:
    """
    Identify PC TV Plus without including Sony recorders or nasne.

    Args:
        description (DeviceDescription): Media server device description

    Returns:
        bool: Whether the server identifies itself as Sony PC TV Plus
    """

    return 'SONY' in description.manufacturer.upper() and description.model_name.casefold() == 'pc tv plus'


SONY_PCTV = Profile(
    name='sony_pctv',
    parent=GENERIC,
    matches=_matches,
    is_unverified=True,
    copy_count_fields=(CopyCountField('{urn:schemas-sony-com:av}COGCount', includes_final_move=True),),
    # The server can disconnect a reused connection even if the request asks it to close the connection
    reuse_soap_connections=False,
)
