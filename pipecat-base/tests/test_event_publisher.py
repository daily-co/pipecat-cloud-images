"""The envelope the event publisher accepts, and how records become one.

The publisher validates what it is sent (pipecat-cloud-sandbox
event-publisher, types.ts): required non-empty strings under 256 characters,
an ISO-8601 ``ts``, and ``event_properties`` as a JSON object under 10KB. A
record that fails validation is a 400 and is lost, so these tests pin the
envelope against those rules rather than against our own idea of it.
"""

import asyncio
import importlib.util
import json
import pathlib
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pcc_events
import pcc_observers
import pcc_structured_logs
import pytest
from pydantic import BaseModel

# The publisher's own check, copied from types.ts.
ISO_8601 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
MAX_STRING_LENGTH = 256
MAX_EVENT_PROPERTIES_LENGTH = 10240
# The pod's intake answers 413 to a request body past 16 KB.
MAX_REQUEST_BYTES = 16384


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Stands in for the aiohttp session, recording what was posted."""

    closed = False

    def __init__(self):
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        return _Response()


@pytest.fixture
def posted(monkeypatch):
    session = _Session()
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")
    return session.posts


class _Record:
    """A stand-in for an observer record, which is a Pydantic model."""

    def __init__(self, dumped):
        self._dumped = dumped

    def model_dump(self, **kwargs):
        return self._dumped


class _Unserializable:
    def model_dump(self, **kwargs):
        raise TypeError("cannot serialize a socket")


def test_the_envelope_carries_what_the_publisher_requires(posted):
    asyncio.run(pcc_events.publish_event("speech_event", {"kind": "user_turn_started"}))

    url, payload = posted[0]
    assert url == "http://publisher:3000/events"
    for field in ("ts", "session_id", "event_name", "event_uuid"):
        assert isinstance(payload[field], str)
        assert 0 < len(payload[field]) <= MAX_STRING_LENGTH
    assert ISO_8601.match(payload["ts"])
    assert payload["event_name"] == "speech_event"
    assert payload["session_id"] == "session-abc"
    assert payload["event_properties"] == {"kind": "user_turn_started"}


def test_each_event_is_published_under_its_own_uuid(posted):
    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))
    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert posted[0][1]["event_uuid"] != posted[1][1]["event_uuid"]


def test_nothing_is_published_outside_a_session(monkeypatch):
    """A record with no session to name is one nothing can be joined to."""
    posts = []

    class _Session:
        closed = False

        def post(self, url, json=None):
            posts.append(url)
            return _Response()

    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: _Session())
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", None)

    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert posts == []


def test_a_record_is_published_as_its_own_fields(posted):
    record = _Record({"kind": "service_latency", "processor": "stt", "seconds": 0.3})

    asyncio.run(pcc_observers._publish_record("service_latency", record))

    _, payload = posted[0]
    assert payload["event_name"] == "service_latency"
    assert payload["event_properties"] == {
        "kind": "service_latency",
        "processor": "stt",
        "seconds": 0.3,
    }


def test_a_record_that_will_not_serialize_is_dropped(posted):
    """A handler's values are the application's, and may be anything."""
    asyncio.run(pcc_observers._publish_record("function_call_event", _Unserializable()))

    assert posted == []


def test_a_failing_publish_never_reaches_the_observer(monkeypatch):
    """Telemetry that cannot be delivered must not disturb the bot."""
    attempts = []

    class _Broken:
        closed = False

        def post(self, url, json=None):
            attempts.append(url)
            raise OSError("connection refused")

    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: _Broken())
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")

    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert attempts == ["http://publisher:3000/events"]


def _always_fails(monkeypatch, session_id="session-abc"):
    """Point publishing at an endpoint that never answers."""
    attempts = []

    class _Broken:
        closed = False

        def post(self, url, json=None):
            attempts.append(url)
            raise OSError("connection refused")

    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: _Broken())
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", session_id)
    monkeypatch.setattr(pcc_events, "_consecutive_failures", {})
    return attempts


def test_a_session_stops_publishing_once_it_has_failed_enough(monkeypatch):
    """A region with no egress to the publisher waits out every record."""
    attempts = _always_fails(monkeypatch)

    for _ in range(pcc_events._MAX_CONSECUTIVE_FAILURES + 20):
        asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert len(attempts) == pcc_events._MAX_CONSECUTIVE_FAILURES


def test_a_publish_that_works_clears_the_count(monkeypatch, posted):
    """Failures have to be consecutive to stop a session trying."""
    monkeypatch.setattr(
        pcc_events,
        "_consecutive_failures",
        {"session-abc": pcc_events._MAX_CONSECUTIVE_FAILURES - 1},
    )

    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert pcc_events._consecutive_failures == {"session-abc": 0}


def test_the_next_session_tries_again(monkeypatch):
    """A publisher that comes back is picked up without restarting the pod."""
    attempts = _always_fails(monkeypatch)
    for _ in range(pcc_events._MAX_CONSECUTIVE_FAILURES + 5):
        asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))
    gave_up_at = len(attempts)

    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-def")
    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert len(attempts) == gave_up_at + 1


def test_overlapping_sessions_each_keep_their_own_failure_count(monkeypatch):
    """Records alternating between two sessions must not keep resetting the count."""
    attempts = _always_fails(monkeypatch)

    for _ in range(pcc_events._MAX_CONSECUTIVE_FAILURES + 20):
        for session_id in ("session-abc", "session-def"):
            monkeypatch.setattr(pcc_structured_logs, "_current_session_id", session_id)
            asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert len(attempts) == 2 * pcc_events._MAX_CONSECUTIVE_FAILURES


def test_failure_counts_stay_bounded_on_a_long_lived_pod(monkeypatch):
    _always_fails(monkeypatch)

    for index in range(pcc_events._MAX_COUNTED_SESSIONS + 20):
        monkeypatch.setattr(pcc_structured_logs, "_current_session_id", f"session-{index}")
        asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert len(pcc_events._consecutive_failures) == pcc_events._MAX_COUNTED_SESSIONS


def test_a_session_that_gave_up_stays_given_up_while_newer_sessions_come_and_go(monkeypatch):
    attempts = _always_fails(monkeypatch)
    for _ in range(pcc_events._MAX_CONSECUTIVE_FAILURES):
        asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))
    gave_up_at = len(attempts)

    newer_sessions = pcc_events._MAX_COUNTED_SESSIONS * 2
    for index in range(newer_sessions):
        for session_id in (f"session-{index}", "session-abc"):
            monkeypatch.setattr(pcc_structured_logs, "_current_session_id", session_id)
            asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    assert len(attempts) == gave_up_at + newer_sessions


@pytest.mark.parametrize("succeeded", [True, False])
def test_a_result_for_a_session_already_forgotten_adds_nothing(monkeypatch, succeeded):
    _always_fails(monkeypatch)
    for index in range(pcc_events._MAX_COUNTED_SESSIONS):
        monkeypatch.setattr(pcc_structured_logs, "_current_session_id", f"session-{index}")
        asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))

    pcc_events._record_publish_result("session-forgotten", succeeded)

    assert "session-forgotten" not in pcc_events._consecutive_failures
    assert len(pcc_events._consecutive_failures) == pcc_events._MAX_COUNTED_SESSIONS


def test_publishing_is_off_when_no_endpoint_is_configured(monkeypatch):
    posts = []

    class _Session:
        closed = False

        def post(self, url, json=None):
            posts.append(url)
            return _Response()

    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: _Session())
    monkeypatch.setattr(pcc_events, "_events_endpoint", None)

    asyncio.run(pcc_events.publish_event("speech_event", {"kind": "user_turn_started"}))

    assert posts == []


def test_the_timestamp_format_the_envelope_sends_is_accepted():
    """The format `pcc_events.publish_event` builds, checked against the publisher's regex."""
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    assert ISO_8601.match(ts)


def test_a_record_stays_within_the_publishers_size_limit():
    """A turn's contributions are the largest record published per turn."""
    breakdown = {
        "contributions": [
            {
                "key": "speech_synthesis",
                "label": "speech synthesis",
                "owner": "CartesiaTTSService#0",
                "owner_kind": "service",
                "start_time": 1758000000.0,
                "duration_secs": 0.359,
            }
        ]
        * 12,
        "measured_from": "user_speech",
        "total_secs": 1.044,
    }

    assert len(json.dumps(breakdown)) < MAX_EVENT_PROPERTIES_LENGTH


class _Pipeline:
    """An empty pipeline, with nothing nested inside it."""

    processors = []


class _Worker:
    """Stands in for the PipelineWorker, recording what was attached."""

    def __init__(self):
        self.observers = []
        self.pipeline = _Pipeline()

    def add_observer(self, observer):
        self.observers.append(observer)


def _pipecat_has(*modules):
    """Whether the installed pipecat-ai has these modules. CI also runs the
    oldest release the image supports, which predates several of them."""
    try:
        return all(importlib.util.find_spec(m) is not None for m in modules)
    except ImportError:
        return False


_AGGREGATORS = "pipecat.processors.aggregators.llm_response_universal"
_needs_aggregators = pytest.mark.skipif(
    not _pipecat_has(_AGGREGATORS), reason=f"this pipecat-ai has no {_AGGREGATORS}"
)


def _hide(monkeypatch, *names):
    """Make importing these observer modules raise, as an older Pipecat would."""
    import sys

    for name in names:
        monkeypatch.setitem(sys.modules, f"pipecat.observers.{name}", None)


@pytest.mark.skipif(
    not _pipecat_has(
        "pipecat.observers.startup_timing_observer",
        "pipecat.observers.user_bot_latency_observer",
    ),
    reason="this pipecat-ai has neither observer the test expects to remain",
)
def test_a_pipecat_without_the_newer_observers_reports_what_it_has(monkeypatch):
    """A bot pins its own Pipecat, so the image cannot assume what is there."""
    worker = _Worker()
    _hide(
        monkeypatch,
        "service_metrics_observer",
        "speaking_observer",
        "function_call_observer",
        "error_observer",
    )

    asyncio.run(pcc_observers.setup_pipeline_worker(worker))

    attached = {type(o).__name__ for o in worker.observers}
    assert attached == {"StartupTimingObserver", "UserBotLatencyObserver"}


def test_a_pipecat_without_any_of_them_still_starts(monkeypatch):
    worker = _Worker()
    _hide(
        monkeypatch,
        "startup_timing_observer",
        "user_bot_latency_observer",
        "service_metrics_observer",
        "speaking_observer",
        "function_call_observer",
        "error_observer",
    )

    asyncio.run(pcc_observers.setup_pipeline_worker(worker))

    assert worker.observers == []


def test_the_fields_that_can_quote_a_conversation_are_not_published():
    """What a bot's tools were asked and answered travels only where the
    transcript does."""
    published = pcc_observers._PUBLISHED_FIELDS
    assert "arguments" not in published["function_call_event"]
    assert "result" not in published["function_call_event"]
    assert "error" not in published["function_call_event"]
    assert "message" not in published["error"]


class _ErrorEvent(BaseModel):
    """The shape of Pipecat's ErrorEvent."""

    message: str
    category: str
    exception_type: str | None = None
    processor: str
    processor_usable: bool
    timestamp: float


_AN_ERROR = _ErrorEvent(
    message="Unable to send message: not joined",
    category="unknown",
    processor="DailyOutputTransport#0",
    processor_usable=True,
    timestamp=1791890285.169,
)


def test_a_deployment_excluding_content_publishes_an_error_without_its_message(monkeypatch, posted):
    """The message is the one error field that can quote what was said."""
    monkeypatch.setattr(pcc_observers, "_exclude_content", True)

    asyncio.run(pcc_observers._publish_record("error", _AN_ERROR))

    assert posted[0][1]["event_properties"] == {
        "category": "unknown",
        "processor": "DailyOutputTransport#0",
        "processor_usable": True,
        "timestamp": 1791890285.169,
    }


def test_an_errors_message_travels_where_the_transcript_does(monkeypatch, posted):
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)

    asyncio.run(pcc_observers._publish_record("error", _AN_ERROR))

    properties = posted[0][1]["event_properties"]
    assert properties["message"] == "Unable to send message: not joined"
    assert properties["processor"] == "DailyOutputTransport#0"


def _stringify_length(value) -> int:
    """The length the publisher measures: JavaScript's JSON.stringify, in UTF-16 code units."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-16-le")) // 2


def _assert_accepted(payload):
    """What both the pod's intake and the publisher will take."""
    assert _stringify_length(payload["event_properties"]) <= MAX_EVENT_PROPERTIES_LENGTH
    # aiohttp sends `json=` as json.dumps writes it.
    assert len(json.dumps(payload).encode()) <= MAX_REQUEST_BYTES


def _published_properties(event_name, record):
    """What publishing one record sends, read back from a fresh session."""
    posts = []

    class _Session:
        closed = False

        def post(self, url, json=None):
            posts.append(json)
            return _Response()

    pcc_events._get_http_session, saved = (lambda: _Session()), pcc_events._get_http_session
    try:
        asyncio.run(pcc_observers._publish_record(event_name, record))
    finally:
        pcc_events._get_http_session = saved
    return posts[0]["event_properties"]


def test_content_fields_are_only_ever_added_to_a_record_that_publishes():
    """A content field belongs to a record the list already names."""
    assert set(pcc_observers._CONTENT_FIELDS) <= set(pcc_observers._PUBLISHED_FIELDS)


class _FunctionCallEvent(BaseModel):
    """The shape of Pipecat's FunctionCallEvent."""

    kind: str
    function_name: str
    tool_call_id: str
    timestamp: float
    arguments: Any | None = None
    result: Any | None = None
    error: str | None = None


_A_CALL = _FunctionCallEvent(
    kind="function_call_completed",
    function_name="book_table",
    tool_call_id="call_1",
    timestamp=1791890285.169,
    arguments={"name": "Jamie Lee", "party_size": 4},
    result={"confirmation": "ABC123"},
    error="the kitchen is closed",
)

_CALL_CONTENT = {"arguments", "result", "error"}


def test_a_deployment_excluding_content_publishes_a_call_without_its_content(monkeypatch, posted):
    monkeypatch.setattr(pcc_observers, "_exclude_content", True)

    asyncio.run(pcc_observers._publish_record("function_call_event", _A_CALL))

    properties = posted[0][1]["event_properties"]
    assert not properties.keys() & _CALL_CONTENT
    assert properties["function_name"] == "book_table"


def test_a_calls_content_travels_where_the_transcript_does(monkeypatch, posted):
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)

    asyncio.run(pcc_observers._publish_record("function_call_event", _A_CALL))

    properties = posted[0][1]["event_properties"]
    assert properties["arguments"] == {"name": "Jamie Lee", "party_size": 4}
    assert properties["result"] == {"confirmation": "ABC123"}
    assert properties["error"] == "the kitchen is closed"


# Each record that carries text, by the field the text is in.
_TEXT_RECORDS = [
    ("error", _AN_ERROR, "message"),
    ("function_call_event", _A_CALL.model_copy(update={"result": None}), "error"),
    ("function_call_event", _A_CALL.model_copy(update={"error": None}), "result"),
]


@pytest.mark.parametrize(("event_name", "record", "field"), _TEXT_RECORDS)
@pytest.mark.parametrize(
    "char",
    ["x", "\x00", "\n", '"', "\\", "é", " ", "😀"],
    ids=["ascii", "nul", "newline", "quote", "backslash", "accented", "separator", "astral"],
)
def test_long_text_is_cut_to_what_both_limits_accept(
    monkeypatch, posted, event_name, record, field, char
):
    """Escaping can make one character twelve, so text is cut by what it is
    written as: the pod's intake refuses an emoji-filled request, and the
    publisher a NUL-filled record, that a character count would let through."""
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    # The longest session id the envelope can carry.
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "s" * (MAX_STRING_LENGTH - 1))
    long = record.model_copy(update={field: char * 20_000})

    asyncio.run(pcc_observers._publish_record(event_name, long))

    payload = posted[0][1]
    text = payload["event_properties"][field]
    assert text.endswith("…")
    assert set(text[:-1]) == {char}
    assert payload["event_properties"]["timestamp"] == record.timestamp
    _assert_accepted(payload)


def _room(event_name, record, field):
    """How long, as json.dumps writes it, the field's value can be and still travel whole."""
    properties = _published_properties(event_name, record.model_copy(update={field: ""}))
    return pcc_observers._MAX_PROPERTIES_CHARS - (len(json.dumps(properties)) - 2)


def test_text_that_fits_exactly_travels_whole_and_one_more_character_is_cut(monkeypatch, posted):
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    room = _room("error", _AN_ERROR, "message")
    # json.dumps writes a string with its two quotes.
    fits = "x" * (room - 2)

    asyncio.run(
        pcc_observers._publish_record("error", _AN_ERROR.model_copy(update={"message": fits}))
    )
    asyncio.run(
        pcc_observers._publish_record("error", _AN_ERROR.model_copy(update={"message": fits + "x"}))
    )

    whole, cut = (payload["event_properties"] for _, payload in posted)
    assert whole["message"] == fits
    assert len(json.dumps(whole)) == pcc_observers._MAX_PROPERTIES_CHARS
    assert cut["message"].endswith("…")
    assert len(json.dumps(cut)) <= pcc_observers._MAX_PROPERTIES_CHARS


def test_text_is_cut_before_a_character_that_does_not_fit_rather_than_inside_it(
    monkeypatch, posted
):
    """An emoji is written as two escapes, twelve characters; half of one is no character."""
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    room = _room("error", _AN_ERROR, "message")
    # Room for the ellipsis and the x's, but not the emoji after them.
    xs = "x" * (room - len(json.dumps("…")) - 11)
    message = xs + "😀" + "x" * 100

    asyncio.run(
        pcc_observers._publish_record("error", _AN_ERROR.model_copy(update={"message": message}))
    )

    properties = posted[0][1]["event_properties"]
    assert properties["message"] == xs + "…"
    _assert_accepted(posted[0][1])


def test_a_result_the_record_has_room_for_travels_whole(monkeypatch, posted):
    """Search results, query rows and API payloads are the calls a dashboard most
    wants to read, and they run to a few KB."""
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    result = {"rows": [{"id": index, "name": "x" * 40} for index in range(100)]}
    assert 5_000 < len(json.dumps(result)) < pcc_observers._MAX_PROPERTIES_CHARS

    asyncio.run(
        pcc_observers._publish_record(
            "function_call_event", _A_CALL.model_copy(update={"result": result})
        )
    )

    properties = posted[0][1]["event_properties"]
    assert properties["result"] == result
    assert "result_omitted" not in properties


def test_a_value_that_fits_exactly_travels_whole(monkeypatch, posted):
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    record = _A_CALL.model_copy(update={"error": None})
    # json.dumps writes {"rows": "…"} with 12 characters around the text.
    result = {"rows": "x" * (_room("function_call_event", record, "result") - 12)}

    asyncio.run(
        pcc_observers._publish_record(
            "function_call_event", record.model_copy(update={"result": result})
        )
    )

    properties = posted[0][1]["event_properties"]
    assert properties["result"] == result
    assert len(json.dumps(properties)) == pcc_observers._MAX_PROPERTIES_CHARS


@pytest.mark.parametrize("field", ["arguments", "result"])
def test_a_value_too_long_to_publish_whole_is_left_out_and_says_so(monkeypatch, posted, field):
    """Cut, it would no longer parse as what the tool was asked or answered."""
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    long = _A_CALL.model_copy(update={field: {"rows": ["😀" * 100] * 200}})

    asyncio.run(pcc_observers._publish_record("function_call_event", long))

    properties = posted[0][1]["event_properties"]
    assert field not in properties
    assert properties[f"{field}_omitted"] is True
    assert properties["kind"] == "function_call_completed"
    assert properties["timestamp"] == _A_CALL.timestamp
    _assert_accepted(posted[0][1])


def test_content_that_together_does_not_fit_is_all_left_out():
    """Pipecat reports one content field a record, but a record whose content
    leaves no room for saying what it left out still keeps its own fields."""
    # json.dumps writes this as 10,151 characters, the arguments as 69 more and
    # the result's flag as 24, past the limit; the flags alone fit.
    properties = {
        "pad": "x" * 10_140,
        "arguments": {"a": "y" * 60},
        "result": {"r": "z" * 100},
    }

    pcc_observers._fit_content(properties, ["arguments", "result"])

    assert properties == {
        "pad": "x" * 10_140,
        "arguments_omitted": True,
        "result_omitted": True,
    }
    assert len(json.dumps(properties)) <= pcc_observers._MAX_PROPERTIES_CHARS


_FUNCTION_CALL_OBSERVER = "pipecat.observers.function_call_observer"


@pytest.mark.skipif(
    not _pipecat_has(_FUNCTION_CALL_OBSERVER),
    reason=f"this pipecat-ai has no {_FUNCTION_CALL_OBSERVER}",
)
@pytest.mark.parametrize("exclude_content", [True, False])
def test_the_real_observer_reports_a_calls_content_where_the_transcript_travels(
    monkeypatch, posted, exclude_content
):
    from pipecat.frames.frames import (
        FunctionCallInProgressFrame,
        FunctionCallResultFrame,
        FunctionCallsStartedFrame,
    )
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.services.llm_service import FunctionCallFromLLM

    monkeypatch.setattr(pcc_observers, "_exclude_content", exclude_content)
    arguments = {"party_size": 4}
    calls = [
        FunctionCallFromLLM(
            function_name="book_table", tool_call_id=call, arguments=arguments, context=None
        )
        for call in ("call_1", "call_2")
    ]
    frames = [
        FunctionCallsStartedFrame(function_calls=calls),
        *(
            FunctionCallInProgressFrame(
                function_name="book_table", tool_call_id=call.tool_call_id, arguments=arguments
            )
            for call in calls
        ),
        FunctionCallResultFrame(
            function_name="book_table",
            tool_call_id="call_1",
            arguments=arguments,
            result={"confirmation": "ABC123"},
        ),
        FunctionCallResultFrame(
            function_name="book_table",
            tool_call_id="call_2",
            arguments=arguments,
            result=None,
            error="the kitchen is closed",
        ),
    ]

    async def run():
        worker = _Worker()
        await pcc_observers._setup_function_call_observer(worker)
        for frame in frames:
            await worker.observers[0].on_push_frame(
                FramePushed(
                    source=None,
                    destination=None,
                    frame=frame,
                    direction=FrameDirection.DOWNSTREAM,
                    timestamp=0,
                )
            )

    asyncio.run(run())

    reported = [
        (p["kind"], p["tool_call_id"], {f: p[f] for f in p.keys() & _CALL_CONTENT})
        for p in (payload["event_properties"] for _, payload in posted)
    ]
    if exclude_content:
        assert [content for _, _, content in reported] == [{}] * 6
        return
    assert reported == [
        ("function_call_started", "call_1", {"arguments": arguments}),
        ("function_call_started", "call_2", {"arguments": arguments}),
        ("function_call_in_progress", "call_1", {"arguments": arguments}),
        ("function_call_in_progress", "call_2", {"arguments": arguments}),
        ("function_call_completed", "call_1", {"result": {"confirmation": "ABC123"}}),
        ("function_call_failed", "call_2", {"error": "the kitchen is closed"}),
    ]


def test_every_record_published_names_its_fields():
    """A record with no field list publishes nothing, silently."""
    import re

    source = pathlib.Path(pcc_observers.__file__).read_text()
    published = set(re.findall(r'_publish_record\(\s*"([a-z_]+)"', source))

    assert published, "no _publish_record call sites found"
    assert published <= set(pcc_observers._PUBLISHED_FIELDS)


def test_a_record_publishes_only_what_its_list_names(monkeypatch, posted):
    """A field the list does not name stays behind, whatever the model holds."""

    class _Record(BaseModel):
        kind: str
        transcript: str

    monkeypatch.setitem(pcc_observers._PUBLISHED_FIELDS, "speech_event", {"kind": True})

    asyncio.run(
        pcc_observers._publish_record(
            "speech_event", _Record(kind="user_turn_started", transcript="my card number is")
        )
    )

    assert posted[0][1]["event_properties"] == {"kind": "user_turn_started"}


class _NestedPipeline:
    """A pipeline holding processors, some of which are pipelines."""

    def __init__(self, *processors):
        self.processors = list(processors)


def _aggregator(base):
    """A real aggregator by type, without running its constructor."""

    class _Stub(base):
        processors = []

        def __init__(self):
            self.handlers = {}

        def event_handler(self, name):
            def decorator(handler):
                self.handlers[name] = handler
                return handler

            return decorator

    return _Stub()


def test_the_walk_reaches_processors_nested_in_pipelines():
    """A bot may put its aggregators inside a pipeline of its own."""
    deep, shallow = _NestedPipeline(), _NestedPipeline()
    pipeline = _NestedPipeline(_NestedPipeline(_NestedPipeline(deep)), shallow)

    found = list(pcc_observers._every_processor(pipeline))

    assert deep in found and shallow in found


@_needs_aggregators
def test_transcripts_are_published_for_both_sides(monkeypatch, posted):
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )

    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    user = _aggregator(LLMUserAggregator)
    assistant = _aggregator(LLMAssistantAggregator)
    worker = _Worker()
    worker.pipeline = _NestedPipeline(_NestedPipeline(user), assistant)

    asyncio.run(pcc_observers._setup_transcripts(worker))

    asyncio.run(
        user.handlers["on_user_turn_message_added"](
            user, SimpleNamespace(content="what is the weather", timestamp="2026-09-24T12:00:00Z")
        )
    )
    asyncio.run(
        assistant.handlers["on_assistant_turn_stopped"](
            assistant,
            SimpleNamespace(
                content="it is nice", interrupted=True, timestamp="2026-09-24T12:00:02Z"
            ),
        )
    )

    assert [p[1]["event_properties"]["role"] for p in posted] == ["user", "assistant"]
    assert posted[0][1]["event_properties"]["text"] == "what is the weather"
    assert posted[1][1]["event_properties"]["interrupted"] is True


@_needs_aggregators
def test_a_turn_with_no_words_is_not_a_transcript(monkeypatch, posted):
    """An assistant turn can be a tool call and nothing else."""
    from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator

    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    assistant = _aggregator(LLMAssistantAggregator)
    worker = _Worker()
    worker.pipeline = _NestedPipeline(assistant)

    asyncio.run(pcc_observers._setup_transcripts(worker))
    asyncio.run(
        assistant.handlers["on_assistant_turn_stopped"](
            assistant,
            SimpleNamespace(content="", interrupted=False, timestamp="2026-09-24T12:00:02Z"),
        )
    )

    assert posted == []


def _transcript_setup(monkeypatch, exclude):
    """Set up transcripts with content excluded or not, returning the aggregator."""
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator

    user = _aggregator(LLMUserAggregator)
    worker = _Worker()
    worker.pipeline = _NestedPipeline(user)
    monkeypatch.setattr(pcc_observers, "_exclude_content", exclude)

    asyncio.run(pcc_observers._setup_transcripts(worker))
    return user


@_needs_aggregators
def test_a_deployment_excluding_content_publishes_no_transcript(monkeypatch, posted):
    """Everything else still travels; only what was said stays behind."""
    user = _transcript_setup(monkeypatch, exclude=True)

    assert user.handlers == {}
    assert posted == []


@_needs_aggregators
def test_transcripts_travel_when_content_is_not_excluded(monkeypatch):
    user = _transcript_setup(monkeypatch, exclude=False)

    assert "on_user_turn_message_added" in user.handlers
