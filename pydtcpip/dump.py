"""Dump recorder responses for device support, optionally masking identifiers and program content."""

import ipaddress
import re
from collections.abc import Callable
from html import escape, unescape
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import aiohttp
from pydantic import BaseModel, ConfigDict

from pydtcpip.dlna.client import MediaServer
from pydtcpip.models import Container, Recording


_REDACTED_TEXT = 'redacted'

# Retain known technical values and short unknown integers for compatibility research
## Names indicating identifiers, credentials, personal data, or dates exclude the integer exception
_SHORT_INTEGER_PATTERN = re.compile(r'(?:0|[1-9][0-9]{0,2})')
_SENSITIVE_NAME_PATTERN = re.compile(
    r'id$|identifier|uuid|udn|serial|credential|password|passwd|pin|secret|token|auth|key|mac|address|owner|user|account|'
    r'date|time|^(?:start|end)$|title|name|description',
    re.IGNORECASE,
)

# DIDL-Lite elements whose text is needed for compatibility and does not identify a program
_KEPT_DIDL_TEXTS = frozenset(
    (
        'class',
        'objectType',
        'writeStatus',
        'genre',
        'genreCode',
        'copycount',
        'COGCount',
        'playbackState',
        # Broadcast network and service IDs are public and do not identify a program or user
        ## REGZA TVs include regza:network_id and regza:service_id only in BrowseDirectChildren responses
        'network_id',
        'service_id',
        'containerUpdateID',
        'createClass',
        'searchClass',
        'res',
    )
)
# DIDL-Lite attributes to retain (resource format, size, duration, and item identifiers)
_KEPT_DIDL_ATTRIBUTES = frozenset(
    (
        'id',
        'parentID',
        'refID',
        'restricted',
        'searchable',
        'childCount',
        'includeDerived',
        'protocolInfo',
        'importUri',
        'uploadInfo',
        'size',
        'duration',
        'bitrate',
        'resolution',
        'framerate',
        'cleartextSize',
        'updateCount',
        'nrAudioChannels',
        'sampleFrequency',
        'bitsPerSample',
        'colorDepth',
        # Remaining copy counts are small integers, and the same value is retained for element text
        'CopyCount',
    )
)

# Device description elements whose text is needed for identification and communication
## Also retain capability declarations beginning with X_, such as X_DLNACAP
## Redact user-assigned friendlyName, serialNumber, UPC, and other values not listed here
_KEPT_DESCRIPTION_TEXTS = frozenset(
    (
        'major',
        'minor',
        'URLBase',
        'deviceType',
        'manufacturer',
        'manufacturerURL',
        'modelDescription',
        'modelName',
        'modelNumber',
        'modelURL',
        'UDN',
        'presentationURL',
        'mimetype',
        'width',
        'height',
        'depth',
        'url',
        'serviceType',
        'serviceId',
        'SCPDURL',
        'controlURL',
        'eventSubURL',
        'CAP',
        'RSRegiSocket',
        'MaxLocalRegistration',
        'IsRemoteDevice',
    )
)

# HTTP response headers to retain, using lowercase names
## Redact retrieval dates and headers such as Content-Disposition that may contain program titles in filenames
_KEPT_HEADERS = frozenset(
    (
        'content-type',
        'content-length',
        'content-range',
        'content-range.dtcp.com',
        'transfer-encoding',
        'transfermode.dlna.org',
        'contentfeatures.dlna.org',
        'timeseekrange.dlna.org',
        'availableseekrange.dlna.org',
        'accept-ranges',
        'server',
        'vary',
        'cache-control',
        'pragma',
        'connection',
        'ext',
    )
)

# XML tokens: CDATA, comments, processing instructions and declarations, end tags, start tags, and text
_XML_TOKEN_PATTERN = re.compile(r'<!\[CDATA\[.*?\]\]>|<!--.*?-->|<[?!][^>]*>|</[^>]*>|<[^>]*>|[^<]+', re.DOTALL)
_TAG_NAME_PATTERN = re.compile(r'<\s*([^\s/>]+)')
_ATTRIBUTE_PATTERN = re.compile(
    r'(?P<name>[^\s=<>/]+)(?P<equals>\s*=\s*)(?P<quote>["\'])(?P<value>.*?)(?P=quote)', re.DOTALL
)

# Values embedded in identifiers that may identify a device or recording
_UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_MAC_PATTERN = re.compile(r'(?<![0-9a-fA-F:-])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:-])')
## Recording timestamps: 12 or 14 digits, such as `199912312359` or `19991231235959`
## These can appear within longer numeric identifiers, so match only plausible date and time sequences
_TIMESTAMP_PATTERN = re.compile(
    r'(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[01]\d|2[0-3])[0-5]\d(?:[0-5]\d)?'
)
## Unseparated MAC addresses and device identifiers: at least 12 hex digits with letters, or exactly 12 digits
## Retain longer numeric sequences such as the 32-digit DLNA.ORG_FLAGS, which do not identify a device
_HEX_RUN_PATTERN = re.compile(r'(?<![0-9a-fA-F])(?:(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,}|[0-9]{12})(?![0-9a-fA-F])')
_IPV4_PATTERN = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
## Validate IPv6 candidates with ipaddress before replacement to exclude sequences such as times
_IPV6_CANDIDATE_PATTERN = re.compile(
    r'(?<![0-9a-fA-F:])[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}(?:%[\w.-]+)?(?![0-9a-fA-F:])'
)

# Retain addresses that do not identify a device, such as the SSDP multicast address
_PUBLIC_ADDRESSES = frozenset(('239.255.255.250', '0.0.0.0', '127.0.0.1'))

# Document types to redact
## didl: recording listing; description: device description; service: fixed service definition without device values
type DocumentKind = Literal['didl', 'description', 'service']


class DumpedFile(BaseModel):
    """A dumped file."""

    model_config = ConfigDict(frozen=True)

    path: Path
    description: str


class Redactor:
    """
    Mask identifiers and program content in device responses.
    Retain known technical values and unknown integers from 0 to 999 without leading zeros.
    Exclude fields named as identifiers, credentials, personal data, or dates from the integer exception.
    Short unknown integers are a diagnostic compromise, not a guarantee of anonymity.
    Replace UUIDs, MAC and IP addresses, and recording timestamps embedded in retained values with documentation values.
    Use consistent replacements so relationships such as shared UDNs and addresses remain traceable.
    Replace XML tokens without rebuilding the document, preserving details such as namespace declaration placement.
    """

    def __init__(self) -> None:
        self._replacements: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def redact_xml(self, text: str, kind: DocumentKind) -> str:
        """
        Redact an XML document according to the allowlist for its type.

        Args:
            text (str): Recording listing, device description, or service definition XML
            kind (DocumentKind): Document type

        Returns:
            str: Redacted XML
        """

        # Service definitions contain fixed actions and state variables, so replace only identifiers
        if kind == 'service':
            return self._mask_identifiers(text)

        kept_texts = _KEPT_DIDL_TEXTS if kind == 'didl' else _KEPT_DESCRIPTION_TEXTS
        kept_attributes = _KEPT_DIDL_ATTRIBUTES if kind == 'didl' else frozenset[str]()
        element_stack: list[str] = []
        output: list[str] = []
        for match in _XML_TOKEN_PATTERN.finditer(text):
            token = match.group(0)
            is_cdata = token.startswith('<![CDATA[')

            # Keep XML declarations and processing instructions; redact comment contents
            if token.startswith('<?') or (token.startswith('<!') and is_cdata is False):
                output.append('<!-- redacted -->' if token.startswith('<!--') else token)
                continue

            # An end tag closes the current element
            if token.startswith('</'):
                if len(element_stack) > 0:
                    element_stack.pop()
                output.append(token)
                continue

            # Redact non-allowlisted attributes and enter non-empty start tags
            if token.startswith('<') and is_cdata is False:
                name_match = _TAG_NAME_PATTERN.match(token)
                local_name = name_match.group(1).rpartition(':')[2] if name_match is not None else ''
                output.append(
                    _ATTRIBUTE_PATTERN.sub(
                        lambda attribute: self._redact_attribute(
                            attribute, kept_attributes, [*element_stack, local_name]
                        ),
                        token,
                    )
                )
                if token.endswith('/>') is False:
                    element_stack.append(local_name)
                continue

            # For allowlisted elements replace identifiers in text and CDATA; otherwise redact them
            ## Apply the same rule to text before and after child elements
            current = element_stack[-1] if len(element_stack) > 0 else ''
            is_capability = kind == 'description' and current.startswith('X_')
            text_value = token[9:-3] if is_cdata is True else unescape(token)
            if token.strip() == '':
                output.append(token)
            elif current in kept_texts or is_capability is True:
                if current == 'res':
                    value = token[9:-3] if is_cdata is True else unescape(token)
                    masked = self._mask_url(value)
                    output.append(f'<![CDATA[{masked}]]>' if is_cdata is True else escape(masked, quote=False))
                else:
                    output.append(self._mask_identifiers(token))
            elif self._is_short_integer(text_value, element_stack):
                output.append(token)
            else:
                leading = token[: len(token) - len(token.lstrip())]
                trailing = token[len(token.rstrip()) :]
                output.append(f'{leading}{_REDACTED_TEXT}{trailing}')

        return ''.join(output)

    def redact_headers(self, lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """
        Redact HTTP response headers according to the allowlist.

        Args:
            lines (list[tuple[str, str]]): Header names and values

        Returns:
            list[tuple[str, str]]: Redacted header names and values
        """

        redacted: list[tuple[str, str]] = []
        for name, value in lines:
            if name.lower() in _KEPT_HEADERS:
                redacted.append((name, self._mask_identifiers(value)))
            else:
                redacted.append((name, _REDACTED_TEXT))
        return redacted

    def _redact_attribute(
        self, attribute: re.Match[str], kept_attributes: frozenset[str], element_names: list[str]
    ) -> str:
        """
        Redact an XML attribute unless it is allowed or declares a namespace.

        Args:
            attribute (re.Match[str]): Matched attribute and its lexical components
            kept_attributes (frozenset[str]): Local names whose values may be retained
            element_names (list[str]): Containing element names, including ancestors

        Returns:
            str: Attribute with its original quoting and a redacted or masked value
        """

        name = attribute.group('name')
        local_name = name.rpartition(':')[2]

        # Retain namespace declarations needed to identify device families
        if name == 'xmlns' or name.startswith('xmlns:'):
            return attribute.group(0)

        if local_name not in kept_attributes:
            value = (
                attribute.group('value')
                if self._is_short_integer(unescape(attribute.group('value')), [*element_names, local_name])
                else _REDACTED_TEXT
            )
        elif local_name in ('id', 'parentID', 'refID'):
            original = unescape(attribute.group('value'))
            # Some object IDs contain the same query key as their media resource URLs
            value = escape(self._mask_url(original) if '?' in original else self._mask_object_id(original), quote=True)
        elif local_name == 'importUri':
            value = escape(self._mask_url(unescape(attribute.group('value'))), quote=True)
        else:
            value = self._mask_identifiers(attribute.group('value'))
        return f'{name}{attribute.group("equals")}{attribute.group("quote")}{value}{attribute.group("quote")}'

    @staticmethod
    def _is_short_integer(value: str, names: list[str]) -> bool:
        """
        Check whether an unknown XML value qualifies for the diagnostic integer exception.

        Args:
            value (str): Decoded XML value
            names (list[str]): Field and ancestor local names

        Returns:
            bool: Whether the value can be retained
        """

        return _SHORT_INTEGER_PATTERN.fullmatch(value.strip()) is not None and all(
            _SENSITIVE_NAME_PATTERN.search(name) is None for name in names
        )

    def _mask_url(self, text: str) -> str:
        """
        Replace resource URL path segments and query values using the same aliases as object IDs.

        Args:
            text (str): Text containing a resource URL

        Returns:
            str: Redacted text
        """

        stripped = text.strip()
        parts = urlsplit(stripped)
        segments = [self._mask_object_id(unquote(segment)) if segment else '' for segment in parts.path.split('/')]
        query = urlencode(
            [
                (self._mask_identifiers(name), self._mask_object_id(value))
                for name, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        # User information and fragments are not required to inspect a resource's format
        authority = self._mask_identifiers(parts.netloc.rsplit('@', 1)[-1])
        masked = urlunsplit((parts.scheme, authority, '/'.join(segments), query, ''))
        return text.replace(stripped, masked)

    def _mask_object_id(self, value: str) -> str:
        """
        Replace an opaque identifier while retaining the standard root and no-parent values.

        Args:
            value (str): Decoded object ID, URL path segment, or query value

        Returns:
            str: Consistent synthetic identifier
        """

        if value in ('0', '-1'):
            return value
        return self._replace('object', value, lambda n: f'object-{n}')

    def _mask_identifiers(self, text: str) -> str:
        """
        Replace embedded UUIDs, MAC and IP addresses, timestamps, and hex identifiers with documentation values.

        Args:
            text (str): Value selected for retention

        Returns:
            str: Value with identifiers replaced
        """

        text = _UUID_PATTERN.sub(
            lambda match: self._replace('uuid', match.group(0), lambda n: f'00000000-0000-0000-0000-{n:012d}'), text
        )
        text = _MAC_PATTERN.sub(
            lambda match: self._replace('mac', match.group(0), lambda n: f'02:00:00:00:00:{n:02x}'), text
        )
        text = _TIMESTAMP_PATTERN.sub(lambda match: '20000101000000'[: len(match.group(0))], text)
        text = _HEX_RUN_PATTERN.sub(
            lambda match: self._replace('hex', match.group(0), lambda n: f'{n:0{len(match.group(0))}d}'), text
        )

        # Replace local addresses with documentation ranges 192.0.2.0/24 and 2001:db8::/32
        def replace_ipv4(match: re.Match[str]) -> str:
            if match.group(0) in _PUBLIC_ADDRESSES:
                return match.group(0)
            return self._replace('ipv4', match.group(0), lambda n: f'192.0.2.{n}')

        def replace_ipv6(match: re.Match[str]) -> str:
            try:
                ipaddress.IPv6Address(match.group(0).partition('%')[0])
            except ValueError:
                return match.group(0)
            return self._replace('ipv6', match.group(0), lambda n: f'2001:db8::{n:x}')

        text = _IPV4_PATTERN.sub(replace_ipv4, text)
        return _IPV6_CANDIDATE_PATTERN.sub(replace_ipv6, text)

    def _replace(self, category: str, original: str, make: Callable[[int], str]) -> str:
        """
        Assign a consistent replacement to an identifier.

        Args:
            category (str): Identifier category with its own counter
            original (str): Identifier to replace
            make (Callable[[int], str]): Function producing a replacement from the counter

        Returns:
            str: Existing or newly generated replacement
        """

        # Reuse replacements for identical values and number each category separately
        key = f'{category}:{original if category == "object" else original.lower()}'
        if key not in self._replacements:
            self._counts[category] = self._counts.get(category, 0) + 1
            self._replacements[key] = make(self._counts[category])
        return self._replacements[key]


async def dump_media_server(
    server: MediaServer,
    output_directory: Path,
    *,
    http_session: aiohttp.ClientSession,
    max_recordings: int = 5,
    should_mask: bool = False,
) -> list[DumpedFile]:
    """
    Dump the media server's device description, service definitions, listings, and media HEAD responses.
    Set should_mask before sharing to redact identifiers and program content.
    This requires no credentials and does not consume copies.

    Args:
        server (MediaServer): Media server to dump
        output_directory (Path): Output directory, created if needed
        http_session (aiohttp.ClientSession): HTTP session for service definitions and HEAD requests
        max_recordings (int): Maximum number of recordings to dump
        should_mask (bool): Whether to redact identifiers and program content (default False)

    Returns:
        list[DumpedFile]: Dumped files
    """

    output_directory.mkdir(parents=True, exist_ok=True)
    redactor = Redactor() if should_mask is True else None
    files: list[DumpedFile] = []

    def write(name: str, content: str, description: str) -> None:
        path = output_directory / name
        path.write_text(content, encoding='utf-8')
        files.append(DumpedFile(path=path, description=description))

    def write_xml(name: str, content: str, kind: DocumentKind, description: str) -> None:
        write(name, redactor.redact_xml(content, kind) if redactor is not None else content, description)

    # Device description and service definitions
    write_xml('description.xml', server.description.raw_xml, 'description', 'Device description')
    for service in server.description.services:
        service_name = service.service_type.split(':')[-2] if service.service_type.count(':') >= 2 else 'service'
        try:
            async with http_session.get(service.scpd_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                write_xml(
                    f'scpd-{service_name}.xml',
                    await response.text(encoding='utf-8', errors='replace'),
                    'service',
                    f'{service_name} service description (HTTP {response.status})',
                )
        except (aiohttp.ClientError, TimeoutError) as ex:
            write(
                f'scpd-{service_name}.txt', f'Could not fetch: {ex}', f'{service_name} service description fetch failed'
            )

    # Browse from the root and dump listings from folders containing recordings
    ## Record each recording's folder and position so it can be fetched individually from the listing
    recordings: list[tuple[Recording, str, int]] = []
    pending_containers = ['0']
    browse_index = 0
    while len(pending_containers) > 0 and len(recordings) < max_recordings and browse_index < 20:
        container_id = pending_containers.pop(0)
        result = await server.browse(container_id, start=0, count=max_recordings)
        browse_index += 1
        write_xml(f'browse-{browse_index:02d}.xml', result.raw_didl, 'didl', f'Folder {browse_index} listing')
        for position, item in enumerate(result.objects):
            if isinstance(item, Container):
                pending_containers.append(item.id)
            elif len(recordings) < max_recordings:
                recordings.append((item, container_id, position))

    # Recording metadata and HEAD responses for original-format resources
    is_listing_richer = server.profile.resolve_has_listing_only_metadata()
    for index, (recording, container_id, position) in enumerate(recordings, start=1):
        # Dump the device's original DIDL-Lite rather than elements rebuilt by the library
        ## For families with listing-only fields, fetch the recording at its listing position
        ## Fall back to BrowseMetadata if the ID no longer matches
        raw_didl: str | None = None
        if is_listing_richer is True:
            listed = await server.browse(container_id, start=position, count=1)
            if len(listed.objects) == 1 and listed.objects[0].id == recording.id:
                raw_didl = listed.raw_didl
        if raw_didl is None:
            raw_didl = (await server.browse_metadata(recording.id)).raw_didl
        write_xml(f'recording-{index:02d}.xml', raw_didl, 'didl', f'Recording {index} metadata')
        resource = recording.original_resource
        if resource is None:
            continue
        headers = {
            'transferMode.dlna.org': 'Streaming',
            'getcontentFeatures.dlna.org': '1',
            'Accept-Encoding': 'identity',
        }
        try:
            async with http_session.head(
                resource.url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                header_lines = list(response.headers.items())
                if redactor is not None:
                    header_lines = redactor.redact_headers(header_lines)
                lines = [f'HTTP {response.status}', *(f'{name}: {value}' for name, value in header_lines)]
                write(
                    f'recording-{index:02d}-head.txt', '\n'.join(lines) + '\n', f'Recording {index} media HEAD response'
                )
        except (aiohttp.ClientError, TimeoutError) as ex:
            write(
                f'recording-{index:02d}-head.txt',
                f'Could not fetch: {ex}\n',
                f'Recording {index} media HEAD request failed',
            )

    return files
