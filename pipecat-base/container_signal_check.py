#!/usr/bin/env python3
"""Container-level check that the base image handles SIGTERM during startup.

PCC-1004. The kernel does not apply default signal dispositions to PID 1: a
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


def sh(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def logs(cid: str) -> str:
    r = sh(["docker", "logs", cid])
    return r.stdout + r.stderr


def wait_for(cid: str, marker: str, timeout: float = 60.0) -> None:
    """Block until the container logs `marker`, so no check races startup."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker in logs(cid):
            return
        if sh(["docker", "inspect", "-f", "{{.State.Running}}", cid]).stdout.strip() != "true":
            raise AssertionError(f"container exited before {marker!r}:\n{logs(cid)}")
        time.sleep(0.1)
    raise AssertionError(f"container never logged {marker!r}:\n{logs(cid)}")


def sigterm_and_wait(cid: str) -> int | None:
    """SIGTERM the container and return its exit code, or None if it survived.

    Anything that stops us finding out — docker itself failing — raises instead,
    so an infrastructure blip is never reported as the PCC-1004 regression.
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
    image: str, *, ready_marker: str, import_delay: str, entrypoint: str | None = None
) -> tuple[int | None, str]:
    """Run the real app.py with a minimal bot.py, SIGTERM it at `ready_marker`."""
    with tempfile.TemporaryDirectory() as tmp:
        bot_path = os.path.join(tmp, "bot.py")
        with open(bot_path, "w") as f:
            f.write(BOT_PY)
        args = ["-v", f"{bot_path}:/app/bot.py:ro", "-e", f"IMPORT_DELAY={import_delay}"]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        args.append(image)
        if entrypoint:
            args.append("app.py")
        cid = start(args)
        try:
            wait_for(cid, ready_marker)
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
        "dropped and the container would keep serving on a deleted pod (PCC-1004). "
        "Check the ENTRYPOINT still runs the command under tini."
    )
    print(f"  ok: python runs as pid {pid}, not pid 1")


def check_unhandled_sigterm_terminates_as_clean_exit(image: str) -> None:
    code, out = run_script_and_sigterm(image, NO_HANDLER)
    assert code is not None, (
        "container SURVIVED a SIGTERM sent while no handler was installed. This is "
        "the PCC-1004 regression: the process would go on to start and serve on a "
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
