"""Exercise family metadata through real local HTTP and SOAP exchanges."""

import asyncio
import errno
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from typer.testing import CliRunner

from pydtcpip import DLNAClient, Recording, RecordingMetadata
from pydtcpip.__main__ import app
from pydtcpip.dlna.didl import parse_didl
from pydtcpip.dlna.extensions.sony import SonyMetadataResult
from pydtcpip.dlna.extensions.xml_chapters import ChapterFormat, parse_chapters
from pydtcpip.dlna.profiles import PROFILES
from pydtcpip.errors import DLNAError
from pydtcpip.events import AuthenticationCompleted, MoveCompleted
from pydtcpip.metadata import MetadataSidecar
from pydtcpip.models import MoveResumeState
from tests.fakes import FakeAuthority, FakeDevice, FakeMediaServer, FakeSourceAKE, Framing


_CDS = 'urn:schemas-upnp-org:service:ContentDirectory:1'
_PVR = 'urn:schemas-s-bras-org:service:X_PvrControl:1'


def recording_xml(fields: str = '', attributes: str = '', resource_url: str = 'http://192.0.2.1/media') -> str:
    return f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
      xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
      xmlns:arib="urn:schemas-arib-or-jp:elements-1-0/" xmlns:av="urn:schemas-sony-com:av"
      xmlns:pxn="urn:schemas-panasonic-com:pxn" xmlns:shp="urn:schemas-sharp-co-jp:shp"
      xmlns:regza="urn:schemas-toshiba-co-jp:regzadata-1-0/">
      <item id="V_42" refID="42" parentID="0"><dc:title>Sample\ue0fe</dc:title>
      <upnp:class>object.item.videoItem</upnp:class><upnp:channelName>Example channel</upnp:channelName>
      <upnp:channelNr>101</upnp:channelNr><upnp:scheduledStartTime>2026-01-01T20:00:00+09:00</upnp:scheduledStartTime>
      <upnp:scheduledEndTime>2026-01-01T20:30:00+09:00</upnp:scheduledEndTime>
      <arib:longDescription>First paragraph</arib:longDescription><upnp:longDescription>Cast</upnp:longDescription>
      <arib:videoComponentType>179</arib:videoComponentType><arib:audioComponentType main="1">3</arib:audioComponentType>
      <arib:serviceID>100</arib:serviceID><upnp:rating type="age">12</upnp:rating>
      {fields}<res {attributes} duration="0:30:00.000" resolution="1920x1080" size="123456"
      protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_CI=0;DTCP.COM_FLAGS=C0000000">{resource_url}</res>
      </item></DIDL-Lite>"""


@pytest.mark.parametrize(
    ('profile_name', 'fields', 'attributes', 'expected'),
    [
        ('sony_bdz', '<av:COGCount>10</av:COGCount>', '', 9),
        ('sharp', '<shp:ccnum>0x01</shp:ccnum>', '', 0),
        ('toshiba', '<regza:copycount>0x0a</regza:copycount>', '', 9),
        ('panasonic', '', 'pxn:CopyCount="9"', 8),
        ('panasonic', '', 'pxn:CopyCount="1"', 0),
        ('stb', '', 'allowedUse="0:0:10"', 9),
        ('stb_hikaritv', '', 'allowedUse="0:0:1"', 0),
        ('stb_stw2000', '', 'allowedUse="0:0:9"', 8),
        ('recon', '', 'allowedUse="0:0:1"', 0),
        ('recbox', '', 'pxn:CopyCount="10"', 9),
        ('linkstation', '', 'pxn:CopyCount="10"', 9),
    ],
)
def test_finite_counts_include_the_final_move(profile_name: str, fields: str, attributes: str, expected: int) -> None:
    profile = next(profile for profile in PROFILES if profile.name == profile_name)
    recording = parse_didl(recording_xml(fields, attributes), profile)[0]
    assert isinstance(recording, Recording)
    assert recording.protection.remaining_copy_count.value == expected
    assert recording.protection.remaining_copy_count.status == 'known'
    assert recording.protection.reported_copy_counts[0].includes_final_move is True
    assert recording.protection.remaining_copy_count.observed_at is not None


@pytest.mark.parametrize(
    ('attributes', 'status', 'unlimited'),
    [
        ('', 'omitted', False),
        ('pxn:CopyCount="CF"', 'unsupported', True),
        ('pxn:CopyCount="-1"', 'unsupported', True),
        ('pxn:CopyCount="0"', 'unsupported', False),
        ('pxn:CopyCount="redacted"', 'unsupported', False),
    ],
)
def test_omitted_unlimited_and_invalid_counts_remain_distinct(attributes: str, status: str, unlimited: bool) -> None:
    profile = next(profile for profile in PROFILES if profile.name == 'panasonic')
    recording = parse_didl(recording_xml(attributes=attributes), profile)[0]
    assert isinstance(recording, Recording)
    assert recording.protection.remaining_copy_count.value is None
    assert recording.protection.remaining_copy_count.status == status
    if attributes:
        assert recording.protection.reported_copy_counts[0].is_unlimited is unlimited
    else:
        assert recording.protection.reported_copy_counts == ()


def test_original_resource_controls_count_and_move_capabilities() -> None:
    profile = next(profile for profile in PROFILES if profile.name == 'panasonic')
    other_resources = """<res pxn:CopyCount="2" protocolInfo="http-get:*:application/x-dtcp1:DTCP.COM_FLAGS=00000000">http://192.0.2.1/unknown</res>
    <res pxn:CopyCount="3" protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_CI=1;DTCP.COM_FLAGS=00000000">http://192.0.2.1/converted</res>"""
    recording = parse_didl(recording_xml(other_resources, 'pxn:CopyCount="10"'), profile)[0]
    assert isinstance(recording, Recording)
    assert recording.original_resource is not None
    assert recording.original_resource.url == 'http://192.0.2.1/media'
    assert recording.protection.remaining_copy_count.value == 9
    assert recording.protection.is_move_supported.value is True
    assert [count.value for count in recording.protection.reported_copy_counts] == [2, 3, 10]


@pytest.mark.parametrize('profile_name', ['panasonic', 'generic'])
@pytest.mark.parametrize('is_reversed', [False, True])
def test_panasonic_conversion_status_survives_resource_order_and_serialization(
    profile_name: str, is_reversed: bool
) -> None:
    """Preserve the CI omission and HD/VGA references reported in Issue #11."""

    profile = next(profile for profile in PROFILES if profile.name == profile_name)
    # Synthetic recording with the resource forms in the report, deliberately placing conversions first
    resources = [
        '<res protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_PN=DTCP_AVC_TS_MP_HD_AAC_LTP_T">'
        'http://192.0.2.1/AV-1_BDY?CI=1&amp;fmt=AVC_TS_MP_HD_AAC_LTP_T</res>',
        '<res protocolInfo="http-get:*:application/x-dtcp1:DTV_MVP_PN=DTCP_AVC_TS_DTV_SD_AAC_T">'
        'http://192.0.2.1/AV-1_VGA</res>',
        '<res pxn:CopyCount="10" pxn:VgaContentUri="http://192.0.2.1/AV-1_VGA" '
        'protocolInfo="http-get:*:application/x-dtcp1:DLNA.ORG_PN=DTCP_AVC_TS_JP_AAC_T">'
        'http://192.0.2.1/AV-1_BDY</res>',
    ]
    if is_reversed is True:
        resources.reverse()
    root = ET.fromstring(recording_xml())
    item = root[0]
    original = item.find('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res')
    assert original is not None
    item.remove(original)
    for resource in resources:
        item.append(
            ET.fromstring(
                '<root xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:pxn="urn:schemas-panasonic-com:pxn">' + resource + '</root>'
            )[0]
        )
    recording = parse_didl(ET.tostring(root, encoding='unicode'), profile)[0]
    assert isinstance(recording, Recording)
    saved = Recording.model_validate_json(recording.model_dump_json())
    assert saved == recording
    assert all(resource.protocol_info.is_converted is None for resource in saved.resources)
    assert saved.original_resource is not None
    if profile_name == 'panasonic':
        assert saved.original_resource.url == 'http://192.0.2.1/AV-1_BDY'
        assert saved.original_resource.is_converted is None
        assert saved.protection.remaining_copy_count.value == 9
        by_url = {resource.url: resource for resource in saved.resources}
        hd = by_url['http://192.0.2.1/AV-1_BDY?CI=1&fmt=AVC_TS_MP_HD_AAC_LTP_T']
        vga = by_url['http://192.0.2.1/AV-1_VGA']
        assert hd.is_converted is True and hd.conversion is not None
        assert vga.is_converted is True and vga.conversion is not None
    else:
        assert all(resource.conversion is None for resource in saved.resources)
        assert saved.original_resource == saved.resources[0]


_CHAPTER_CASES: list[tuple[ChapterFormat, str]] = [
    (
        'sony',
        '<root xmlns:s="urn:schemas-sony-com:av"><s:content_chapter_info><s:chapter><s:chapter_point>12.345</s:chapter_point></s:chapter></s:content_chapter_info></root>',
    ),
    ('panasonic', '<root><chapterList><item><timeCode>00:00:12.345</timeCode></item></chapterList></root>'),
    ('sharp', '<root><chapter>00:00:12.345</chapter></root>'),
    ('toshiba', '<root xmlns="urn:example:chapters"><Chapter>00:00:12.345</Chapter></root>'),
    (
        'shared_tags',
        '<root xmlns="urn:example:tags"><item><tagList><tag><posTime>00:00:12.345</posTime></tag></tagList></item></root>',
    ),
]


@pytest.mark.parametrize(('format', 'document'), _CHAPTER_CASES)
def test_chapter_formats_preserve_only_device_points(format: ChapterFormat, document: str) -> None:
    assert [chapter.position_ms for chapter in parse_chapters(document, format)] == [12345]


@pytest.mark.parametrize(
    'document',
    [
        '<root/>',
        '<root><chapterList><item/></chapterList></root>',
        '<root><chapterList><item><timeCode>invalid</timeCode></item></chapterList></root>',
    ],
)
def test_invalid_chapter_documents_are_not_empty_successes(document: str) -> None:
    with pytest.raises(DLNAError):
        parse_chapters(document, 'panasonic')
    assert parse_chapters('<root><chapterList/></root>', 'panasonic') == ()


class LocalResolver(AbstractResolver):
    """Route documented fixed metadata ports to one private in-process test server."""

    def __init__(self, port: int) -> None:
        self.port = port

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        return [ResolveResult(hostname=host, host='127.0.0.1', port=self.port, family=socket.AF_INET, proto=0, flags=0)]

    async def close(self) -> None:
        pass


class MetadataSource:
    """A synthetic device serving independent metadata documents over HTTP and SOAP."""

    def __init__(self, manufacturer: str, model: str, didl: str, chapter: str = '', action: str = '') -> None:
        self.manufacturer = manufacturer
        self.model = model
        self.didl = didl
        self.chapter = chapter
        self.action = action
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.requests: list[str] = []
        self.raw_description = ''
        self.requery_didl: str | None = None
        self.requery_ready: asyncio.Event | None = None
        self.metadata_didl: str | None = None
        self.metadata_by_id: dict[str, str] = {}
        self.response_bodies: dict[str, bytes] = {}

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(request.path)
        if request.path in self.response_bodies:
            return web.Response(body=self.response_bodies[request.path])
        if request.path == '/description.xml':
            self.raw_description = f"""<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
            <manufacturer>{self.manufacturer}</manufacturer><modelName>{self.model}</modelName>
            <friendlyName>Test recorder</friendlyName><serviceList><service><serviceType>{_CDS}</serviceType>
            <controlURL>/control</controlURL><SCPDURL>/scpd.xml</SCPDURL></service>
            <service><serviceType>{_PVR}</serviceType><controlURL>/pvr</controlURL><SCPDURL>/pvr.xml</SCPDURL>
            </service></serviceList></device></root>"""
            return web.Response(text=self.raw_description, content_type='text/xml')
        if request.path.endswith('.xml'):
            actions = (
                [self.action]
                if request.path == '/scpd.xml'
                else ['X_GetTitleList2', 'X_GetTitleDetail', 'X_GetTitleInfo', 'X_GetMediaInfo']
            )
            entries = ''.join(f'<action><name>{name}</name></action>' for name in actions if name)
            return web.Response(
                text=f'<scpd xmlns="urn:schemas-upnp-org:service-1-0"><actionList>{entries}</actionList></scpd>'
            )
        if request.path == '/chapters':
            return web.Response(text=self.chapter)
        if request.path == '/recorded/thumbnailMapGet':
            assert request.query == {'id': '42'}
            return web.Response(body=bytes.fromhex('00000003 00000064 000000c8 0000012c'))
        if request.path == '/recorded/recordedContentChpDataGet':
            assert request.query == {'id': '42', 'offset': '0', 'length': '0'}
            # Stream positions 150 and 350; only the first lies between thumbnail map entries
            return web.Response(
                body=bytes.fromhex('00b0040000000000 0000000000000000 00f00a0000000000 0000000000000000')
            )
        if request.path == '/recorded/titleListGet':
            assert request.query['withDescriptionLong'] == '1'
            assert request.query['withUserData'] == '0'
            return web.json_response(
                {
                    'errorcode': 0,
                    'totalMatches': 1,
                    'numberReturned': 1,
                    'item': [{'id': 42, 'descriptionLong': 'Extended information', 'startDateTime': 1767265200}],
                }
            )
        envelope = ET.fromstring(await request.read())
        body = envelope.find('{http://schemas.xmlsoap.org/soap/envelope/}Body')
        assert body is not None
        action = body[0]
        name = action.tag.rpartition('}')[2]
        arguments = {child.tag: child.text or '' for child in action}
        self.calls.append((name, arguments))
        if name in self.response_bodies:
            return web.Response(body=self.response_bodies[name], content_type='text/xml')
        if name == 'Browse':
            result = self.didl
            if self.metadata_didl is not None and arguments['BrowseFlag'] == 'BrowseMetadata':
                result = self.metadata_didl
            if arguments['BrowseFlag'] == 'BrowseMetadata' and arguments['ObjectID'] in self.metadata_by_id:
                result = self.metadata_by_id[arguments['ObjectID']]
            if self.requery_didl is not None and self.requery_ready is not None:
                if self.requery_ready.is_set():
                    result = self.requery_didl
            elif self.requery_didl is not None:
                self.didl = self.requery_didl
        elif name == self.action:
            result = self.chapter
        elif name == 'X_GetTitleList2':
            result = '<root><item id="00000000000000002a"><recordDestinationID>HDD</recordDestinationID><description>Extra details</description></item></root>'
        elif name == 'X_GetTitleInfo':
            assert arguments == {'TitleID': '00000000000000002a'}
            result = '<root><recordStartDate>2026-01-01T19:59:57</recordStartDate><startPTS>270000</startPTS></root>'
        elif name == 'X_GetTitleDetail':
            assert arguments == {'Id': '00000000000000002a'}
            result = '<root><summary>Program summary</summary><detail0>First section</detail0><detail7>Last section</detail7><size>1200</size></root>'
        elif name == 'X_GetMediaInfo':
            assert arguments == {'recordDestinationID': 'HDD'}
            result = '<root><freeCapacity>123456789</freeCapacity></root>'
        else:
            raise AssertionError(f'Unexpected SOAP action: {name}')
        namespace = action.tag[1:].partition('}')[0]
        return web.Response(
            text=f'''<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
        <u:{name}Response xmlns:u="{namespace}"><Result>{escape(result)}</Result><NumberReturned>1</NumberReturned>
        <TotalMatches>1</TotalMatches></u:{name}Response></s:Body></s:Envelope>''',
            content_type='text/xml',
        )

    @asynccontextmanager
    async def serve(self) -> AsyncGenerator[tuple[str, int]]:
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            yield f'http://127.0.0.1:{port}/description.xml', port
        finally:
            await runner.cleanup()


@pytest.mark.anyio
@pytest.mark.parametrize('model', ['PC TV Plus', 'Sony-BDZ', 'Example Server'])
async def test_pctv_browse_closes_connections_and_reads_copy_counts(model: str) -> None:
    """Exercise successive Browse requests without changing connection reuse for other servers."""

    transports: list[asyncio.Transport] = []

    class ConnectionSource(MetadataSource):
        async def handle(self, request: web.Request) -> web.Response:
            if request.method == 'POST':
                transport = request.transport
                assert transport is not None
                if model == 'PC TV Plus' and transport in transports:
                    transport.close()
                    return web.Response()
                transports.append(transport)
            return await super().handle(request)

    source = ConnectionSource('Sony Corporation', model, recording_xml('<av:COGCount>1</av:COGCount>'))
    async with source.serve() as (location, _), DLNAClient() as client:
        server = await client.get_server(location)
        for _ in range(3):
            result = await server.browse()
            recording = result.objects[0]
            assert isinstance(recording, Recording)
            count = recording.protection.remaining_copy_count
            assert count.status == ('unsupported' if model == 'Example Server' else 'known')
            assert count.value == (None if model == 'Example Server' else 0)
        assert (
            server.profile.name
            == {'PC TV Plus': 'sony_pctv', 'Sony-BDZ': 'sony_bdz', 'Example Server': 'generic'}[model]
        )
    assert len(transports) == 3
    assert len(set(transports)) == (3 if model == 'PC TV Plus' else 1)


@pytest.mark.anyio
async def test_pctv_dump_uses_fresh_connections_even_when_the_server_ignores_connection_close(tmp_path: Path) -> None:
    """Complete the standard dump when the server keeps the socket open but refuses a second Browse on it."""

    description = (
        f'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><manufacturer>Sony Corporation</manufacturer>'
        f'<modelName>PC TV Plus</modelName><friendlyName>Test server</friendlyName><serviceList><service>'
        f'<serviceType>{_CDS}</serviceType><controlURL>/browse</controlURL><SCPDURL>/scpd.xml</SCPDURL>'
        '</service></serviceList></device></root>'
    ).encode()
    connections: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Keep connections open after SOAP responses and disconnect on a repeated Browse.

        Args:
            reader (asyncio.StreamReader): HTTP request input
            writer (asyncio.StreamWriter): HTTP response output
        """

        try:
            while request := await reader.readuntil(b'\r\n\r\n'):
                lines = request.decode('ascii').split('\r\n')
                method, path, _ = lines[0].split(' ')
                if method == 'POST':
                    headers = dict(line.lower().split(':', 1) for line in lines[1:] if ':' in line)
                    await reader.readexactly(int(headers['content-length']))
                    if writer in connections:
                        return
                    connections.append(writer)
                    port = writer.get_extra_info('sockname')[1]
                    didl = recording_xml('<av:COGCount>1</av:COGCount>', resource_url=f'http://127.0.0.1:{port}/media')
                    body = (
                        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:BrowseResponse xmlns:u="{_CDS}"><Result>{escape(didl)}</Result><NumberReturned>1</NumberReturned>'
                        '<TotalMatches>1</TotalMatches></u:BrowseResponse></s:Body></s:Envelope>'
                    ).encode()
                else:
                    body = description if path == '/description.xml' else b'<scpd/>'
                # Keep-alive is intentional even if the client sends Connection: close
                writer.write(
                    f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n'.encode()
                    + (body if method != 'HEAD' else b'')
                )
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(handle, '127.0.0.1', 0) as source:
        location = f'http://127.0.0.1:{source.sockets[0].getsockname()[1]}/description.xml'
        result = await asyncio.to_thread(
            CliRunner().invoke, app, ['dump', str(tmp_path), '--mask-private', '--server', location]
        )
    assert result.exit_code == 0, result.output
    assert len(connections) >= 2
    assert (tmp_path / 'recording-01.xml').is_file()


@pytest.mark.anyio
@pytest.mark.parametrize('count', ['10', '1', 'CF', '-1', None])
async def test_cli_panasonic_fetches_parent_only_for_missing_copy_count(count: str | None) -> None:
    """Known counts avoid a full folder scan, while omitted counts retain the listing fallback."""

    source = MetadataSource('Panasonic', 'BD/DVD Recorder', recording_xml(attributes='pxn:CopyCount="9"'))
    source.metadata_didl = recording_xml(attributes=f'pxn:CopyCount="{count}"' if count is not None else '')
    async with source.serve() as (location, _):
        result = await asyncio.to_thread(
            CliRunner().invoke, app, ['info', '--server', location, '--id', 'V_42', '--basic']
        )
    assert result.exit_code == 0, result.output
    flags = [arguments['BrowseFlag'] for name, arguments in source.calls if name == 'Browse']
    assert flags == (['BrowseMetadata', 'BrowseDirectChildren'] if count is None else ['BrowseMetadata'])


@pytest.mark.anyio
async def test_cli_device_capabilities_are_separate_from_recording_permissions() -> None:
    """Advertised device Move support must not grant Move support to a recording without DTCP flags."""

    source = MetadataSource('Panasonic', 'BD/DVD Recorder', recording_xml().replace(';DTCP.COM_FLAGS=C0000000', ''))
    description = (Path(__file__).parent / 'fixtures/panasonic/dmr-brx2060/description.xml').read_text(encoding='utf-8')
    # Route the sanitized device description's advertised services to the local test source
    source.response_bodies['/description.xml'] = description.replace(
        'http://192.0.2.1:60606/Server0/CDS_control', '/control'
    ).encode('utf-8')
    async with source.serve() as (location, _):
        result = await asyncio.to_thread(
            CliRunner().invoke, app, ['info', '--server', location, '--id', 'V_42', '--basic']
        )
        async with DLNAClient() as client:
            server = await client.get_server(location)
            recording = await server.get_metadata('V_42')
            assert isinstance(recording, Recording)
            metadata = await server.collect_metadata(recording, additional=False)
    assert result.exit_code == 0, result.output
    assert 'Device capabilities' in result.output and 'dtcp-move' in result.output
    assert 'do not establish Move permission' in result.output
    saved = RecordingMetadata.model_validate_json(metadata.model_dump_json())
    assert saved.recording.protection.is_move_supported.value is None
    assert saved.recording.protection.is_move_supported.status == 'omitted'
    assert saved.device_description_xml == description.replace('http://192.0.2.1:60606/Server0/CDS_control', '/control')
    assert saved.device_declarations['{urn:schemas-skyperfectv-co-jp:device-1-0}X_SPTVCAP'].startswith('REC-1.00')
    assert 'upload_dub_sink-1.0' in saved.device_declarations['{urn:schemas-jlabs-or-jp:device-1-0}X_JLABSCAP']


@pytest.mark.anyio
@pytest.mark.parametrize(
    ('manufacturer', 'model', 'fields', 'attributes', 'format', 'action'),
    [
        ('Sony', 'BDZ-TEST', '<av:chapterInfo>/chapters</av:chapterInfo><av:COGCount>10</av:COGCount>', '', 'sony', ''),
        ('Panasonic', 'BD/DVD Recorder', '', 'pxn:ChapterList="/chapters" pxn:CopyCount="10"', 'panasonic', ''),
        ('Sharp Corporation', 'BD-TEST', '<shp:chapterList>/chapters</shp:chapterList>', '', 'sharp', ''),
        ('TOSHIBA', 'DBR-TEST', '', '', 'toshiba', 'X_Toshiba_GetChapterList'),
        ('I-O DATA', 'GV-NTX2', '', '', 'shared_tags', 'X_TShrGetTags'),
    ],
)
async def test_metadata_collection_and_sidecars(
    manufacturer: str,
    model: str,
    fields: str,
    attributes: str,
    format: ChapterFormat,
    action: str,
    tmp_path: Path,
) -> None:
    chapter = dict(_CHAPTER_CASES)[format]
    # Sony detail IDs match the original numeric suffix, while chapter SOAP must retain the original DIDL ID
    didl = recording_xml(fields, attributes).replace(' refID="42"', '')
    source = MetadataSource(manufacturer, model, didl, chapter, action)
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recording = await server.get_metadata('V_42')
        assert isinstance(recording, Recording)
        assert source.requests == ['/description.xml', '/control']
        metadata = await server.collect_metadata(recording)
    assert metadata.chapters.chapters[0].position_ms == 12345
    assert metadata.chapters.raw_responses
    assert recording.long_description == 'First paragraph\nCast'
    assert recording.audio_components[0].attributes == {'main': '1'}
    assert recording.channel_number == '101'
    assert recording.source_xml is not None and '\ue0fe' in recording.source_xml
    assert '\ue0fe' not in recording.raw_xml
    assert metadata.device_description_xml == source.raw_description
    if action:
        expected = {'ObjectID': 'V_42'}
        if action == 'X_TShrGetTags':
            expected.update({'TagIDs': '', 'Filter': '', 'Type': ''})
        assert (action, expected) in source.calls
    if model == 'BDZ-TEST':
        assert [(query.name, query.status) for query in metadata.queries] == [
            ('chapters', 'known'),
            ('recording_details', 'known'),
            ('recording_description', 'known'),
            ('recording_start', 'known'),
            ('storage', 'known'),
        ]
        assert '270000' in metadata.model_dump_json()
        assert 'Last section' in metadata.model_dump_json()
    output = tmp_path / 'recording.m2ts'
    sidecar = MetadataSidecar(output, metadata)
    sidecar.on_event(AuthenticationCompleted(transfer_id='test', exchange_key_label=7))
    resumed = MetadataSidecar.resume(output)
    assert resumed is not None
    resumed.on_event(MoveCompleted(transfer_id='test'))
    saved = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
    assert saved.recording == recording
    assert saved.chapters == metadata.chapters
    assert saved.queries == metadata.queries
    assert [event['type'] for event in saved.transfer_events] == ['move_completed']
    assert output.with_suffix('.xml').read_text(encoding='utf-8') == recording.raw_xml
    with pytest.raises(FileExistsError):
        MetadataSidecar(output, metadata)


@pytest.mark.anyio
@pytest.mark.parametrize(
    'encoding', ['document', 'fragment', 'embedded', 'single_fragment', 'single_embedded', 'failed', 'unsupported']
)
async def test_sony_descriptions_reach_cli_and_archive(encoding: str, tmp_path: Path) -> None:
    source = MetadataSource('Sony', 'BDZ-TEST', recording_xml().replace(' refID="42"', ''))
    fields = '<summary>Program &amp; summary</summary><detail0>First section</detail0><detail7>Last section</detail7>'
    if encoding.startswith('single_'):
        fields = '<summary>Program &amp; summary</summary>'
    if encoding == 'unsupported':
        source.response_bodies['/pvr.xml'] = (
            b'<scpd xmlns="urn:schemas-upnp-org:service-1-0"><actionList><action><name>X_GetTitleList2</name></action>'
            b'<action><name>X_GetTitleInfo</name></action><action><name>X_GetMediaInfo</name></action></actionList></scpd>'
        )
    elif encoding != 'document':
        content = (
            fields
            if encoding in ('embedded', 'single_embedded')
            else escape(fields if encoding in ('fragment', 'single_fragment') else '<broken>')
        )
        source.response_bodies['X_GetTitleDetail'] = (
            f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:X_GetTitleDetailResponse xmlns:u="{_PVR}"><Result>{content}</Result>'
            '</u:X_GetTitleDetailResponse></s:Body></s:Envelope>'
        ).encode()
    async with source.serve() as (location, _):
        args = ['info', '--server', location, '--id', 'V_42']
        result = await asyncio.to_thread(CliRunner().invoke, app, [*args, '--json'])
        assert result.exit_code == 0, result.output
        metadata = RecordingMetadata.model_validate_json(result.stdout)
        queries = {query.name: query for query in metadata.queries}
        expected_status = encoding if encoding in ('failed', 'unsupported') else 'known'
        assert queries['recording_description'].status == expected_status
        assert queries['recording_start'].status == 'known'
        assert queries['storage'].status == 'known'
        output = tmp_path / 'recording.m2ts'
        MetadataSidecar(output, metadata)
        saved = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
        assert saved.queries == metadata.queries
        display = await asyncio.to_thread(CliRunner().invoke, app, args)
        assert display.exit_code == 0, display.output
        if expected_status == 'known':
            summary = 'Program summary' if encoding == 'document' else 'Program & summary'
            assert summary in display.output
            details = SonyMetadataResult.model_validate(queries['recording_description'].data)
            assert details.items[0].get_value('summary') == summary
            if encoding.startswith('single_') is False:
                assert 'Last section' in display.output
                assert 'First section' in display.output
                assert 'Last section' in saved.model_dump_json()
        if encoding == 'unsupported':
            assert all(action != 'X_GetTitleDetail' for action, _ in source.calls)
        elif encoding != 'document':
            assert source.response_bodies['X_GetTitleDetail'] in [response.body for response in saved.responses]


@pytest.mark.anyio
@pytest.mark.parametrize(('format', 'document'), _CHAPTER_CASES[:3])
async def test_chapter_document_format_is_independent_of_reference_field(format: ChapterFormat, document: str) -> None:
    # A storage server can retain a chapter URL while serving another recorder's document format
    source = MetadataSource('I-O DATA', 'RECBOX', recording_xml('<av:chapterInfo>/chapters</av:chapterInfo>'), document)
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recording = await server.get_metadata('V_42')
        assert isinstance(recording, Recording)
        chapters = await server.get_chapters(recording)
    assert [chapter.position_ms for chapter in chapters.chapters] == [12345]
    assert chapters.raw_responses == (document.encode('utf-8'),)


@pytest.mark.anyio
@pytest.mark.parametrize('is_embedded', [True, False])
@pytest.mark.parametrize('is_single', [True, False])
async def test_sony_start_fields_without_document_wrapper(is_embedded: bool, is_single: bool) -> None:
    source = MetadataSource('Sony', 'BDZ-TEST', recording_xml().replace(' refID="42"', ''))
    fields = '<recordStartDate>2026-01-01T19:59:57</recordStartDate>'
    if is_single is False:
        fields += '<startPTS>270000</startPTS>'
    content = fields if is_embedded else escape(fields)
    source.response_bodies['X_GetTitleInfo'] = (
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<u:X_GetTitleInfoResponse xmlns:u="{_PVR}"><Result>{content}</Result>'
        '</u:X_GetTitleInfoResponse></s:Body></s:Envelope>'
    ).encode()
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recording = await server.get_metadata('V_42')
        assert isinstance(recording, Recording)
        metadata = await server.collect_metadata(recording)
    start = next(query for query in metadata.queries if query.name == 'recording_start')
    assert start.status == 'known'
    assert start.data is not None
    details = SonyMetadataResult.model_validate(start.data)
    assert details.items[0].get_value('recordStartDate') == '2026-01-01T19:59:57'
    assert details.items[0].get_value('startPTS') == (None if is_single else '270000')
    assert source.response_bodies['X_GetTitleInfo'] in [response.body for response in metadata.responses]


@pytest.mark.anyio
async def test_nasne_http_metadata_keeps_unmappable_chapters() -> None:
    source = MetadataSource('Buffalo', 'buffalo-nasne', recording_xml('<av:COGCount>9</av:COGCount>'))
    async with source.serve() as (_, port):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=LocalResolver(port))) as session:
            async with DLNAClient(http_session=session) as dlna:
                server = await dlna.get_server('http://nasne.test/description.xml')
                recording = await server.get_metadata('V_42')
                assert isinstance(recording, Recording)
                metadata = await server.collect_metadata(recording)
    assert [chapter.position_ms for chapter in metadata.chapters.chapters] == [7500, None]
    assert [chapter.source_position for chapter in metadata.chapters.chapters] == [150, 350]
    assert [chapter.is_estimated for chapter in metadata.chapters.chapters] == [True, False]
    assert metadata.queries[-1].data['recording'] == {
        'id': 42,
        'descriptionLong': 'Extended information',
        'startDateTime': 1767265200,
    }
    assert len(metadata.chapters.raw_responses) == 2


@pytest.mark.anyio
@pytest.mark.parametrize('additional', [True, False])
async def test_optional_failure_preserves_metadata_and_cli_output(additional: bool) -> None:
    source = MetadataSource(
        'Panasonic',
        'BD/DVD Recorder',
        recording_xml(attributes='pxn:ChapterList="/chapters" pxn:CopyCount="1"'),
        '<broken>',
    )
    async with source.serve() as (location, _):
        args = ['info', '--server', location, '--id', 'V_42']
        if additional is False:
            args.append('--basic')
        result = await asyncio.to_thread(CliRunner().invoke, app, [*args, '--json'])
        assert result.exit_code == 0, result.output
        saved = RecordingMetadata.model_validate_json(result.stdout)
        assert saved.recording.protection.remaining_copy_count.value == 0
        assert saved.queries[0].status == ('failed' if additional else 'not_observed')
        assert ('/chapters' in source.requests) is additional
        assert [response.body for response in saved.responses] == ([b'<broken>'] if additional else [])
        result = await asyncio.to_thread(CliRunner().invoke, app, args)
        assert result.exit_code == 0, result.output
        assert 'Includes final Move' in result.output
        assert '1920x1080' in result.output
        assert '2026-01-01T20:00:00+09:00' in result.output
        assert (
            'Could not retrieve chapters' in result.output
            if additional
            else 'Could not retrieve chapters' not in result.output
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ('manufacturer', 'model', 'endpoint', 'body', 'failed_query'),
    [
        ('TOSHIBA', 'DBR-TEST', 'X_Toshiba_GetChapterList', b'<broken-soap>', 'chapters'),
        ('TOSHIBA', 'DBR-TEST', '/scpd.xml', b'<broken-service>', 'chapters'),
        ('TOSHIBA', 'DBR-TEST', '/scpd.xml', b'<scpd>\xff</scpd>', 'chapters'),
        (
            'Sony',
            'BDZ-TEST',
            'X_GetTitleList2',
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            b'<s:Body><Response><Result>&lt;broken-title&gt;</Result></Response></s:Body></s:Envelope>',
            'recording_details',
        ),
        ('Buffalo', 'buffalo-nasne', '/recorded/thumbnailMapGet', b'\x00\xff', 'chapters'),
        ('Buffalo', 'buffalo-nasne', '/recorded/titleListGet', b'{"unknown": "title data"}', 'recording_details'),
    ],
)
async def test_failed_metadata_parsing_archives_received_responses(
    manufacturer: str,
    model: str,
    endpoint: str,
    body: bytes,
    failed_query: str,
    tmp_path: Path,
) -> None:
    """Keep HTTP and SOAP documents available for later parsing after an optional query fails.

    Args:
        manufacturer (str): Device manufacturer
        model (str): Device model used to select the family
        endpoint (str): HTTP path or SOAP action returning the invalid document
        body (bytes): Complete but unparseable metadata response
        failed_query (str): Query expected to report the parsing failure
        tmp_path (Path): Isolated sidecar destination
    """

    source = MetadataSource(manufacturer, model, recording_xml(), action='X_Toshiba_GetChapterList')
    source.response_bodies[endpoint] = body
    async with source.serve() as (_, port):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=LocalResolver(port))) as session:
            async with DLNAClient(http_session=session) as dlna:
                server = await dlna.get_server('http://recorder.test/description.xml')
                recording = await server.get_metadata('V_42')
                assert isinstance(recording, Recording)
                metadata = await server.collect_metadata(recording)
    query_statuses = {query.name: query.status for query in metadata.queries}
    assert query_statuses[failed_query] == 'failed'
    output = tmp_path / 'recording.m2ts'
    MetadataSidecar(output, metadata)
    saved = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
    matching_responses = []
    for response in saved.responses:
        if response.body == body:
            matching_responses.append(response)
    (response,) = matching_responses
    assert response.status_code == 200
    assert response.headers
    if endpoint.startswith('/'):
        assert urlsplit(response.url).path == endpoint
        assert response.soap_action is None
    else:
        assert response.soap_action is not None and response.soap_action.endswith(f'#{endpoint}')


@pytest.mark.anyio
@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'utf-16-be', 'iso-8859-1'])
async def test_service_description_preserves_declared_encoding(encoding: str) -> None:
    """Query chapters using service definitions with a BOM or a declared character encoding."""

    xml = (
        f'<?xml version="1.0" encoding="{encoding.removesuffix("-sig")}"?>'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0"><!-- café -->'
        '<actionList><action><name>X_Toshiba_GetChapterList</name></action></actionList></scpd>'
    )
    body = xml.encode(encoding)
    if encoding == 'utf-16-be':
        body = b'\xfe\xff' + body
    source = MetadataSource(
        'TOSHIBA', 'DBR-TEST', recording_xml(), dict(_CHAPTER_CASES)['toshiba'], 'X_Toshiba_GetChapterList'
    )
    source.response_bodies['/scpd.xml'] = body
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recording = await server.get_metadata('V_42')
        assert isinstance(recording, Recording)
        metadata = await server.collect_metadata(recording)
    assert metadata.chapters.status == 'known'
    assert metadata.chapters.chapters[0].position_ms == 12345
    assert list(metadata.service_descriptions.values()) == [xml]
    assert metadata.responses[0].body == body


@pytest.mark.anyio
async def test_concurrent_metadata_collections_keep_separate_responses() -> None:
    """Concurrent collections on one connection archive only their own documents."""

    source = MetadataSource('Panasonic', 'BD/DVD Recorder', recording_xml())
    source.response_bodies = {'/chapters-first': b'<first>', '/chapters-second': b'<second>'}
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recordings: list[Recording] = []
        for name in ('first', 'second'):
            recording = parse_didl(recording_xml(attributes=f'pxn:ChapterList="/chapters-{name}"'), server.profile)[0]
            assert isinstance(recording, Recording)
            recordings.append(recording)
        archives = await asyncio.gather(*(server.collect_metadata(recording) for recording in recordings))
        assert [[response.body for response in archive.responses] for archive in archives] == [
            [b'<first>'],
            [b'<second>'],
        ]
        await server.fetch_metadata('/chapters-first')
        basic = await server.collect_metadata(recordings[0], additional=False)
        assert basic.responses == ()


@pytest.mark.anyio
@pytest.mark.parametrize('framing', ['length', 'chunked'])
@pytest.mark.parametrize('should_fail_finalized_sidecar', [False, True])
async def test_cli_move_archives_metadata_and_completion(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    framing: Framing,
    should_fail_finalized_sidecar: bool,
) -> None:
    """Run the CLI through listing, metadata collection, encrypted transfer and finalization."""

    monkeypatch.setenv('PYDTCPIP_CERTIFICATE', sink_device.certificate.hex())
    monkeypatch.setenv('PYDTCPIP_PRIVATE_KEY', sink_device.private_key.hex())
    monkeypatch.setattr('pydtcpip.__main__._COPY_COUNT_REQUERY_INTERVAL', 0.01)
    output = tmp_path / 'recording.m2ts'
    replace = os.replace
    failed_updates: list[str] = []

    def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        """Fail only the metadata update after the source has accepted finalization.

        Args:
            src (str | os.PathLike[str]): Completed temporary file
            dst (str | os.PathLike[str]): Destination of the replacement
        """

        if should_fail_finalized_sidecar is True and Path(dst) == output.with_suffix('.metadata.json'):
            archive = RecordingMetadata.model_validate_json(Path(src).read_bytes())
            if archive.transfer_events[-1]['type'] == 'move_finalized':
                failed_updates.append('move_finalized')
                raise OSError(errno.ENOSPC, 'Metadata volume is full')
        replace(src, dst)

    monkeypatch.setattr('pydtcpip.metadata.os.replace', replace_file)
    content = b'Test-only media bytes' * 1000
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, content, framing=framing) as media,
    ):
        metadata_source = MetadataSource(
            'Panasonic',
            'BD/DVD Recorder',
            recording_xml(
                attributes='pxn:CopyCount="10" pxn:ChapterList="/chapters"',
                resource_url=media.url,
            ),
            dict(_CHAPTER_CASES)['panasonic'],
        )
        metadata_source.requery_didl = recording_xml(attributes='pxn:CopyCount="9"', resource_url=media.url)
        metadata_source.requery_ready = source.completed
        metadata_source.metadata_didl = recording_xml(resource_url=media.url)
        async with metadata_source.serve() as (location, _):
            result = await asyncio.to_thread(
                CliRunner().invoke,
                app,
                [
                    '--data-dir',
                    str(tmp_path / 'device'),
                    'move',
                    str(output),
                    '--server',
                    location,
                    '--id',
                    'V_42',
                    '--yes',
                ],
            )
        assert result.exit_code == 0, result.output
        assert source.errors == []
        assert source.log[-2:] == ['finalize', 'complete']
    assert output.read_bytes() == content
    assert output.with_name(f'{output.name}.pydtcpip-resume.json').exists() is False
    assert failed_updates == (['move_finalized'] if should_fail_finalized_sidecar else [])
    saved_text = output.with_suffix('.metadata.json').read_text(encoding='utf-8')
    metadata = RecordingMetadata.model_validate_json(saved_text)
    assert metadata.recording.protection.remaining_copy_count.value == 9
    assert metadata.chapters.chapters[0].position_ms == 12345
    events = [event['type'] for event in metadata.transfer_events]
    responses = [event for event in metadata.transfer_events if event['type'] == 'media_info_received']
    assert [event['method'] for event in responses] == ['HEAD', 'GET']
    assert events.index('sink_persisted') < events.index('move_finalize_requested')
    assert events[-2:] == ['move_finalized', 'move_completed']
    assert sink_device.private_key.hex() not in saved_text
    assert 'exchange_key' not in saved_text
    assert 'resume_secret' not in saved_text


@pytest.mark.anyio
@pytest.mark.parametrize('lost_response', ['finalize', 'complete'])
@pytest.mark.parametrize('sidecar_error', ['malformed', 'unreadable'])
async def test_cli_resume_continues_without_readable_metadata(
    authority: FakeAuthority,
    sink_device: FakeDevice,
    source_device: FakeDevice,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    lost_response: str,
    sidecar_error: str,
) -> None:
    """Complete a real interrupted Move despite an unreadable optional metadata archive."""

    monkeypatch.setenv('PYDTCPIP_CERTIFICATE', sink_device.certificate.hex())
    monkeypatch.setenv('PYDTCPIP_PRIVATE_KEY', sink_device.private_key.hex())
    output = tmp_path / 'recording.m2ts'
    resume_path = output.with_name(f'{output.name}.pydtcpip-resume.json')
    sidecar_path = output.with_suffix('.metadata.json')
    content = b'Test-only resumed recording' * 1000
    async with (
        FakeSourceAKE(authority, source_device) as source,
        FakeMediaServer(source, content) as media,
    ):
        if lost_response == 'finalize':
            source.drop_finalize_responses = 1
        else:
            source.drop_complete_responses = 1
        args = ['--data-dir', str(tmp_path / 'device')]
        result = await asyncio.to_thread(
            CliRunner().invoke, app, [*args, 'move', str(output), '--url', media.url, '--yes']
        )
        assert result.exit_code == 1, result.output
        state = MoveResumeState.model_validate_json(resume_path.read_bytes())
        assert state.state == ('finalizing' if lost_response == 'finalize' else 'finalized')
        get_count = media.count('GET')
        sidecar_path.write_bytes(b'{"recording":')
        read_bytes = Path.read_bytes

        def read_file(path: Path) -> bytes:
            """Fail only the metadata read while keeping media and resume state readable."""

            if path == sidecar_path and sidecar_error == 'unreadable':
                raise PermissionError(errno.EACCES, 'Metadata access denied')
            return read_bytes(path)

        monkeypatch.setattr(Path, 'read_bytes', read_file)
        result = await asyncio.to_thread(CliRunner().invoke, app, [*args, 'resume', str(output)])
        assert result.exit_code == 0, result.output
        assert media.count('GET') == get_count
        assert source.log[-1] == 'complete'
        assert source.errors == []
    assert output.read_bytes() == content
    assert resume_path.exists() is False
    assert read_bytes(sidecar_path) == b'{"recording":'
    assert 'Could not load recording metadata' in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize('folder_title', ['USB1:Archive drive', 'ｕｓｂ２：Archive drive', 'All recordings'])
async def test_regza_storage_name_reaches_details_and_sidecar(tmp_path: Path, folder_title: str) -> None:
    """Follow a recording's advertised ancestors without assuming that an unrecognized folder is the HDD."""

    source = MetadataSource('TOSHIBA', 'DBR-TEST', recording_xml().replace('parentID="0"', 'parentID="folder"'))
    for identifier, parent, title in [('folder', 'usb', 'Programs'), ('usb', '0', folder_title)]:
        source.metadata_by_id[identifier] = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<container id="{identifier}" parentID="{parent}"><dc:title>{title}</dc:title>'
            '<upnp:class>object.container</upnp:class></container></DIDL-Lite>'
        )
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recording = await server.get_metadata('V_42')
        assert isinstance(recording, Recording)
        await server.collect_metadata(recording, additional=False)
        assert len(source.calls) == 1
        metadata = await server.collect_metadata(recording)
        storage = next(query for query in metadata.queries if query.name == 'storage')
        if folder_title == 'All recordings':
            assert storage.status == 'omitted'
            assert storage.data == {}
        else:
            assert storage.status == 'known'
            assert storage.data == {'name': 'Archive drive', 'container_id': 'usb', 'container_title': folder_title}
        assert [arguments['ObjectID'] for name, arguments in source.calls if name == 'Browse'] == [
            'V_42',
            'folder',
            'usb',
        ]
        MetadataSidecar(tmp_path / 'recording.m2ts', metadata)
        saved = RecordingMetadata.model_validate_json(
            (tmp_path / 'recording.metadata.json').read_text(encoding='utf-8')
        )
        assert saved.queries == metadata.queries
        assert any(folder_title.encode('utf-8') in response.body for response in saved.responses)
        info = await asyncio.to_thread(CliRunner().invoke, app, ['info', '--server', location, '--id', 'V_42'])
        assert info.exit_code == 0, info.output
        if storage.status == 'known':
            assert 'Archive drive' in info.output


@pytest.mark.anyio
@pytest.mark.parametrize(
    ('manufacturer', 'model', 'root'),
    [
        ('HUAWEI', 'TT01', '0/video/rec/all'),
        ('Mitsubishi Electric Corporation', 'AM900', '0-0-0-0'),
        ('Panasonic', 'STW2000', '0/rec/all'),
        ('Example', 'Standard media server', '0'),
    ],
)
async def test_recording_enumeration_uses_the_family_container(manufacturer: str, model: str, root: str) -> None:
    source = MetadataSource(manufacturer, model, recording_xml())
    async with source.serve() as (location, _), DLNAClient() as dlna:
        server = await dlna.get_server(location)
        recordings = [recording async for recording in server.iter_recordings()]
        assert len(recordings) == 1
        assert source.calls[0][1]['ObjectID'] == root
        source.calls.clear()
        recordings = [recording async for recording in server.iter_recordings('explicit-folder')]
        assert len(recordings) == 1
        assert source.calls[0][1]['ObjectID'] == 'explicit-folder'
