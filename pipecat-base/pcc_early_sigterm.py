"""Terminate on a SIGTERM that arrives before the server is up (PCC-1004).

uvicorn installs its SIGTERM handler inside Server.serve(), after every
module-level import in app.py — the customer's bot module included, whose
import time is unbounded. Until then the interpreter has no handler. Under the
image's own ENTRYPOINT that is fine: tini holds PID 1 and Python, as its child,
dies on the default disposition. But a derived image that sets its own
ENTRYPOINT puts Python back at PID 1, where the kernel discards unhandled
signals, and the container would ignore the kubelet and go on to serve on a
pod already marked for deletion.

install() covers that window with a handler that exits at once: nothing is
initialised yet, so there is nothing to drain. Right before the server starts,
defer_to_server() keeps the handler in place but tells it how to recognise a
SIGTERM the server has already dealt with.

That second job matters because of how uvicorn exits. Its capture_signals()
saves whatever handler it finds, restores it after the drain, and then
re-raises the signal so the process "dies from it". As PID 1 that re-raise was
silently discarded: run() returned and the interpreter exited normally, with
asyncio's teardown, atexit hooks and every tracing and logging flush along the
way. Under tini the re-raise would kill the process on the spot and skip all of
that. So the handler absorbs a SIGTERM the server reports it has already
handled (uvicorn's should_exit), reproducing the PID 1 exit path exactly, and
still exits at once for one the server has not — nothing is serving yet.

The exit is os._exit rather than SystemExit: the signal can land inside the
customer's import, and an import-time ``except BaseException`` would swallow
SystemExit and carry on starting. It exits 0, matching the ENTRYPOINT's
``-e 143`` remap — being asked to stop is not an error, before or after startup.
"""

import os
import signal
import sys

_MESSAGE = "SIGTERM received before the server was up; nothing to drain, exiting\n"

# Set by defer_to_server(): returns True once the server has taken a SIGTERM
# itself. None until then, so any SIGTERM exits.
_server_handled = None


def _console():
    # Once pcc_structured_logs has captured the process's stderr, fd 2 is its
    # pipe and a line written there would die with us in the pump thread. Its
    # saved console stream is the real stderr; fall back to sys.stderr when the
    # capture is not installed.
    logs = sys.modules.get("pcc_structured_logs")
    stream = logs.console_stream() if logs is not None else None
    return stream or sys.stderr


def _on_sigterm(signum, frame):
    if _server_handled is not None and _server_handled():
        # uvicorn's post-drain re-raise. Absorb it, as PID 1 did, so run()
        # returns and the interpreter exits normally.
        return
    try:
        stream = _console()
        stream.write(_MESSAGE)
        stream.flush()
    except Exception:
        pass
    os._exit(0)


def install():
    """Exit immediately on SIGTERM. Call before any other import in app.py."""
    signal.signal(signal.SIGTERM, _on_sigterm)


def defer_to_server(server_handled):
    """Call right before serving. ``server_handled()`` must return True once the
    server has handled a SIGTERM of its own (uvicorn: ``server.should_exit``);
    such a signal is then absorbed rather than acted on. The handler itself is
    left alone — a bot module may have installed its own at import time, and
    uvicorn saves and restores whatever it finds."""
    global _server_handled
    _server_handled = server_handled
