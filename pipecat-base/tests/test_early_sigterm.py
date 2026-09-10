"""pcc_early_sigterm: a SIGTERM during startup must terminate the process, and
one the server has already handled must be absorbed.

PCC-1004. Each test runs a child interpreter, because the behaviour under test
is what happens to the *process* on a signal: whether it exits, with which code,
and whether its final line reaches stderr. Python is not PID 1 here, so the
default disposition would already kill the child — which is exactly what makes
the exit code informative: 0 plus the message means the handler ran, -SIGTERM
means the default disposition did.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pcc_early_sigterm

PIPECAT_BASE_DIR = Path(__file__).resolve().parent.parent

READY = "READY"

INSTALLED = textwrap.dedent(
    f"""
    import time
    import pcc_early_sigterm
    pcc_early_sigterm.install()
    print({READY!r}, flush=True)
    time.sleep(30)
    """
)

# Deferred, but the server has not handled anything yet: the moments between
# defer_to_server() and uvicorn installing its own handler. Still exits.
DEFERRED_UNHANDLED = textwrap.dedent(
    f"""
    import time
    import pcc_early_sigterm
    pcc_early_sigterm.install()
    pcc_early_sigterm.defer_to_server(lambda: False)
    print({READY!r}, flush=True)
    time.sleep(30)
    """
)

# uvicorn's exit, in miniature: after the drain it restores our handler and
# re-raises the signal it captured. should_exit is True by then. The child
# raises it on itself; if the handler absorbs it the next line runs.
DEFERRED_HANDLED_RERAISE = textwrap.dedent(
    """
    import signal, sys
    import pcc_early_sigterm
    pcc_early_sigterm.install()
    pcc_early_sigterm.defer_to_server(lambda: True)
    signal.raise_signal(signal.SIGTERM)
    print("ABSORBED", flush=True)
    sys.exit(0)
    """
)

# A bot module that installs its own SIGTERM handler at import time, between
# install() and defer_to_server(). Prints so the parent can tell whose ran.
IMPORT_TIME_HANDLER = textwrap.dedent(
    f"""
    import signal, sys, time
    import pcc_early_sigterm
    pcc_early_sigterm.install()
    signal.signal(signal.SIGTERM, lambda *a: (print("CUSTOM", flush=True), sys.exit(0)))
    pcc_early_sigterm.defer_to_server(lambda: False)
    print({READY!r}, flush=True)
    time.sleep(30)
    """
)

# The order matters and mirrors app.py: the signal handler goes in first, the
# structured-log capture second, and the capture then owns fd 2.
CAPTURED = textwrap.dedent(
    f"""
    import time
    import pcc_early_sigterm
    pcc_early_sigterm.install()
    import pcc_structured_logs
    pcc_structured_logs.install()
    print({READY!r}, flush=True)
    time.sleep(30)
    """
)


# As CAPTURED, but the bot module also prints during its import, right before
# the signal lands: that line is still in the capture pipe when the handler
# runs and must be drained into bot.jsonl before the process is gone.
CAPTURED_WITH_IMPORT_OUTPUT = textwrap.dedent(
    f"""
    import time
    import pcc_early_sigterm
    pcc_early_sigterm.install()
    import pcc_structured_logs
    pcc_structured_logs.install()
    print({READY!r}, flush=True)
    print("IMPORT_LINE", flush=True)
    time.sleep(30)
    """
)


def _spawn(child: str, env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env or {})},
        cwd=str(PIPECAT_BASE_DIR),
    )


def sigterm_once_ready(child: str, env: dict | None = None) -> tuple[int, str, str]:
    """Run `child`, SIGTERM it after it prints READY, return (returncode, stdout, stderr)."""
    proc = _spawn(child, env)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if READY in line:
                break
        else:
            raise AssertionError(f"child exited before {READY}: {proc.stderr.read()}")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode, out, err


def test_installed_handler_exits_zero_with_message():
    code, _, err = sigterm_once_ready(INSTALLED)
    assert code == 0, f"expected the handler's clean exit, got {code}\n{err}"
    assert pcc_early_sigterm._MESSAGE.strip() in err


def test_deferred_but_unhandled_sigterm_still_exits():
    code, _, err = sigterm_once_ready(DEFERRED_UNHANDLED)
    assert code == 0, f"expected the handler's clean exit, got {code}\n{err}"
    assert pcc_early_sigterm._MESSAGE.strip() in err


def test_server_handled_reraise_is_absorbed():
    """The signal uvicorn re-raises after its drain must be absorbed, so the
    process goes on to exit normally — atexit hooks and flushes included —
    exactly as it did when PID 1 discarded that re-raise."""
    proc = _spawn(DEFERRED_HANDLED_RERAISE)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"expected a normal exit, got {proc.returncode}\n{err}"
    assert "ABSORBED" in out, f"the re-raised SIGTERM was not absorbed\n{err}"
    assert pcc_early_sigterm._MESSAGE.strip() not in err


def test_defer_keeps_a_handler_installed_by_the_bot_module():
    """defer_to_server() must not evict a handler the customer's import
    installed: uvicorn saves and restores it, and its post-drain re-raise
    invokes it, so that handler behaves exactly as it did before this module."""
    code, out, err = sigterm_once_ready(IMPORT_TIME_HANDLER)
    assert "CUSTOM" in out, f"the bot module's own handler did not run\n{err}"
    assert code == 0, f"expected the custom handler's clean exit, got {code}\n{err}"
    assert pcc_early_sigterm._MESSAGE.strip() not in err


def test_message_survives_stderr_capture(tmp_path):
    """With pcc_structured_logs installed, fd 2 is its capture pipe; a line
    written there is still in the pump thread when os._exit runs and is lost.
    The handler must write to the capture's saved console stream instead."""
    code, _, err = sigterm_once_ready(CAPTURED, env={"PCC_LOG_DIR": str(tmp_path)})
    assert code == 0, f"expected the handler's clean exit, got {code}\n{err}"
    assert pcc_early_sigterm._MESSAGE.strip() in err


def test_early_exit_is_recorded_in_the_structured_lane(tmp_path):
    """The structured lane is where a startup exit gets diagnosed, so the
    handler leaves a framework-lane record there in _serialize's shape — and
    the bot module's own import output, still in the capture pipe when the
    signal lands, is drained ahead of it rather than lost to os._exit."""
    code, _, err = sigterm_once_ready(
        CAPTURED_WITH_IMPORT_OUTPUT, env={"PCC_LOG_DIR": str(tmp_path)}
    )
    assert code == 0, f"expected the handler's clean exit, got {code}\n{err}"
    lines = (tmp_path / "bot.jsonl").read_text().splitlines()
    by_line = {r["line"]: r for r in (json.loads(line) for line in lines if line)}
    ours = by_line.get(pcc_early_sigterm._MESSAGE)
    assert ours is not None, f"no record of the early exit in bot.jsonl:\n{lines}"
    assert ours["stream"] == "app" and ours["level"] == "WARNING" and ours["@timestamp"]
    assert "IMPORT_LINE" in by_line, f"captured import output was not drained:\n{lines}"
