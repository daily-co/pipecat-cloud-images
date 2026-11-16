#!/usr/bin/env python3
"""Container-level check that the base image handles SIGTERM during startup.

The kernel does not apply default signal dispositions to PID 1: a
signal with no registered handler is discarded rather than terminating the
process. The image's CMD execs Python into PID 1, and uvicorn only installs its
SIGTERM handler after every module-level import — the customer's bot module
included — so a SIGTERM in that window was dropped and the container went on
to start and serve on a pod the kubelet had already told to stop. The fix is in
two layers: tini holds PID 1 and runs Python as its child (Dockerfile), and
app.py installs its own handler for the startup window (pcc_early_sigterm) so
the fix holds in derived images that replace the ENTRYPOINT.

These are properties of the image's entrypoint and of app.py's import order,
which no unit test can reach — hence a container check. It drives the real
image: `-c` scripts isolate the entrypoint's signal behaviour with no timing
race, and the real app.py, with a minimal bot module bind-mounted in, covers
the handler and the graceful path end to end.

Not named test_*.py on purpose: the unit-test job runs `pytest` over this tree
and must not collect a check that needs a Docker daemon.

Usage: python3 container_signal_check.py [IMAGE]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

DEFAULT_IMAGE = "pipecat-base:signal-check"
READY = "READY"
HANDLED = "HANDLED"
IMPORTING = "IMPORTING"
STARTED = "Application startup complete"
SHUTDOWN = "Application shutdown complete"
EARLY_EXIT = "SIGTERM received before the server was up"
IMPORT_LOG = "customer import log"
MAX_LOG_BYTES = 1_000_000
DRAIN_MARKERS = ("PCC_LOG_LANE_DRAIN_STDOUT", "PCC_LOG_LANE_DRAIN_STDERR")

# Sleeps long enough that nothing exits on its own within the check's timeouts;
# every container here is killed or signalled explicitly.
NO_HANDLER = f"import time; print({READY!r}, flush=True); time.sleep(60)"
WITH_HANDLER = (
    "import signal, sys, time; "
    f"signal.signal(signal.SIGTERM, lambda *a: (print({HANDLED!r}, flush=True), sys.exit(0))); "
    f"print({READY!r}, flush=True); "
    "time.sleep(60)"
)

# The smallest bot module app.py will import. IMPORT_DELAY holds the process
# inside the import — the window under test — for as long as the check needs.
BOT_PY = f"""\
import os
import time

print({IMPORTING!r}, flush=True)
time.sleep(float(os.environ.get("IMPORT_DELAY", "0")))


async def bot(args):
    pass
"""

LOGGING_BOT_PY = f"""\
import atexit
import json
import os
import sys
import time
from pathlib import Path

import pcc_structured_logs as logs
from loguru import logger


def _drain_capture():
    # Uvicorn's final raw console lines otherwise race daemon-pump teardown.
    # Drain this fixture's normal exit; early SIGTERM uses os._exit and skips it.
    sys.stdout.flush()
    sys.stderr.flush()
    for fd, marker in ((1, {DRAIN_MARKERS[0]!r}), (2, {DRAIN_MARKERS[1]!r})):
        os.write(fd, (marker + "\\n").encode())
    path = Path(os.environ["PCC_LOG_DIR"]) / "bot.jsonl"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            content = path.read_bytes()
            complete = content[:content.rfind(b"\\n") + 1]
            lines = {{json.loads(line)["line"] for line in complete.splitlines()}}
        except (OSError, ValueError):
            lines = set()
        if all(marker in lines for marker in {DRAIN_MARKERS!r}):
            return
        time.sleep(0.005)
    console = logs.console_stream() or sys.__stderr__
    console.write("Timed out draining the log-lane test fixture's capture pipes\\n")
    console.flush()


atexit.register(_drain_capture)

logger.remove(0)
logger.add(sys.stderr, format="{{message}}")
for index in range(200):
    logger.info(f"{IMPORT_LOG} {{index}}")

{BOT_PY}
"""


def sh(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def logs(cid: str, timeout: float = 60.0) -> str:
    """Read all console output under a byte cap, without truncating old lines.

    Spool to temporary files so a capture loop cannot exhaust this check's
    memory before its size limit or timeout is checked. Both streams count
    toward the same limit, including a command that exits between polls.
    """
    command = ["docker", "logs", cid]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:

        def check_size():
            size = sum(os.fstat(stream.fileno()).st_size for stream in (stdout, stderr))
            assert size <= MAX_LOG_BYTES, (
                f"docker logs exceeded {MAX_LOG_BYTES} bytes for {cid}: {size} bytes"
            )

        proc = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                check_size()
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"docker logs timed out for {cid} after {timeout:g}s"
                time.sleep(min(0.01, remaining))
            check_size()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(MAX_LOG_BYTES + 1).decode("utf-8", errors="replace")
        error = stderr.read(MAX_LOG_BYTES + 1).decode("utf-8", errors="replace")
        assert proc.returncode == 0, (
            f"docker logs failed for {cid}: rc={proc.returncode}\n"
            f"stdout={output!r} stderr={error!r}"
        )
        return output + error


def wait_for(
    cid: str,
    marker: str,
    timeout: float = 60.0,
    log_dir: str | None = None,
    captured_marker: str | None = None,
) -> None:
    """Block until the container logs `marker`, so no check races startup."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        captured = captured_marker is None
        if log_dir:
            path = os.path.join(log_dir, "bot.jsonl")
            if os.path.exists(path):
                assert os.path.getsize(path) < MAX_LOG_BYTES, "structured log capture flooded"
                if captured_marker:
                    with open(path) as file:
                        content = file.read(MAX_LOG_BYTES + 1)
                    assert len(content) < MAX_LOG_BYTES, "structured log capture flooded"
                    # The last write may still be in progress. Only decode
                    # complete lines, and wait for the stderr pipe's barrier
                    # before signalling a process whose capture is asynchronous.
                    complete = content[: content.rfind("\n") + 1]
                    captured = any(
                        json.loads(line)["line"] == captured_marker
                        for line in complete.splitlines()
                    )
        if marker in logs(cid) and captured:
            return
        if sh(["docker", "inspect", "-f", "{{.State.Running}}", cid]).stdout.strip() != "true":
            raise AssertionError(f"container exited before {marker!r}:\n{logs(cid)}")
        time.sleep(0.1)
    raise AssertionError(f"container never logged {marker!r}:\n{logs(cid)}")


def sigterm_and_wait(cid: str) -> int | None:
    """SIGTERM the container and return its exit code, or None if it survived.

    Anything that stops us finding out — docker itself failing — raises instead,
    so an infrastructure blip is never reported as the startup-SIGTERM regression.
    """
    sh(["docker", "kill", "-s", "TERM", cid])
    try:
        # docker wait blocks until exit; a timeout means it survived.
        waited = sh(["docker", "wait", cid], timeout=15)
    except subprocess.TimeoutExpired:
        return None
    status = waited.stdout.strip()
    if not status.lstrip("-").isdigit():
        raise AssertionError(
            f"docker wait failed for {cid}: rc={waited.returncode} "
            f"stdout={waited.stdout!r} stderr={waited.stderr!r}"
        )
    return int(status)


def start(args: list[str]) -> str:
    cid = sh(["docker", "run", "-d", *args]).stdout.strip()
    if not cid:
        raise AssertionError(f"could not start container: docker run -d {' '.join(args)}")
    return cid


def run_script_and_sigterm(image: str, script: str) -> tuple[int | None, str]:
    """Run `script` under the image's ENTRYPOINT, SIGTERM it once up."""
    cid = start([image, "python", "-c", script])
    try:
        wait_for(cid, READY)
        return sigterm_and_wait(cid), logs(cid)
    finally:
        sh(["docker", "rm", "-f", cid])


def run_app_and_sigterm(
    image: str,
    *,
    ready_marker: str,
    import_delay: str,
    entrypoint: str | None = None,
    bot_source: str = BOT_PY,
    log_dir: str | None = None,
    captured_marker: str | None = None,
) -> tuple[int | None, str]:
    """Run the real app.py with a minimal bot.py, SIGTERM it at `ready_marker`."""
    with tempfile.TemporaryDirectory() as tmp:
        bot_path = os.path.join(tmp, "bot.py")
        with open(bot_path, "w") as f:
            f.write(bot_source)
        args = ["-v", f"{bot_path}:/app/bot.py:ro", "-e", f"IMPORT_DELAY={import_delay}"]
        if log_dir:
            args += [
                "-v",
                f"{log_dir}:/pcc-logs",
                "-e",
                "PCC_LOG_DIR=/pcc-logs",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=1m",
                "--log-opt",
                "max-file=2",
            ]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        args.append(image)
        if entrypoint:
            args.append("app.py")
        cid = start(args)
        try:
            wait_for(cid, ready_marker, log_dir=log_dir, captured_marker=captured_marker)
            return sigterm_and_wait(cid), logs(cid)
        finally:
            sh(["docker", "rm", "-f", cid])


def check_python_is_not_pid1(image: str) -> None:
    r = sh(["docker", "run", "--rm", image, "python", "-c", "import os; print(os.getpid())"])
    pid = r.stdout.strip()
    assert pid.isdigit(), f"could not read pid from image: {r.stdout!r} {r.stderr!r}"
    assert pid != "1", (
        "Python is running as PID 1. The kernel discards unhandled signals to PID 1, "
        "so a SIGTERM arriving before the handler is installed would be silently "
        "dropped and the container would keep serving on a deleted pod. "
        "Check the ENTRYPOINT still runs the command under tini."
    )
    print(f"  ok: python runs as pid {pid}, not pid 1")


def check_unhandled_sigterm_terminates_as_clean_exit(image: str) -> None:
    code, out = run_script_and_sigterm(image, NO_HANDLER)
    assert code is not None, (
        "container SURVIVED a SIGTERM sent while no handler was installed. This is "
        "the startup-SIGTERM regression: the process would go on to start and serve on a "
        f"deleted pod until the grace deadline.\n{out}"
    )
    assert code == 0, (
        f"expected exit 0, got {code}. tini's `-e 143` remaps death-by-SIGTERM to a "
        "clean exit so a scale-in still reads Completed / Succeeded, exactly as it "
        f"did when Python was PID 1 and swallowed uvicorn's re-raise.\n{out}"
    )
    print("  ok: unhandled SIGTERM terminated the container, reported as exit 0")


def check_handled_sigterm_reaches_python(image: str) -> None:
    code, out = run_script_and_sigterm(image, WITH_HANDLER)
    assert code is not None, f"container survived SIGTERM with a handler installed\n{out}"
    assert HANDLED in out, (
        "SIGTERM did not reach Python's own handler. tini must forward signals to "
        f"its child, or uvicorn's graceful drain never runs.\n{out}"
    )
    assert code == 0, f"expected a clean exit through the handler, got {code}\n{out}"
    print("  ok: SIGTERM reached Python's handler and exited cleanly")


def check_early_sigterm_under_tini(image: str) -> None:
    code, out = run_app_and_sigterm(image, ready_marker=IMPORTING, import_delay="60")
    assert code is not None, f"app.py survived a SIGTERM during bot import\n{out}"
    assert code == 0, f"expected exit 0 from the early handler, got {code}\n{out}"
    assert EARLY_EXIT in out, f"the early handler did not run; something else exited\n{out}"
    assert STARTED not in out, f"the server started after the SIGTERM\n{out}"
    print("  ok: SIGTERM during bot import exited via the early handler (under tini)")


def check_early_sigterm_with_python_as_pid1(image: str) -> None:
    """Bypass tini so Python is PID 1 — a derived image that replaced the
    ENTRYPOINT. Without pcc_early_sigterm this is the original bug verbatim."""
    code, out = run_app_and_sigterm(
        image, ready_marker=IMPORTING, import_delay="60", entrypoint="python"
    )
    assert code is not None, (
        "app.py as PID 1 SURVIVED a SIGTERM during bot import. pcc_early_sigterm "
        f"must be installed before any other import in app.py.\n{out}"
    )
    assert code == 0, f"expected exit 0 from the early handler, got {code}\n{out}"
    assert EARLY_EXIT in out, f"the early handler did not run\n{out}"
    print("  ok: SIGTERM during bot import exited via the early handler (python as PID 1)")


def check_graceful_shutdown_after_startup(image: str) -> None:
    """Once uvicorn is serving, a SIGTERM drains through it; the early handler
    then absorbs uvicorn's post-drain re-raise so the process exits normally,
    with exit 0, rather than dying from the signal on the spot."""
    code, out = run_app_and_sigterm(image, ready_marker=STARTED, import_delay="0")
    assert code is not None, f"app.py survived a SIGTERM after startup\n{out}"
    assert SHUTDOWN in out, f"uvicorn's shutdown did not run; the drain path is broken\n{out}"
    assert EARLY_EXIT not in out, (
        f"the early handler exited after startup; defer_to_server() is missing or "
        f"ineffective, or uvicorn's should_exit was not consulted\n{out}"
    )
    assert code == 0, f"expected exit 0 after a graceful shutdown, got {code}\n{out}"
    print("  ok: SIGTERM after startup drained through uvicorn and exited 0")


def _check_log_lane(image: str, *, during_import: bool) -> None:
    with tempfile.TemporaryDirectory() as log_dir:
        code, out = run_app_and_sigterm(
            image,
            ready_marker=IMPORTING if during_import else STARTED,
            import_delay="60" if during_import else "0",
            bot_source=LOGGING_BOT_PY,
            log_dir=log_dir,
            captured_marker=f"{IMPORT_LOG} 199",
        )
        assert code == 0, f"log-lane agent did not exit cleanly: {code}\n{out}"
        assert len(out.encode()) < MAX_LOG_BYTES, "console capture flooded"
        with open(os.path.join(log_dir, "bot.jsonl")) as file:
            content = file.read(MAX_LOG_BYTES + 1)
        assert len(content) < MAX_LOG_BYTES, "structured log capture flooded"
        records = [json.loads(line) for line in content.splitlines()]
        imported = [record for record in records if IMPORT_LOG in record["line"]]
        assert [record["line"] for record in imported] == [
            f"{IMPORT_LOG} {index}" for index in range(200)
        ], "import logs were lost or duplicated"
        assert all(record["stream"] == "stderr" and "level" not in record for record in imported)
        assert [line for line in out.splitlines() if IMPORT_LOG in line] == [
            f"{IMPORT_LOG} {index}" for index in range(200)
        ], "console import logs were lost or duplicated"
        if during_import:
            assert STARTED not in out, f"server started after early SIGTERM\n{out}"
            exits = [record for record in records if EARLY_EXIT in record["line"]]
            assert len(exits) == 1, "early shutdown record was lost or duplicated"
            assert exits[0]["stream"] == "app" and exits[0]["level"] == "WARNING"
        else:
            assert SHUTDOWN in out, f"log-lane agent did not drain through uvicorn\n{out}"
            assert EARLY_EXIT not in out, f"early handler ran after startup\n{out}"
            for stream, marker in zip(("stdout", "stderr"), DRAIN_MARKERS):
                drained = [record for record in records if record["line"] == marker]
                assert len(drained) == 1, f"{stream} capture did not drain once at normal exit"
                assert drained[0]["stream"] == stream and "level" not in drained[0]
                assert out.splitlines().count(marker) == 1, (
                    f"{stream} drain marker was not forwarded"
                )


def check_log_lane_during_import(image: str) -> None:
    _check_log_lane(image, during_import=True)
    print("  ok: log-lane import handlers emitted once and early SIGTERM exited 0")


def check_log_lane_after_startup(image: str) -> None:
    _check_log_lane(image, during_import=False)
    print("  ok: log-lane import handlers emitted once and uvicorn drained on SIGTERM")


def main() -> int:
    image = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMAGE
    print(f"container signal checks against {image}")
    failures = []
    for check in (
        check_python_is_not_pid1,
        check_unhandled_sigterm_terminates_as_clean_exit,
        check_handled_sigterm_reaches_python,
        check_early_sigterm_under_tini,
        check_early_sigterm_with_python_as_pid1,
        check_graceful_shutdown_after_startup,
        check_log_lane_during_import,
        check_log_lane_after_startup,
    ):
        try:
            check(image)
        except AssertionError as exc:
            failures.append(f"{check.__name__}: {exc}")
            print(f"  FAIL: {check.__name__}")
    if failures:
        print("\n" + "\n\n".join(failures))
        return 1
    print("all container signal checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
