"""The pipecat-ai this image can serve: startup checks, and building session arguments.

The image does not install pipecat-ai; the agent image built on it does, at
whatever release its author pins, and it may pin its own pipecatcloud too. The
image meets them in one place: the session arguments it hands ``bot()`` for
every session, whose types ``pipecatcloud.agent`` defines on top of
pipecat-ai's runner arguments. app.py checks them in two steps, so an image
that could never serve a session fails its deploy with the reason instead of
failing every session behind healthy probes:

- ``check_pipecat()``, before the bot module is imported: pipecat-ai is
  installed and has runner arguments (0.0.77 and newer). It imports only
  pipecat-ai, which leaves loguru alone, so the bot module finds logging as
  it always has.
- ``check_session_types()``, right after the bot module is imported: each
  session type builds, the way the handlers build it. Importing pipecatcloud
  replaces loguru's handlers (its ``__init__`` removes them all and adds one
  on stderr), which a bot module that removes the default handler at import
  would trip over, so this waits until the bot module has run. An optional
  type (SmallWebRTC's) that does not build refuses nothing: app.py serves
  without it, as it does when the type is absent, and logs why.

Both decide by what works, never by version number: pipecat-ai 0.0.77 builds
on pipecatcloud 1.x but not on 0.4.4 (the Python 3.10 image's), and a fork or
a build from source has whatever version it has. Versions appear in messages
only.

``build()`` is how every handler builds session arguments. pipecat-ai added
``body`` to its base runner arguments in 0.0.91; before that, passing it to
the constructor fails for most of the types, so there ``build()`` sets it after
construction instead, the way ``flow_config`` is attached. A later release will
drop that fallback, so ``check_session_types()`` reports a deprecation warning
for app.py to log once logging is set up: for a pipecat-ai older than 0.0.91,
and for a pipecatcloud older than 0.2.1, whose own WebSocket type takes no
``body`` whatever pipecat-ai is.

A refusal is a single line, written where it will be read, without loguru, whose
handlers a bot module may have replaced by then:

- ``/dev/termination-log``, where the kubelet takes a container's final
  message from. The platform shows it as the agent's health message, so it is
  kept short, and free of square brackets, which the CLI reads as markup.
- the container's stderr, the real one: ``pcc_structured_logs``' saved stream
  once its capture is installed, whose pipe a line written just before exiting
  could otherwise die in.
- with the log lane on, one record in its file.

It then exits 78, a configuration error: distinct from 1, a crashing
``bot.py``, and never 0 or 143, which the image's ``tini -e 143`` reports as a
clean stop. The exit is ``os._exit``, as in pcc_early_sigterm: by the second
check the bot module has run, and a non-daemon thread it started would hold a
``SystemExit`` at interpreter shutdown for good.
"""

import dataclasses
import importlib
import importlib.metadata
import importlib.util
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, NoReturn, Optional

import pcc_structured_logs

# The oldest pipecat-ai this image supports: the first release every session
# type builds on, with either pipecatcloud the image ships.
MINIMUM_VERSION = "0.0.78"

# The first release whose runner arguments take ``body``. Older ones work
# through build()'s fallback, deprecated; a later release will require this.
BODY_VERSION = "0.0.91"

# The first pipecatcloud whose session types all take ``body``, given
# pipecat-ai BODY_VERSION: from it they are built on pipecat-ai's runner
# arguments, or declare it themselves.
PIPECATCLOUD_BODY_VERSION = "0.2.1"

EXIT_CODE = 78

# Tests point this elsewhere; in a pod it is the kubelet's default path, the
# one the operator configures for the user container.
TERMINATION_LOG_ENV = "PCC_TERMINATION_LOG_PATH"
_DEFAULT_TERMINATION_LOG = "/dev/termination-log"

# The kubelet keeps the first 4096 bytes. The message is far shorter; this only
# bounds an error text quoted into it.
_MAX_DETAIL_CHARS = 300

# One beat of pcc_structured_logs' pump threads, so what the bot module printed
# while importing reaches the capture lane before a refusal ends the process.
_PUMP_BEAT_SECONDS = 0.05

# The runner types pipecatcloud's session types are built on. All four arrived
# together with their module in pipecat-ai 0.0.77, so a release without the
# module is too old, and one whose module lacks a type is not: it is later, or
# a fork.
_RUNNER_TYPES = (
    "RunnerArguments",
    "DailyRunnerArguments",
    "WebSocketRunnerArguments",
    "SmallWebRTCRunnerArguments",
)

# Stands in for the connection objects a real session passes.
_PROBE = object()

# The session types the handlers build, whether app.py cannot start without
# them, and the fields each handler passes besides ``body``, with values that
# pass a presence check: pipecat-ai validates some of its runner arguments in
# ``__post_init__`` (1.12.0's MoQ arguments check their dial target), and the
# check must fail only where a real session's build would, on a structural
# fault. SmallWebRTC is imported only when that feature is on, which needs
# pipecatcloud 0.2.5 or newer; an agent image pinning an older one, or one
# whose type does not build, runs without it.
_SESSION_TYPES = (
    ("DailySessionArguments", True, {"room_url": "https://probe.invalid/", "token": "probe"}),
    ("PipecatSessionArguments", True, {}),
    ("WebSocketSessionArguments", True, {"websocket": _PROBE}),
    ("SmallWebRTCSessionArguments", False, {"webrtc_connection": _PROBE}),
)

# Tests replace this; see the module docstring for why it is not SystemExit.
_exit: Callable[[int], NoReturn] = os._exit


def takes_body(cls: type) -> bool:
    """True when ``cls`` accepts ``body`` as a constructor argument."""
    return dataclasses.is_dataclass(cls) and any(
        f.name == "body" and f.init for f in dataclasses.fields(cls)
    )


def build(cls: type, *, body: Any, **fields: Any):
    """Build session arguments of type ``cls`` carrying ``body``.

    ``body`` goes to the constructor when the type accepts it, and is set
    after construction when it does not (pipecat-ai older than 0.0.91, and the
    WebSocket type of pipecatcloud older than 0.2.1), so the bot reads
    ``runner_args.body`` the same way on either.
    """
    if takes_body(cls):
        return cls(body=body, **fields)
    args = cls(**fields)
    args.body = body
    return args


def preload_pipecatcloud() -> None:
    """Import pipecatcloud, for pcc_structured_logs.install() to run just
    before its capture starts.

    pipecatcloud's import replaces loguru's handlers with one on sys.stderr,
    and once the capture is installed that is the capture's own pipe, whose
    lines are logged again, into the same pipe: the lane floods, and can fill
    the pipe and stall startup. Imported first, its handler is among those the
    capture replaces, and every later import finds it loaded. A failure is
    left for the checks to report.
    """
    try:
        importlib.import_module("pipecatcloud")
    except Exception:
        pass


def check_pipecat() -> None:
    """Refuse to start unless pipecat-ai is installed and has runner arguments.

    Ends the process on a refusal. A fault in the check itself is reported and
    startup goes on: refusing on a bug here would stop agents that can serve,
    which the check exists to spare.
    """
    try:
        _check_pipecat(importlib.util.find_spec, importlib.import_module, _installed_version)
    except Exception as e:
        _warn(f"The pipecat-ai check did not run ({_describe(e)}).")


@dataclasses.dataclass
class SessionTypes:
    """What check_session_types() found, for app.py to act on.

    ``warnings`` are for app.py to log once logging is set up. ``unbuildable``
    maps each optional session type that is present but does not build to why;
    app.py serves without it, as it would if the type were absent.
    """

    warnings: List[str] = dataclasses.field(default_factory=list)
    unbuildable: Dict[str, str] = dataclasses.field(default_factory=dict)


def check_session_types() -> SessionTypes:
    """Refuse to start unless each required session type the handlers build builds.

    Ends the process on a refusal. A fault in the check itself is reported and
    startup goes on.
    """
    try:
        return _check_session_types(importlib.import_module, _installed_version)
    except Exception as e:
        _warn(f"The session arguments check did not run ({_describe(e)}).")
        return SessionTypes()


def _check_pipecat(
    find_spec: Callable[[str], Any],
    import_module: Callable[[str], Any],
    installed_version: Callable[[str], Optional[str]],
) -> None:
    pipecat = _label("pipecat-ai", installed_version("pipecat-ai"))
    too_old = (
        f"{pipecat} is too old for this image, which needs pipecat-ai {MINIMUM_VERSION} or "
        "newer: upgrade pipecat-ai in the agent image and deploy again."
    )
    broken = f"{pipecat} failed to import ({{}}): fix the agent image's dependencies."

    if find_spec("pipecat") is None:
        _refuse(
            f"pipecat-ai is not installed, and this image needs pipecat-ai {MINIMUM_VERSION} "
            "or newer: add it to the agent image and deploy again."
        )
    try:
        runner_types = import_module("pipecat.runner.types")
    except ImportError as e:
        if e.name in ("pipecat.runner", "pipecat.runner.types"):
            _refuse(too_old)
        _refuse(broken.format(_describe(e)))
    except Exception as e:
        _refuse(broken.format(_describe(e)))
    missing = [n for n in _RUNNER_TYPES if not isinstance(getattr(runner_types, n, None), type)]
    if missing:
        them = "them" if len(missing) > 1 else "it"
        _refuse(
            f"{pipecat} has no {', '.join(missing)} in pipecat.runner.types, which this image "
            f"builds session arguments on: pin a pipecat-ai release that provides {them}."
        )


def _check_session_types(
    import_module: Callable[[str], Any],
    installed_version: Callable[[str], Optional[str]],
) -> SessionTypes:
    pipecat_version = installed_version("pipecat-ai")
    pipecat = _label("pipecat-ai", pipecat_version)
    pipecatcloud = _label("pipecatcloud", installed_version("pipecatcloud"))
    supported = f"This image supports pipecat-ai {MINIMUM_VERSION} or newer."

    try:
        agent = import_module("pipecatcloud.agent")
    except Exception as e:
        _refuse(
            f"{pipecatcloud} cannot load its session types with {pipecat} "
            f"({_describe(e.__cause__ or e)}). {supported}"
        )
    runner_arguments = import_module("pipecat.runner.types").RunnerArguments

    report = SessionTypes()
    # Types that take no body although they are not built on pipecat-ai's
    # runner arguments: an old pipecatcloud's own, whatever pipecat-ai is.
    standalone_without_body = []
    for type_name, required, fields in _SESSION_TYPES:
        cls = getattr(agent, type_name, None)
        if not isinstance(cls, type):
            if required:
                _refuse(
                    f"{pipecatcloud} has no {type_name}, which this image hands bot(): pin a "
                    "pipecatcloud release that has it, or leave pipecatcloud to the image."
                )
            continue
        try:
            build(cls, body={}, session_id="probe", **fields)
        except Exception as e:
            reason = (
                f"{pipecatcloud} with {pipecat} cannot build the {type_name} this image "
                f"hands bot() ({_describe(e)})"
            )
            if required:
                _refuse(f"{reason}. {supported}")
            # An optional type that does not build is treated as absent: the
            # image serves its other transports rather than refusing them all.
            report.unbuildable[type_name] = _one_line(reason)
            report.warnings.append(
                _one_line(
                    f"{reason}: the image serves without it, and the transports that need it "
                    "are off."
                )
            )
            continue
        if not takes_body(cls) and not issubclass(cls, runner_arguments):
            standalone_without_body.append(type_name)

    if not takes_body(runner_arguments):
        subject = pipecat if pipecat_version else "The installed pipecat-ai"
        report.warnings.append(
            f"{subject} is older than {BODY_VERSION} and deprecated on this image, which "
            f"supports pipecat-ai {MINIMUM_VERSION} or newer: a future release will need "
            f"pipecat-ai {BODY_VERSION} or newer, so upgrade it in the agent image."
        )
    if standalone_without_body:
        types = " and ".join(standalone_without_body)
        verb = "does" if len(standalone_without_body) == 1 else "do"
        report.warnings.append(
            f"{pipecatcloud}'s {types} {verb} not take the request body, which is deprecated on "
            f"this image: a future release will need pipecatcloud {PIPECATCLOUD_BODY_VERSION} "
            "or newer, so upgrade it in the agent image, or leave pipecatcloud to the image."
        )
    return report


def _installed_version(distribution: str) -> Optional[str]:
    try:
        # None when a distribution's metadata is incomplete.
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _label(distribution: str, version: Optional[str]) -> str:
    return f"{distribution} {version}" if version else distribution


def _describe(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 3] + "..."
    return text


def _one_line(text: str) -> str:
    # The CLI shows only a health message's last line, and renders it as Rich
    # markup, in which a bracketed span disappears and a "[/" raises. A lone
    # surrogate, as an undecodable file name in an error carries, is escaped:
    # no destination could encode it.
    text = text.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return re.sub(r"\s+", " ", text.replace("[", "(").replace("]", ")")).strip()


def _write(level: str, line: str) -> None:
    """Write ``line`` to the real stderr and, with the log lane on, its file."""
    try:
        # The bot module may have replaced sys.stderr; __stderr__ is the process's.
        stream = pcc_structured_logs.console_stream() or sys.__stderr__ or sys.stderr
        stream.write(f"{level}: {line}\n")
        stream.flush()
    except Exception:
        pass
    try:
        pcc_structured_logs.write_record(level, line)
    except Exception:
        pass


def _warn(message: str) -> None:
    _write("WARNING", _one_line(message))


def _refuse(reason: str) -> NoReturn:
    line = _one_line(f"Refusing to start: {reason}")
    # What the bot module printed may still be in these buffers, which
    # os._exit does not flush (the process's own too: the bot module may have
    # replaced sys.stdout); once flushed into the capture, the pump threads
    # get a beat to take it to the lane.
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            stream.flush()
        except Exception:
            pass
    if pcc_structured_logs.console_stream() is not None:
        time.sleep(_PUMP_BEAT_SECONDS)
    try:
        with open(
            os.environ.get(TERMINATION_LOG_ENV, _DEFAULT_TERMINATION_LOG), "w", encoding="utf-8"
        ) as f:
            f.write(line)
    except Exception:
        pass  # not in a pod, or no termination log mounted
    _write("CRITICAL", line)
    _exit(EXIT_CODE)
