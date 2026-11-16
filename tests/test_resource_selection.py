"""Select recorder resources without substituting a mobile copy for the saved recording."""

import asyncio
from html import escape
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pydtcpip import Recording, RecordingMetadata
from pydtcpip.__main__ import app
from pydtcpip.dlna.didl import parse_didl
from pydtcpip.dlna.profiles import PROFILES
from tests.fakes import FakeAuthority, FakeDevice, FakeMediaServer, FakeSourceAKE
from tests.test_metadata import MetadataSource, recording_xml


def recording_with_resources(profile_name: str, resources: str) -> Recording:
    """Parse independent resource descriptions through the selected family profile."""

    profile = next(profile for profile in PROFILES if profile.name == profile_name)
    xml = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" xmlns:pxn="urn:schemas-panasonic-com:pxn">'
        '<item id="V_42" parentID="0"><dc:title>Example recording</dc:title>'
        '<upnp:class>object.item.videoItem</upnp:class>' + resources + '</item></DIDL-Lite>'
    )
    recording = parse_didl(xml, profile)[0]
    assert isinstance(recording, Recording)
    return Recording.model_validate_json(recording.model_dump_json())


@pytest.mark.parametrize('quality', ['dr', 'x3'])
@pytest.mark.parametrize('conversion', ['', ';DLNA.ORG_CI=0'])
@pytest.mark.parametrize('is_reversed', [False, True])
def test_nasne_keeps_the_recorded_quality_and_separates_move_urls(
    quality: str, conversion: str, is_reversed: bool
) -> None:
    resources = [
        f'<res protocolInfo="http-get:*:application/x-dtcp1:DTCP.COM_FLAGS=C0000000{conversion}">'
        'http://192.0.2.1/video?id=42&amp;quality=mobile&amp;cp</res>',
        f'<res protocolInfo="http-get:*:application/x-dtcp1:DTCP.COM_FLAGS=C0000000{conversion}">'
        f'http://192.0.2.1/video?id=42&amp;quality={quality}</res>',
        f'<res protocolInfo="http-get:*:application/x-dtcp1:DTCP.COM_FLAGS=C0000000{conversion}">'
        f'http://192.0.2.1/video?id=42&amp;quality={quality}&amp;cp</res>',
    ]
    if is_reversed is True:
        resources.reverse()
    recording = recording_with_resources('nasne', ''.join(resources))
    assert recording.original_resource is not None
    assert recording.original_resource.url == f'http://192.0.2.1/video?id=42&quality={quality}'
    assert recording.original_move_resource is not None
    assert recording.original_move_resource.url == f'http://192.0.2.1/video?id=42&quality={quality}&cp'
    mobile = next(resource for resource in recording.resources if 'quality=mobile' in resource.url)
    assert mobile.is_original_quality is False
    assert mobile.protocol_info.is_converted == (False if conversion else None)
    assert mobile.variant is not None
    assert mobile.variant.raw_value == 'mobile'


@pytest.mark.parametrize('original_profile', ['DTCP_MPEG_TS_JP_T', 'DTCP_AVC_TS_JP_AAC_T'])
@pytest.mark.parametrize('mobile_profile', ['DTCP_AVC_TS_SD_30_JP_AAC_T', 'DTCP_AVC_TS_BP_360p_30_AAC_T'])
@pytest.mark.parametrize('is_reversed', [False, True])
def test_sony_move_uses_the_main_recording_and_transfer_resource(
    original_profile: str, mobile_profile: str, is_reversed: bool
) -> None:
    resources = [
        f'<res protocolInfo="http-get:*:application/x-dtcp1:SONY.COM_PN={mobile_profile};'
        'DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000" dlna:cleartextSize="9000">http://192.0.2.1/mobile</res>',
        f'<res protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_PN={original_profile};'
        'DTCP.COM_FLAGS=00000000" dlna:cleartextSize="1000">http://192.0.2.1/play</res>',
        f'<res protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_PN={original_profile};'
        'DTCP.COM_FLAGS=C0000000" dlna:cleartextSize="1000">http://192.0.2.1/move</res>',
    ]
    if is_reversed is True:
        resources.reverse()
    recording = recording_with_resources('sony_bdz', ''.join(resources))
    assert recording.original_move_resource is not None
    assert recording.original_move_resource.url == 'http://192.0.2.1/move'
    assert recording.protection.is_move_supported.value is True
    assert recording.original_resource is not None
    assert recording.original_resource.url != 'http://192.0.2.1/mobile'


@pytest.mark.parametrize('profile_name', ['nasne', 'sony_bdz'])
def test_mobile_only_recordings_require_an_explicit_resource(profile_name: str) -> None:
    recording = recording_with_resources(
        profile_name,
        '<res protocolInfo="http-get:*:application/x-dtcp1:SONY.COM_PN=DTCP_AVC_TS_SD_30_JP_AAC_T;'
        'DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000">http://192.0.2.1/video?quality=mobile</res>',
    )
    assert recording.original_resource is None
    assert recording.original_move_resource is None
    assert len(recording.resources) == 1


def test_move_copy_count_comes_from_the_selected_resource() -> None:
    recording = recording_with_resources(
        'panasonic',
        '<res pxn:CopyCount="10" protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_CI=0;'
        'DTCP.COM_FLAGS=00000000">http://192.0.2.1/play</res>'
        '<res pxn:CopyCount="1" protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_CI=0;'
        'DTCP.COM_FLAGS=C0000000">http://192.0.2.1/move</res>',
    )
    assert recording.original_resource is not None
    assert recording.original_resource.url == 'http://192.0.2.1/play'
    assert recording.original_move_resource is not None
    assert recording.original_move_resource.url == 'http://192.0.2.1/move'
    assert recording.protection.remaining_copy_count.value == 0


def test_converted_mpeg_resource_cannot_replace_a_stored_avc_recording() -> None:
    recording = recording_with_resources(
        'sony_bdz',
        '<res dlna:cleartextSize="9000" protocolInfo="http-get:*:application/x-dtcp1:'
        'DLNA.ORG_PN=DTCP_MPEG_TS_JP_T;DLNA.ORG_CI=1;DTCP.COM_FLAGS=C0000000">http://192.0.2.1/converted</res>'
        '<res dlna:cleartextSize="1000" protocolInfo="http-get:*:application/x-dtcp1:'
        'DLNA.ORG_PN=DTCP_AVC_TS_JP_AAC_T;DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000">http://192.0.2.1/original</res>',
    )
    assert recording.original_move_resource is not None
    assert recording.original_move_resource.url == 'http://192.0.2.1/original'


@pytest.mark.anyio
@pytest.mark.parametrize('model', ['nasne', 'BDZ-TEST'])
@pytest.mark.parametrize('operation', ['stream', 'move'])
async def test_cli_transfers_the_main_recording_and_preserves_selection_evidence(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    operation: str,
) -> None:
    """Choose among distinct encrypted endpoints through the ordinary CLI and save Move metadata."""

    monkeypatch.setenv('PYDTCPIP_CERTIFICATE', sink_device.certificate.hex())
    monkeypatch.setenv('PYDTCPIP_PRIVATE_KEY', sink_device.private_key.hex())
    monkeypatch.setattr('pydtcpip.__main__._COPY_COUNT_REQUERY_INTERVAL', 0.01)
    content = b'Original AVC recording' * 1000
    output = tmp_path / 'recording.m2ts'
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, content) as playback,
        FakeMediaServer(source, content) as movable,
        FakeMediaServer(source, b'Mobile copy') as mobile,
    ):
        mobile_url = mobile.url + '?quality=mobile&cp'
        playback_url = playback.url + '?quality=x3'
        move_url = movable.url + '?quality=x3&cp'
        resources = (
            '<res protocolInfo="http-get:*:application/x-dtcp1:SONY.COM_PN=DTCP_AVC_TS_SD_30_JP_AAC_T;'
            f'DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000">{escape(mobile_url)}</res>'
            '<res protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_PN=DTCP_AVC_TS_JP_AAC_T;'
            f'DTCP.COM_FLAGS=00000000">{escape(playback_url)}</res>'
        )
        didl = recording_xml('<av:COGCount>10</av:COGCount>' + resources, resource_url=escape(move_url))
        metadata_source = MetadataSource('Sony', model, didl)
        metadata_source.requery_didl = didl.replace('<av:COGCount>10</av:COGCount>', '<av:COGCount>9</av:COGCount>')
        metadata_source.requery_ready = source.completed
        async with metadata_source.serve() as (location, _):
            arguments = ['--data-dir', str(tmp_path / 'device'), operation]
            if operation == 'move':
                arguments.extend([str(output), '--yes'])
            arguments.extend(['--server', location, '--id', 'V_42'])
            result = await asyncio.to_thread(CliRunner().invoke, app, arguments)
            assert result.exit_code == 0, result.output
            info = await asyncio.to_thread(CliRunner().invoke, app, ['info', '--server', location, '--id', 'V_42'])
            assert info.exit_code == 0, info.output
            assert 'mobile version' in info.output
        assert mobile.requests == []
        if operation == 'move':
            assert playback.requests == []
            assert movable.count('GET') == 1
            assert output.read_bytes() == content
            saved = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
            assert saved.recording.original_move_resource is not None
            assert saved.recording.original_move_resource.url == move_url
            assert saved.recording.resources[0].variant is not None
            assert saved.recording.resources[0].variant.kind == 'mobile'
            assert source.log[-2:] == ['finalize', 'complete']
        else:
            assert movable.requests == []
            assert playback.count('GET') == 1
            assert result.stdout_bytes == content
        assert source.errors == []
