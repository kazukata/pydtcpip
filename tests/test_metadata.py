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
                else ['X_GetTitleList2', 'X_GetTitleInfo', 'X_GetMediaInfo']
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
            ('recording_start', 'known'),
            ('storage', 'known'),
        ]
        assert '270000' in metadata.model_dump_json()
    output = tmp_path / 'recording.m2ts'
    sidecar = MetadataSidecar(output, metadata)
    sidecar.on_event(AuthenticationCompleted(transfer_id='test', exchange_key_label=7))
    resumed = MetadataSidecar.resume(output)
    assert resumed is not None
    resumed.on_event(MoveCompleted(transfer_id='test'))
    saved = RecordingMetadata.model_validate_json(output.with_suffix('.metadata.json').read_bytes())
    assert saved.recording == recording
    assert saved.chapters == metadata.chapters
    assert [event['type'] for event in saved.transfer_events] == ['move_completed']
    assert output.with_suffix('.xml').read_text(encoding='utf-8') == recording.raw_xml
    with pytest.raises(FileExistsError):
        MetadataSidecar(output, metadata)


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
