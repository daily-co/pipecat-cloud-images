"""Each session reports that it ended.

A ``session_end`` record is published when the session's bot returns, so a
reader can tell a session that ended from one still running. Pinned here:

* It is the session's last record, attributed to the session that ran.
* A bot that raises, or a session cancelled while its bot runs, still ends.
* A publisher that never answers holds the pod for a moment at most.
* A cancellation while the record is sent still frees the session.
* A session that gave up publishing, and the SmallWebRTC request that only
  waits for the connection, publish no ``session_end``.
"""

import asyncio
import sys
import types
from functools import wraps
from types import SimpleNamespace

import pcc_events
import pcc_pipecat_compat
import pytest

# app.py does `from bot import bot` at import time; the real module is supplied
# by the customer's image.
if "bot" not in sys.modules:
    _stub = types.ModuleType("bot")

    async def _noop_bot(args):
        return None

    _stub.bot = _noop_bot
    sys.modules["bot"] = _stub

import app  # noqa: E402


def sync(fn):
    """Run a coroutine test body. This repo has no pytest-asyncio plugin."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class _Response:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Hangs:
    """A response that never arrives, like a publisher with no route to it."""

    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Stands in for the aiohttp session, recording what was posted.

    Every post is answered with `status`, or never answered if `hangs`.
    """

    closed = False

    def __init__(self, status=202, hangs=False):
        self.posts = []
        self._status = status
        self._hangs = hangs

    def post(self, url, json=None):
        self.posts.append(json)
        return _Hangs() if self._hangs else _Response(self._status)


def _publish_to(monkeypatch, session):
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_events, "_consecutive_failures", {})
    return session.posts


@pytest.fixture
def posted(monkeypatch):
    return _publish_to(monkeypatch, _Session())


def _args(session_id="sess-1"):
    return SimpleNamespace(session_id=session_id, body=None)


async def _run_session(args, transport_type=None):
    """Run a session, and let what it published finish sending."""
    await app.run_bot(args, transport_type)
    await asyncio.gather(*app._publishing)


def _ends(posts):
    return [p for p in posts if p["event_name"] == "session_end"]


@sync
async def test_a_session_ends_with_its_last_record(monkeypatch, posted):
    async def fake_bot(args):
        await pcc_events.publish_event("speech_event", {"n": 1})

    monkeypatch.setattr(app, "bot", fake_bot)

    await _run_session(_args("sess-1"))

    (end,) = _ends(posted)
    assert end["session_id"] == "sess-1"
    assert "event_properties" not in end
    assert posted[-1] is end


@sync
async def test_a_bot_that_raises_still_ends(monkeypatch, posted):
    async def failing_bot(args):
        raise RuntimeError("transport closed")

    monkeypatch.setattr(app, "bot", failing_bot)

    await _run_session(_args())

    assert len(_ends(posted)) == 1


@sync
async def test_a_session_cancelled_while_its_bot_runs_still_ends(monkeypatch, posted):
    started = asyncio.Event()

    async def long_bot(args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "bot", long_bot)

    session = asyncio.create_task(app.run_bot(_args()))
    await started.wait()
    session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session

    assert len(_ends(posted)) == 1


@sync
async def test_a_publisher_that_never_answers_holds_the_pod_a_moment_at_most(monkeypatch):
    posted = _publish_to(monkeypatch, _Session(hangs=True))
    monkeypatch.setattr(app, "_SESSION_END_TIMEOUT_SECONDS", 0.05)

    try:
        await asyncio.wait_for(app.run_bot(_args()), timeout=1)
    finally:
        for task in list(app._publishing):
            task.cancel()

    assert len(_ends(posted)) == 1


@sync
async def test_a_session_that_gave_up_publishing_publishes_no_end(monkeypatch):
    posted = _publish_to(monkeypatch, _Session(status=500))

    async def fake_bot(args):
        for _ in range(pcc_events._MAX_CONSECUTIVE_FAILURES):
            await pcc_events.publish_event("speech_event", {"n": 1})

    monkeypatch.setattr(app, "bot", fake_bot)

    await _run_session(_args())

    assert _ends(posted) == []


class _WaitingSessionManager:
    """A SmallWebRTC session manager whose connection arrives at once."""

    async def wait_for_webrtc(self):
        return None

    def complete_session(self):
        pass


@sync
async def test_the_request_that_waits_for_webrtc_publishes_no_end(monkeypatch, posted):
    """Its session ends from the offer's call, which runs the bot."""
    monkeypatch.setitem(app.GLOBALS, "session_manager", _WaitingSessionManager())
    monkeypatch.setitem(app.GLOBALS, "pipecat_session_body", None)
    monkeypatch.setitem(app.GLOBALS, app._FLOW_CONFIG_KEY, None)
    args = pcc_pipecat_compat.build(app.PipecatSessionArguments, session_id="sess-1", body={})

    await _run_session(args, "webrtc")

    assert _ends(posted) == []


class _RecordingSessionManager:
    """A session manager that records whether the session was completed."""

    def __init__(self):
        self.completed = False

    def complete_session(self):
        self.completed = True


@sync
async def test_a_cancellation_during_the_end_still_frees_the_session(monkeypatch):
    """What one session stashed must not be handed to the pod's next one."""
    posted = _publish_to(monkeypatch, _Session(hangs=True))
    manager = _RecordingSessionManager()
    monkeypatch.setitem(app.GLOBALS, "session_manager", manager)
    monkeypatch.setitem(app.GLOBALS, "pipecat_session_body", {"stale": True})
    monkeypatch.setitem(app.GLOBALS, app._FLOW_CONFIG_KEY, {"stale": True})

    session = asyncio.create_task(app.run_bot(_args()))
    try:
        while not _ends(posted):
            await asyncio.sleep(0)
        session.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session
    finally:
        for task in list(app._publishing):
            task.cancel()

    assert manager.completed
    assert app.GLOBALS["pipecat_session_body"] is None
    assert app.GLOBALS[app._FLOW_CONFIG_KEY] is None
