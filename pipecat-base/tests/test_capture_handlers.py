"""Real-fd regressions for customer Loguru configuration during bot import.

Each child drains both capture pipes before exiting. Output goes to bounded
files, rather than subprocess.PIPE: a feedback regression must fail without
exhausting the test runner's memory.
"""

import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

PIPECAT_BASE_DIR = Path(__file__).resolve().parent.parent
_OUTPUT_LIMIT = 1_000_000
_DRAIN_MARKERS = {"stdout": "PCC_CAPTURE_DRAIN_STDOUT", "stderr": "PCC_CAPTURE_DRAIN_STDERR"}

_PREAMBLE = """
import json
import os
import sys
import time
from pathlib import Path

from loguru import logger
import pcc_structured_logs as logs

logs.install()
"""

_DRAIN = """
# Queued customer sinks must finish writing before these pipe barriers.
logger.complete()
sys.stdout.flush()
sys.stderr.flush()
os.write(1, b"PCC_CAPTURE_DRAIN_STDOUT\\n")
os.write(2, b"PCC_CAPTURE_DRAIN_STDERR\\n")
path = Path(os.environ["PCC_LOG_DIR"]) / "bot.jsonl"
deadline = time.monotonic() + 5
while True:
    try:
        # A concurrent writer may be between write and newline; only inspect
        # complete records. Seeing each barrier proves its earlier bytes drained.
        data = path.read_bytes()
        complete = data[:data.rfind(b"\\n") + 1]
        lines = {json.loads(line)["line"] for line in complete.splitlines()}
    except FileNotFoundError:
        lines = set()
    if {"PCC_CAPTURE_DRAIN_STDOUT", "PCC_CAPTURE_DRAIN_STDERR"} <= lines:
        break
    if time.monotonic() >= deadline:
        raise RuntimeError("capture did not drain both pipes")
    time.sleep(0.005)
"""


def _run_child(tmp_path, source):
    log_dir = tmp_path / "logs"
    stdout_path = tmp_path / "stdout"
    stderr_path = tmp_path / "stderr"
    log_path = log_dir / "bot.jsonl"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PCC_LOG_DIR")}
    env["PCC_LOG_DIR"] = str(log_dir)
    code = _PREAMBLE + textwrap.dedent(source) + _DRAIN
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=PIPECAT_BASE_DIR,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        try:
            while proc.poll() is None:
                size = sum(
                    path.stat().st_size
                    for path in (stdout_path, stderr_path, log_path)
                    if path.exists()
                )
                assert size <= _OUTPUT_LIMIT, "capture amplified output beyond its bound"
                assert time.monotonic() < deadline, "child failed to finish startup"
                time.sleep(0.01)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # the child exited between poll() and killpg()
            proc.wait(timeout=5)

    stdout = stdout_path.read_bytes()
    stderr = stderr_path.read_bytes()
    assert proc.returncode == 0, stderr[-4000:].decode(errors="replace")
    assert len(stdout) + len(stderr) + log_path.stat().st_size <= _OUTPUT_LIMIT
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    for stream, marker in _DRAIN_MARKERS.items():
        matches = [r for r in records if r["line"] == marker]
        assert len(matches) == 1, matches
        assert matches[0]["stream"] == stream
    return stdout, stderr, [r for r in records if r["line"] not in _DRAIN_MARKERS.values()]


def _assert_raw(record, stream, line):
    assert record["stream"] == stream
    assert record["line"] == line
    assert record["@timestamp"]
    assert "level" not in record


def test_repeated_install_leaves_the_default_handler_removable(tmp_path):
    stdout, stderr, records = _run_child(
        tmp_path,
        """
        logs.install()
        logger.remove(0)
        logger.add(sys.stderr, format="CUSTOM:{message}")
        logger.info("default-removed")
        """,
    )
    assert stdout == b"PCC_CAPTURE_DRAIN_STDOUT\n"
    assert stderr == b"CUSTOM:default-removed\nPCC_CAPTURE_DRAIN_STDERR\n"
    assert len(records) == 1
    _assert_raw(records[0], "stderr", "CUSTOM:default-removed")
    assert "session_id" not in records[0]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("enqueue", [False, True], ids=["synchronous", "queued"])
def test_adding_a_console_handler_cannot_feed_capture_back(tmp_path, stream, enqueue):
    stdout, stderr, records = _run_child(
        tmp_path,
        f"""
        # Keep handler 0 as well: both customer-selected renderings must survive.
        logger.add(sys.{stream}, format="CUSTOM:{{message}}", enqueue={enqueue!r})
        for number in range(200):
            logger.warning(f"IMPORT-EVENT-{{number:04d}}-END")
        """,
    )
    assert len(records) == 400
    for number in range(200):
        marker = f"IMPORT-EVENT-{number:04d}-END"
        matches = [r for r in records if marker in r["line"]]
        assert len(matches) == 2
        custom = [r for r in matches if r["line"] == f"CUSTOM:{marker}"]
        assert len(custom) == 1
        _assert_raw(custom[0], stream, f"CUSTOM:{marker}")
        original = [r for r in matches if r is not custom[0]]
        assert len(original) == 1
        _assert_raw(original[0], "stderr", original[0]["line"])
        # CI can force ANSI colors even when stderr is a file. Capture keeps
        # those original bytes; only normalize SGR for the format assertion.
        original_line = original[0]["line"]
        assert (original_line + "\n").encode() in stderr
        assert re.sub(r"\x1b\[[0-9;]*m", "", original_line).endswith(f" - {marker}")
        assert stdout.count(marker.encode()) == (1 if stream == "stdout" else 0)
        assert stderr.count(marker.encode()) == (2 if stream == "stderr" else 1)


def test_removing_every_loguru_handler_keeps_raw_capture_alive(tmp_path):
    stdout, stderr, records = _run_child(
        tmp_path,
        """
        logger.remove()
        print("printed stdout", flush=True)
        sys.stderr.write("written stderr\\n")
        sys.stderr.flush()
        os.write(1, b"native stdout\\n")
        os.write(2, b"native \\xff stderr\\n")
        """,
    )
    assert stdout == b"printed stdout\nnative stdout\nPCC_CAPTURE_DRAIN_STDOUT\n"
    assert stderr == b"written stderr\nnative \xff stderr\nPCC_CAPTURE_DRAIN_STDERR\n"
    assert len(records) == 4
    actual = {(r["stream"], r["line"]) for r in records}
    assert actual == {
        ("stdout", "printed stdout"),
        ("stdout", "native stdout"),
        ("stderr", "written stderr"),
        ("stderr", "native \ufffd stderr"),
    }
    assert all("level" not in r for r in records)


def test_configure_with_both_queued_console_sinks_preserves_each_output_once(tmp_path):
    stdout, stderr, records = _run_child(
        tmp_path,
        """
        logger.configure(handlers=[
            dict(sink=sys.stdout, format="OUT:{message}", enqueue=True),
            dict(sink=sys.stderr, format="ERR:{message}", enqueue=True),
        ])
        logger.info("configured")
        """,
    )
    assert stdout == b"OUT:configured\nPCC_CAPTURE_DRAIN_STDOUT\n"
    assert stderr == b"ERR:configured\nPCC_CAPTURE_DRAIN_STDERR\n"
    assert len(records) == 2
    assert {(r["stream"], r["line"]) for r in records} == {
        ("stdout", "OUT:configured"),
        ("stderr", "ERR:configured"),
    }


def test_custom_multiline_sink_and_identical_printed_text_are_not_deduplicated(tmp_path):
    stdout, stderr, records = _run_child(
        tmp_path,
        """
        logger.remove(0)
        logger.add(
            lambda message: sys.stdout.write(str(message)),
            format="CUSTOM:{message}\\nsecond line",
            enqueue=True,
        )
        logger.info("{braces} \\U0001f916")
        logger.complete()
        print("CUSTOM:{braces} \\U0001f916", flush=True)
        """,
    )
    expected = "CUSTOM:{braces} \U0001f916"
    assert stdout.decode() == f"{expected}\nsecond line\n{expected}\nPCC_CAPTURE_DRAIN_STDOUT\n"
    assert stderr == b"PCC_CAPTURE_DRAIN_STDERR\n"
    assert len(records) == 3
    assert [r["line"] for r in records] == [expected, "second line", expected]
    assert all(r["stream"] == "stdout" and "level" not in r for r in records)


def test_pipecatcloud_can_be_imported_after_capture_starts(tmp_path):
    stdout, stderr, records = _run_child(
        tmp_path,
        """
        import pipecatcloud
        logger.info("AFTER-PIPECATCLOUD-IMPORT")
        print("after import stdout", flush=True)
        """,
    )
    imported = [r for r in records if "AFTER-PIPECATCLOUD-IMPORT" in r["line"]]
    assert len(imported) == 1
    _assert_raw(imported[0], "stderr", imported[0]["line"])
    assert stderr.count(b"AFTER-PIPECATCLOUD-IMPORT") == 1
    assert stdout == b"after import stdout\nPCC_CAPTURE_DRAIN_STDOUT\n"
    printed = [r for r in records if r["line"] == "after import stdout"]
    assert len(printed) == 1
    _assert_raw(printed[0], "stdout", "after import stdout")


def test_runtime_framework_record_and_extra_console_output_both_survive(tmp_path):
    stdout, stderr, records = _run_child(
        tmp_path,
        """
        # app.py installs the normal structured + saved-console sinks after import.
        logger.remove()
        logger.add(logs.console_stream(), format="{message}")
        logs.add_file_sink(logger, "DEBUG")
        logger.add(sys.stderr, format="EXTRA:{message}")
        logs.set_current_session("runtime-session")
        logger.bind(session_id="runtime-session").warning("runtime event")
        # This is a distinct real event even though its text matches the app one.
        print("runtime event", flush=True)
        """,
    )
    assert stdout == b"runtime event\nPCC_CAPTURE_DRAIN_STDOUT\n"
    assert stderr == b"runtime event\nEXTRA:runtime event\nPCC_CAPTURE_DRAIN_STDERR\n"
    assert len(records) == 3
    app = [r for r in records if r["stream"] == "app"]
    assert len(app) == 1
    assert app[0]["line"] == "runtime event"
    assert app[0]["level"] == "WARNING"
    assert app[0]["session_id"] == "runtime-session"
    raw = [r for r in records if r["stream"] != "app"]
    assert {(r["stream"], r["line"]) for r in raw} == {
        ("stdout", "runtime event"),
        ("stderr", "EXTRA:runtime event"),
    }
    assert all("level" not in r and r["session_id"] == "runtime-session" for r in raw)
