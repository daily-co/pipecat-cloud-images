"""The `conversation` record: a whole call, as Pipecat's evals judge it.

Pipecat's EvalConversationObserver (pipecat-ai 1.13.0+) reports the
conversation once the pipeline is done. The image publishes it where the
transcript is published, so a production call can be judged with the same
judge as a simulated one.
"""

import asyncio
import importlib.util
import json
import sys

import pcc_events
import pcc_observers
import pcc_structured_logs
import pytest

# The event publisher's limit on a record's properties (types.ts), and the
# pod's event intake's limit on a whole request (pipecat-queue-sidecar,
# event_intake.py), which a record passes through first.
MAX_EVENT_PROPERTIES_LENGTH = 10240
MAX_INTAKE_REQUEST_BYTES = 16 * 1024

_OBSERVER = "pipecat.evals.conversation_observer"


def _pipecat_has(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:
        return False


_needs_observer = pytest.mark.skipif(
    not _pipecat_has(_OBSERVER), reason=f"this pipecat-ai has no {_OBSERVER}"
)


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
        self.posts.append(json)
        return _Response()


@pytest.fixture
def posted(monkeypatch):
    session = _Session()
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://127.0.0.1:9092/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")
    return session.posts


@pytest.fixture
def content_published(monkeypatch):
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)


class _Worker:
    def __init__(self):
        self.observers = []

    def add_observer(self, observer):
        self.observers.append(observer)


_A_CALL = [
    {"role": "assistant", "content": "Hi, how can I help?"},
    {"role": "user", "content": "A table for four on Friday, please."},
    {
        "role": "tool_call",
        "name": "check_availability",
        "arguments": {"day": "Friday", "party_size": 4},
        "cancelled": False,
    },
    {"role": "assistant", "content": "Let me check. Friday at seven works."},
]


@_needs_observer
def test_a_call_run_through_a_pipeline_is_published_whole(monkeypatch):
    """The setup file's hook, a real worker, and the observer's own cleanup.

    Pipecat loads the setup file from its path, as a module of its own that
    reads PCC_EXCLUDE_CONTENT as it loads, so the switch is set in the
    environment rather than on the module the tests import. Pipecat keeps the
    module it loaded for the rest of the process, so the test loads it into a
    cache of its own, which leaves with the test.
    """
    monkeypatch.setenv("PCC_EXCLUDE_CONTENT", "false")
    monkeypatch.setattr("pipecat.utils.startup._module_cache", {}, raising=False)
    from pipecat.frames.frames import (
        EndFrame,
        FunctionCallInProgressFrame,
        LLMContextAssistantTurnFrame,
        LLMContextUserTurnFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import WorkerRunner
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.processors.frame_processor import FrameProcessor

    published = []

    async def publish_event(name, properties=None):
        published.append((name, properties))

    monkeypatch.setattr(pcc_events, "publish_event", publish_event)
    monkeypatch.setenv("PIPECAT_SETUP_FILES", pcc_observers.__file__)

    class _Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    async def run():
        worker = PipelineWorker(
            Pipeline([_Passthrough()]), idle_timeout_secs=None, enable_rtvi=False
        )
        await worker.queue_frames(
            [
                LLMContextAssistantTurnFrame(text="Hi, how can I help?", timestamp=""),
                LLMContextUserTurnFrame(text="A table for four on Friday, please.", timestamp=""),
                FunctionCallInProgressFrame(
                    function_name="check_availability",
                    tool_call_id="call-1",
                    arguments={"day": "Friday", "party_size": 4},
                ),
                LLMContextAssistantTurnFrame(
                    text="Let me check. Friday at seven works.", timestamp=""
                ),
                EndFrame(),
            ]
        )
        await WorkerRunner(handle_sigint=False).run(worker)

    asyncio.run(run())

    conversations = [p for name, p in published if name == "conversation"]
    assert conversations == [{"message_count": 4, "messages": _A_CALL}]


@_needs_observer
def test_the_record_is_posted_as_an_event(posted, content_published):
    worker = _Worker()
    asyncio.run(pcc_observers._setup_conversation_observer(worker))
    (observer,) = worker.observers

    from pipecat.evals.conversation_observer import EvalConversationEndedMessage

    async def end():
        await observer._call_event_handler(
            "on_conversation_ended", EvalConversationEndedMessage(conversation=_A_CALL)
        )
        await observer.cleanup()

    asyncio.run(end())

    (payload,) = posted
    assert payload["event_name"] == "conversation"
    assert payload["session_id"] == "session-abc"
    assert payload["event_properties"] == {"message_count": 4, "messages": _A_CALL}


def test_a_deployment_excluding_content_records_no_conversation(monkeypatch):
    """It quotes the call, tool arguments included, as a transcript does."""
    monkeypatch.setattr(pcc_observers, "_exclude_content", True)
    worker = _Worker()

    asyncio.run(pcc_observers._setup_conversation_observer(worker))

    assert worker.observers == []


def test_a_pipecat_without_the_observer_still_starts(monkeypatch, content_published):
    """pipecat-ai before 1.13.0 has no EvalConversationObserver."""
    monkeypatch.setitem(sys.modules, _OBSERVER, None)
    worker = _Worker()

    asyncio.run(pcc_observers._setup_conversation_observer(worker))

    assert worker.observers == []


def test_a_worker_that_held_no_conversation_sends_nothing():
    """In a bot with several workers, only the one holding the context has one."""
    assert pcc_observers._conversation_record([]) is None


def test_only_the_fields_each_role_names_are_published():
    """A field or a role added upstream waits for someone to let it travel."""
    record = pcc_observers._conversation_record(
        [
            {"role": "user", "content": "hello", "timestamp": 1791890285.1},
            {"role": "tool_result", "name": "lookup", "content": "account 4242"},
            {
                "role": "tool_call",
                "name": "lookup",
                "arguments": {"id": 7},
                "cancelled": True,
                "result": "account 4242",
            },
        ]
    )

    assert record == {
        "message_count": 2,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "tool_call", "name": "lookup", "arguments": {"id": 7}, "cancelled": True},
        ],
    }


def test_a_conversation_that_will_not_serialize_is_dropped():
    """Tool arguments are the application's, and may be anything."""
    record = pcc_observers._conversation_record(
        [{"role": "tool_call", "name": "f", "arguments": {"at": object()}, "cancelled": False}]
    )

    assert record is None


def _call_of(characters):
    """A conversation whose record is about this many characters long."""
    return [{"role": "user", "content": "x" * characters}]


def test_a_conversation_past_the_limit_is_published_as_its_size():
    """Part of a call would be judged as if it were the whole one."""
    record = pcc_observers._conversation_record(_call_of(20_000))

    assert record["omitted"] == "too_large"
    assert record["message_count"] == 1
    assert record["length"] > pcc_observers._MAX_CONVERSATION_CHARS
    assert "messages" not in record


def _largest_call():
    """The longest conversation of one turn that is still published whole."""
    empty = len(json.dumps(pcc_observers._conversation_record(_call_of(0))))
    return _call_of(pcc_observers._MAX_CONVERSATION_CHARS - empty)


def test_a_conversation_at_the_limit_is_published_and_one_past_it_is_not():
    at_limit = _largest_call()
    past_limit = _call_of(len(at_limit[0]["content"]) + 1)

    assert "messages" in pcc_observers._conversation_record(at_limit)
    assert pcc_observers._conversation_record(past_limit)["omitted"] == "too_large"


def test_the_largest_conversation_published_fits_both_hops(posted):
    """The intake and the publisher each refuse a record past their limit."""
    largest = pcc_observers._conversation_record(_largest_call())

    asyncio.run(pcc_events.publish_event("conversation", largest))

    (payload,) = posted
    assert len(json.dumps(payload["event_properties"])) <= MAX_EVENT_PROPERTIES_LENGTH
    assert len(json.dumps(payload).encode()) < MAX_INTAKE_REQUEST_BYTES


def test_the_limit_is_the_publishers():
    assert pcc_observers._MAX_CONVERSATION_CHARS == MAX_EVENT_PROPERTIES_LENGTH
