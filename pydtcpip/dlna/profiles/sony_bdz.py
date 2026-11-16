"""Sony BDZ recorder metadata and optional service extensions."""

from pydtcpip.dlna.description import DeviceDescription
from pydtcpip.dlna.profiles.base import ChapterField, CopyCountField, Profile
from pydtcpip.dlna.profiles.generic import GENERIC
from pydtcpip.models import Resource, ResourceVariant


def _matches(description: DeviceDescription) -> bool:
    """
    Identify BDZ recorders without including Sony's other media servers.

    Args:
        description (DeviceDescription): Device description to inspect

    Returns:
        bool: Whether the device advertises a BDZ model
    """

    model = description.model_name.upper()
    return 'SONY' in description.manufacturer.upper() and (model == 'SONY-BDZ' or model.startswith('BDZ-'))


def _read_resource_variant(resource: Resource) -> ResourceVariant | None:
    """
    Identify the main recording and stored mobile versions by their advertised media profile.
    A stored mobile version can declare CI=0 because it needs no conversion during transfer.

    Args:
        resource (Resource): Resource with standard and Sony media profile declarations

    Returns:
        ResourceVariant | None: Recording version, or None for an unclassified format
    """

    for field in ('SONY.COM_PN', 'DLNA.ORG_PN'):
        value = resource.protocol_info.parameters.get(field)
        if value is None:
            continue
        name = value.upper().removeprefix('DTCP_')
        if name in ('AVC_TS_SD_30_JP_AAC_T', 'AVC_TS_BP_360P_30_AAC_T', 'AVC_TS_BP_180P_30_AAC_T'):
            return ResourceVariant(kind='mobile')
        if name in ('MPEG_TS_JP_T', 'AVC_TS_JP_AAC_T'):
            return ResourceVariant(kind='main')
    return None


SONY_BDZ = Profile(
    name='sony_bdz',
    parent=GENERIC,
    matches=_matches,
    is_unverified=True,
    copy_count_fields=(CopyCountField('{urn:schemas-sony-com:av}COGCount', includes_final_move=True),),
    chapter_fields=(ChapterField('{urn:schemas-sony-com:av}chapterInfo', 'sony'),),
    extension='sony',
    read_resource_variant=_read_resource_variant,
)
