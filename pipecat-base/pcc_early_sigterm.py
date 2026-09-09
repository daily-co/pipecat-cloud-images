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

When it does exit, it leaves a record where the platform will look for one: a
line on the container's stderr and, when the structured log lane is on, a
WARNING record in $PCC_LOG_DIR/bot.jsonl. That record is written with os.write
and never through loguru — its handler lock is not reentrant, and the frame
this signal interrupted may already hold it. The handler also waits one pump
beat first, so whatever the bot module printed during its import reaches the
capture lane before the process is gone.

The exit is os._exit rather than SystemExit: the signal can land inside the
customer's import, and an import-time ``except BaseException`` would swallow
SystemExit and carry on starting. It exits 0, matching the ENTRYPOINT's
``-e 143`` remap — being asked to stop is not an error, before or after startup.

A bot module, or an SDK it imports, that installs its own SIGTERM handler at
import time replaces this one — signal.signal replaces, it does not chain —
and from then on owns the window. defer_to_server() deliberately leaves such a
handler alone, because uvicorn saves and restores whatever it finds. Nothing in
pipecat or the pipecatcloud SDK does this at import time.
"""

import json
import os
import signal
import sys
import time
from datetime import datetime

_MESSAGE = "SIGTERM received before the server was up; nothing to drain, exiting"

# One beat of pcc_structured_logs' pump threads: the capture lane's documented
# best-effort hop between a print() and the JSONL file (PCC-1038).
_PUMP_BEAT_SECONDS = 0.05

# Set by defer_to_server(): returns True once the server has taken a SIGTERM
# itself. None until then, so any SIGTERM exits.
_server_handled = None


def _structured_logs():
    return sys.modules.get("pcc_structured_logs")


def _console():
    # Once pcc_structured_logs has captured the process's stderr, fd 2 is its
    # pipe and a line written there would die with us in the pump thread. Its
    # saved console stream is the real stderr; fall back to sys.stderr when the
    # capture is not installed.
    logs = _structured_logs()
    stream = logs.console_stream() if logs is not None else None
    return stream or sys.stderr


def _record_in_structured_lane():
    """Append the exit to bot.jsonl as a framework-lane record, in the shape
    pcc_structured_logs._serialize emits. No Python-level lock is taken."""
    log_dir = os.environ.get("PCC_LOG_DIR")
    if not log_dir:
        return
    logs = _structured_logs()
    name = getattr(logs, "_LOG_FILE_NAME", "bot.jsonl")
    payload = {
        "@timestamp": datetime.now().astimezone().isoformat(),
        "stream": "app",
        "level": "WARNING",
        "line": _MESSAGE,
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(os.path.join(log_dir, name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _on_sigterm(signum, frame):
    if _server_handled is not None and _server_handled():
        # uvicorn's post-drain re-raise. Absorb it, as PID 1 did, so run()
        # returns and the interpreter exits normally.
        return
    logs = _structured_logs()
    if logs is not None and logs.console_stream() is not None:
        time.sleep(_PUMP_BEAT_SECONDS)
    try:
        _record_in_structured_lane()
    except Exception:
        pass
    try:
        stream = _console()
        stream.write(_MESSAGE + "\n")
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
