"""Server-sent events parsing for ``GET /v1/operations/{id}/events``.

Wire format (https://docs.tokenfactory.nebius.com/api-reference/sandboxes/operations/stream-the-operation-event-log-via-server-sent-events.md):

* normal frame: ``id: N`` / ``event: <type>`` / ``data: <OperationEvent JSON>``
* in-band failure: ``: stream ended with error...`` then ``event: sse_error`` with plain-text data
* keepalive: ``: keepalive`` comment lines, no application meaning
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from variator.sandboxes.models import OperationEvent

SSE_ERROR_EVENT = "sse_error"


@dataclass(frozen=True)
class SSEFrame:
    event: str
    data: str
    id: str | None = None


def iter_sse_frames(lines: Iterable[str]) -> Iterator[SSEFrame]:
    """Yield complete SSE frames from decoded lines (without line terminators).

    Comment lines are skipped, multi-line ``data`` is joined with ``\\n`` and a
    trailing frame without its terminating blank line is dropped, so a frame cut
    by a disconnect never advances a resume cursor.
    """
    data: list[str] = []
    event: str | None = None
    event_id: str | None = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data or event is not None:
                yield SSEFrame(event=event or "message", data="\n".join(data), id=event_id)
            data, event, event_id = [], None, None
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]  # the SSE spec strips at most one leading space
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id":
            event_id = value
        # "retry" and unknown fields are ignored.


def parse_event(frame: SSEFrame) -> OperationEvent:
    """Decode a normal frame's JSON payload. Raises ``ValueError`` on malformed data."""
    payload = json.loads(frame.data)
    if not isinstance(payload, dict):
        raise ValueError("SSE data is not a JSON object")
    if "id" not in payload and frame.id is not None:
        payload["id"] = int(frame.id)
    payload.setdefault("type", frame.event)
    return OperationEvent.model_validate(payload)
