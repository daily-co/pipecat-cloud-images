"""A finished session's memory is handed back before the pod takes another.

Pinned here:

* The release frees reference cycles, which reference counting never does, and
  then asks glibc to trim (malloc_trim(0)); without glibc it still collects.
* run_bot() releases after bot() returns and before it returns itself, so the
  session that just ended is what gets freed (including when bot() raised) and
  the pod's next session cannot start first and skip it. It yields event-loop
  turns first: teardown leaves the session referenced until the loop has run
  the cancelled tasks' callbacks.
* It is skipped while another session is running in the process, whose audio
  would stall for the collection's pause.
* PCC_RELEASE_SESSION_MEMORY=false turns it off.

Automatic collection is disabled inside each test, so only the release can be
what freed a cycle.
"""

import asyncio
import gc
import sys
import types
import weakref
from functools import wraps
from types import SimpleNamespace

import pcc_memory
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


class _Session:
    """Stands in for a finished pipeline: an object that only a cycle keeps alive."""


def _cyclic_session() -> weakref.ref:
    session = _Session()
    session.self_ref = session
    return weakref.ref(session)


@pytest.fixture(autouse=True)
def _no_automatic_gc(monkeypatch):
    monkeypatch.setattr(pcc_memory, "ENABLED", True)
    app._active_sessions.clear()
    gc.collect()
    gc.disable()
    yield
    gc.enable()
    gc.collect()
    app._active_sessions.clear()


class TestReleaseSessionMemory:
    def test_frees_reference_cycles(self):
        session = _cyclic_session()
        assert session() is not None
        pcc_memory.release_session_memory()
        assert session() is None

    def test_trims_with_zero_padding(self, monkeypatch):
        calls = []
        monkeypatch.setattr(pcc_memory, "_malloc_trim", lambda pad: calls.append(pad) or 1)
        pcc_memory.release_session_memory()
        assert calls == [0]

    def test_still_collects_without_malloc_trim(self, monkeypatch):
        monkeypatch.setattr(pcc_memory, "_malloc_trim", None)
        session = _cyclic_session()
        pcc_memory.release_session_memory()
        assert session() is None

    def test_does_nothing_when_turned_off(self, monkeypatch):
        monkeypatch.setattr(pcc_memory, "ENABLED", False)
        trims = []
        monkeypatch.setattr(pcc_memory, "_malloc_trim", lambda pad: trims.append(pad) or 1)
        session = _cyclic_session()
        pcc_memory.release_session_memory()
        assert session() is not None
        assert trims == []


class TestEnabledSetting:
    def test_on_when_unset(self, monkeypatch):
        monkeypatch.delenv(pcc_memory.ENABLED_ENV, raising=False)
        assert pcc_memory._read_enabled() is True

    @pytest.mark.parametrize(
        "value, expected", [("true", True), ("false", False), ("False", False)]
    )
    def test_true_or_false(self, monkeypatch, value, expected):
        monkeypatch.setenv(pcc_memory.ENABLED_ENV, value)
        assert pcc_memory._read_enabled() is expected

    def test_other_values_log_an_error_and_stay_on(self, monkeypatch):
        errors = []
        monkeypatch.setattr(pcc_memory.logger, "error", errors.append)
        monkeypatch.setenv(pcc_memory.ENABLED_ENV, "off")
        assert pcc_memory._read_enabled() is True
        assert len(errors) == 1 and pcc_memory.ENABLED_ENV in errors[0]


def _args(session_id="sess-1"):
    return SimpleNamespace(session_id=session_id, body=None)


class TestRunBotReleasesMemory:
    @sync
    async def test_frees_the_session_that_just_ended(self, monkeypatch):
        """End to end: nothing in run_bot() still holds the finished session."""
        sessions = []

        async def fake_bot(args):
            session = _Session()
            session.self_ref = session
            sessions.append(weakref.ref(session))

        monkeypatch.setattr(app, "bot", fake_bot)
        await app.run_bot(_args())
        assert sessions and sessions[0]() is None

    @sync
    async def test_waits_for_the_loop_to_let_go_of_the_session(self, monkeypatch):
        """Like Pipecat's teardown, this bot's session stays referenced for a
        couple of loop turns after bot() returns; the release must come after."""
        sessions = []
        held = []

        async def fake_bot(args):
            session = _Session()
            session.self_ref = session
            sessions.append(weakref.ref(session))
            held.append(session)
            loop = asyncio.get_running_loop()
            loop.call_soon(loop.call_soon, held.clear)

        monkeypatch.setattr(app, "bot", fake_bot)
        await app.run_bot(_args())
        assert sessions[0]() is None

    @sync
    async def test_releases_after_bot_returns(self, monkeypatch):
        events = []

        async def fake_bot(args):
            events.append("bot")

        monkeypatch.setattr(app, "bot", fake_bot)
        monkeypatch.setattr(
            app.pcc_memory,
            "release_session_memory",
            lambda: events.append(("release", dict(app._active_sessions))),
        )
        await app.run_bot(_args())
        # The session has left _active_sessions by the time memory is released.
        assert events == ["bot", ("release", {})]

    @sync
    async def test_releases_when_bot_raises(self, monkeypatch):
        released = []

        async def failing_bot(args):
            raise RuntimeError("boom")

        monkeypatch.setattr(app, "bot", failing_bot)
        monkeypatch.setattr(app.pcc_memory, "release_session_memory", lambda: released.append(True))
        await app.run_bot(_args())
        assert released == [True]

    @sync
    async def test_skipped_while_another_session_runs(self, monkeypatch):
        released = []

        async def fake_bot(args):
            return None

        monkeypatch.setattr(app, "bot", fake_bot)
        monkeypatch.setattr(app.pcc_memory, "release_session_memory", lambda: released.append(True))
        app._active_sessions["other-session"] = object()
        await app.run_bot(_args("sess-2"))
        assert released == []

    @sync
    async def test_turned_off_does_nothing(self, monkeypatch):
        released = []

        async def fake_bot(args):
            return None

        monkeypatch.setattr(app, "bot", fake_bot)
        monkeypatch.setattr(pcc_memory, "ENABLED", False)
        monkeypatch.setattr(app.pcc_memory, "release_session_memory", lambda: released.append(True))
        await app.run_bot(_args())
        assert released == []
