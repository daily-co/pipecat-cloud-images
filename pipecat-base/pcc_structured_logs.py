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
  they saw before) and write each captured line directly to the JSONL writer.
  Because the customer's bot runs inside this
  process, this captures raw ``print()`` output and C-extension writes too.

Record schema (one JSON object per line; the shipper adds pod-constant fields
like namespace/service/deployment):

    @timestamp  RFC 3339 emit time
    stream      "app" (framework logger) | "stdout" | "stderr"
    level       loguru level name (app lane only; raw streams have no level)
    session_id  active session (omitted when none) — app-lane lines carry the
                loguru context value; captured lines are stamped with the
                module-level current session, the latest to start on the pod
    line        the log line body

Capture never dispatches through loguru, so customer console handlers cannot
feed it back into its own pipes. ``install()`` leaves those handlers alone:
import-time logs reach the file as raw console output. After bot import,
app.py adds the structured framework sink and renders its console output to
``console_stream()`` (the saved real stderr), avoiding a second captured copy.
Additional customer console handlers produce their own raw records.

Crash durability: after app.py configures framework logging, its file sink
writes and flushes synchronously into the OS page cache, so framework-lane
lines survive a hard process death (``os._exit``, SIGKILL) the instant the
``logger.*`` call returns. Capture-lane lines, including import-time console
logging, have one unavoidable async hop —
the pump thread must read the pipe before the process dies — so a ``print()``
in the final microseconds before a hard exit is best-effort. Bots that want
guaranteed dying words should use the configured framework logger.
"""

import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from os import environ

# Emission cap per captured line; passthrough is unaffected (raw bytes are
# always forwarded verbatim).
_MAX_LINE_BYTES = 16 * 1024

_LOG_FILE_NAME = "bot.jsonl"
_ROTATION_BYTES = 64_000_000  # safety valve; the shipper tails continuously
_RETENTION = 1  # rotated files to keep

_log_dir = environ.get("PCC_LOG_DIR")
_installed = False
_console_stream = None  # TextIO on the saved real stderr, once installed
_current_session_id = None  # set by run_bot; the latest session to start on this pod
_clear_timer = None
_writer = None
_writer_lock = threading.Lock()

# The session the running code belongs to. A websocket pod can take its next
# session while the last one is still finishing, so the slot above can name either.
_session_context: ContextVar[str | None] = ContextVar("pcc_session", default=None)

# What the platform set, read as this module loads. app.py imports it before the
# customer's bot, so a bot calling load_dotenv(override=True) can't loosen it.
_platform_exclude_content = environ.get("PCC_EXCLUDE_CONTENT", "")

# How long the session slot lingers after a session ends. The pump threads
# emit captured lines asynchronously, so output written in a session's final
# instants (a crashing bot's last words — the case that matters most) is still
# in the pipe when the session exits; an immediate clear would strip its
# attribution. A new session cancels the linger and takes over immediately.
_SESSION_LINGER_SECONDS = 0.25


def enabled() -> bool:
    """True when PCC_LOG_DIR is set and structured emission is in effect."""
    return bool(_log_dir)


def set_current_session(session_id):
    """Record the latest session to start on this pod (None to clear).

    Captured stdout/stderr lines are attributed to this session. They arrive with
    no context to say otherwise, so while one session finishes and the next starts
    they go to the next.

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


def current_session():
    """The session a line or a record belongs to, or None between sessions.

    Code running for a session names that session, even once a newer one has
    started on the pod. Anything else gets the slot `set_current_session` keeps.
    """
    return _session_context.get() or _current_session_id


def platform_exclude_content():
    """What the platform set `PCC_EXCLUDE_CONTENT` to when this process started."""
    return _platform_exclude_content


@contextmanager
def session_scope(session_id):
    """Attribute output and records to *session_id* for the block's span.

    Stack this alongside ``logger.contextualize`` in run_bot; the finally
    guarantees the slot clears on every exit path (including early returns),
    unless a newer session has taken it.
    """
    token = _session_context.set(session_id)
    set_current_session(session_id)
    try:
        yield
    finally:
        if _current_session_id == session_id:
            set_current_session(None)
        _session_context.reset(token)


def console_stream():
    """The real stderr stream, bypassing capture for framework console output.

    Using this for the framework's console sink avoids capturing a second
    copy of its structured app records. Customer handlers may still use
    ``sys.stderr`` safely. Returns None before capture is installed.
    """
    return _console_stream


def console_filter(record) -> bool:
    """Keep explicitly tagged raw records off the framework console sink.

    Capture writes directly to the file now, so it never reaches this filter.
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
        # Never let serialization break logging; emit a marker record instead
        # — one shaped like every other record, so the query that
        # is actually opened can find it. A @timestamp: a store that stamps
        # entries with the record time — Cloud Logging — would otherwise file
        # this line at the moment it was SHIPPED, hours late after a backlog
        # restart and outside any time-range query, and a checkpoint-less
        # re-read would not dedup it. The current session: every store kind
        # filters a session query on session_id, so a marker without one is
        # unreachable from the per-session view no matter what its timestamp
        # says; the module slot is what the raw lane stamps from, and nothing
        # about the malformed record can make it unreadable. A level: the
        # record's own is one of the things that may have raised, but a line
        # that could not be serialised is an error, and without one a store
        # that maps level to severity files it BELOW debug.
        marker = {
            "@timestamp": datetime.now().astimezone().isoformat(),
            "stream": "app",
            "level": "ERROR",
        }
        if _current_session_id:
            marker["session_id"] = _current_session_id
        marker["line"] = "<pcc_structured_logs: serialization failed>"
        return json.dumps(marker)


def write_record(level: str, line: str) -> None:
    """Append one app-lane record to bot.jsonl without going through loguru.

    For a line that must reach the lane whatever loguru's handlers are at that
    moment: a bot module may have replaced them, and before ``install()`` the
    file sink does not exist yet. One ``os.write`` of one JSON line, no
    Python-level lock. No-op when the lane is off; an unwritable directory is
    ignored.
    """
    if not _log_dir:
        return
    payload = {
        "@timestamp": datetime.now().astimezone().isoformat(),
        "stream": "app",
        "level": level,
    }
    if _current_session_id:
        payload["session_id"] = _current_session_id
    payload["line"] = line
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8", errors="backslashreplace"
    )
    try:
        os.makedirs(_log_dir, exist_ok=True)
        fd = os.open(
            os.path.join(_log_dir, _LOG_FILE_NAME), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass


def _report_file_error(error):
    # Never report a writer failure through a captured descriptor or loguru:
    # that diagnostic would itself be captured and fail to write again.
    try:
        stream = _console_stream or sys.stderr
        stream.write(
            f"pcc_structured_logs: cannot write log file in {_log_dir!r}: {error}; "
            "structured file emission disabled\n"
        )
        stream.flush()
    except Exception:
        pass


class _JsonlWriter(RotatingFileHandler):
    """One synchronous, rotating writer shared by app and capture threads.

    This handler is not attached to the standard-library logger hierarchy.
    Loguru only owns its adapter, so removing Loguru handlers cannot close
    this writer or stop capture. Emergency exit records deliberately bypass
    its lock (see write_record and pcc_early_sigterm).
    """

    def __init__(self, path):
        super().__init__(
            path,
            maxBytes=_ROTATION_BYTES,
            backupCount=_RETENTION,
            encoding="utf-8",
            errors="backslashreplace",
        )
        self._failed = False

    def write_line(self, serialized: str):
        record = logging.LogRecord("pcc_structured_logs", 0, "", 0, serialized, (), None)
        # handle() serializes rotation, writes and flushes across both pumps
        # and every framework producer. Calling emit() directly would not.
        self.handle(record)

    def emit(self, record):
        if not self._failed:
            super().emit(record)

    def handleError(self, record):
        # Called with the handler lock held. Stop file emission on failure,
        # while pumps keep forwarding the original bytes to the real console.
        self._failed = True
        _report_file_error(sys.exc_info()[1])


def _get_writer():
    global _writer
    if not _log_dir:
        return None
    with _writer_lock:
        if _writer is None:
            try:
                os.makedirs(_log_dir, exist_ok=True)
                _writer = _JsonlWriter(os.path.join(_log_dir, _LOG_FILE_NAME))
            except OSError as error:
                _report_file_error(error)
        return _writer


def add_file_sink(target_logger, level: str):
    """Add the JSONL file sink to *target_logger*; no-op when disabled.

    ``level`` applies to the app lane only — captured raw lines have no level
    and always pass. Returns the sink id, or None when disabled/failed.
    """
    writer = _get_writer()
    if writer is None:
        return None
    try:
        min_level_no = target_logger.level(level).no
    except ValueError:
        min_level_no = 0

    def _file_filter(record) -> bool:
        if "pcc_stream" in record["extra"]:
            return True
        return record["level"].no >= min_level_no

    return target_logger.add(
        lambda message: writer.write_line(_serialize(message.record)),
        format="{message}",
        filter=_file_filter,
        level=0,
        # The shared writer flushes on the emitting thread, preserving dying
        # words even when os._exit/SIGKILL immediately follows logger.*().
        enqueue=False,
    )


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


def _pump(read_fd: int, passthrough_fd: int, stream_name: str, writer):
    assembler = _LineAssembler()
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
            writer.write_line(
                _serialize(
                    {
                        "time": datetime.now().astimezone(),
                        "extra": {"pcc_stream": stream_name},
                        "message": line,
                    }
                )
            )


def install():
    """Capture stdout/stderr without changing the customer's loguru handlers.

    Must run before the customer's bot module is imported (it may print at
    import time). No-op when PCC_LOG_DIR is unset or already installed.

    Import-time logging is captured as raw console output, preserving the
    default handler for bot modules that remove it themselves. Framework
    structured logging is configured by app.py after the bot module imports.
    """
    global _installed, _console_stream
    if not _log_dir or _installed:
        return
    writer = _get_writer()
    if writer is None:
        return
    _installed = True

    # Save the real streams before capturing. One dup for byte passthrough,
    # one wrapped for loguru console sinks.
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    _console_stream = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")

    for fd, saved_fd, name in ((1, saved_stdout, "stdout"), (2, saved_stderr, "stderr")):
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, fd)
        os.close(write_fd)
        threading.Thread(
            target=_pump,
            args=(read_fd, saved_fd, name, writer),
            name=f"pcc-log-pump-{name}",
            daemon=True,
        ).start()
