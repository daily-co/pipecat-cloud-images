"""Structured log emission for Pipecat Cloud bot pods.

Active only when the ``PCC_LOG_DIR`` environment variable is set (the PCC
operator sets it when a region's log-collection lane is enabled). Without it,
``install()`` and every other entry point are no-ops and the runner behaves
exactly as before.

Two lanes feed one JSONL file (``$PCC_LOG_DIR/bot.jsonl``), which an in-pod
shipper tails:

- **framework lane** — a loguru file sink serializes every ``logger.*`` call.
- **capture lane** — ``install()`` dup2's a pipe over the process's stdout and
  stderr file descriptors; pump threads write the original bytes through to
  the real streams (``kubectl logs`` and any node collector see exactly what
  they saw before) and emit each captured line through loguru tagged
  ``pcc_stream=stdout|stderr``. Because the customer's bot runs inside this
  process, this captures raw ``print()`` output and C-extension writes too.

Record schema (one JSON object per line; the shipper adds pod-constant fields
like namespace/service/deployment):

    @timestamp  RFC 3339 emit time
    stream      "app" (framework logger) | "stdout" | "stderr"
    level       loguru level name (app lane only; raw streams have no level)
    session_id  active session (omitted when none) — app-lane lines carry the
                loguru context value; captured lines are stamped with the
                module-level current session, which is exact because session
                concurrency is strictly 1 today
    line        the log line body

Recursion safety: a captured line is re-emitted through loguru, so no loguru
console sink may ever write to the *captured* descriptors — ``install()``
removes loguru's default sink before capturing, and every console sink must
use ``console_stream()`` (the saved real stderr) with ``console_filter``
(which also keeps captured lines from being displayed twice).
"""

import json
import os
import sys
import threading
from contextlib import contextmanager
from os import environ

from loguru import logger

# Emission cap per captured line; passthrough is unaffected (raw bytes are
# always forwarded verbatim).
_MAX_LINE_BYTES = 16 * 1024

_LOG_FILE_NAME = "bot.jsonl"
_ROTATION = "64 MB"  # safety valve; the shipper tails continuously
_RETENTION = 1  # rotated files to keep

_log_dir = environ.get("PCC_LOG_DIR")
_installed = False
_console_stream = None  # TextIO on the saved real stderr, once installed
_current_session_id = None  # set by run_bot; exact under enforced concurrency-1
_clear_timer = None

# How long the session slot lingers after a session ends. The pump threads
# emit captured lines asynchronously, so output written in a session's final
# instants (a crashing bot's last words — the case that matters most) is still
# in the pipe when the session exits; an immediate clear would strip its
# attribution. A new session cancels the linger and takes over immediately,
# so attribution can never bleed into the NEXT session.
_SESSION_LINGER_SECONDS = 0.25


def enabled() -> bool:
    """True when PCC_LOG_DIR is set and structured emission is in effect."""
    return bool(_log_dir)


def set_current_session(session_id):
    """Record the session owning the pod right now (None to clear).

    Captured stdout/stderr lines are attributed to this session. Session
    concurrency is strictly enforced at 1, so a single slot is exact; this
    must be revisited if concurrency ever becomes configurable.

    Clearing is deferred by a short linger (see _SESSION_LINGER_SECONDS) so
    in-flight captured output keeps its attribution; setting is immediate.
    """
    global _current_session_id, _clear_timer
    if _clear_timer is not None:
        _clear_timer.cancel()
        _clear_timer = None
    if session_id is None:

        def _clear():
            global _current_session_id, _clear_timer
            _current_session_id = None
            _clear_timer = None

        _clear_timer = threading.Timer(_SESSION_LINGER_SECONDS, _clear)
        _clear_timer.daemon = True
        _clear_timer.start()
    else:
        _current_session_id = session_id


@contextmanager
def session_scope(session_id):
    """Attribute captured stdout/stderr to *session_id* for the block's span.

    Stack this alongside ``logger.contextualize`` in run_bot; the finally
    guarantees the slot clears on every exit path (including early returns).
    """
    set_current_session(session_id)
    try:
        yield
    finally:
        set_current_session(None)


def console_stream():
    """The stream loguru console sinks must write to.

    After install() the process's real stderr fd is only reachable through
    this saved stream — writing to ``sys.stderr`` would loop console output
    back into the capture. Returns None when capture is not installed (use
    ``sys.stderr`` then).
    """
    return _console_stream


def console_filter(record) -> bool:
    """Keep captured lines off the console sink.

    The capture lane already wrote the original bytes through to the real
    stream; rendering the re-emitted record would display every captured line
    twice.
    """
    return "pcc_stream" not in record["extra"]


def _serialize(record) -> str:
    try:
        extra = record["extra"]
        stream = extra.get("pcc_stream", "app")
        payload = {"@timestamp": record["time"].isoformat(), "stream": stream}
        if stream == "app":
            payload["level"] = record["level"].name
            session_id = extra.get("session_id")
            if session_id and session_id != "NONE":
                payload["session_id"] = session_id
        else:
            if _current_session_id:
                payload["session_id"] = _current_session_id
        payload["line"] = record["message"]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        # Never let serialization break logging; emit a marker record instead.
        return json.dumps({"stream": "app", "line": "<pcc_structured_logs: serialization failed>"})


def _format_record(record) -> str:
    # Loguru custom-serialization recipe: stash the serialized payload on the
    # record and reference it from the (brace-safe) format template.
    record["extra"]["_pcc_serialized"] = _serialize(record)
    return "{extra[_pcc_serialized]}\n"


def add_file_sink(target_logger, level: str):
    """Add the JSONL file sink to *target_logger*; no-op when disabled.

    ``level`` applies to the app lane only — captured raw lines have no level
    and always pass. Returns the sink id, or None when disabled/failed.
    """
    if not _log_dir:
        return None
    try:
        min_level_no = target_logger.level(level).no
    except ValueError:
        min_level_no = 0

    def _file_filter(record) -> bool:
        if "pcc_stream" in record["extra"]:
            return True
        return record["level"].no >= min_level_no

    try:
        return target_logger.add(
            os.path.join(_log_dir, _LOG_FILE_NAME),
            format=_format_record,
            filter=_file_filter,
            level=0,
            rotation=_ROTATION,
            retention=_RETENTION,
            encoding="utf-8",
            enqueue=True,  # never block the event loop on file I/O
        )
    except OSError as e:
        (_console_stream or sys.stderr).write(
            f"pcc_structured_logs: cannot open log file in {_log_dir!r}: {e}; "
            "structured file emission disabled\n"
        )
        return None


class _LineAssembler:
    """Incremental byte-stream -> line splitter with an emission size cap.

    Overlong lines are emitted truncated once, then discarded until the next
    newline (the passthrough still carries the full original bytes).
    """

    def __init__(self, max_line_bytes: int = _MAX_LINE_BYTES):
        self._max = max_line_bytes
        self._buf = b""
        self._discarding = False

    def feed(self, chunk: bytes):
        lines = []
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl == -1:
                if self._discarding:
                    self._buf = b""
                elif len(self._buf) > self._max:
                    lines.append(self._decode(self._buf[: self._max]) + " ...[truncated]")
                    self._buf = b""
                    self._discarding = True
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            if self._discarding:
                # Tail of a line whose head was already emitted truncated.
                self._discarding = False
                continue
            if len(line) > self._max:
                line = line[: self._max]
            lines.append(self._decode(line))
        return lines

    @staticmethod
    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace").rstrip("\r")


def _write_all(fd: int, data: bytes):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pump(read_fd: int, passthrough_fd: int, stream_name: str):
    assembler = _LineAssembler()
    bound = logger.bind(pcc_stream=stream_name)
    while True:
        try:
            chunk = os.read(read_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        try:
            _write_all(passthrough_fd, chunk)
        except OSError:
            pass  # keep emitting even if the real stream is gone
        for line in assembler.feed(chunk):
            bound.info(line)


def install():
    """Capture stdout/stderr and route loguru off the captured descriptors.

    Must run before the customer's bot module is imported (it may print at
    import time). No-op when PCC_LOG_DIR is unset or already installed.
    """
    global _installed, _console_stream
    if not _log_dir or _installed:
        return
    try:
        os.makedirs(_log_dir, exist_ok=True)
    except OSError as e:
        sys.stderr.write(
            f"pcc_structured_logs: cannot create PCC_LOG_DIR {_log_dir!r}: {e}; "
            "structured log emission disabled\n"
        )
        return
    _installed = True

    # Save the real streams before capturing. One dup for byte passthrough,
    # one wrapped for loguru console sinks.
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    _console_stream = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")

    # Loguru's default sink writes to sys.stderr, which is about to become a
    # captured pipe — that would echo every captured line back into the
    # capture (unbounded recursion). Replace it with a bootstrap console sink
    # on the saved stream + the file sink, so lines emitted before app.py's
    # own logger setup (e.g. during bot import) are visible and shipped.
    # app.py's logger.remove()/add() later replaces both, preserving today's
    # sink-reset semantics.
    logger.remove()
    logger.add(
        _console_stream,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        level=environ.get("PIPECAT_LOG_LEVEL", "DEBUG").upper(),
        filter=console_filter,
    )
    add_file_sink(logger, environ.get("PIPECAT_LOG_LEVEL", "DEBUG").upper())

    for fd, saved_fd, name in ((1, saved_stdout, "stdout"), (2, saved_stderr, "stderr")):
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, fd)
        os.close(write_fd)
        threading.Thread(
            target=_pump,
            args=(read_fd, saved_fd, name),
            name=f"pcc-log-pump-{name}",
            daemon=True,
        ).start()
