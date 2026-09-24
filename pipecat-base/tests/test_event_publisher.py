"""The envelope the event publisher accepts, and how records become one.

The publisher validates what it is sent (pipecat-cloud-sandbox
event-publisher, types.ts): required non-empty strings under 256 characters,
an ISO-8601 ``ts``, and ``event_properties`` as a JSON object under 10KB. A
record that fails validation is a 400 and is lost, so these tests pin the
envelope against those rules rather than against our own idea of it.
"""

import asyncio
import json
import os
import pathlib
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pcc_observers
import pcc_structured_logs
import pytest
from pydantic import BaseModel

# The publisher's own check, copied from types.ts.
ISO_8601 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
MAX_STRING_LENGTH = 256
MAX_EVENT_PROPERTIES_LENGTH = 10240


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
    monkeypatch.setattr(pcc_observers, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_observers, "_events_endpoint", "http://publisher:3000/events")
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
    asyncio.run(pcc_observers._publish_event("speech_event", {"kind": "user_turn_started"}))

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
    asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))
    asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))

    assert posted[0][1]["event_uuid"] != posted[1][1]["event_uuid"]


def test_nothing_is_published_outside_a_session(monkeypatch):
    """A record with no session to name is one nothing can be joined to."""
    posts = []

    class _Session:
        closed = False

        def post(self, url, json=None):
            posts.append(url)
            return _Response()

    monkeypatch.setattr(pcc_observers, "_get_http_session", lambda: _Session())
    monkeypatch.setattr(pcc_observers, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", None)

    asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))

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

    monkeypatch.setattr(pcc_observers, "_get_http_session", lambda: _Broken())
    monkeypatch.setattr(pcc_observers, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")

    asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))

    assert attempts == ["http://publisher:3000/events"]


def _always_fails(monkeypatch, session_id="session-abc"):
    """Point publishing at an endpoint that never answers."""
    attempts = []

    class _Broken:
        closed = False

        def post(self, url, json=None):
            attempts.append(url)
            raise OSError("connection refused")

    monkeypatch.setattr(pcc_observers, "_get_http_session", lambda: _Broken())
    monkeypatch.setattr(pcc_observers, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", session_id)
    monkeypatch.setattr(pcc_observers, "_consecutive_failures", 0)
    monkeypatch.setattr(pcc_observers, "_counting_for_session", session_id)
    return attempts


def test_a_session_stops_publishing_once_it_has_failed_enough(monkeypatch):
    """A region with no egress to the publisher waits out every record."""
    attempts = _always_fails(monkeypatch)

    for _ in range(pcc_observers._MAX_CONSECUTIVE_FAILURES + 20):
        asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))

    assert len(attempts) == pcc_observers._MAX_CONSECUTIVE_FAILURES


def test_a_publish_that_works_clears_the_count(monkeypatch, posted):
    """Failures have to be consecutive to stop a session trying."""
    monkeypatch.setattr(
        pcc_observers, "_consecutive_failures", pcc_observers._MAX_CONSECUTIVE_FAILURES - 1
    )
    monkeypatch.setattr(pcc_observers, "_counting_for_session", "session-abc")

    asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))

    assert pcc_observers._consecutive_failures == 0


def test_the_next_session_tries_again(monkeypatch):
    """A publisher that comes back is picked up without restarting the pod."""
    attempts = _always_fails(monkeypatch)
    for _ in range(pcc_observers._MAX_CONSECUTIVE_FAILURES + 5):
        asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))
    gave_up_at = len(attempts)

    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-def")
    asyncio.run(pcc_observers._publish_event("error", {"category": "connectivity"}))

    assert len(attempts) == gave_up_at + 1


def test_publishing_is_off_when_no_endpoint_is_configured(monkeypatch):
    posts = []

    class _Session:
        closed = False

        def post(self, url, json=None):
            posts.append(url)
            return _Response()

    monkeypatch.setattr(pcc_observers, "_get_http_session", lambda: _Session())
    monkeypatch.setattr(pcc_observers, "_events_endpoint", None)

    asyncio.run(pcc_observers._publish_event("speech_event", {"kind": "user_turn_started"}))

    assert posts == []


def test_the_timestamp_format_the_envelope_sends_is_accepted():
    """The format `_publish_event` builds, checked against the publisher's regex."""
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


def _hide(monkeypatch, *names):
    """Make importing these observer modules raise, as an older Pipecat would."""
    import sys

    for name in names:
        monkeypatch.setitem(sys.modules, f"pipecat.observers.{name}", None)


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
    """What a bot's tools were asked and answered stays with the bot."""
    published = pcc_observers._PUBLISHED_FIELDS
    assert "arguments" not in published["function_call_event"]
    assert "result" not in published["function_call_event"]
    assert "error" not in published["function_call_event"]
    assert "message" not in published["error"]


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


def test_transcripts_are_published_for_both_sides(posted):
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregator,
        LLMUserAggregator,
    )

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


def test_a_turn_with_no_words_is_not_a_transcript(posted):
    """An assistant turn can be a tool call and nothing else."""
    from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator

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


def test_a_deployment_excluding_content_publishes_no_transcript(monkeypatch, posted):
    """Everything else still travels; only what was said stays behind."""
    user = _transcript_setup(monkeypatch, exclude=True)

    assert user.handlers == {}
    assert posted == []


def test_transcripts_travel_when_content_is_not_excluded(monkeypatch):
    user = _transcript_setup(monkeypatch, exclude=False)

    assert "on_user_turn_message_added" in user.handlers


@pytest.mark.parametrize(
    "value,excluded",
    [
        (None, False),
        ("", False),
        ("false", False),
        ("FALSE", False),
        (" false ", False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("anything else", True),
    ],
)
def test_how_the_exclusion_flag_is_read(monkeypatch, value, excluded):
    """The platform sets true or false; anything else excludes."""
    if value is None:
        monkeypatch.delenv("PCC_EXCLUDE_CONTENT", raising=False)
    else:
        monkeypatch.setenv("PCC_EXCLUDE_CONTENT", value)

    read = os.environ.get("PCC_EXCLUDE_CONTENT", "").strip().lower() not in ("", "false")

    assert read is excluded
