"""Invoke UPnP actions over SOAP."""

from collections.abc import Callable
from datetime import UTC, datetime
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp
from pydantic import BaseModel, ConfigDict

from pydtcpip.errors import DLNAError
from pydtcpip.models import MetadataResponse


SOAP_ENVELOPE_NAMESPACE = 'http://schemas.xmlsoap.org/soap/envelope/'
_UPNP_CONTROL_NAMESPACE = 'urn:schemas-upnp-org:control-1-0'


class SOAPResult(BaseModel):
    """
    SOAP response output arguments and the complete response XML.
    Argument values remain strings; callers interpret DIDL-Lite and other content.
    """

    model_config = ConfigDict(frozen=True)

    arguments: dict[str, str]
    raw_xml: str


async def call_action(
    session: aiohttp.ClientSession,
    control_url: str,
    service_type: str,
    action: str,
    arguments: dict[str, str],
    *,
    timeout: float,
    extra_headers: dict[str, str] | None = None,
    on_response: Callable[[MetadataResponse], None] | None = None,
) -> SOAPResult:
    """
    Invoke a UPnP action and return its output arguments.

    Args:
        session (aiohttp.ClientSession): HTTP session
        control_url (str): Service control URL
        service_type (str): Service type (e.g. `urn:schemas-upnp-org:service:ContentDirectory:1`)
        action (str): Action name
        arguments (dict[str, str]): Input argument names and values, sent in insertion order
        timeout (float): Response timeout in seconds
        extra_headers (dict[str, str] | None): Additional headers required by an optional service
        on_response (Callable[[MetadataResponse], None] | None): Retain the complete response before XML parsing

    Returns:
        SOAPResult: Output arguments and response XML
    """

    # Preserve input argument order from the service definition because some devices check it
    argument_xml = ''.join(f'<{name}>{escape(value)}</{name}>' for name, value in arguments.items())
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NAMESPACE}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{argument_xml}</u:{action}></s:Body>'
        '</s:Envelope>'
    )
    headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': f'"{service_type}#{action}"',
    }
    if extra_headers is not None:
        headers.update(extra_headers)

    try:
        async with session.post(
            control_url,
            data=body.encode('utf-8'),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            raw_body = await response.read()
            status = response.status
            if on_response is not None:
                on_response(
                    MetadataResponse(
                        url=str(response.url),
                        headers=dict(response.headers),
                        body=raw_body,
                        observed_at=datetime.now(UTC),
                        status_code=status,
                        soap_action=f'{service_type}#{action}',
                    )
                )
    except (aiohttp.ClientError, TimeoutError) as ex:
        raise DLNAError(f'{action} request failed ({ex}).') from ex

    raw_xml = raw_body.decode('utf-8', errors='replace')
    try:
        envelope = ET.fromstring(raw_xml)
    except ET.ParseError as ex:
        raise DLNAError(f'Could not parse the {action} response (HTTP {status}).', http_status=status) from ex

    # A SOAP Fault's UPnPError contains the error code and description
    fault = envelope.find(f'{{{SOAP_ENVELOPE_NAMESPACE}}}Body/{{{SOAP_ENVELOPE_NAMESPACE}}}Fault')
    if fault is not None or status >= 400:
        error_code = None
        error_description = None
        if fault is not None:
            error_code = fault.findtext(f'.//{{{_UPNP_CONTROL_NAMESPACE}}}errorCode')
            error_description = fault.findtext(f'.//{{{_UPNP_CONTROL_NAMESPACE}}}errorDescription')
        raise DLNAError(
            f'{action} failed (HTTP {status}, UPnP error code: {error_code}, description: {error_description}).',
            http_status=status,
        )

    # Collect the response action element's children as output arguments
    response_body = envelope.find(f'{{{SOAP_ENVELOPE_NAMESPACE}}}Body')
    if response_body is None or len(response_body) == 0:
        raise DLNAError(f'The {action} response has no body.')
    result_arguments: dict[str, str] = {}
    for child in response_body[0]:
        name = child.tag.rpartition('}')[2]
        result_arguments[name] = child.text or ''
    return SOAPResult(arguments=result_arguments, raw_xml=raw_xml)
