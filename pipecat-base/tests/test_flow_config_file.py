"""A websocket session's flow arrives as a file the handshake names.

On the websocket transport Pipecat Cloud reaches ``/ws`` through a handshake,
which has no body. The platform's sidecar writes the session's flow to a file
in ``PCC_SESSION_FILES_DIR`` and names it in ``X-Pcc-Flow-Config-File``.

The properties pinned here:

* **The flow reaches the bot** as ``runner_args.flow_config``, as it does on
  ``/bot``; a handshake naming no file leaves it ``None``.
* **Only the named file, only inside the directory.** A name with a path
  separator, a symlink, or no configured directory is refused.
* **A flow that cannot be read refuses the session** before the handshake is
  accepted, so ``bot()`` never runs on the flow the image ships with.
* **The flow is never logged.**
"""

import os
import sys
import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

if "bot" not in sys.modules:
    _stub = types.ModuleType("bot")

    async def _noop_bot(args):
        return None

    _stub.bot = _noop_bot
    sys.modules["bot"] = _stub

import app  # noqa: E402

FLOW = "initial_node: greet\nnodes:\n  greet:\n    task_messages: [{content: secret-prompt}]\n"
NAME = "9b3f6f0e-2c7a-4d1e-9f61-0d5c1e2a7b44.flow"


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setenv(app._SESSION_FILES_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    async def fake_run_bot(args, transport_type=None):
        seen["args"] = args

    monkeypatch.setattr(app, "run_bot", fake_run_bot)
    return seen


@pytest.fixture
def logs():
    lines = []
    sink = app.logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    app.logger.remove(sink)


def _connect(headers=None):
    with TestClient(app.app).websocket_connect("/ws", headers=headers or {}):
        pass


class TestTheFlowReachesTheBot:
    def test_from_the_named_file(self, volume, captured, logs):
        (volume / NAME).write_text(FLOW, encoding="utf-8")
        _connect({"X-Pcc-Flow-Config-File": NAME, "X-Daily-Session-Id": "s1"})
        assert captured["args"].flow_config == FLOW
        assert not any("secret-prompt" in line for line in logs)

    def test_no_header_means_no_flow(self, volume, captured):
        # Also every handshake from a platform that predates this.
        _connect({"X-Daily-Session-Id": "s1"})
        assert captured["args"].flow_config is None

    def test_non_ascii_text_round_trips(self, volume, captured):
        flow = "nodes:\n  greet:\n    task_messages: [{content: 'ありがとう 😀'}]\n"
        (volume / NAME).write_text(flow, encoding="utf-8")
        _connect({"X-Pcc-Flow-Config-File": NAME})
        assert captured["args"].flow_config == flow


class TestAFlowThatCannotBeReadRefusesTheSession:
    def _refused(self, headers, captured, logs):
        with pytest.raises(WebSocketDisconnect) as exc:
            _connect(headers)
        assert exc.value.code == 1011
        assert "args" not in captured, "bot() must never run"
        assert any("Refusing websocket session" in line for line in logs)
        assert not any("secret-prompt" in line for line in logs)

    def test_a_missing_file(self, volume, captured, logs):
        self._refused({"X-Pcc-Flow-Config-File": NAME}, captured, logs)

    def test_an_empty_file(self, volume, captured, logs):
        (volume / NAME).write_bytes(b"")
        self._refused({"X-Pcc-Flow-Config-File": NAME}, captured, logs)

    def test_a_file_that_is_not_utf8(self, volume, captured, logs):
        (volume / NAME).write_bytes(b"\xff\xfe secret-prompt")
        self._refused({"X-Pcc-Flow-Config-File": NAME}, captured, logs)

    def test_no_configured_directory(self, monkeypatch, captured, logs):
        monkeypatch.delenv(app._SESSION_FILES_DIR_ENV, raising=False)
        self._refused({"X-Pcc-Flow-Config-File": NAME}, captured, logs)

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "../secret.flow", "sub/secret.flow", "/etc/passwd", "a\\b.flow"],
    )
    def test_anything_but_a_bare_filename(self, name, volume, captured, logs, tmp_path_factory):
        # Put a readable file where each escape would land, so a refusal is
        # the only way the test passes.
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.flow").write_text(FLOW)
        (volume / "sub").mkdir()
        (volume / "sub" / "secret.flow").write_text(FLOW)
        self._refused({"X-Pcc-Flow-Config-File": name}, captured, logs)

    def test_a_symlink_is_not_followed(self, volume, captured, logs, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside") / "elsewhere.flow"
        outside.write_text(FLOW)
        os.symlink(outside, volume / NAME)
        self._refused({"X-Pcc-Flow-Config-File": NAME}, captured, logs)


class TestReadFlowConfigFile:
    def test_the_error_names_the_file_not_the_flow(self, volume):
        (volume / NAME).write_bytes(b"\xff secret-prompt")
        with pytest.raises(app._FlowConfigFileError) as exc:
            app._read_flow_config_file(NAME)
        assert NAME in str(exc.value)
        assert "secret-prompt" not in str(exc.value)
        # Neither chain: a decode error carries the bytes it failed on, and
        # `from None` only hides __context__, it does not drop it.
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None

    def test_a_flow_at_the_size_cap_round_trips(self, volume):
        flow = "nodes:\n" + ("  # " + "x" * 60 + "\n") * 4100
        assert len(flow.encode()) > 256 * 1024
        (volume / NAME).write_text(flow, encoding="utf-8")
        assert app._read_flow_config_file(NAME) == flow

    def test_the_refusal_is_attributed_to_its_session(self, volume, captured):
        records = []
        sink = app.logger.add(lambda m: records.append(m.record), level="ERROR")
        try:
            with pytest.raises(WebSocketDisconnect):
                _connect({"X-Pcc-Flow-Config-File": NAME, "X-Daily-Session-Id": "s-42"})
        finally:
            app.logger.remove(sink)
        assert any(r["extra"].get("session_id") == "s-42" for r in records)
