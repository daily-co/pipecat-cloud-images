"""End-to-end proof in a subprocess (install() rewires real fds — never do
that inside the pytest process).

The child mimics app.py's structure: install → logger setup (console with
filter + file sink) → a session (contextualize + session_scope) emitting a
framework line and a print, plus stderr and out-of-session output. Asserts:

- passthrough: the child's real stdout/stderr still carry everything
  (kubectl-logs equivalence), with no duplicated console rendering;
- the JSONL file carries every line with the right stream/level/session.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

PIPECAT_BASE_DIR = Path(__file__).resolve().parent.parent

CHILD = textwrap.dedent(
    """
    import sys

    import pcc_structured_logs
    pcc_structured_logs.install()

    from loguru import logger

    logger.remove()
    logger.add(
        pcc_structured_logs.console_stream() or sys.stderr,
        format="{extra[session_id]} - {message}",
        level="DEBUG",
        filter=pcc_structured_logs.console_filter,
    )
    pcc_structured_logs.add_file_sink(logger, "DEBUG")
    logger.configure(extra={"session_id": "NONE"})

    with (
        logger.contextualize(session_id="sess-e2e"),
        pcc_structured_logs.session_scope("sess-e2e"),
    ):
        logger.info("framework line")
        print("printed in session")
        sys.stderr.write("stderr in session\\n")
        sys.stdout.flush()
        sys.stderr.flush()

    # The session slot lingers briefly after session end (so in-flight captured
    # output keeps its attribution); wait it out before the unattributed line.
    import time
    time.sleep(pcc_structured_logs._SESSION_LINGER_SECONDS + 0.2)

    print("printed outside session")
    sys.stdout.flush()

    time.sleep(0.3)   # let the pump threads drain the pipes
    logger.remove()   # flush the enqueue file sink
    """
)


def run_child(tmp_path):
    return subprocess.run(
        [sys.executable, "-c", CHILD],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PCC_LOG_DIR": str(tmp_path)},
        cwd=str(PIPECAT_BASE_DIR),
    )


def test_end_to_end(tmp_path):
    proc = run_child(tmp_path)
    assert proc.returncode == 0, proc.stderr

    # --- passthrough: kubectl-logs equivalence ---
    assert "printed in session" in proc.stdout
    assert "printed outside session" in proc.stdout
    assert "stderr in session" in proc.stderr
    # Framework line rendered once by the console sink (on real stderr).
    assert proc.stderr.count("sess-e2e - framework line") == 1
    # Captured lines are NOT re-rendered by the console sink (filter works):
    # each appears exactly once across the real streams.
    assert proc.stdout.count("printed in session") == 1
    assert proc.stderr.count("printed in session") == 0

    # --- the structured lane ---
    records = [
        json.loads(line) for line in (tmp_path / "bot.jsonl").read_text().splitlines() if line
    ]
    by_line = {r["line"]: r for r in records}

    fw = by_line["framework line"]
    assert fw["stream"] == "app"
    assert fw["level"] == "INFO"
    assert fw["session_id"] == "sess-e2e"
    assert fw["@timestamp"]

    printed = by_line["printed in session"]
    assert printed["stream"] == "stdout"
    assert "level" not in printed
    assert printed["session_id"] == "sess-e2e"

    err = by_line["stderr in session"]
    assert err["stream"] == "stderr"
    assert err["session_id"] == "sess-e2e"

    outside = by_line["printed outside session"]
    assert outside["stream"] == "stdout"
    assert "session_id" not in outside


CRASH_CHILD = textwrap.dedent(
    """
    import os
    import sys
    import time

    import pcc_structured_logs
    pcc_structured_logs.install()

    from loguru import logger

    logger.remove()
    logger.add(
        pcc_structured_logs.console_stream() or sys.stderr,
        format="{extra[session_id]} - {message}",
        level="DEBUG",
        filter=pcc_structured_logs.console_filter,
    )
    pcc_structured_logs.add_file_sink(logger, "DEBUG")
    logger.configure(extra={"session_id": "NONE"})

    with (
        logger.contextualize(session_id="sess-crash"),
        pcc_structured_logs.session_scope("sess-crash"),
    ):
        logger.info("pre-crash line")
        print("crash-tail stdout marker", flush=True)
        time.sleep(0.05)  # one pump-thread beat: the capture lane's async hop
        logger.error("these are the dying words")
        os._exit(17)  # hard death: no atexit, no flush, no loguru teardown
    """
)


def test_crash_tail_survives_hard_exit(tmp_path):
    """PCC-1038: framework-lane lines written in the final microseconds before
    os._exit must be on disk (synchronous line-buffered writes to the page
    cache) — with enqueue=True this deterministically loses the dying words.
    The captured stdout marker gets one pump beat (50ms) — the capture lane's
    documented best-effort hop — and must also survive."""
    proc = subprocess.run(
        [sys.executable, "-c", CRASH_CHILD],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PCC_LOG_DIR": str(tmp_path)},
        cwd=str(PIPECAT_BASE_DIR),
    )
    assert proc.returncode == 17

    records = [
        json.loads(line) for line in (tmp_path / "bot.jsonl").read_text().splitlines() if line
    ]
    by_line = {r["line"]: r for r in records}

    # The last framework record before death — the whole point of PCC-1038.
    dying = by_line["these are the dying words"]
    assert dying["level"] == "ERROR"
    assert dying["session_id"] == "sess-crash"

    assert by_line["pre-crash line"]["session_id"] == "sess-crash"

    marker = by_line["crash-tail stdout marker"]
    assert marker["stream"] == "stdout"
    assert marker["session_id"] == "sess-crash"
