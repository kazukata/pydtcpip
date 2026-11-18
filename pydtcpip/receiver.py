"""Act as a DTCP-IP NAS-like destination and receive Moves sent by recorders over POST."""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web

from pydtcpip.dlna.didl import DC_NAMESPACE, DIDL_NAMESPACE, UPNP_NAMESPACE, parse_didl
from pydtcpip.dlna.profiles import GENERIC
from pydtcpip.dlna.protocol_info import parse_content_type
from pydtcpip.dlna.soap import SOAP_ENVELOPE_NAMESPACE
from pydtcpip.dlna.ssdp import MEDIA_SERVER_DEVICE_TYPE, SSDPAdvertiser
from pydtcpip.dtcp.ake import SRMUpdateCallback, authenticate, open_ake_connection
from pydtcpip.dtcp.crypto import DeviceCredentials
from pydtcpip.dtcp.move import MoveTransaction
from pydtcpip.dtcp.pcp import read_packets
from pydtcpip.dtcp.srm import SRM
from pydtcpip.errors import DTCPError
from pydtcpip.events import EventCallback, TransferStarted, dispatch_event
from pydtcpip.models import MoveResumeState, Recording
from pydtcpip.sinks import Sink
from pydtcpip.transfer import abort_move, finish_move, receive_move_body


_LOGGER = logging.getLogger(__name__)

_DTCP_METADATA_NAMESPACE = 'urn:schemas-dtcp-com:metadata-1-0/'
_CONTENT_DIRECTORY = 'urn:schemas-upnp-org:service:ContentDirectory:1'
_CONNECTION_MANAGER = 'urn:schemas-upnp-org:service:ConnectionManager:1'
_EVENT_NAMESPACE = 'urn:schemas-upnp-org:event-1-0'

# Accepted upload profiles (protected TS from Japanese terrestrial, BS, and CS broadcasts)
_UPLOAD_PROFILES = ('DTCP_MPEG_TS_JP_T', 'DTCP_AVC_TS_JP_AAC_T')

# dtcp:uploadInfo for transaction-based Move (same bit layout as DTCP.COM_FLAGS)
_TRANSACTION_MOVE_UPLOAD_INFO = 0xC0000000

# Actions and input and output argument names for each service
_ACTIONS: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    'ContentDirectory': {
        'GetSearchCapabilities': ((), ('SearchCaps',)),
        'GetSortCapabilities': ((), ('SortCaps',)),
        'GetSystemUpdateID': ((), ('Id',)),
        'Browse': (
            ('ObjectID', 'BrowseFlag', 'Filter', 'StartingIndex', 'RequestedCount', 'SortCriteria'),
            ('Result', 'NumberReturned', 'TotalMatches', 'UpdateID'),
        ),
        'CreateObject': (('ContainerID', 'Elements'), ('ObjectID', 'Result')),
        'DestroyObject': (('ObjectID',), ()),
        'X_GetDLNAUploadProfiles': (('UploadProfiles',), ('SupportedUploadProfiles',)),
    },
    'ConnectionManager': {
        'GetProtocolInfo': ((), ('Source', 'Sink')),
        'GetCurrentConnectionIDs': ((), ('ConnectionIDs',)),
    },
}
_NUMERIC_STATE_VARIABLES = frozenset(
    ('StartingIndex', 'RequestedCount', 'NumberReturned', 'TotalMatches', 'UpdateID', 'Id')
)

# Request key for passing the Move authenticated before the body to the upload handler
_MOVE_TRANSACTION_KEY = web.RequestKey('move_transaction', MoveTransaction)

# Callback that creates a sink for each incoming recording
type SinkFactory = Callable[[Recording], Sink | Awaitable[Sink]]

# Callback receiving resume state and the recording it belongs to
## Pass None when completion or cancellation makes the resume state unnecessary
type DubbingResumeStateCallback = Callable[[Recording, MoveResumeState | None], Awaitable[None] | None]


@dataclass(frozen=True)
class _UploadItem:
    """A recording registered by CreateObject and awaiting transfer."""

    item_id: str
    # item element returned by Browse, with importUri added
    element: ET.Element
    recording: Recording


class _UPnPActionError(Exception):
    """Action failure returned as a UPnPError in a SOAP Fault."""

    def __init__(self, error_code: int, description: str) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description


class DubbingReceiver:
    """
    Act as a DTCP-IP NAS-like destination and receive Moves sent by recorders over POST.
    Advertise pydtcpip as the device, manufacturer, and model name, as a generic NAS.
    Transfers consume a source copy; if only the final Move remains or copying is prohibited, the source recording is deleted.
    """

    def __init__(
        self,
        credentials: DeviceCredentials,
        sink_factory: SinkFactory,
        *,
        interface: str = '0.0.0.0',
        allowed_sources: frozenset[str] | None = None,
        port: int = 57000,
        name: str = 'pydtcpip',
        udn: str | None = None,
        srm: SRM | None = None,
        on_srm_update: SRMUpdateCallback | None = None,
        on_resume_state: DubbingResumeStateCallback | None = None,
        on_event: EventCallback | None = None,
        timeout: float = 60.0,
    ) -> None:
        """
        Configure the transfer destination.

        Args:
            credentials (DeviceCredentials): Sink device certificate and private key
            sink_factory (SinkFactory): Callback creating a sink for each incoming recording
            interface (str): Local IPv4 address to bind (0.0.0.0 listens on all interfaces)
            allowed_sources (frozenset[str] | None): Permitted recorder addresses (None accepts any source)
            port (int): HTTP listening port
            name (str): Name shown in the recorder destination list
            udn (str | None): Device UDN (derived from name and address if None; changing it identifies a new device)
            srm (SRM | None): Saved SRM
            on_srm_update (SRMUpdateCallback | None): Callback for a newer SRM
            on_resume_state (DubbingResumeStateCallback | None): Callback for recording and resume state before finalization
            on_event (EventCallback | None): Event callback
            timeout (float): Authentication and body read timeout in seconds
        """

        self.credentials = credentials
        self.srm = srm if srm is not None else SRM()
        self.interface = interface
        self.allowed_sources = allowed_sources
        self.port = port
        self.name = name
        self.udn = udn if udn is not None else f'uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f"pydtcpip:{name}:{interface}")}'
        self._sink_factory = sink_factory
        self._on_srm_update = on_srm_update
        self._on_resume_state = on_resume_state
        self._on_event = on_event
        self._timeout = timeout
        self._items: dict[str, _UploadItem] = {}
        self._subscriptions: dict[str, tuple[str, str, float]] = {}
        self._update_id = 0
        self._upload_lock = asyncio.Lock()
        self._notify_tasks: set[asyncio.Task[None]] = set()
        self._runner: web.AppRunner | None = None
        self._advertiser: SSDPAdvertiser | None = None
        self._http_session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        """Return the listener URL (a wildcard address is not a URL to advertise to recorders)."""

        return f'http://{self.interface}:{self.port}'

    async def start(self) -> None:
        """Start the HTTP listener and advertise the transfer destination."""

        application = web.Application()
        application.router.add_get('/description.xml', self._handle_description)
        application.router.add_get('/services/{name}.xml', self._handle_service_description)
        application.router.add_post('/control/{service}', self._handle_control)
        application.router.add_route('SUBSCRIBE', '/events/{service}', self._handle_subscribe)
        application.router.add_route('UNSUBSCRIBE', '/events/{service}', self._handle_unsubscribe)
        application.router.add_post('/upload/{item_id}', self._handle_upload, expect_handler=self._handle_upload_expect)

        self._http_session = aiohttp.ClientSession()
        self._runner = web.AppRunner(application, access_log=_LOGGER)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.interface, self.port)
            await site.start()

            # Advertise the OS-assigned port when the requested listening port was 0
            if self.port == 0:
                server = site._server  # pyright: ignore[reportPrivateUsage]
                if isinstance(server, asyncio.Server) and len(server.sockets) > 0:
                    self.port = int(server.sockets[0].getsockname()[1])

            self._advertiser = SSDPAdvertiser(
                interface=self.interface,
                location=f'{self.base_url}/description.xml',
                udn=self.udn,
                targets=(MEDIA_SERVER_DEVICE_TYPE, _CONTENT_DIRECTORY, _CONNECTION_MANAGER),
                allowed_senders=self.allowed_sources,
                server='Python UPnP/1.0 pydtcpip/1.0',
            )
            await self._advertiser.start()
        except BaseException:
            # Startup may fail after HTTP begins listening; release every acquired resource before retrying
            await self.stop()
            raise
        _LOGGER.info(
            f'Listening for incoming transfers: {self.name} ({self.base_url}), '
            f'sources: {", ".join(sorted(self.allowed_sources)) if self.allowed_sources is not None else "any"}'
        )

    async def stop(self) -> None:
        """Withdraw advertisements, wait for an active transfer, and stop the HTTP listener."""

        if self._advertiser is not None:
            await self._advertiser.stop()
            self._advertiser = None
        # Let an active transfer finish finalization and completion before stopping
        async with self._upload_lock:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def _handle_description(self, request: web.Request) -> web.Response:
        """
        Return the device description, including to devices not listed as sources.

        Args:
            request (web.Request): Device description request

        Returns:
            web.Response: Device description XML
        """

        root = ET.Element('root', {'xmlns': 'urn:schemas-upnp-org:device-1-0'})
        spec_version = ET.SubElement(root, 'specVersion')
        ET.SubElement(spec_version, 'major').text = '1'
        ET.SubElement(spec_version, 'minor').text = '0'
        device = ET.SubElement(root, 'device')
        namespaces = {
            'dlna': 'urn:schemas-dlna-org:device-1-0',
            'move': 'urn:schemas-toshiba-co-jp:movemetadata-1-0/',
        }
        for key, value in {
            'deviceType': MEDIA_SERVER_DEVICE_TYPE,
            'friendlyName': self.name,
            'manufacturer': 'pydtcpip',
            'modelName': 'pydtcpip',
            'UDN': self.udn,
            'dlna:X_DLNADOC': 'DMS-1.50',
            'dlna:X_DLNACAP': 'av-upload,dtcp-move',
            # REGZA 43Z570L hides destinations without X_move_support indicating supported Move formats
            'move:X_move_support': 'MPEG_TTS_CP,PN_FLAG=C0000000',
        }.items():
            element = ET.SubElement(device, key)
            element.text = value
            # REGZA 43Z570L requires extension namespaces on each element rather than on the root
            if ':' in key:
                prefix = key.partition(':')[0]
                element.set(f'xmlns:{prefix}', namespaces[prefix])
        service_list = ET.SubElement(device, 'serviceList')
        for service_name in _ACTIONS:
            service = ET.SubElement(service_list, 'service')
            for key, value in {
                'serviceType': f'urn:schemas-upnp-org:service:{service_name}:1',
                'serviceId': f'urn:upnp-org:serviceId:{service_name}',
                'SCPDURL': f'/services/{service_name}.xml',
                'controlURL': f'/control/{service_name}',
                'eventSubURL': f'/events/{service_name}',
            }.items():
                ET.SubElement(service, key).text = value
        return self._xml_response(ET.tostring(root, encoding='utf-8', xml_declaration=True))

    async def _handle_service_description(self, request: web.Request) -> web.Response:
        """
        Return the service description (SCPD).

        Args:
            request (web.Request): Request containing the service name

        Returns:
            web.Response: Service description XML or a 404 response
        """

        service_name = request.match_info['name']
        if service_name not in _ACTIONS:
            return web.Response(status=404)

        root = ET.Element('scpd', {'xmlns': 'urn:schemas-upnp-org:service-1-0'})
        spec_version = ET.SubElement(root, 'specVersion')
        ET.SubElement(spec_version, 'major').text = '1'
        ET.SubElement(spec_version, 'minor').text = '0'

        # List each action's input and output arguments and collect their state variables
        action_list = ET.SubElement(root, 'actionList')
        variables: set[str] = set()
        for action_name, (inputs, outputs) in _ACTIONS[service_name].items():
            action = ET.SubElement(action_list, 'action')
            ET.SubElement(action, 'name').text = action_name
            if len(inputs) + len(outputs) == 0:
                continue
            argument_list = ET.SubElement(action, 'argumentList')
            for direction, names in (('in', inputs), ('out', outputs)):
                for argument_name in names:
                    argument = ET.SubElement(argument_list, 'argument')
                    ET.SubElement(argument, 'name').text = argument_name
                    ET.SubElement(argument, 'direction').text = direction
                    ET.SubElement(argument, 'relatedStateVariable').text = f'A_ARG_TYPE_{argument_name}'
                    variables.add(argument_name)

        # Define argument state variables and evented state variables
        state_table = ET.SubElement(root, 'serviceStateTable')
        for variable in sorted(variables):
            state = ET.SubElement(state_table, 'stateVariable', {'sendEvents': 'no'})
            ET.SubElement(state, 'name').text = f'A_ARG_TYPE_{variable}'
            ET.SubElement(state, 'dataType').text = 'ui4' if variable in _NUMERIC_STATE_VARIABLES else 'string'
        evented_variables = (
            ('SystemUpdateID',)
            if service_name == 'ContentDirectory'
            else ('SourceProtocolInfo', 'SinkProtocolInfo', 'CurrentConnectionIDs')
        )
        for variable in evented_variables:
            state = ET.SubElement(state_table, 'stateVariable', {'sendEvents': 'yes'})
            ET.SubElement(state, 'name').text = variable
            ET.SubElement(state, 'dataType').text = 'ui4' if variable == 'SystemUpdateID' else 'string'
        return self._xml_response(ET.tostring(root, encoding='utf-8', xml_declaration=True))

    async def _handle_control(self, request: web.Request) -> web.Response:
        """
        Handle a SOAP action.

        Args:
            request (web.Request): SOAP request containing the service and action

        Returns:
            web.Response: SOAP response, fault, or HTTP error
        """

        service_name = request.match_info['service']
        if service_name not in _ACTIONS:
            return web.Response(status=404)

        try:
            # Extract the action and input arguments from the SOAP body
            body = await request.read()
            try:
                envelope = ET.fromstring(body)
            except ET.ParseError as ex:
                raise _UPnPActionError(401, 'Could not parse the request XML.') from ex
            soap_body = envelope.find(f'{{{SOAP_ENVELOPE_NAMESPACE}}}Body')
            if soap_body is None or len(soap_body) != 1:
                raise _UPnPActionError(401, 'Specify exactly one SOAP action.')
            action = soap_body[0].tag.rpartition('}')[2]
            if action not in _ACTIONS[service_name]:
                raise _UPnPActionError(401, f'Unsupported action: {action}')
            arguments = {child.tag.rpartition('}')[2]: child.text or '' for child in soap_body[0]}
            if set(_ACTIONS[service_name][action][0]).issubset(arguments) is False:
                raise _UPnPActionError(402, 'A required action argument is missing.')

            # Accept transfer registration and cancellation only from allowed sources
            if (
                action in ('CreateObject', 'DestroyObject')
                and self.allowed_sources is not None
                and request.remote not in self.allowed_sources
            ):
                return web.Response(status=403)

            _LOGGER.info(f'Received UPnP action: {request.remote} {service_name} {action}')
            # Use the accepted socket address, not the Host header or the wildcard listener address
            assert request.transport is not None
            local_address = request.transport.get_extra_info('sockname')
            base_url = f'http://{local_address[0]}:{local_address[1]}'
            result = await self._run_action(action, arguments, base_url)
        except _UPnPActionError as ex:
            _LOGGER.warning(f'Could not process UPnP action: {ex.description}')
            fault = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NAMESPACE}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
                '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                f'<errorCode>{ex.error_code}</errorCode><errorDescription>{escape(ex.description)}</errorDescription>'
                '</UPnPError></detail></s:Fault></s:Body></s:Envelope>'
            )
            return self._xml_response(fault.encode('utf-8'), status=500)

        # Place output arguments in the response in service definition order
        output_xml = ''.join(f'<{name}>{escape(value)}</{name}>' for name, value in result.items())
        response = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NAMESPACE}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action}Response xmlns:u="urn:schemas-upnp-org:service:{service_name}:1">{output_xml}</u:{action}Response></s:Body>'
            '</s:Envelope>'
        )
        return self._xml_response(response.encode('utf-8'))

    async def _run_action(self, action: str, arguments: dict[str, str], base_url: str) -> dict[str, str]:
        """
        Run an action and return its output arguments.

        Args:
            action (str): Action name
            arguments (dict[str, str]): Input arguments
            base_url (str): Local HTTP endpoint used by the recorder

        Returns:
            dict[str, str]: Output argument names and values
        """

        # Search, sorting, and connection management are unsupported; return empty values
        if action == 'GetSearchCapabilities':
            return {'SearchCaps': ''}
        if action == 'GetSortCapabilities':
            return {'SortCaps': ''}
        if action == 'GetSystemUpdateID':
            return {'Id': str(self._update_id)}
        if action == 'GetProtocolInfo':
            return {'Source': '', 'Sink': ''}
        if action == 'GetCurrentConnectionIDs':
            return {'ConnectionIDs': ''}

        # Return the accepted upload profiles offered by the source
        if action == 'X_GetDLNAUploadProfiles':
            if arguments['UploadProfiles'] == '':
                return {'SupportedUploadProfiles': ','.join(_UPLOAD_PROFILES)}
            offered = arguments['UploadProfiles'].split(',')
            supported = [profile for profile in _UPLOAD_PROFILES if profile in offered]
            return {'SupportedUploadProfiles': ','.join(supported)}

        if action == 'Browse':
            return self._browse(arguments)
        if action == 'CreateObject':
            return await self._create_object(arguments, base_url)

        # DestroyObject arrives when the source cancels; received data remains in the sink
        if action == 'DestroyObject':
            self._items.pop(arguments['ObjectID'], None)
            self._update_id += 1
            return {}
        raise _UPnPActionError(401, f'Unsupported action: {action}')

    def _browse(self, arguments: dict[str, str]) -> dict[str, str]:
        """
        Return the destination root folder and registered recordings.

        Args:
            arguments (dict[str, str]): Browse input arguments

        Returns:
            dict[str, str]: Browse output arguments
        """

        root = ET.Element(f'{{{DIDL_NAMESPACE}}}DIDL-Lite')
        object_id = arguments['ObjectID']
        browse_flag = arguments['BrowseFlag']

        # Return the root folder as a destination accepting video uploads
        if browse_flag == 'BrowseMetadata' and object_id == '0':
            container = ET.SubElement(
                root,
                f'{{{DIDL_NAMESPACE}}}container',
                {'id': '0', 'parentID': '-1', 'restricted': '0', 'childCount': str(len(self._items))},
            )
            ET.SubElement(container, f'{{{DC_NAMESPACE}}}title').text = self.name
            ET.SubElement(container, f'{{{UPNP_NAMESPACE}}}class').text = 'object.container.storageFolder'
            ET.SubElement(
                container, f'{{{UPNP_NAMESPACE}}}createClass', {'includeDerived': '1'}
            ).text = 'object.item.videoItem'
            total_matches = 1
        elif browse_flag == 'BrowseMetadata' and object_id in self._items:
            root.append(self._items[object_id].element)
            total_matches = 1
        elif browse_flag == 'BrowseDirectChildren' and object_id == '0':
            if arguments['StartingIndex'].isdecimal() is False or arguments['RequestedCount'].isdecimal() is False:
                raise _UPnPActionError(402, 'Invalid listing range.')
            start = int(arguments['StartingIndex'])
            count = int(arguments['RequestedCount'])
            items = list(self._items.values())
            total_matches = len(items)
            end = start + count if count > 0 else None
            for item in items[start:end]:
                root.append(item.element)
        else:
            raise _UPnPActionError(701, 'The specified item does not exist.')

        return {
            'Result': ET.tostring(root, encoding='unicode'),
            'NumberReturned': str(len(root)),
            'TotalMatches': str(total_matches),
            'UpdateID': str(self._update_id),
        }

    async def _create_object(self, arguments: dict[str, str], base_url: str) -> dict[str, str]:
        """
        Register an incoming recording and return the body destination (importUri).

        Args:
            arguments (dict[str, str]): CreateObject input arguments
            base_url (str): Local HTTP endpoint used by the recorder

        Returns:
            dict[str, str]: Registered item ID and DIDL-Lite with importUri
        """

        if arguments['ContainerID'] not in ('0', 'DLNA.ORG_AnyContainer'):
            raise _UPnPActionError(710, 'The specified destination does not exist.')
        try:
            root = ET.fromstring(arguments['Elements'])
        except ET.ParseError as ex:
            raise _UPnPActionError(712, 'Could not parse recording metadata (DIDL-Lite).') from ex

        # Accept a recording with exactly one DTCP-IP-protected video resource
        items = root.findall(f'{{{DIDL_NAMESPACE}}}item')
        if len(items) != 1:
            raise _UPnPActionError(712, 'Specify exactly one recording.')
        item = items[0]
        resources = item.findall(f'{{{DIDL_NAMESPACE}}}res')
        upnp_class = item.findtext(f'{{{UPNP_NAMESPACE}}}class') or ''
        if upnp_class.startswith('object.item.videoItem') is False or len(resources) != 1:
            raise _UPnPActionError(712, 'Specify exactly one video resource.')
        resource = resources[0]
        if 'application/x-dtcp1' not in resource.get('protocolInfo', ''):
            raise _UPnPActionError(712, 'Specify a DTCP-IP-protected resource.')

        # uploadInfo identifies the Move method chosen by the source; the sink cannot substitute another method
        try:
            upload_info = int(resource.get(f'{{{_DTCP_METADATA_NAMESPACE}}}uploadInfo', '0'), 16)
        except ValueError as ex:
            raise _UPnPActionError(712, 'Invalid dtcp:uploadInfo value.') from ex
        if upload_info != _TRANSACTION_MOVE_UPLOAD_INFO:
            raise _UPnPActionError(712, 'Only transaction-based Move is supported.')

        # Assign the item ID and body destination, and retain metadata for sink creation
        item_id = uuid.uuid4().hex
        item.set('id', item_id)
        item.set('parentID', '0')
        resource.set('importUri', f'{base_url}/upload/{item_id}')
        resource.text = ''
        recordings = [
            parsed
            for parsed in parse_didl(ET.tostring(root, encoding='unicode'), GENERIC)
            if isinstance(parsed, Recording)
        ]
        # Keep the sender's original IDs, URLs and metadata before assigning receiver-local values
        recordings[0] = recordings[0].model_copy(update={'source_xml': arguments['Elements']})
        self._items[item_id] = _UploadItem(item_id, item, recordings[0])
        self._update_id += 1
        # Preserve the sender's namespace prefixes without changing ElementTree's global namespace registry
        document = minidom.parseString(arguments['Elements'])
        response_root = document.documentElement
        assert response_root is not None
        # Match the direct children validated above, not same-named descendants inside vendor extensions
        response_item = next(
            child
            for child in response_root.childNodes
            if isinstance(child, minidom.Element) and child.namespaceURI == DIDL_NAMESPACE and child.localName == 'item'
        )
        response_item.setAttribute('id', item_id)
        response_item.setAttribute('parentID', '0')
        response_resource = next(
            child
            for child in response_item.childNodes
            if isinstance(child, minidom.Element) and child.namespaceURI == DIDL_NAMESPACE and child.localName == 'res'
        )
        response_resource.setAttribute('importUri', f'{base_url}/upload/{item_id}')
        while response_resource.firstChild is not None:
            response_resource.removeChild(response_resource.firstChild)
        result_xml = response_root.toxml()
        document.unlink()
        return {'ObjectID': item_id, 'Result': result_xml}

    async def _handle_upload_expect(self, request: web.Request) -> web.StreamResponse | None:
        """
        Authenticate the Move before the body and return 100 Continue with the exchange key label.
        aiohttp normally sends 100 Continue immediately without headers, so control its timing and headers here.

        Args:
            request (web.Request): POST request before reading its body

        Returns:
            web.StreamResponse | None: Rejection response, or None if accepted
        """

        item_id = request.match_info['item_id']
        if self.allowed_sources is not None and request.remote not in self.allowed_sources:
            return web.Response(status=403)
        if item_id not in self._items:
            return web.Response(status=404)
        if request.headers.get('Expect', '').lower() != '100-continue':
            return web.Response(status=417)

        # Accept one transfer at a time to preserve the order through finalization
        if self._upload_lock.locked() is True:
            return web.Response(status=503)
        await self._upload_lock.acquire()

        writer: asyncio.StreamWriter | None = None
        transaction: MoveTransaction | None = None
        try:
            # The source advertises its authentication endpoint in Content-Type parameters
            content_type, parameters = parse_content_type(request.headers.get('Content-Type', ''))
            host = parameters.get('DTCP1HOST', '')
            port = parameters.get('DTCP1PORT', '')
            if (
                content_type != 'application/x-dtcp1'
                or host != request.remote
                or port.isdecimal() is False
                or not 1 <= int(port) <= 65535
            ):
                raise DTCPError('The source DTCP-IP authentication endpoint is invalid.')

            # Receive a transfer-specific exchange key through Move authentication
            reader, writer = await open_ake_connection(host, int(port), timeout=self._timeout)
            async with asyncio.timeout(self._timeout):
                result = await authenticate(
                    reader,
                    writer,
                    self.credentials,
                    self.srm,
                    is_move=True,
                    transfer_id=item_id,
                    host=host,
                    port=int(port),
                    on_srm_update=self._on_srm_update,
                    on_event=self._on_event,
                )
            self.srm = result.srm
            transaction = MoveTransaction(
                host,
                int(port),
                result.exchange_key,
                transfer_id=item_id,
                timeout=self._timeout,
                connection=(reader, writer),
                on_event=self._on_event,
            )
            request[_MOVE_TRANSACTION_KEY] = transaction

            # Return 100 Continue with the key label so the source starts sending the Move-encrypted body
            await request.writer.write(
                f'HTTP/1.1 100 Continue\r\nBLKMove.dtcp.com: {result.exchange_key.label:02X}\r\n\r\n'.encode()
            )
            return None

        except BaseException as ex:
            # Since the body will not be received, cancel any started Move and release the lock for every exception
            ## Otherwise all later transfers return 503 and stop() never finishes
            try:
                if transaction is not None:
                    await abort_move(transaction, None, on_event=self._on_event)
                    await transaction.close()
                elif writer is not None:
                    writer.close()
            finally:
                self._upload_lock.release()
            if isinstance(ex, Exception) is False:
                raise
            _LOGGER.error(f'Incoming transfer authentication failed: {request.remote}', exc_info=ex)
            return web.Response(status=500)

    async def _handle_upload(self, request: web.Request) -> web.StreamResponse:
        """
        Receive a Move body into the sink and finalize after persistence.
        The source processes finalization only after the final HTTP response, so send 200 first.

        Args:
            request (web.Request): POST request whose body follows 100 Continue

        Returns:
            web.StreamResponse: Response to the source
        """

        transaction = request.get(_MOVE_TRANSACTION_KEY)
        if transaction is None:
            return web.Response(status=500)

        item_id = request.match_info['item_id']
        response = web.Response(status=200)
        sink: Sink | None = None
        try:
            # If the source sent DestroyObject after authentication, cancel without receiving the body
            item = self._items.get(item_id)
            if item is None:
                raise DTCPError('The source cancelled the transfer registration.')
            recording = item.recording
            created = self._sink_factory(recording)
            sink = await created if isinstance(created, Awaitable) else created
            await dispatch_event(
                self._on_event,
                TransferStarted(transfer_id=item_id, total_http_bytes=request.content_length, total_content_bytes=None),
            )

            # aiohttp accepts only Content-Length or chunked bodies, so HTTP framing identifies the body end
            await receive_move_body(
                transaction,
                read_packets(request.content),
                sink,
                expected_http_bytes=request.content_length,
                expected_content_bytes=None,
                on_event=self._on_event,
            )

            # Send 200 after saving resume state and before finalization; the source waits for the HTTP response
            async def send_response() -> None:
                await response.prepare(request)
                await response.write_eof()

            async def save_resume_state(resume_state: MoveResumeState | None) -> None:
                if self._on_resume_state is not None:
                    result = self._on_resume_state(recording, resume_state)
                    if result is not None:
                        await result

            await finish_move(
                transaction,
                sink,
                on_resume_state=save_resume_state,
                on_event=self._on_event,
                before_finalize=send_response,
            )
            _LOGGER.info(f'Received transfer: {recording.title} ({transaction.written_bytes} bytes)')
            return response

        except BaseException as ex:
            # Before finalization cancel and discard partial data; afterward retain the full data for resumption
            await abort_move(transaction, sink, on_event=self._on_event)
            if isinstance(ex, Exception) is False:
                raise
            _LOGGER.error(f'Incoming transfer failed: {item_id}', exc_info=ex)
            return response if response.prepared is True else web.Response(status=500)

        finally:
            await transaction.close()
            self._upload_lock.release()

    async def _handle_subscribe(self, request: web.Request) -> web.Response:
        """
        Accept GENA SUBSCRIBE requests from allowed source recorders.

        Args:
            request (web.Request): Subscription or renewal request

        Returns:
            web.Response: Subscription response or HTTP error
        """

        if self.allowed_sources is not None and request.remote not in self.allowed_sources:
            return web.Response(status=403)
        service_name = request.match_info['service']
        if service_name not in _ACTIONS:
            return web.Response(status=404)

        # New subscriptions provide a CALLBACK; renewals provide an SID
        sid = request.headers.get('SID')
        if sid is None:
            callback = request.headers.get('CALLBACK', '').strip().strip('<>')
            parsed = urlsplit(callback)
            if parsed.scheme != 'http' or parsed.hostname != request.remote:
                return web.Response(status=412)
            sid = f'uuid:{uuid.uuid4()}'
        elif sid in self._subscriptions:
            callback = self._subscriptions[sid][0]
        else:
            return web.Response(status=412)

        self._subscriptions[sid] = (callback, service_name, time.monotonic() + 1800)
        task = asyncio.create_task(self._notify(sid))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)
        return web.Response(status=200, headers={'SID': sid, 'TIMEOUT': 'Second-1800'})

    async def _handle_unsubscribe(self, request: web.Request) -> web.Response:
        """
        Accept GENA UNSUBSCRIBE requests.

        Args:
            request (web.Request): Cancellation request containing the subscription ID

        Returns:
            web.Response: Cancellation response or HTTP error
        """

        if self.allowed_sources is not None and request.remote not in self.allowed_sources:
            return web.Response(status=403)
        self._subscriptions.pop(request.headers.get('SID', ''), None)
        return web.Response(status=200)

    async def _notify(self, sid: str) -> None:
        """
        Send initial state variable values after accepting a subscription.

        Args:
            sid (str): Subscription identifier
        """

        # Wait for the subscription response to arrive; otherwise the source sees an unknown SID
        await asyncio.sleep(0.5)
        subscription = self._subscriptions.get(sid)
        if subscription is None or subscription[2] <= time.monotonic() or self._http_session is None:
            return
        callback, service_name, _ = subscription
        values = (
            {'SystemUpdateID': str(self._update_id)}
            if service_name == 'ContentDirectory'
            else {'SourceProtocolInfo': '', 'SinkProtocolInfo': '', 'CurrentConnectionIDs': ''}
        )
        properties = ''.join(
            f'<e:property><{name}>{escape(value)}</{name}></e:property>' for name, value in values.items()
        )
        body = f'<?xml version="1.0" encoding="utf-8"?><e:propertyset xmlns:e="{_EVENT_NAMESPACE}">{properties}</e:propertyset>'
        try:
            async with self._http_session.request(
                'NOTIFY',
                callback,
                data=body.encode('utf-8'),
                headers={
                    'Content-Type': 'text/xml; charset="utf-8"',
                    'NT': 'upnp:event',
                    'NTS': 'upnp:propchange',
                    'SID': sid,
                    'SEQ': '0',
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                await response.read()
        except (aiohttp.ClientError, TimeoutError) as ex:
            _LOGGER.warning(f'Could not connect to event callback: {callback}', exc_info=ex)

    def _xml_response(self, body: bytes, *, status: int = 200) -> web.Response:
        """
        Create an XML HTTP response.

        Args:
            body (bytes): Encoded XML document
            status (int): HTTP status code

        Returns:
            web.Response: Response with the XML content type
        """

        return web.Response(status=status, body=body, headers={'Content-Type': 'text/xml; charset="utf-8"'})
