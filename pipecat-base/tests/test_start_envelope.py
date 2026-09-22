"""A session can name the Pipecat Flows config it runs.

Pipecat Cloud sends such a session's ``/bot`` request as an envelope,
``{"body": ..., "flow_config": ...}``, marked by ``X-Pcc-Start-Envelope``. A
session that names no flow sends its body alone, which is every request this
image saw before flows could be named.

The two properties pinned here:

* **The marker decides, not the shape of the body.** A session body that itself
  holds ``body`` and ``flow_config`` keys is a plausible thing for a caller to
  send, and unwrapping it on shape would hand the bot a fragment of their own
  data.
* **The flow survives the SmallWebRTC detour.** There the bot runs from a later
  ``/api/offer`` request that never saw the ``/bot`` headers, so the flow waits
  in the same place the session body does.
"""

import asyncio
import sys
import types
from functools import wraps
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


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

FLOW = "initial_node: greet\nnodes:\n  greet:\n    task_messages: []\n"


@pytest.fixture(autouse=True)
def _clean_globals():
    # The session body is stashed in the same place as the flow, so a test that
    # leaves one behind would seed the next one.
    keys = (app._BUDGET_KEY, app._FLOW_CONFIG_KEY, "pipecat_session_body")
    for key in keys:
        app.GLOBALS.pop(key, None)
    yield
    for key in keys:
        app.GLOBALS.pop(key, None)


@pytest.fixture
def captured(monkeypatch):
    """Capture the session arguments the handler hands to run_bot."""
    seen = {}

    async def fake_run_bot(args, transport_type=None):
        seen["args"] = args
        seen["transport_type"] = transport_type

    monkeypatch.setattr(app, "run_bot", fake_run_bot)
    return seen


class TestSplitStartEnvelope:
    """Separating the session body from the flow."""

    def test_an_unmarked_body_is_the_session_body(self):
        body = {"user": "alice"}
        assert app._split_start_envelope(body, None) == (body, None)

    def test_an_unmarked_body_shaped_like_an_envelope_is_left_alone(self):
        lookalike = {"body": {"real": "payload"}, "flow_config": "nope"}
        assert app._split_start_envelope(lookalike, None) == (lookalike, None)

    def test_a_marked_body_splits(self):
        envelope = {"body": {"user": "alice"}, "flow_config": FLOW}
        assert app._split_start_envelope(envelope, "1") == ({"user": "alice"}, FLOW)

    @pytest.mark.parametrize(
        "body",
        [
            {"flow_config": FLOW},
            {"body": {"a": 1}},
            {},
        ],
    )
    def test_a_marked_body_missing_a_half_is_refused(self, body):
        with pytest.raises(HTTPException) as excinfo:
            app._split_start_envelope(body, "1")
        assert excinfo.value.status_code == 400

    def test_an_unknown_envelope_version_is_refused(self):
        envelope = {"body": {}, "flow_config": FLOW}
        with pytest.raises(HTTPException) as excinfo:
            app._split_start_envelope(envelope, "99")
        assert excinfo.value.status_code == 400

    @pytest.mark.parametrize("flow_config", [None, "", " \n\t", 42, {"initial_node": "greet"}])
    def test_a_flow_that_is_not_text_is_refused(self, flow_config):
        # Any of these would be dropped on the way to the session arguments and
        # the bot would run the flow its image ships with, which is what the
        # refusal exists to prevent. Pipecat Cloud cannot send one today, so
        # this is the image holding the contract on its own.
        with pytest.raises(HTTPException) as excinfo:
            app._split_start_envelope({"body": {}, "flow_config": flow_config}, "1")
        assert excinfo.value.status_code == 400


class TestBotRequest:
    """What the handler hands the bot."""

    @sync
    async def test_a_session_naming_no_flow_is_unchanged(self, captured):
        await app.handle_bot_request(
            body={"user": "alice"},
            x_daily_session_id="sess-1241",
        )
        args = captured["args"]
        assert args.body == {"user": "alice"}
        assert getattr(args, "flow_config", None) is None

    @sync
    async def test_a_named_flow_reaches_the_session_arguments(self, captured):
        await app.handle_bot_request(
            body={"body": {"user": "alice"}, "flow_config": FLOW},
            x_daily_session_id="sess-1241",
            x_pcc_start_envelope="1",
        )
        args = captured["args"]
        # The bot gets its own body, not the envelope around it.
        assert args.body == {"user": "alice"}
        assert args.flow_config == FLOW

    @sync
    async def test_a_daily_session_carries_the_flow_too(self, captured):
        await app.handle_bot_request(
            body={"body": {}, "flow_config": FLOW},
            x_daily_room_url="https://example.daily.co/room",
            x_daily_room_token="token",
            x_daily_session_id="sess-1241",
            x_pcc_start_envelope="1",
        )
        args = captured["args"]
        assert args.room_url == "https://example.daily.co/room"
        assert args.flow_config == FLOW

    @sync
    async def test_a_malformed_envelope_never_reaches_the_bot(self, captured):
        with pytest.raises(HTTPException):
            await app.handle_bot_request(
                body={"user": "alice"},
                x_daily_session_id="sess-1241",
                x_pcc_start_envelope="1",
            )
        assert "args" not in captured


class TestAttachFlowConfig:
    """Why the flow is assigned rather than passed to the constructor."""

    def test_assigning_works_without_the_field_on_the_class(self):
        # Pipecat's RunnerArguments only carries flow_config from the release
        # that added it. A constructor keyword would fail outright on an image
        # pinned to an older pipecat-ai; assigning afterwards works on both.
        args = SimpleNamespace(session_id="s", body=None)
        app._attach_flow_config(args, FLOW)
        assert args.flow_config == FLOW

    def test_the_attribute_is_readable_even_when_no_flow_was_named(self):
        # A bot reads runner_args.flow_config whether or not its session named
        # one, and the pipecat-ai it is built against may predate the field.
        # Leaving the attribute off would make that read raise on an older one.
        args = SimpleNamespace(session_id="s", body=None)
        app._attach_flow_config(args, None)
        assert args.flow_config is None


class TestSmallWebRTCDetour:
    """The bot runs from a later request that never saw the /bot headers."""

    @sync
    async def test_the_flow_waits_with_the_session_body(self, monkeypatch):
        # The /bot request only waits for the connection, so it stashes both.
        class _Manager:
            async def wait_for_webrtc(self):
                return None

            def cancel_timeout(self):
                return None

            def complete_session(self):
                return None

        monkeypatch.setitem(app.GLOBALS, "session_manager", _Manager())
        args = app.PipecatSessionArguments(session_id="sess-1241", body={"u": 1})
        app._attach_flow_config(args, FLOW)

        await app.run_bot(args, "webrtc")

        assert app.GLOBALS["pipecat_session_body"] == {"u": 1}
        assert app.GLOBALS[app._FLOW_CONFIG_KEY] == FLOW

    @sync
    async def test_the_offer_side_collects_the_flow_and_clears_it(self, monkeypatch):
        from pipecatcloud.agent import SmallWebRTCSessionArguments

        class _Manager:
            def cancel_timeout(self):
                return None

            def complete_session(self):
                return None

        captured = {}

        async def fake_run_with_budget(args):
            captured["args"] = args

        # app only binds this name when the SmallWebRTC feature is enabled; the
        # branch under test is the same either way.
        monkeypatch.setattr(
            app, "SmallWebRTCSessionArguments", SmallWebRTCSessionArguments, raising=False
        )
        monkeypatch.setattr(app, "_run_bot_with_budget", fake_run_with_budget)
        monkeypatch.setitem(app.GLOBALS, "session_manager", _Manager())
        monkeypatch.setitem(app.GLOBALS, "pipecat_session_body", {"u": 1})
        monkeypatch.setitem(app.GLOBALS, app._FLOW_CONFIG_KEY, FLOW)

        args = SmallWebRTCSessionArguments(
            session_id="sess-1241", webrtc_connection=None, body=None
        )
        await app.run_bot(args)

        assert captured["args"].flow_config == FLOW
        assert captured["args"].body == {"u": 1}
        # Cleared when the session ends, so the next session on this pod starts
        # with neither.
        assert app.GLOBALS[app._FLOW_CONFIG_KEY] is None
        assert app.GLOBALS["pipecat_session_body"] is None

    @sync
    async def test_a_connection_that_never_arrives_leaves_nothing_behind(self, monkeypatch):
        # Without this the stash outlives the failed session, and the next one
        # to run on the pod picks it up — including a session that builds its
        # own arguments and never had a /bot request of its own.
        class _Manager:
            async def wait_for_webrtc(self):
                raise TimeoutError("no offer arrived")

            def complete_session(self):
                return None

        monkeypatch.setitem(app.GLOBALS, "session_manager", _Manager())
        args = app.PipecatSessionArguments(session_id="sess-1241", body={"u": 1})
        app._attach_flow_config(args, FLOW)

        with pytest.raises(TimeoutError):
            await app.run_bot(args, "webrtc")

        assert app.GLOBALS[app._FLOW_CONFIG_KEY] is None
        assert app.GLOBALS["pipecat_session_body"] is None
