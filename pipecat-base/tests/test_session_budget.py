"""PCC-1066: the platform's session budget is enforced inside this process.

Pipecat Cloud caps sessions at ``maxSessionDuration``, but the only thing it can
do from outside is close its own request to us. When a session is started by an
HTTP request the bot goes on to connect its own media transport, so that close
is invisible to ``bot()`` and the session carries on — which is how a pod ended
up running two live pipelines at once. The platform now sends its budget on the
``/bot`` request and we stop the bot ourselves.

Two behaviours here are easy to get wrong and are pinned deliberately:

* A Pipecat bot **absorbs** the cancellation (WorkerRunner catches it and
  unwinds the pipeline normally), so nothing is raised. Detection based only on
  catching an exception misses the common case.
* Cancellation we did **not** ask for — this process being torn down — must
  still propagate.
"""

import asyncio
import sys
import types
from functools import wraps
from types import SimpleNamespace

import pytest


def sync(fn):
    """Run a coroutine test body. This repo has no pytest-asyncio plugin."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# app.py does `from bot import bot` at import time; the real module is supplied
# by the customer's image.
if "bot" not in sys.modules:
    _stub = types.ModuleType("bot")

    async def _noop_bot(args):
        return None

    _stub.bot = _noop_bot
    sys.modules["bot"] = _stub

import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_budget():
    app.GLOBALS.pop(app._BUDGET_KEY, None)
    app._active_sessions.clear()
    yield
    app.GLOBALS.pop(app._BUDGET_KEY, None)
    app._active_sessions.clear()


def _args(session_id="sess-1"):
    return SimpleNamespace(session_id=session_id, body=None)


class TestBudgetEnforcement:
    @sync
    async def test_absorbing_bot_is_stopped_and_returns_cleanly(self, monkeypatch):
        """The Pipecat shape: the runner swallows the cancel and tears down."""
        saw_cancel = asyncio.Event()

        async def fake_bot(args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                saw_cancel.set()
                # Graceful teardown, then return normally — no re-raise.
            await asyncio.sleep(0)

        monkeypatch.setattr(app, "bot", fake_bot)
        app.GLOBALS[app._BUDGET_KEY] = 0.05

        # Must not raise: the caller still has an HTTP response to send, and
        # that response is what tells the platform this pod is free again.
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(app._run_bot_with_budget(_args()), timeout=2.0)
        elapsed = asyncio.get_running_loop().time() - started

        assert saw_cancel.is_set()
        # Timed, because an absorbing bot makes wait_for return normally rather
        # than raise: without this the test also passes when the budget is not
        # enforced at all and the outer wait_for is what stops the bot.
        assert elapsed < 1.0, f"budget was not enforced (took {elapsed:.2f}s)"

    @sync
    async def test_propagating_bot_is_stopped_and_returns_cleanly(self, monkeypatch):
        """The plain-asyncio shape: CancelledError comes back out of bot()."""

        async def fake_bot(args):
            await asyncio.Event().wait()

        monkeypatch.setattr(app, "bot", fake_bot)
        app.GLOBALS[app._BUDGET_KEY] = 0.05

        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(app._run_bot_with_budget(_args()), timeout=2.0)
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1.0, f"budget was not enforced (took {elapsed:.2f}s)"

    @sync
    async def test_bot_within_budget_is_left_alone(self, monkeypatch):
        finished = asyncio.Event()

        async def fake_bot(args):
            await asyncio.sleep(0.01)
            finished.set()

        monkeypatch.setattr(app, "bot", fake_bot)
        app.GLOBALS[app._BUDGET_KEY] = 5

        await asyncio.wait_for(app._run_bot_with_budget(_args()), timeout=2.0)
        assert finished.is_set()

    @sync
    async def test_no_budget_means_no_enforcement(self, monkeypatch):
        """Absent header → nothing enforced. Never guess a default.

        Only the platform knows the configured value; a hardcoded default would
        cut sessions short for anyone configured above it (the cap goes up to
        4h). Without the header the session simply runs on, which is safe
        because the platform keeps the pod marked occupied.
        """
        started = asyncio.Event()

        async def fake_bot(args):
            started.set()
            await asyncio.sleep(0.2)

        monkeypatch.setattr(app, "bot", fake_bot)
        # No _BUDGET_KEY set at all.

        run = asyncio.create_task(app._run_bot_with_budget(_args()))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0.1)
        assert not run.done(), "bot must not be stopped when no budget was supplied"
        await asyncio.wait_for(run, timeout=2.0)


class TestTeardownIsNotSwallowed:
    @sync
    async def test_outer_cancellation_still_propagates(self, monkeypatch):
        """Only the cancellation we asked for is absorbed.

        If this process is being torn down, the caller must see it rather than
        us reporting a tidy completion.
        """

        async def fake_bot(args):
            await asyncio.Event().wait()

        monkeypatch.setattr(app, "bot", fake_bot)
        app.GLOBALS[app._BUDGET_KEY] = 30  # long enough not to interfere

        run = asyncio.create_task(app._run_bot_with_budget(_args()))
        await asyncio.sleep(0.05)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    @sync
    async def test_teardown_during_the_unwind_still_propagates(self, monkeypatch):
        """The case the budget makes easy to miss.

        ``cancel_reason`` stays set for the rest of the session, so once the
        budget has fired it can no longer distinguish our own cancellation from
        a teardown arriving while the bot is still unwinding. Without a separate
        discriminator this returns normally and the caller never learns the
        process is going away.
        """

        async def slow_unwind_bot(args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # absorb, like Pipecat's runner
            await asyncio.sleep(0.5)  # ...then take a while to tear down

        monkeypatch.setattr(app, "bot", slow_unwind_bot)
        app.GLOBALS[app._BUDGET_KEY] = 0.05

        run = asyncio.create_task(app._run_bot_with_budget(_args()))
        await asyncio.sleep(0.15)  # budget has fired; bot is mid-unwind
        run.cancel()

        with pytest.raises(asyncio.CancelledError):
            await run


class TestCancellationPrimitive:
    """The budget is the first caller; a platform stop request would be the next."""

    @sync
    async def test_session_is_registered_while_running_and_cleared_after(self, monkeypatch):
        running = asyncio.Event()
        release = asyncio.Event()

        async def fake_bot(args):
            running.set()
            await release.wait()

        monkeypatch.setattr(app, "bot", fake_bot)

        run = asyncio.create_task(app._run_bot_with_budget(_args("sess-registered")))
        await asyncio.wait_for(running.wait(), timeout=1.0)
        assert "sess-registered" in app._active_sessions

        release.set()
        await asyncio.wait_for(run, timeout=2.0)
        assert "sess-registered" not in app._active_sessions

    @sync
    async def test_cancel_stops_the_bot_and_records_a_reason(self, monkeypatch):
        running = asyncio.Event()

        async def fake_bot(args):
            running.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(app, "bot", fake_bot)

        run = asyncio.create_task(app._run_bot_with_budget(_args("sess-cancel")))
        await asyncio.wait_for(running.wait(), timeout=1.0)

        handle = app._active_sessions["sess-cancel"]
        assert handle.cancel("stopped by request") is True
        assert handle.cancel_reason == "stopped by request"

        await asyncio.wait_for(run, timeout=2.0)

    @sync
    async def test_cancelling_a_finished_session_is_a_no_op(self, monkeypatch):
        async def fake_bot(args):
            return None

        monkeypatch.setattr(app, "bot", fake_bot)
        task = asyncio.create_task(fake_bot(None))
        await task
        handle = app._SessionRun(task)
        assert handle.cancel("too late") is False
        assert handle.cancel_reason is None


class TestBudgetHeaderParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("7200", 7200.0),
            ("0.5", 0.5),
            (None, None),
            ("", None),
            ("not-a-number", None),
            ("0", None),
            ("-1", None),
        ],
    )
    def test_parse(self, raw, expected):
        assert app._parse_budget(raw) == expected


class TestSmallWebRTCStash:
    """The budget must survive into a *different* request.

    SmallWebRTC calls run_bot twice: /bot only waits for the WebRTC connection
    and returns, while the real bot() runs from the /api/offer background task,
    which never sees these headers. Passing the budget down from the handler
    would therefore leave that path unbounded — and stopping the /bot wait
    instead of the bot would hand the pod out while the bot was still running,
    which is the bug this whole change exists to fix.
    """

    @sync
    async def test_handler_stashes_the_budget_for_later_invocations(self, monkeypatch):
        async def fake_run_bot(args, transport_type=None):
            return None

        monkeypatch.setattr(app, "run_bot", fake_run_bot)

        await app.handle_bot_request(
            body={},
            x_daily_session_id="sess-webrtc",
            x_pcc_max_session_seconds="1234",
        )
        assert app.GLOBALS[app._BUDGET_KEY] == 1234.0

    @sync
    async def test_absent_header_stashes_none(self, monkeypatch):
        async def fake_run_bot(args, transport_type=None):
            return None

        monkeypatch.setattr(app, "run_bot", fake_run_bot)

        await app.handle_bot_request(body={}, x_daily_session_id="sess-none")
        assert app.GLOBALS[app._BUDGET_KEY] is None
