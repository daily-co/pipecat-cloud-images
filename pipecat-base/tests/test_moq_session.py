"""Media over QUIC (MoQ) sessions on /bot.

A session chooses MoQ with ``X-Daily-Transport-Type: moq`` and carries its
relay URL and namespace in ``X-Moq-Relay-Url`` and ``X-Moq-Namespace``. The
image hands bot() pipecatcloud's ``MOQSessionArguments`` built from them.

Pipecat Cloud starts a MoQ session only on an image whose capability document
reports ``transport.moq`` available, so an image that cannot serve one never
gets one. If one arrives anyway, the image refuses it with a 400 before bot()
runs, for exactly the reason it reports, and serves every other session.

The relay URL carries the session's token, so it never reaches a log line or a
refusal.

Whether the arguments can be built is tested in test_pipecat_compat. The CI leg
that installs pipecat-ai's moq extra sets PCC_EXPECT_MOQ=1, so the test against
the real types there cannot pass by skipping.
"""

import asyncio
import dataclasses
import os
import sys
import types
from functools import wraps
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import pytest
from fastapi import HTTPException

# app.py does `from bot import bot` at import time; the real module is supplied
# by the customer's image.
if "bot" not in sys.modules:
    _stub = types.ModuleType("bot")

    async def _noop_bot(args):
        return None

    _stub.bot = _noop_bot
    sys.modules["bot"] = _stub

import app  # noqa: E402

TOKEN = "relay-token-7731"
RELAY_URL = f"https://relay.example.test:4443/?jwt={TOKEN}"
NAMESPACE = "pcc/sess-moq"


def sync(fn):
    """Run a coroutine test body. This repo has no pytest-asyncio plugin."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@dataclasses.dataclass
class _RunnerArguments:
    body: Any = dataclasses.field(default_factory=dict, kw_only=True)
    session_id: Optional[str] = dataclasses.field(default=None, kw_only=True)


def _moq_type(post_init_error: Optional[Exception] = None):
    """A MOQSessionArguments that checks its dial target, as pipecat-ai 1.12's does."""

    @dataclasses.dataclass
    class MOQSessionArguments(_RunnerArguments):
        relay_url: Optional[str] = dataclasses.field(default=None, kw_only=True)
        namespace: str = "pipecat"
        participant_id: str = "response"
        peer_id: str = "request"

        def __post_init__(self):
            if post_init_error is not None:
                raise post_init_error
            if self.relay_url is None:
                raise ValueError("MOQRunnerArguments needs `relay_url`")

    return MOQSessionArguments


# ------------------------------------------------------------
# What the image decides at startup, from what it reports
# ------------------------------------------------------------
class TestMoqUnavailableFrom:
    def test_reported_available_serves_moq(self):
        reported = {"transport.moq": {"available": True}}
        assert app._moq_unavailable_from(reported) is None

    def test_reported_unavailable_gives_the_reported_reason(self):
        reported = {"transport.moq": {"available": False, "reason": "No module named 'moq'"}}
        assert app._moq_unavailable_from(reported) == "No module named 'moq'"

    @pytest.mark.parametrize(
        "reported",
        [
            None,
            {},
            {"transport.daily": {"available": True}},
            {"transport.moq": {"available": False}},
        ],
        ids=["nothing reported", "an empty report", "no MoQ entry", "unavailable without a reason"],
    )
    def test_anything_else_serves_no_moq(self, reported):
        assert app._moq_unavailable_from(reported) == "this image reports no MoQ support"

    def test_what_this_image_decided_matches_what_it_reports(self):
        reported = app._reported or {}
        entry = reported.get("transport.moq", {"available": False})
        assert (app._moq_unavailable is None) == entry["available"]
        assert (app._moq_session_type is not None) == entry["available"]

    @pytest.mark.skipif(
        "PCC_EXPECT_MOQ" not in os.environ, reason="set by CI legs that know their MoQ"
    )
    def test_this_image_reports_moq_exactly_where_ci_expects(self):
        """1 on the leg that installs pipecat-ai 1.12+ with its moq extra, over
        the pipecatcloud the image locks; 0 on every other. So a check that finds
        MoQ where it is not, or misses it where it is, fails a real run."""
        expected = os.environ["PCC_EXPECT_MOQ"] == "1"
        assert (app._moq_unavailable is None) == expected, app._moq_unavailable


# ------------------------------------------------------------
# /bot
# ------------------------------------------------------------
@pytest.fixture
def captured(monkeypatch):
    seen = {}

    async def fake_run_bot(args, transport_type=None):
        seen["args"] = args
        seen["transport_type"] = transport_type

    monkeypatch.setattr(app, "run_bot", fake_run_bot)
    return seen


@pytest.fixture
def logs():
    lines = []
    sink = app.logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    app.logger.remove(sink)


@pytest.fixture(autouse=True)
def _clean_globals():
    keys = (app._BUDGET_KEY, app._FLOW_CONFIG_KEY, "pipecat_session_body")
    for key in keys:
        app.GLOBALS.pop(key, None)
    yield
    for key in keys:
        app.GLOBALS.pop(key, None)


def _serves_moq(monkeypatch, moq_type=None):
    moq_type = moq_type or _moq_type()
    monkeypatch.setattr(app, "_moq_session_type", moq_type)
    monkeypatch.setattr(app, "_moq_unavailable", None)
    return moq_type


def _serves_no_moq(monkeypatch, reason):
    monkeypatch.setattr(app, "_moq_session_type", None)
    monkeypatch.setattr(app, "_moq_unavailable", reason)


def _moq_start(**overrides):
    request = {
        "body": {"user": "alice"},
        "x_daily_session_id": "sess-moq",
        "x_daily_transport_type": "moq",
        "x_moq_relay_url": RELAY_URL,
        "x_moq_namespace": NAMESPACE,
    }
    request.update(overrides)
    return app.handle_bot_request(**request)


def _nothing_carries_the_token(logs, refusal: Optional[HTTPException] = None):
    assert not any(TOKEN in line for line in logs)
    if refusal is not None:
        assert TOKEN not in str(refusal.detail)


class TestMoqBotRequest:
    @sync
    async def test_a_moq_session_gets_moq_session_arguments(self, monkeypatch, captured, logs):
        moq_type = _serves_moq(monkeypatch)
        await _moq_start()
        args = captured["args"]
        assert type(args) is moq_type
        assert args.session_id == "sess-moq"
        assert args.relay_url == RELAY_URL
        assert args.namespace == NAMESPACE
        assert (args.participant_id, args.peer_id) == ("response", "request")
        assert args.body == {"user": "alice"}
        assert args.flow_config is None
        assert captured["transport_type"] == "moq"
        _nothing_carries_the_token(logs)

    @sync
    async def test_a_moq_session_carries_its_flow(self, monkeypatch, captured):
        _serves_moq(monkeypatch)
        flow = "initial_node: greet\nnodes:\n  greet:\n    task_messages: []\n"
        await _moq_start(
            body={"body": {"user": "alice"}, "flow_config": flow}, x_pcc_start_envelope="1"
        )
        assert captured["args"].body == {"user": "alice"}
        assert captured["args"].flow_config == flow

    @pytest.mark.parametrize(
        "missing", [{"x_moq_relay_url": None}, {"x_moq_namespace": None}, {"x_moq_relay_url": ""}]
    )
    @sync
    async def test_a_moq_start_without_its_headers_is_refused(
        self, monkeypatch, captured, logs, missing
    ):
        _serves_moq(monkeypatch)
        with pytest.raises(HTTPException) as refused:
            await _moq_start(**missing)
        assert refused.value.status_code == 400
        assert "args" not in captured
        assert any("Refusing MoQ session sess-moq" in line for line in logs)
        _nothing_carries_the_token(logs, refused.value)

    @sync
    async def test_an_image_that_reports_no_moq_refuses_with_its_reason(
        self, monkeypatch, captured, logs
    ):
        reason = "No module named 'moq'"
        _serves_no_moq(monkeypatch, reason)
        with pytest.raises(HTTPException) as refused:
            await _moq_start()
        assert refused.value.status_code == 400
        assert refused.value.detail == f"Refusing MoQ session sess-moq: {reason}"
        assert "args" not in captured
        assert any(reason in line for line in logs)
        _nothing_carries_the_token(logs, refused.value)

    @pytest.mark.parametrize(
        "quote",
        [
            lambda url: url,
            lambda url: f"'{url}'",
            lambda url: urlsplit(url).query,
            lambda url: TOKEN,
        ],
        ids=["the URL", "the URL quoted", "its query", "the token alone"],
    )
    @sync
    async def test_a_build_error_is_refused_without_the_token(
        self, monkeypatch, captured, logs, quote
    ):
        @dataclasses.dataclass
        class Echoing(_RunnerArguments):
            relay_url: Optional[str] = dataclasses.field(default=None, kw_only=True)
            namespace: str = "pipecat"
            participant_id: str = "response"
            peer_id: str = "request"

            def __post_init__(self):
                raise ValueError(f"bad relay_url {quote(self.relay_url)}")

        _serves_moq(monkeypatch, Echoing)
        with pytest.raises(HTTPException) as refused:
            await _moq_start()
        assert refused.value.status_code == 400
        assert "<relay URL>" in refused.value.detail
        assert "args" not in captured
        _nothing_carries_the_token(logs, refused.value)
        # Raised outside the except block: the error, unscrubbed, is not on it.
        assert refused.value.__context__ is None

    @sync
    async def test_a_relay_url_that_does_not_parse_is_scrubbed_whole(
        self, monkeypatch, captured, logs
    ):
        @dataclasses.dataclass
        class Echoing(_RunnerArguments):
            relay_url: Optional[str] = dataclasses.field(default=None, kw_only=True)
            namespace: str = "pipecat"
            participant_id: str = "response"
            peer_id: str = "request"

            def __post_init__(self):
                raise ValueError(f"bad relay_url {self.relay_url}")

        _serves_moq(monkeypatch, Echoing)
        malformed = f"https://[relay/?jwt={TOKEN}"
        with pytest.raises(HTTPException) as refused:
            await _moq_start(x_moq_relay_url=malformed)
        # A 400, not the 500 an error raised while scrubbing would give.
        assert refused.value.status_code == 400
        assert refused.value.__context__ is None
        _nothing_carries_the_token(logs, refused.value)

    @sync
    async def test_a_short_query_value_is_not_scrubbed_from_everywhere(self, monkeypatch, captured):
        @dataclasses.dataclass
        class Failing(_RunnerArguments):
            relay_url: Optional[str] = dataclasses.field(default=None, kw_only=True)
            namespace: str = "pipecat"
            participant_id: str = "response"
            peer_id: str = "request"

            def __post_init__(self):
                raise ValueError("attempt 1 of 1 failed")

        _serves_moq(monkeypatch, Failing)
        with pytest.raises(HTTPException) as refused:
            await _moq_start(x_moq_relay_url=f"{RELAY_URL}&v=1")
        assert "attempt 1 of 1 failed" in refused.value.detail

    @sync
    async def test_an_image_without_moq_gives_its_reason_before_anything_else(
        self, monkeypatch, captured
    ):
        _serves_no_moq(monkeypatch, "No module named 'moq'")
        with pytest.raises(HTTPException) as refused:
            await _moq_start(x_moq_relay_url=None, x_moq_namespace=None)
        assert refused.value.detail == "Refusing MoQ session sess-moq: No module named 'moq'"

    @sync
    async def test_other_transports_are_served_when_moq_is_not(self, monkeypatch, captured):
        _serves_no_moq(monkeypatch, "no MoQ here")
        await app.handle_bot_request(body={"user": "alice"}, x_daily_session_id="sess-http")
        assert captured["args"].body == {"user": "alice"}
        await app.handle_bot_request(
            body={},
            x_daily_session_id="sess-daily",
            x_daily_room_url="https://example.daily.co/room",
            x_daily_room_token="token",
        )
        assert captured["args"].room_url == "https://example.daily.co/room"

    @sync
    async def test_moq_headers_without_the_moq_transport_are_ignored(self, monkeypatch, captured):
        """Only the transport type chooses MoQ."""
        _serves_moq(monkeypatch)
        await _moq_start(x_daily_transport_type=None)
        assert not hasattr(captured["args"], "relay_url")

    @pytest.mark.skipif(os.environ.get("PCC_EXPECT_MOQ") != "1", reason="set by CI's moq-extra leg")
    @sync
    async def test_the_real_session_type_is_what_create_transport_dispatches_on(self, captured):
        """With what this image found at startup, on the leg that installs MoQ."""
        from pipecat.runner.types import MOQRunnerArguments

        assert app._moq_unavailable is None
        await _moq_start()
        args = captured["args"]
        assert isinstance(args, MOQRunnerArguments)
        assert args.relay_url == RELAY_URL
        assert TOKEN not in repr(args)


class TestRelayUrlSecrets:
    def test_a_short_query_is_not_scrubbed_from_everywhere(self):
        text = "ValueError: unsupported moq version v=1.0"
        assert app._without_relay_url(text, "https://relay.example.test:4443/?v=1") == text

    def test_a_percent_encoded_token_is_scrubbed_as_written_and_decoded(self):
        token = "tok+en/with=chars"
        relay_url = f"https://relay.example.test:4443/?jwt={quote(token, safe='')}"
        for quoted in (token, quote(token, safe="")):
            scrubbed = app._without_relay_url(f"bad token {quoted}", relay_url)
            assert token not in scrubbed and quoted not in scrubbed

    def test_a_session_without_a_relay_url_is_logged_unchanged(self):
        assert app._without_relay_url("boom 12345678", None) == "boom 12345678"

    def test_a_relay_url_it_cannot_read_withholds_the_text_without_raising(self):
        relay_url = f"https://relay.example.test/?jwt={TOKEN}".encode()
        assert app._without_relay_url(f"bad {TOKEN}", relay_url) == (
            "<withheld: it could not be checked for the relay URL>"
        )


class TestRunBotLogs:
    """run_bot logs what bot() raised; a MoQ bot's error may quote its relay URL."""

    @sync
    async def test_a_bot_error_quoting_the_relay_url_is_logged_without_it(self, monkeypatch, logs):
        async def failing_bot(args):
            raise ConnectionError(f"cannot reach {args.relay_url}")

        monkeypatch.setattr(app, "bot", failing_bot)
        args = _moq_type()(session_id="sess-moq", relay_url=RELAY_URL, namespace=NAMESPACE)
        await app.run_bot(args, "moq")
        assert any("Exception running bot(): cannot reach <relay URL>" in line for line in logs)
        _nothing_carries_the_token(logs)

    @sync
    async def test_a_bot_that_replaced_its_relay_url_cannot_make_run_bot_raise(
        self, monkeypatch, logs
    ):
        async def failing_bot(args):
            args.relay_url = args.relay_url.encode()
            raise ConnectionError(f"cannot reach {args.relay_url.decode()}")

        monkeypatch.setattr(app, "bot", failing_bot)
        args = _moq_type()(session_id="sess-moq", relay_url=RELAY_URL, namespace=NAMESPACE)
        await app.run_bot(args, "moq")
        assert any(
            "Exception running bot(): <withheld: it could not be checked for the relay URL>" in line
            for line in logs
        )
        _nothing_carries_the_token(logs)

    @sync
    async def test_another_sessions_error_is_logged_as_it_was(self, monkeypatch, logs):
        async def failing_bot(args):
            raise RuntimeError("boom 12345678")

        monkeypatch.setattr(app, "bot", failing_bot)
        args = app.pcc_pipecat_compat.build(app.PipecatSessionArguments, session_id="s", body={})
        await app.run_bot(args, None)
        assert any("Exception running bot(): boom 12345678" in line for line in logs)
