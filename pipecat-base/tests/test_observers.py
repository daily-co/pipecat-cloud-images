"""How Pipecat finds the hooks in the observers' setup file, and how a conversation publishes.

From 1.3.0, Pipecat offers every PIPECAT_SETUP_FILES entry to the runner as
well as to each worker, and warns about a file without the hook it looks for.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

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


# What the publisher allows a record's properties, in the publisher's own terms.
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
        assert properties["v"] == 1
        texts.setdefault(properties["conversation_id"], []).append(properties)
    conversations = []
    for parts in texts.values():
        assert [part["part"] for part in parts] == list(range(parts[0]["parts"]))
        conversations.append(json.loads("".join(part["text"] for part in parts)))
    return conversations


@pytest.mark.parametrize(
    ("platform", "developer", "excluded"),
    [
        ("false", None, False),
        ("false", "", False),
        ("false", "false", False),
        ("false", " False ", False),
        ("false", "true", True),
        ("false", " TRUE ", True),
        ("false", "1", True),
        ("false", "yes", True),
        ("FALSE", None, False),
        (" false ", None, False),
        ("true", None, True),
        ("True", None, True),
        ("1", None, True),
        ("anything else", None, True),
        ("true", "false", True),
        ("", None, True),
        ("", "false", True),
    ],
)
def test_a_developer_can_only_make_excluding_content_stricter(
    monkeypatch, platform, developer, excluded
):
    monkeypatch.setattr(pcc_structured_logs, "_platform_exclude_content", platform)
    if developer is None:
        monkeypatch.delenv("PCC_WITHHOLD_CONTENT", raising=False)
    else:
        monkeypatch.setenv("PCC_WITHHOLD_CONTENT", developer)

    assert pcc_observers._content_excluded() is excluded


def test_a_bots_dotenv_cannot_loosen_the_platforms_setting():
    code = (
        "import os, pcc_structured_logs;"
        "os.environ['PCC_EXCLUDE_CONTENT'] = 'false';"
        "import pcc_observers;"
        "print(pcc_observers._exclude_content, pcc_observers._conversation_opted_in())"
    )
    base = Path(pcc_observers.__file__).parent
    env = {**os.environ, "PCC_EXCLUDE_CONTENT": "true", "PCC_EVAL_CONVERSATIONS": "true"}
    env.pop("PCC_WITHHOLD_CONTENT", None)

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=base, env=env, capture_output=True, text=True
    )

    assert result.stdout.split()[-2:] == ["True", "False"], result.stderr


def test_an_agent_the_platform_told_nothing_withholds():
    code = "import pcc_observers; print(pcc_observers._exclude_content)"
    base = Path(pcc_observers.__file__).parent
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in ("PCC_EXCLUDE_CONTENT", "PCC_WITHHOLD_CONTENT")
    }

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=base, env=env, capture_output=True, text=True
    )

    assert result.stdout.split()[-1:] == ["True"], result.stderr


def test_the_platforms_setting_is_read_before_the_bot_is_imported():
    source = (Path(pcc_observers.__file__).parent / "app.py").read_text()

    assert source.index("import pcc_structured_logs") < source.index("import bot as bot_module")


def test_a_record_names_its_own_session_when_sessions_overlap(published):
    async def run():
        b_started = asyncio.Event()
        a_finished = asyncio.Event()

        async def session_a():
            with pcc_structured_logs.session_scope("session-a"):
                await pcc_events.publish_event("before", {"n": 1})
                await b_started.wait()
                await pcc_events.publish_event("during", {"n": 2})
            a_finished.set()

        async def session_b():
            with pcc_structured_logs.session_scope("session-b"):
                b_started.set()
                await a_finished.wait()
                # Past the linger, in case the older session's exit cleared the slot.
                await asyncio.sleep(pcc_structured_logs._SESSION_LINGER_SECONDS + 0.1)
                await pcc_events.publish_event("after", {"n": 3})
                assert pcc_structured_logs.current_session() == "session-b"
                assert pcc_structured_logs._current_session_id == "session-b"

        await asyncio.gather(session_a(), session_b())

    asyncio.run(run())

    assert [(record["event_name"], record["session_id"]) for record in published] == [
        ("before", "session-a"),
        ("during", "session-a"),
        ("after", "session-b"),
    ]


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


def test_an_older_pipecat_says_so_for_every_worker_so_each_sessions_logs_explain(
    published, monkeypatch
):
    from loguru import logger

    monkeypatch.setitem(sys.modules, "pipecat.evals.conversation_observer", None)
    logged = []
    sink = logger.add(logged.append, level="INFO", format="{message}")
    try:
        _attached_observers()
        _attached_observers()
    finally:
        logger.remove(sink)

    assert sum("needs pipecat-ai 1.13" in line for line in logged) == 2


@pytest.mark.parametrize(
    "said",
    [
        "plain words " * 5000,
        '"quotes" and \\backslashes\\ ' * 5000,
        "日本語のテキストです。" * 5000,
        # The worst case: every character escaped again when a part is published.
        '"' * 20000,
        "\\" * 20000,
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


def test_a_reader_can_survive_a_duplicate_part_and_detect_a_missing_one(published):
    conversation = [{"role": "user", "content": "words " * 5000}]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    records = [record["event_properties"] for record in published]
    assert len(records) > 2
    assert len({record["sha256"] for record in records}) == 1

    def read(delivered):
        by_part = {record["part"]: record["text"] for record in delivered}
        text = "".join(by_part[part] for part in sorted(by_part))
        return text if hashlib.sha256(text.encode()).hexdigest() == records[0]["sha256"] else None

    assert json.loads(read(records + records[:1])) == conversation
    assert read(records[1:]) is None


def test_only_the_roles_and_fields_named_here_are_published(published):
    conversation = [
        {"role": "user", "content": "Hi", "timestamp": "2026-11-04T12:00:00+00:00"},
        {"role": "tool_result", "name": "note", "content": "a result"},
        {
            "role": "tool_call",
            "name": "note",
            "arguments": {"text": "Hi"},
            "cancelled": False,
            "result": {"saved": True},
        },
        {"role": "assistant", "content": "Done", "audio_url": "https://example.com/a.wav"},
    ]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    assert _conversations(published) == [
        [
            {"role": "user", "content": "Hi"},
            {"role": "tool_call", "name": "note", "arguments": {"text": "Hi"}, "cancelled": False},
            {"role": "assistant", "content": "Done"},
        ]
    ]


def test_a_conversation_of_only_unknown_roles_publishes_nothing(published):
    asyncio.run(pcc_observers._publish_conversation([{"role": "system", "content": "Be nice"}]))

    assert published == []


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
    assert published[0]["event_properties"]["v"] == 1


def test_a_conversation_past_the_part_limit_says_so(published, monkeypatch):
    monkeypatch.setattr(pcc_observers, "_MAX_CONVERSATION_PARTS", 1)
    conversation = [{"role": "user", "content": "long " * 5000}]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    assert [record["event_properties"]["omitted"] for record in published] == [True]


@pytest.mark.parametrize(("extra_chars", "parts"), [(0, 2), (1, 0)])
def test_a_conversation_exactly_at_the_part_limit_still_travels(
    published, monkeypatch, extra_chars, parts
):
    monkeypatch.setattr(pcc_observers, "_MAX_CONVERSATION_PARTS", 2)
    limit = 2 * pcc_observers._CONVERSATION_PART_CHARS
    structure = len(json.dumps([{"role": "user", "content": ""}]))
    conversation = [{"role": "user", "content": "x" * (limit - structure + extra_chars)}]

    asyncio.run(pcc_observers._publish_conversation(conversation))

    assert {record["event_properties"]["parts"] for record in published} == {parts}


class _SlowSession(_Session):
    """Takes `answers_after` seconds to answer each record, then accepts it."""

    def __init__(self, answers_after):
        super().__init__()
        self.answers_after = answers_after

    def post(self, url, json=None):
        self.records.append(json)
        answers_after = self.answers_after

        class _SlowResponse(_Response):
            async def __aenter__(self):
                await asyncio.sleep(answers_after)
                return self

        return _SlowResponse()


# Long enough that a publish can't finish inside the shortened limit below.
_LONG = [{"role": "user", "content": "x" * pcc_observers._CONVERSATION_PART_CHARS * 100}]


def _publish_through(monkeypatch, session):
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")
    monkeypatch.setattr(pcc_observers, "_CONVERSATION_PUBLISH_SECS", 0.05)


@pytest.mark.parametrize(
    "answers_after",
    # The second never fails, so the failure cutoff can't be what stops it.
    [3600, 0.01],
    ids=["stops answering", "slow but answers"],
)
def test_publishing_stops_at_the_time_limit(monkeypatch, answers_after):
    session = _SlowSession(answers_after)
    _publish_through(monkeypatch, session)

    # A guard, so a publish with no limit fails this test rather than hanging the run.
    asyncio.run(asyncio.wait_for(pcc_observers._publish_conversation(_LONG), timeout=5))

    assert 0 < len(session.records) < 100


class _SwallowsOneCancelSession(_Session):
    """Like aiohttp when its own timeout falls due with the limit: the cancel becomes a TimeoutError."""

    def post(self, url, json=None):
        self.records.append(json)
        first = len(self.records) == 1

        class _Answer(_Response):
            async def __aenter__(self):
                if first:
                    try:
                        await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        asyncio.current_task().uncancel()
                        raise TimeoutError from None
                return self

        return _Answer()


def test_publishing_stops_even_when_the_http_client_swallowed_the_limits_cancel(monkeypatch):
    session = _SwallowsOneCancelSession()
    _publish_through(monkeypatch, session)

    asyncio.run(asyncio.wait_for(pcc_observers._publish_conversation(_LONG), timeout=5))

    assert len(session.records) == 1


def test_a_cancelled_publish_is_not_the_limit_or_a_failure(monkeypatch):
    session = _SlowSession(3600)
    _publish_through(monkeypatch, session)
    monkeypatch.setattr(pcc_observers, "_CONVERSATION_PUBLISH_SECS", 60)
    monkeypatch.setattr(pcc_events, "_consecutive_failures", {})

    async def run():
        task = asyncio.create_task(pcc_observers._publish_conversation(_LONG))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert pcc_events._consecutive_failures == {"session-abc": 0}
    session.answers_after = 0
    posted = len(session.records)
    asyncio.run(pcc_events.publish_event("error", {"category": "connectivity"}))
    assert len(session.records) == posted + 1
