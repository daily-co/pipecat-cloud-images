"""Emergency appends must not wait for a normal writer held by another thread."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pcc_early_sigterm
import pytest

PIPECAT_BASE_DIR = Path(__file__).resolve().parent.parent

CHILD = textwrap.dedent(
    """
    import os
    import signal
    import threading

    import pcc_early_sigterm
    import pcc_structured_logs as logs

    pcc_early_sigterm.install()
    logs.install()
    held = threading.Event()
    release = threading.Event()

    def hold_writer_lock():
        # A different thread owns the lock, so even an RLock cannot be
        # acquired by the main thread's emergency path.
        with logs._writer.lock:
            held.set()
            release.wait()

    threading.Thread(target=hold_writer_lock, daemon=True).start()
    assert held.wait(5), "worker did not acquire the writer lock"

    if os.environ["PCC_TEST_EMERGENCY_ACTION"] == "sigterm":
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("early SIGTERM handler returned")

    logs.write_record("CRITICAL", "direct append with normal writer locked")
    logs.console_stream().write("DIRECT_APPEND_FINISHED\\n")
    logs.console_stream().flush()
    os._exit(23)
    """
)


@pytest.mark.parametrize("action", ["sigterm", "write_record"])
def test_emergency_append_ignores_the_normal_writer_lock(tmp_path, action):
    log_dir = tmp_path / "logs"
    log_path = log_dir / "bot.jsonl"
    stdout_path = tmp_path / "stdout"
    stderr_path = tmp_path / "stderr"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PCC_LOG_DIR")}
    env.update(PCC_LOG_DIR=str(log_dir), PCC_TEST_EMERGENCY_ACTION=action)
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD],
            cwd=PIPECAT_BASE_DIR,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        try:
            while proc.poll() is None:
                size = sum(
                    path.stat().st_size
                    for path in (stdout_path, stderr_path, log_path)
                    if path.exists()
                )
                assert size < 64 * 1024, "emergency logging amplified its output"
                assert time.monotonic() < deadline, "emergency append waited for the writer lock"
                time.sleep(0.01)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)

    stderr = stderr_path.read_text()
    assert stdout_path.read_bytes() == b""
    assert stdout_path.stat().st_size + stderr_path.stat().st_size + log_path.stat().st_size < (
        64 * 1024
    )
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["stream"] == "app"
    assert record["@timestamp"]
    if action == "sigterm":
        assert proc.returncode == 0, stderr
        assert record["level"] == "WARNING"
        assert record["line"] == pcc_early_sigterm._MESSAGE
        assert stderr.count(pcc_early_sigterm._MESSAGE) == 1
    else:
        assert proc.returncode == 23, stderr
        assert record["level"] == "CRITICAL"
        assert record["line"] == "direct append with normal writer locked"
        assert stderr == "DIRECT_APPEND_FINISHED\n"
