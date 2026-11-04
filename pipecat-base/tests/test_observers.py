"""How Pipecat finds the hooks in the observers' setup file, and how a conversation publishes.

From 1.3.0, Pipecat offers every PIPECAT_SETUP_FILES entry to the runner as
well as to each worker, and warns about a file without the hook it looks for.
"""

import asyncio
import importlib.util
import json
import sys

import pcc_events
import pcc_observers
import pcc_structured_logs
import pytest


def _pipecat_has(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:
        return False


@pytest.mark.skipif(
    not _pipecat_has("pipecat.utils.startup"),
    reason="this pipecat-ai offers setup files to the pipeline task only",
)
@pytest.mark.parametrize("hook", ["setup_worker_runner", "setup_pipeline_worker"])
def test_pipecat_finds_each_hook_in_the_setup_file(monkeypatch, hook):
    from loguru import logger
    from pipecat.utils.startup import run_setup_hook

    monkeypatch.setenv("PIPECAT_SETUP_FILES", pcc_observers.__file__)
    target = _Worker()
    logged = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    try:
        asyncio.run(run_setup_hook(target=target, function_name=hook))
    finally:
        logger.remove(sink)

    assert logged == []


class _Worker:
    """Stands in for the runner or worker a hook is handed."""

    def __init__(self):
        self.observers = []

    def add_observer(self, observer):
        self.observers.append(observer)


# What the publisher allows a record's properties, in event-publisher's own terms.
MAX_EVENT_PROPERTIES_LENGTH = 10240

_needs_conversation_observer = pytest.mark.skipif(
    not _pipecat_has("pipecat.evals.conversation_observer"),
    reason="this pipecat-ai has no conversation observer",
)


class _Response:
    status = 202

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Stands in for the aiohttp session, recording each record it's handed."""

    closed = False

    def __init__(self):
        self.records = []

    def post(self, url, json=None):
        self.records.append(json)
        return _Response()


@pytest.fixture
def published(monkeypatch):
    session = _Session()
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")
    monkeypatch.setattr(pcc_observers, "_exclude_content", False)
    monkeypatch.setenv("PCC_EVAL_CONVERSATIONS", "true")
    return session.records


def _attached_observers():
    worker = _Worker()
    asyncio.run(pcc_observers._setup_conversation_observer(worker))
    return worker.observers


def _conversations(records):
    """What a reader gets back: each conversation's parts joined and decoded, by id."""
    assert {record["event_name"] for record in records} <= {"eval_conversation"}
    texts = {}
    for record in records:
        properties = record["event_properties"]
        assert "text" in properties
        texts.setdefault(properties["conversation_id"], []).append(properties)
    conversations = []
    for parts in texts.values():
        assert [part["part"] for part in parts] == list(range(parts[0]["parts"]))
        conversations.append(json.loads("".join(part["text"] for part in parts)))
    return conversations


@_needs_conversation_observer
def test_an_opted_in_deployment_publishes_what_the_real_observer_records(published):
    from pipecat.frames.frames import (
        FunctionCallInProgressFrame,
        LLMContextAssistantTurnFrame,
        LLMContextUserTurnFrame,
    )
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection

    arguments = {"name": "Jamie Lee", "party_size": 4}
    frames = [
        LLMContextUserTurnFrame(
            text="A table for four, please.", timestamp="2026-11-04T12:00:00+00:00"
        ),
        FunctionCallInProgressFrame(
            function_name="book_table", tool_call_id="call_1", arguments=arguments
        ),
        LLMContextAssistantTurnFrame(text="You're all set.", timestamp="2026-11-04T12:00:05+00:00"),
    ]

    async def run():
        worker = _Worker()
        await pcc_observers._setup_conversation_observer(worker)
        observer = worker.observers[0]
        for frame in frames:
            await observer.on_push_frame(
                FramePushed(
                    source=None,
                    destination=None,
                    frame=frame,
                    direction=FrameDirection.DOWNSTREAM,
                    timestamp=0,
                )
            )
        await observer.cleanup()

    asyncio.run(run())

    assert _conversations(published) == [
        [
            {"role": "user", "content": "A table for four, please."},
            {
                "role": "tool_call",
                "name": "book_table",
                "arguments": arguments,
                "cancelled": False,
            },
            {"role": "assistant", "content": "You're all set."},
        ]
    ]


@pytest.mark.parametrize("value", [None, "", "false", "1", "yes"])
def test_only_true_opts_a_deployment_in(published, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PCC_EVAL_CONVERSATIONS")
    else:
        monkeypatch.setenv("PCC_EVAL_CONVERSATIONS", value)

    assert _attached_observers() == []


def test_opting_in_cannot_override_a_deployment_excluding_content(published, monkeypatch):
    monkeypatch.setattr(pcc_observers, "_exclude_content", True)

    assert _attached_observers() == []


def test_an_older_pipecat_opted_in_still_starts(published, monkeypatch):
    monkeypatch.setitem(sys.modules, "pipecat.evals.conversation_observer", None)

    assert _attached_observers() == []


@pytest.mark.parametrize(
    "said",
    [
        "plain words " * 5000,
        '"quotes" and \\backslashes\\ ' * 5000,
        "日本語のテキストです。" * 5000,
    ],
)
def test_a_long_conversation_fits_the_publishers_limit_and_comes_back_whole(published, said):
    conversation = [
        {"role": "user", "content": said},
        {"role": "tool_call", "name": "note", "arguments": {"text": said}, "cancelled": False},
        {"role": "assistant", "content": said},
    ]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    assert len(published) > 1
    for record in published:
        length = len(json.dumps(record["event_properties"], ensure_ascii=False))
        assert length <= MAX_EVENT_PROPERTIES_LENGTH
    assert _conversations(published) == [conversation]


def test_each_workers_conversation_stays_apart(published):
    one = [{"role": "user", "content": "one"}]
    other = [{"role": "user", "content": "other"}]

    asyncio.run(pcc_observers._publish_conversation(one))
    asyncio.run(pcc_observers._publish_conversation(other))

    assert sorted(_conversations(published), key=str) == sorted([one, other], key=str)


def test_a_worker_that_heard_nothing_publishes_nothing(published):
    asyncio.run(pcc_observers._publish_conversation([]))

    assert published == []


@pytest.mark.parametrize(
    "arguments",
    [{"handle": object()}, {"score": float("nan")}],
    ids=["unserializable", "nan"],
)
def test_a_conversation_that_cannot_travel_says_so(published, arguments):
    conversation = [
        {"role": "tool_call", "name": "note", "arguments": arguments, "cancelled": False}
    ]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    assert [record["event_properties"]["omitted"] for record in published] == [True]
    assert "text" not in published[0]["event_properties"]


def test_a_conversation_past_the_part_limit_says_so(published, monkeypatch):
    monkeypatch.setattr(pcc_observers, "_MAX_CONVERSATION_PARTS", 1)
    conversation = [{"role": "user", "content": "long " * 5000}]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    assert [record["event_properties"]["omitted"] for record in published] == [True]
