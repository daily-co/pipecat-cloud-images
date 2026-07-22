import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pcc_structured_logs
from pcc_structured_logs import _serialize

TS = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def record(extra=None, message="hello", level_name="INFO"):
    return {
        "time": TS,
        "level": SimpleNamespace(name=level_name, no=20),
        "extra": extra or {},
        "message": message,
    }


def test_app_lane_fields():
    out = json.loads(_serialize(record(extra={"session_id": "sess-1"})))
    assert out == {
        "@timestamp": "2026-07-21T12:00:00+00:00",
        "stream": "app",
        "level": "INFO",
        "session_id": "sess-1",
        "line": "hello",
    }


def test_app_lane_omits_placeholder_session():
    out = json.loads(_serialize(record(extra={"session_id": "NONE"})))
    assert "session_id" not in out


def test_app_lane_omits_missing_session():
    out = json.loads(_serialize(record()))
    assert "session_id" not in out


def test_raw_lane_has_no_level_and_uses_current_session():
    pcc_structured_logs.set_current_session("sess-2")
    try:
        out = json.loads(_serialize(record(extra={"pcc_stream": "stdout"}, message="printed")))
    finally:
        pcc_structured_logs.set_current_session(None)
    assert out["stream"] == "stdout"
    assert "level" not in out
    assert out["session_id"] == "sess-2"
    assert out["line"] == "printed"


def test_raw_lane_without_session_omits_it():
    out = json.loads(_serialize(record(extra={"pcc_stream": "stderr"})))
    assert out["stream"] == "stderr"
    assert "session_id" not in out


def test_braces_and_unicode_survive():
    msg = 'weird {braces} and "quotes" and emoji \U0001f916'
    out = json.loads(_serialize(record(message=msg)))
    assert out["line"] == msg


def test_serialization_failure_yields_marker_not_exception():
    # A record missing required keys must not raise.
    out = json.loads(_serialize({"extra": {}}))
    assert "serialization failed" in out["line"]


def test_session_scope_sets_lingers_then_clears_even_on_error():
    import time

    try:
        with pcc_structured_logs.session_scope("sess-3"):
            assert pcc_structured_logs._current_session_id == "sess-3"
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Clearing is deferred (linger) so in-flight captured output keeps its
    # attribution; after the linger the slot must be empty.
    assert pcc_structured_logs._current_session_id == "sess-3"
    time.sleep(pcc_structured_logs._SESSION_LINGER_SECONDS + 0.15)
    assert pcc_structured_logs._current_session_id is None


def test_new_session_cancels_linger_and_takes_over_immediately():
    pcc_structured_logs.set_current_session("sess-old")
    pcc_structured_logs.set_current_session(None)  # linger starts
    pcc_structured_logs.set_current_session("sess-new")  # cancels linger
    import time

    time.sleep(pcc_structured_logs._SESSION_LINGER_SECONDS + 0.15)
    # The cancelled linger must not have cleared the new session.
    assert pcc_structured_logs._current_session_id == "sess-new"
    pcc_structured_logs.set_current_session(None)
    time.sleep(pcc_structured_logs._SESSION_LINGER_SECONDS + 0.15)
    assert pcc_structured_logs._current_session_id is None


def test_console_filter_excludes_captured_lines():
    assert pcc_structured_logs.console_filter({"extra": {}}) is True
    assert pcc_structured_logs.console_filter({"extra": {"pcc_stream": "stdout"}}) is False
