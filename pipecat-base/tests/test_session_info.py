"""Each session reports the runtime it runs on.

A ``session_info`` record names the pipecat-ai and Python versions, the image
version and the machine architecture, so the versions in use can be counted
over time. Pinned here:

* It carries those four fields and nothing else, and is attributed to the
  session that ran.
* A SmallWebRTC session reports once: its first request only waits for the
  connection, and the bot runs from the offer's own call.
* It is sent in the background, so a publisher that never answers does not
  hold up the bot.
* It is published from app.py, so a pipecat-ai too old for the observers
  still reports.
* The session's start log line carries the same fields.
"""

import asyncio
import importlib.metadata
import platform
import sys
import types
from functools import wraps
from types import SimpleNamespace

import pcc_events
import pcc_pipecat_compat
import pytest
from feature_manager import FeatureKeys
from loguru import logger

# app.py does `from bot import bot` at import time; the real module is supplied
# by the customer's image.
if "bot" not in sys.modules:
    _stub = types.ModuleType("bot")

    async def _noop_bot(args):
        return None

    _stub.bot = _noop_bot
    sys.modules["bot"] = _stub

import app  # noqa: E402

RUNTIME_FIELDS = {"pipecat_version", "python_version", "image_version", "arch"}


def sync(fn):
    """Run a coroutine test body. This repo has no pytest-asyncio plugin."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class _Response:
    status = 200

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
    """Stands in for the aiohttp session, recording what was posted."""

    closed = False

    def __init__(self, response=_Response):
        self.posts = []
        self._response = response

    def post(self, url, json=None):
        self.posts.append(json)
        return self._response()


def _publish_to(monkeypatch, session):
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_events, "_consecutive_failures", 0)
    monkeypatch.setattr(pcc_events, "_counting_for_session", None)


@pytest.fixture
def posted(monkeypatch):
    session = _Session()
    _publish_to(monkeypatch, session)
    return session.posts


def _args(session_id="sess-1"):
    return SimpleNamespace(session_id=session_id, body=None)


async def _run_session(args, transport_type=None):
    """Run a session, and let what it published finish sending."""
    await app.run_bot(args, transport_type)
    await asyncio.gather(*app._publishing)


def _session_infos(posts):
    return [p for p in posts if p["event_name"] == "session_info"]


@sync
async def test_a_session_reports_the_runtime_it_runs_on(posted):
    await _run_session(_args("sess-1"))

    (record,) = _session_infos(posted)
    assert record["session_id"] == "sess-1"
    properties = record["event_properties"]
    assert set(properties) == RUNTIME_FIELDS
    assert properties["pipecat_version"] == importlib.metadata.version("pipecat-ai")
    assert properties["python_version"] == platform.python_version()
    assert properties["image_version"] == app.image_version
    assert properties["arch"] == platform.machine()


@sync
async def test_each_session_reports_its_own(posted):
    await _run_session(_args("sess-1"))
    await _run_session(_args("sess-2"))

    assert [r["session_id"] for r in _session_infos(posted)] == ["sess-1", "sess-2"]


class _WaitingSessionManager:
    """The SmallWebRTC session manager, with the offer already arrived."""

    async def wait_for_webrtc(self):
        return None

    def complete_session(self):
        pass


@sync
async def test_the_request_that_waits_for_webrtc_reports_nothing(monkeypatch, posted):
    """Its session reports from the offer's call, which runs the bot."""
    monkeypatch.setitem(app.GLOBALS, "session_manager", _WaitingSessionManager())
    monkeypatch.setitem(app.GLOBALS, "pipecat_session_body", None)
    monkeypatch.setitem(app.GLOBALS, app._FLOW_CONFIG_KEY, None)
    ran = []

    async def fake_bot(args):
        ran.append(args)

    monkeypatch.setattr(app, "bot", fake_bot)
    args = pcc_pipecat_compat.build(app.PipecatSessionArguments, session_id="sess-1", body={})

    await _run_session(args, "webrtc")

    assert ran == []
    assert _session_infos(posted) == []


@sync
async def test_a_publisher_that_never_answers_does_not_hold_up_the_bot(monkeypatch):
    session = _Session(response=_Hangs)
    _publish_to(monkeypatch, session)
    ran = []

    async def fake_bot(args):
        ran.append(args)

    monkeypatch.setattr(app, "bot", fake_bot)

    try:
        await asyncio.wait_for(app.run_bot(_args()), timeout=5)
    finally:
        for task in list(app._publishing):
            task.cancel()

    assert len(ran) == 1
    assert _session_infos(session.posts)


@pytest.mark.skipif(
    app.feature_manager.is_enabled(FeatureKeys.OBSERVABILITY_OBSERVERS),
    reason="this pipecat-ai loads the observers",
)
@sync
async def test_a_pipecat_too_old_for_the_observers_still_reports(posted):
    """The versions a feature is dropped for are the oldest, which load no observers."""
    await _run_session(_args())

    assert len(_session_infos(posted)) == 1


@sync
async def test_the_start_log_line_carries_the_runtime():
    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        await _run_session(_args())
    finally:
        logger.remove(sink)

    (start,) = [line for line in lines if line.startswith("Starting bot session")]
    for field in RUNTIME_FIELDS:
        assert f'"{field}": "{app._RUNTIME_INFO[field]}"' in start
