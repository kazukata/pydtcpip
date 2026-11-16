"""Connection-independent models for recordings, resources, and protection information."""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


# Protection information acquisition status
## known: the value was obtained from a response or packet
## not_observed: the step that obtains the information has not yet been reached
## omitted: the response was obtained but the field was absent
## unsupported: the library cannot parse the information's format
type AcquisitionStatus = Literal['known', 'not_observed', 'omitted', 'unsupported']

# Protection information source
## metadata: DIDL-Lite recording listing, including device-specific elements
## protocol_info: resource protocolInfo
## http_header: media HTTP response header
## pcp_header: received protected packet (PCP) header
type ProtectionSource = Literal['metadata', 'protocol_info', 'http_header', 'pcp_header']


class ProtectionValue[T](BaseModel):
    """
    A protection value with its source, observation time, and acquisition status.
    Values omitted by the device remain distinct from zero and unsupported values.
    """

    model_config = ConfigDict(frozen=True)

    status: AcquisitionStatus
    value: T | None = None
    source: ProtectionSource
    observed_at: datetime | None = None


class ReportedCopyCount(BaseModel):
    """
    A recorder's copy-count field before normalization to copies excluding the final Move.
    An unknown counting convention leaves the normalized count unavailable even when the number was read.
    """

    model_config = ConfigDict(frozen=True)

    field_name: str
    raw_value: str
    status: AcquisitionStatus
    value: int | None = None
    is_unlimited: bool = False
    includes_final_move: bool | None = None
    resource_url: str | None = None
    observed_at: datetime | None = None

    @property
    def remaining_copies(self) -> ProtectionValue[int]:
        """
        Normalize the finite count when its counting convention is known.

        Returns:
            ProtectionValue[int]: Copies excluding the final Move and their acquisition status
        """

        value = self.value
        status = self.status
        if self.is_unlimited is True or self.includes_final_move is None:
            value = None
            status = 'unsupported'
        elif value is not None and self.includes_final_move is True:
            # A total of zero is not equivalent to one remaining Move
            if value == 0:
                value = None
                status = 'unsupported'
            else:
                value -= 1
        return ProtectionValue[int](status=status, value=value, source='metadata', observed_at=self.observed_at)


class MetadataValue(BaseModel):
    """An XML metadata value with its expanded name and attributes, preserving repeated elements."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: str
    attributes: dict[str, str] = Field(default_factory=dict)


class ChapterResource(BaseModel):
    """A chapter document referenced by a recording's metadata."""

    model_config = ConfigDict(frozen=True)

    url: str
    format: Literal['sony', 'panasonic', 'sharp']
    source_field: str


class Chapter(BaseModel):
    """A device-provided chapter point, optionally converted from a stream position."""

    model_config = ConfigDict(frozen=True)

    position_ms: int | None
    source_position: int | None = None
    is_estimated: bool = False


class ChapterList(BaseModel):
    """
    Chapters and the responses used to obtain them.
    A successfully retrieved empty list is distinct from missing or unsupported chapter information.
    """

    model_config = ConfigDict(frozen=True, ser_json_bytes='base64', val_json_bytes='base64')

    status: AcquisitionStatus
    chapters: tuple[Chapter, ...] = ()
    sources: tuple[str, ...] = ()
    raw_responses: tuple[bytes, ...] = ()
    observed_at: datetime | None = None


class MetadataResponse(BaseModel):
    """An additional HTTP metadata response, retaining its source and observation time."""

    model_config = ConfigDict(frozen=True, ser_json_bytes='base64', val_json_bytes='base64')

    url: str
    headers: dict[str, str]
    body: bytes
    observed_at: datetime
    status_code: int | None = None
    soap_action: str | None = None


class MetadataQuery(BaseModel):
    """An optional metadata query, distinguishing failed requests from omitted device values."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: Literal['known', 'not_observed', 'omitted', 'unsupported', 'failed']
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


class TimeRange(BaseModel):
    """A time range in milliseconds from the start of the media."""

    model_config = ConfigDict(frozen=True)

    start_ms: int
    end_ms: int | None = None

    @model_validator(mode='after')
    def _validate(self) -> Self:
        """
        Validate the start and end of the time range.

        Returns:
            Self: Validated time range
        """

        if self.start_ms < 0 or (self.end_ms is not None and self.end_ms < self.start_ms):
            raise ValueError('The time range must start at or after zero and end at or after the start.')
        return self

    def to_header(self) -> str:
        """
        Convert to a DLNA TimeSeekRange.dlna.org header value (NPT with three decimal places).

        Returns:
            str: Value in the form `npt=60.000-90.000`
        """

        start_seconds, start_fraction = divmod(self.start_ms, 1000)
        start = f'npt={start_seconds}.{start_fraction:03d}-'
        if self.end_ms is None:
            return start
        end_seconds, end_fraction = divmod(self.end_ms, 1000)
        return f'{start}{end_seconds}.{end_fraction:03d}'


class ProtocolInfo(BaseModel):
    """Parsed resource protocolInfo (`<protocol>:<network>:<MIME type>:<additional info>`)."""

    model_config = ConfigDict(frozen=True)

    raw: str
    protocol: str
    network: str
    content_type: str
    # Semicolon-separated `name=value` pairs in the fourth field (e.g. DLNA.ORG_PN, DLNA.ORG_CI, DTCP.COM_FLAGS)
    parameters: dict[str, str]
    # Parameters following the MIME type (e.g. DTCP1HOST, DTCP1PORT, CONTENTFORMAT)
    content_type_parameters: dict[str, str]

    @property
    def is_dtcp(self) -> bool:
        """Return whether the resource is protected by DTCP-IP."""

        return self.content_type == 'application/x-dtcp1'

    @property
    def is_converted(self) -> bool | None:
        """
        Return whether the recorder converted this resource.
        DLNA.ORG_CI=1 indicates a conversion and 0 the stored format; return None if omitted.
        """

        value = self.parameters.get('DLNA.ORG_CI')
        if value is None:
            return None
        return value == '1'


class ResourceConversion(BaseModel):
    """Evidence identifying a resource as stored content or a converted representation."""

    model_config = ConfigDict(frozen=True)

    is_converted: bool
    source: Literal['protocol_info', 'metadata', 'resource_url']
    field_name: str
    raw_value: str


class Resource(BaseModel):
    """A recording resource (a DIDL-Lite res element)."""

    model_config = ConfigDict(frozen=True)

    url: str
    protocol_info: ProtocolInfo
    # res attributes (namespaced attributes use the `{namespace}name` format)
    attributes: dict[str, str]
    raw_xml: str
    conversion: ResourceConversion | None = None

    @property
    def is_converted(self) -> bool | None:
        """Return the conversion status supported by standard or family-specific evidence."""

        if self.conversion is not None:
            return self.conversion.is_converted
        return self.protocol_info.is_converted

    @property
    def duration(self) -> str | None:
        """
        Read this resource's duration.

        Returns:
            str | None: Original duration string, or None when omitted
        """

        return self.attributes.get('duration')

    @property
    def resolution(self) -> str | None:
        """
        Read this resource's resolution.

        Returns:
            str | None: Original resolution string, or None when omitted
        """

        return self.attributes.get('resolution')

    @property
    def size(self) -> int | None:
        """Return the HTTP body size from res@size in bytes (None if absent)."""

        value = self.attributes.get('size')
        if value is None or value.isdecimal() is False:
            return None
        return int(value)

    @property
    def cleartext_size(self) -> int | None:
        """Return the decrypted content size from dlna:cleartextSize in bytes (None if absent)."""

        value = self.attributes.get('{urn:schemas-dlna-org:metadata-1-0/}cleartextSize')
        if value is None or value.isdecimal() is False:
            return None
        return int(value)


class RecordingProtection(BaseModel):
    """
    Protection information available from a recording listing.
    Transfer copy control (E-EMI) is known only after reception starts and is reported separately.
    """

    model_config = ConfigDict(frozen=True)

    # Copies remaining on the source, obtained from a recorder-specific element
    ## Like the TV display, this excludes the final Move: 0 means only a Move remains (9 for unused Dubbing 10)
    remaining_copy_count: ProtectionValue[int]
    # Retain resource-specific values and unknown counting conventions separately from the normalized count
    reported_copy_counts: tuple[ReportedCopyCount, ...] = ()
    # DTCP.COM_FLAGS bit 31: Move support
    is_move_supported: ProtectionValue[bool]
    # DTCP.COM_FLAGS bit 30: transaction-based Move support
    is_transaction_move_supported: ProtectionValue[bool]


class Container(BaseModel):
    """A recorder folder, with ARIB text normalized in metadata and raw_xml."""

    model_config = ConfigDict(frozen=True)

    id: str
    parent_id: str
    title: str
    upnp_class: str
    child_count: int | None
    raw_xml: str


class Recording(BaseModel):
    """A recorder's recording and resources, with ARIB text normalized in metadata and raw_xml."""

    model_config = ConfigDict(frozen=True)

    id: str
    parent_id: str
    reference_id: str | None = None
    title: str
    upnp_class: str
    description: str | None
    long_description: str | None
    channel_name: str | None
    channel_number: str | None = None
    genres: tuple[str, ...]
    # Broadcast start and duration as reported by the device (format varies by device)
    date: str | None
    scheduled_start_time: str | None = None
    scheduled_end_time: str | None = None
    duration: str | None
    write_status: str | None = None
    ratings: tuple[MetadataValue, ...] = ()
    video_components: tuple[MetadataValue, ...] = ()
    audio_components: tuple[MetadataValue, ...] = ()
    broadcast_metadata: tuple[MetadataValue, ...] = ()
    icons: tuple[MetadataValue, ...] = ()
    chapter_resources: tuple[ChapterResource, ...] = ()
    # Family-specific XML elements retain their names and attributes rather than an assumed interpretation
    vendor_metadata: tuple[MetadataValue, ...] = ()
    resources: tuple[Resource, ...]
    protection: RecordingProtection
    raw_xml: str
    # Original metadata values before ARIB normalization, serialized as an XML item
    source_xml: str | None = None
    observed_at: datetime | None = None

    @property
    def original_resource(self) -> Resource | None:
        """
        Return a DTCP-IP resource in the recorder's stored format.
        Prefer a resource identified as stored content; otherwise use the first with unknown conversion status.
        """

        dtcp_resources = [resource for resource in self.resources if resource.protocol_info.is_dtcp is True]
        for resource in dtcp_resources:
            if resource.is_converted is False:
                return resource
        for resource in dtcp_resources:
            if resource.is_converted is None:
                return resource
        return None


class RecordingMetadata(BaseModel):
    """A recording's metadata archive, independent of the transferred media and resume secrets."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    recording: Recording
    profile: str
    profile_unverified: bool
    observed_at: datetime
    device_description_xml: str | None = None
    device_location: str | None = None
    device_declarations: dict[str, str] = Field(default_factory=dict)
    chapters: ChapterList = Field(default_factory=lambda: ChapterList(status='not_observed'))
    queries: tuple[MetadataQuery, ...] = ()
    service_descriptions: dict[str, str] = Field(default_factory=dict)
    # Complete responses remain available even when their metadata format could not be parsed
    responses: tuple[MetadataResponse, ...] = ()
    transfer_events: tuple[dict[str, JsonValue], ...] = ()


class MediaInfo(BaseModel):
    """DTCP-IP authentication endpoint and transfer information from a media HTTP response."""

    model_config = ConfigDict(frozen=True)

    url: str
    # Source address and port for DTCP-IP mutual authentication
    host: str
    port: int
    content_type: str
    content_format: str | None
    content_features: str | None
    # HTTP body size in bytes (None if unknown, e.g. with chunked encoding)
    content_length: int | None
    # Time range actually returned by the server (TimeSeekRange.dlna.org value)
    time_range: str | None
    headers: dict[str, str]


class MoveResumeState(BaseModel):
    """
    Information for resuming finalization of the same Move after losing its response.
    This contains an exchange key and must be stored where only its owner can read it.
    """

    # Serialize the exchange key and nonce as Base64 strings in JSON
    model_config = ConfigDict(frozen=True, ser_json_bytes='base64', val_json_bytes='base64')

    host: str
    port: int
    exchange_key: bytes = Field(repr=False)
    exchange_key_label: int
    # Nonce for finalization (reuse the same value when retrying the same transfer)
    nonce: bytes = Field(repr=False)
    # finalizing: immediately before the request, or sent without receiving a response
    # finalized: finalize response verified, completion notification not yet sent
    state: Literal['finalizing', 'finalized']
    written_bytes: int
