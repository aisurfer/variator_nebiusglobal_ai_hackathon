from __future__ import annotations

import base64
import json

import httpx
import pytest

from variator.sandboxes import SandboxAuthError, SandboxStreamError, SSEFrame, iter_sse_frames
from variator.sandboxes.sse import parse_event

OP_ID = "12345678-9abc-baba-deda-0123456789ab"
EVENTS_PATH = f"/operations/{OP_ID}/events"


def frame(event_id: int, event_type: str, data: dict, spid: int | None = 1) -> str:
    payload = {"id": event_id, "ts": "2026-06-08T20:00:00Z", "type": event_type, "data": data}
    if spid is not None:
        payload["spid"] = spid
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(payload)}\n\n"


def completion(event_id: int, status: str = "SUCCESS") -> str:
    return frame(event_id, "completion", {"status": status, "duration_ms": 12}, spid=None)


def sse(*frames: str) -> httpx.Response:
    return httpx.Response(200, text="".join(frames), headers={"Content-Type": "text/event-stream"})


SSE_ERROR = (
    ": stream ended with error, retry since last event id\n\n"
    "event: sse_error\ndata: upstream closed\n\n"
)


# -- frame parser --------------------------------------------------------------


def test_parser_skips_keepalives_and_joins_multiline_data():
    lines = [
        ": keepalive",
        "",
        "id: 7",
        "event: stdout",
        "data: first line",
        "data:second line",
        "",
        ": keepalive",
        "",
    ]
    assert list(iter_sse_frames(lines)) == [
        SSEFrame(event="stdout", data="first line\nsecond line", id="7")
    ]


def test_parser_drops_incomplete_trailing_frame_and_handles_crlf():
    lines = ["id: 1\r", "event: spawn\r", 'data: {"a": 1}\r', "\r", "id: 2", "event: exit", "data: {"]
    frames = list(iter_sse_frames(lines))
    assert [f.id for f in frames] == ["1"]
    assert frames[0].data == '{"a": 1}'


def test_parser_reports_sse_error_frames():
    frames = list(iter_sse_frames(SSE_ERROR.splitlines()))
    assert frames == [SSEFrame(event="sse_error", data="upstream closed", id=None)]


def test_parse_event_without_spid_and_with_base64_output():
    done = parse_event(next(iter_sse_frames(completion(9).splitlines())))
    assert done.type == "completion" and done.spid is None and done.data["status"] == "SUCCESS"
    encoded = base64.b64encode(b"\xffcaf\xc3\xa9\n").decode()
    out = parse_event(
        next(iter_sse_frames(frame(3, "stdout", {"value": encoded, "encoding": "base64"}).splitlines()))
    )
    assert out.is_output and out.output_text() == "�café\n"


def test_parse_event_rejects_non_object():
    with pytest.raises(ValueError):
        parse_event(SSEFrame(event="stdout", data="[1, 2]", id="1"))


# -- client.stream_events ------------------------------------------------------


def test_stream_yields_lifecycle_until_completion(api, client):
    route = api.get(EVENTS_PATH).mock(
        return_value=sse(
            ": keepalive\n\n",
            frame(0, "init", {"init_pid": 1}, spid=0),
            frame(1, "spawn", {"pid": 4242}),
            frame(2, "stdout", {"value": "hello\n", "encoding": "ascii"}),
            frame(3, "exit", {"pid": 4242, "code": 0, "signal": -1, "timed_out": False}),
            completion(4),
        )
    )
    events = list(client.stream_events(OP_ID))
    assert [e.type for e in events] == ["init", "spawn", "stdout", "exit", "completion"]
    assert events[2].output_text() == "hello\n"
    request = route.calls.last.request
    assert request.url.params["follow"] == "1"
    assert request.headers["Accept"] == "text/event-stream"
    assert request.headers["Project"] == "proj-123"
    assert "Last-Event-Id" not in request.headers


def test_stream_resumes_after_sse_error_without_duplicates(api, client, clock):
    route = api.get(EVENTS_PATH).mock(
        side_effect=[
            sse(frame(0, "init", {}, spid=0), frame(1, "spawn", {"pid": 1}), SSE_ERROR),
            sse(frame(1, "spawn", {"pid": 1}), frame(2, "stdout", {"value": "x", "encoding": "ascii"}),
                completion(3)),
        ]
    )
    events = list(client.stream_events(OP_ID))
    assert [e.id for e in events] == [0, 1, 2, 3]
    assert route.call_count == 2
    assert route.calls[1].request.headers["Last-Event-Id"] == "1"
    assert clock.sleeps  # backed off before reconnecting


def test_stream_reconnects_when_closed_before_completion(api, client):
    route = api.get(EVENTS_PATH).mock(
        side_effect=[sse(frame(5, "stdout", {"value": "a", "encoding": "ascii"})), sse(completion(6))]
    )
    assert [e.id for e in client.stream_events(OP_ID)] == [5, 6]
    assert route.calls[1].request.headers["Last-Event-Id"] == "5"


def test_stream_resume_from_caller_cursor_and_spid_filter(api, client):
    route = api.get(EVENTS_PATH).mock(return_value=sse(frame(42, "stdout", {"value": "a"}), completion(43)))
    events = list(client.stream_events(OP_ID, last_event_id=41, spid=1))
    assert [e.id for e in events] == [42, 43]
    request = route.calls.last.request
    assert request.headers["Last-Event-Id"] == "41"
    assert request.url.params["spid"] == "1"


def test_stream_retries_425_with_retry_after(api, client, clock):
    api.get(EVENTS_PATH).mock(
        side_effect=[
            httpx.Response(425, json={"error": "events not yet available"}, headers={"Retry-After": "2"}),
            sse(completion(0)),
        ]
    )
    assert [e.type for e in client.stream_events(OP_ID)] == ["completion"]
    assert clock.sleeps == [2.0]


def test_stream_without_follow_ends_at_end_of_log(api, client):
    route = api.get(EVENTS_PATH).mock(return_value=sse(frame(0, "init", {}, spid=0)))
    assert [e.id for e in client.stream_events(OP_ID, follow=False)] == [0]
    assert route.call_count == 1
    assert route.calls.last.request.url.params["follow"] == "0"


def test_stream_gives_up_after_max_reconnects(api, client):
    route = api.get(EVENTS_PATH).mock(return_value=sse(SSE_ERROR))
    with pytest.raises(SandboxStreamError, match="sse_error"):
        list(client.stream_events(OP_ID, max_reconnects=2))
    assert route.call_count == 3


def test_stream_auth_error_is_not_retried(api, client):
    route = api.get(EVENTS_PATH).mock(return_value=httpx.Response(401, json={"error": "bad token"}))
    with pytest.raises(SandboxAuthError):
        list(client.stream_events(OP_ID))
    assert route.call_count == 1


def test_consumer_can_stop_early(api, client):
    api.get(EVENTS_PATH).mock(return_value=sse(frame(0, "init", {}, spid=0), frame(1, "spawn", {})))
    stream = client.stream_events(OP_ID)
    assert next(stream).id == 0
    stream.close()
