"""Parsing of server-sent event streams."""

import json
from collections.abc import Iterable, Iterator
from typing import Any

EVENT_PREFIX = "event:"
DATA_PREFIX = "data:"
DEFAULT_EVENT = "message"


def format_sse(event: str, payload: str) -> str:
    """Format a server-sent event frame.

    Args:
        event: The event name.
        payload: The already-serialised JSON payload.

    Returns:
        The wire representation of the event, terminated by a blank line.

    Example:
        >>> format_sse("progress", '{"page": 1}')
        'event: progress\\ndata: {"page": 1}\\n\\n'
    """
    return f"{EVENT_PREFIX} {event}\n{DATA_PREFIX} {payload}\n\n"


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse a server-sent event stream into event name and payload pairs.

    Args:
        lines: The response body, line by line, without trailing newlines.

    Yields:
        The event name and its decoded JSON payload, in arrival order.

    Example:
        >>> list(parse_sse(["event: progress", 'data: {"page": 1}', ""]))
        [('progress', {'page': 1})]
    """
    event = DEFAULT_EVENT
    data: list[str] = []

    for line in lines:
        if line.startswith(EVENT_PREFIX):
            event = line[len(EVENT_PREFIX) :].strip()
        elif line.startswith(DATA_PREFIX):
            data.append(line[len(DATA_PREFIX) :].strip())
        elif not line.strip() and data:
            yield event, json.loads("".join(data))
            event = DEFAULT_EVENT
            data = []

    if data:
        yield event, json.loads("".join(data))
