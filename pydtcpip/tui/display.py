"""Text shown by the TUI for recordings, resources, copy counts and devices."""

import json
from datetime import datetime
from typing import Literal, NamedTuple

from rich.text import Text

from pydtcpip.dlna.client import MediaServer
from pydtcpip.models import ProtectionValue, Recording, RecordingMetadata, Resource


# Descriptions of acquisition statuses for values that were not obtained
_UNAVAILABLE = {
    'omitted': 'Not reported by device',
    'not_observed': 'Not queried',
    'unsupported': 'Unsupported by device',
}


class SizeEstimate(NamedTuple):
    """A recording size and how it was obtained."""

    value: int | None
    # content: decrypted size reported by the device, http: encrypted body size, estimated: bitrate x duration
    kind: Literal['content', 'http', 'estimated', 'unknown']


def parse_duration(text: str | None) -> float | None:
    """
    Convert a DLNA duration such as 0:30:00.000 into seconds.

    Args:
        text (str | None): Duration reported by the device

    Returns:
        float | None: Seconds, or None when absent or not in H:MM:SS form
    """

    if not text:
        return None
    parts = text.strip().split(':')
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def format_duration(text: str | None) -> str:
    """
    Format a DLNA duration as H:MM:SS.

    Args:
        text (str | None): Duration reported by the device

    Returns:
        str: Formatted duration, the original text when it cannot be parsed, or an empty string when absent
    """

    seconds = parse_duration(text)
    if seconds is None:
        return text or ''
    total = int(seconds)
    return f'{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}'


def format_bytes(value: int) -> str:
    """
    Format a byte count with decimal units, as recorders display disk usage.

    Args:
        value (int): Number of bytes

    Returns:
        str: Size such as 6.1 GB
    """

    size = float(value)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1000 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1000
    return f'{size:.1f} GB'


def resource_size(resource: Resource | None) -> SizeEstimate:
    """
    Determine the saved size of a resource from the most direct information the device reports.

    Args:
        resource (Resource | None): Resource to be saved

    Returns:
        SizeEstimate: Size and its origin
    """

    if resource is None:
        return SizeEstimate(None, 'unknown')
    if resource.cleartext_size is not None:
        return SizeEstimate(resource.cleartext_size, 'content')
    if resource.size is not None:
        return SizeEstimate(resource.size, 'http')
    seconds = parse_duration(resource.duration)
    if resource.bitrate is not None and seconds is not None:
        # UPnP defines res@bitrate in bytes per second
        return SizeEstimate(int(resource.bitrate * seconds), 'estimated')
    return SizeEstimate(None, 'unknown')


def format_size(estimate: SizeEstimate) -> str:
    """
    Format a size with a marker for its origin.

    Args:
        estimate (SizeEstimate): Size and its origin

    Returns:
        str: Size, prefixed with ~ when estimated and suffixed with * for the encrypted body size
    """

    if estimate.value is None:
        return 'Unknown'
    text = format_bytes(estimate.value)
    if estimate.kind == 'estimated':
        return f'~{text}'
    if estimate.kind == 'http':
        return f'{text}*'
    return text


def describe_size(estimate: SizeEstimate) -> str:
    """
    Describe a size and its origin for the details pane.

    Args:
        estimate (SizeEstimate): Size and its origin

    Returns:
        str: Size with an explanation of how it was obtained
    """

    if estimate.value is None:
        return 'Not reported by device'
    text = f'{format_bytes(estimate.value)} ({estimate.value:,} bytes)'
    if estimate.kind == 'content':
        return f'{text}, decrypted size reported by the device'
    if estimate.kind == 'http':
        return f'{text}, encrypted transfer size (the saved file is slightly smaller)'
    return f'{text}, estimated from the advertised bitrate and duration'


def format_date(text: str | None) -> str:
    """
    Format a recording date as YYYY-MM-DD HH:MM when it is in ISO 8601 form.

    Args:
        text (str | None): Date reported by the device

    Returns:
        str: Formatted date, the original text when it is not ISO 8601, or an empty string when absent
    """

    if not text:
        return ''
    try:
        return datetime.fromisoformat(text.strip()).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return text


def default_move_resource(recording: Recording, resource_index: int | None) -> Resource | None:
    """
    Return the resource a Move would transfer.

    Args:
        recording (Recording): Recording to move
        resource_index (int | None): One-based resource chosen by the user, or None for the stored recording

    Returns:
        Resource | None: Resource to transfer, or None when the recording has no original-quality Move resource
    """

    if resource_index is not None and 1 <= resource_index <= len(recording.resources):
        return recording.resources[resource_index - 1]
    return recording.original_move_resource


def resource_version(resource: Resource) -> str:
    """
    Describe which version of a recording a resource provides.

    Args:
        resource (Resource): Advertised resource

    Returns:
        str: Recording version and transfer restriction
    """

    if resource.variant is not None and resource.variant.kind == 'mobile':
        version = 'Mobile version'
    elif resource.is_converted is True:
        version = 'Converted'
    elif resource.is_original_quality is True:
        version = 'Original recording'
    else:
        version = 'Quality unknown'
    if resource.protocol_info.is_dtcp is False:
        version += ', not DTCP-IP'
    if resource.variant is not None and resource.variant.is_move_only is True:
        version += ', Move only'
    return version


def copy_count(recording: Recording, resource: Resource | None) -> ProtectionValue[int]:
    """
    Return the copy count that applies to the resource a Move would transfer.

    Args:
        recording (Recording): Recording to inspect
        resource (Resource | None): Resource to transfer, or None for the recording-level count

    Returns:
        ProtectionValue[int]: Remaining copies excluding the final Move
    """

    return recording.copy_count_for(resource) if resource is not None else recording.protection.remaining_copy_count


def format_copy_count(recording: Recording, resource: Resource | None) -> Text:
    """
    Format the remaining copy count for the recording table.

    Args:
        recording (Recording): Recording to inspect
        resource (Resource | None): Resource to transfer

    Returns:
        Text: Count, or a symbol for an unavailable value (zero means only the final Move remains)
    """

    count = copy_count(recording, resource)
    if count.status == 'known' and count.value is not None:
        return Text(str(count.value), style='bold red' if count.value == 0 else '')
    for reported in recording.protection.reported_copy_counts:
        if reported.resource_url is not None and (resource is None or reported.resource_url != resource.url):
            continue
        if reported.is_unlimited is True:
            return Text('Unlimited')
        if reported.value is not None:
            return Text(f'{reported.raw_value}?', style='yellow')
    return Text({'omitted': '-', 'not_observed': '?', 'unsupported': 'x'}.get(count.status, '?'), style='yellow')


def describe_protection(value: ProtectionValue[int] | ProtectionValue[bool]) -> str:
    """
    Describe a protection value with its acquisition status.

    Args:
        value (ProtectionValue[int] | ProtectionValue[bool]): Value and acquisition status

    Returns:
        str: Value or a description of its acquisition status
    """

    if value.status == 'known':
        if isinstance(value.value, bool):
            return 'Yes' if value.value is True else 'No'
        return str(value.value)
    if value.status == 'omitted':
        return 'Not reported by device'
    if value.status == 'not_observed':
        return 'Available after transfer starts'
    return 'Unsupported format'


def recording_fields(recording: Recording, resource: Resource | None) -> list[tuple[str, str]]:
    """
    List the recording details shown in the details pane.

    Args:
        recording (Recording): Recording to describe
        resource (Resource | None): Resource a Move would transfer

    Returns:
        list[tuple[str, str]]: Field names and values
    """

    fields: list[tuple[str, str]] = [
        ('Title', recording.title),
        ('Channel', recording.channel_name or 'Not reported by device'),
        ('Date', recording.date or 'Not reported by device'),
        ('Duration', format_duration(resource.duration if resource is not None else recording.duration) or '-'),
    ]
    if recording.genres:
        fields.append(('Genres', ', '.join(recording.genres)))
    if recording.description:
        fields.append(('Description', recording.description))
    if recording.long_description:
        fields.append(('Program details', recording.long_description))
    for label, value in (
        ('Scheduled start', recording.scheduled_start_time),
        ('Scheduled end', recording.scheduled_end_time),
        ('Channel number', recording.channel_number),
        ('Write status', recording.write_status),
    ):
        if value is not None:
            fields.append((label, value))
    fields.append(('Copies remaining', describe_protection(copy_count(recording, resource))))
    for count in recording.protection.reported_copy_counts:
        convention = 'final Move convention unknown'
        if count.is_unlimited is True:
            convention = 'reported as unlimited'
        elif count.includes_final_move is not None:
            convention = 'includes final Move' if count.includes_final_move else 'excludes final Move'
        fields.append((f'Reported count ({count.field_name.rpartition("}")[2]})', f'{count.raw_value} ({convention})'))
    if resource is None:
        fields.append(('Move resource', 'No original recording; press v to choose another version'))
    else:
        fields.append(('Move resource', f'{recording.resources.index(resource) + 1}: {resource.quality_label}'))
        fields.append(('Size', describe_size(resource_size(resource))))
        if resource.is_original_quality is None:
            fields.append(('Note', 'The recorder does not identify whether this is the original recording'))
    for label, values in (
        ('Parental rating', recording.ratings),
        ('Video', recording.video_components),
        ('Audio', recording.audio_components),
        ('Broadcast', recording.broadcast_metadata),
        ('Device metadata', recording.vendor_metadata),
    ):
        for value in values:
            attributes = ', '.join(f'{name.rpartition("}")[2]}={text}' for name, text in value.attributes.items())
            fields.append(
                (f'{label} ({value.name.rpartition("}")[2]})', value.value + (f' ({attributes})' if attributes else ''))
            )
    fields.append(('ID', recording.id))
    return fields


def resource_fields(recording: Recording, selected: Resource | None) -> list[tuple[str, str]]:
    """
    List every advertised resource with its quality, version, size and default use.

    Args:
        recording (Recording): Recording to describe
        selected (Resource | None): Resource a Move would transfer

    Returns:
        list[tuple[str, str]]: Field names and values
    """

    fields: list[tuple[str, str]] = []
    for index, resource in enumerate(recording.resources, start=1):
        marker = ' [Move]' if resource == selected else ''
        fields.append((f'Resource {index}{marker}', resource.quality_label))
        fields.append(('  Version', resource_version(resource)))
        uses = [
            use
            for use, default in (('Streaming', recording.original_resource), ('Move', recording.original_move_resource))
            if resource == default
        ]
        if uses:
            fields.append(('  Default for', ', '.join(uses)))
        for name, value in resource.protocol_info.parameters.items():
            if name.endswith('_PN'):
                fields.append((f'  {name}', value))
        fields.append(('  Size', describe_size(resource_size(resource))))
    return fields


def metadata_fields(metadata: RecordingMetadata) -> list[tuple[str, str]]:
    """
    List chapters and optional query results, keeping failed queries visible beside other details.

    Args:
        metadata (RecordingMetadata): Metadata collected for the recording

    Returns:
        list[tuple[str, str]]: Field names and values
    """

    fields: list[tuple[str, str]] = []
    chapters = metadata.chapters
    if chapters.status == 'known':
        positions = [
            f'{chapter.position_ms / 1000:.1f}s' if chapter.position_ms is not None else 'unknown'
            for chapter in chapters.chapters
        ]
        fields.append(('Chapter positions', ', '.join(positions) or 'None'))
    for query in metadata.queries:
        name = query.name.replace('_', ' ').capitalize()
        if query.status == 'failed':
            fields.append((name, f'Could not retrieve: {query.error}'))
            continue
        if query.status != 'known' or not query.data:
            fields.append((name, _UNAVAILABLE.get(query.status, 'Not reported by device')))
            continue
        selected = query.data.get('recording')
        items = [selected] if isinstance(selected, dict) else query.data.get('items', [query.data])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_fields = item.get('fields')
            if isinstance(item_fields, list):
                for field in item_fields:
                    if isinstance(field, dict):
                        fields.append((str(field.get('name', '')).rpartition('}')[2], str(field.get('value', ''))))
                continue
            for key, value in item.items():
                text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
                fields.append((key, text))
    return fields


def device_sections(server: MediaServer) -> list[tuple[str, list[tuple[str, str]]]]:
    """
    List device description details and profile, with device-wide capability declarations in their own section.

    Args:
        server (MediaServer): Recorder

    Returns:
        list[tuple[str, list[tuple[str, str]]]]: Section names and their fields
    """

    description = server.description
    profile = f'{server.profile.name} (unverified)' if server.profile.is_unverified else server.profile.name
    fields: list[tuple[str, str]] = [
        ('Name', description.friendly_name),
        ('Manufacturer', description.manufacturer),
        ('Model', description.model_name),
        ('Model number', description.model_number or '-'),
        ('Description', description.model_description or '-'),
        ('Profile', profile),
        ('Location', description.location),
        ('UDN', description.udn),
    ]
    for service in description.services:
        fields.append(('Service', service.service_type))
    sections = [('Recorder', fields)]
    capabilities = [
        (name.rpartition('}')[2], value) for name, value in description.extensions.items() if name.endswith('CAP')
    ]
    if capabilities:
        # These describe the whole device and do not grant Move permission for a particular recording
        sections.append(('Device-wide declarations (not per-recording Move permission)', capabilities))
    return sections
