"""pcc_pipecat_compat: which pipecat-ai the image can serve, and building session arguments.

The decisions are tested in-process with the lookups injected. What happens to
the *process* at startup — exit code, the line on stderr, the termination
message, the structured-lane record, that the bot module never ran, and how
loguru is left for it — is tested in child interpreters importing app, as in
test_early_sigterm, against a stub pipecat-ai placed ahead of the installed one
on PYTHONPATH, or with none at all.

The CI matrix runs this module at pipecat-ai 0.0.78 (build()'s fallback), 0.0.91
(the first release without it) and the latest, so the tests against the
installed pipecat-ai cover both paths for real.
"""

import dataclasses
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional

import pcc_pipecat_compat
import pcc_structured_logs
import pytest

PIPECAT_BASE_DIR = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------
# Stand-ins for pipecat.runner.types and pipecatcloud.agent
# ------------------------------------------------------------
def _modules(
    *,
    with_body: bool = True,
    related: bool = True,
    smallwebrtc: bool = True,
    post_init_error: Optional[Exception] = None,
):
    """with_body: runner arguments declare ``body`` (pipecat-ai 0.0.91+).
    related: session types are built on the runner types (pipecatcloud 0.2.1+);
        otherwise they are pipecatcloud 0.2.0's own.
    smallwebrtc: pipecatcloud has SmallWebRTCSessionArguments (0.2.5+).
    post_init_error: raised when a Daily session type is built.
    """

    @dataclasses.dataclass
    class RunnerArguments:
        if with_body:
            body: Any = dataclasses.field(default_factory=dict, kw_only=True)

    @dataclasses.dataclass
    class DailyRunnerArguments(RunnerArguments):
        room_url: str
        token: str

        def __post_init__(self):
            if post_init_error is not None:
                raise post_init_error

    @dataclasses.dataclass
    class WebSocketRunnerArguments(RunnerArguments):
        websocket: Any

    @dataclasses.dataclass
    class SmallWebRTCRunnerArguments(RunnerArguments):
        webrtc_connection: Any

    runner = type(
        "runner_types",
        (),
        {
            "RunnerArguments": RunnerArguments,
            "DailyRunnerArguments": DailyRunnerArguments,
            "WebSocketRunnerArguments": WebSocketRunnerArguments,
            "SmallWebRTCRunnerArguments": SmallWebRTCRunnerArguments,
        },
    )

    @dataclasses.dataclass
    class SessionArguments:
        session_id: Optional[str]

    def session_type(base, *standalone_fields, standalone_body=True):
        if related:
            return dataclasses.dataclass(type("T", (base, SessionArguments), {}))
        # An old pipecatcloud's own types, not built on pipecat-ai's: as in
        # 0.2.0, Daily and Pipecat declare body and WebSocket does not.
        annotations = {name: Any for name in standalone_fields}
        if standalone_body:
            annotations["body"] = Any
        return dataclasses.dataclass(
            type("T", (SessionArguments,), {"__annotations__": annotations})
        )

    types = {
        "DailySessionArguments": session_type(DailyRunnerArguments, "room_url", "token"),
        "PipecatSessionArguments": session_type(RunnerArguments),
        "WebSocketSessionArguments": session_type(
            WebSocketRunnerArguments, "websocket", standalone_body=False
        ),
    }
    if smallwebrtc:
        types["SmallWebRTCSessionArguments"] = session_type(
            SmallWebRTCRunnerArguments, "webrtc_connection"
        )
    return runner, type("agent", (), types)


def _importer(runner=None, agent=None, *, runner_error=None, agent_error=None):
    def import_module(name):
        if name == "pipecat.runner.types":
            if runner_error is not None:
                raise runner_error
            return runner
        if name == "pipecatcloud.agent":
            if agent_error is not None:
                raise agent_error
            return agent
        raise AssertionError(f"unexpected import {name}")

    return import_module


def _versions(pipecat: Optional[str] = "0.0.95", pipecatcloud: Optional[str] = "1.2.0"):
    return {"pipecat-ai": pipecat, "pipecatcloud": pipecatcloud}.get


def _check_pipecat(import_module, *, pipecat="0.0.95", installed=True):
    pcc_pipecat_compat._check_pipecat(
        lambda name: object() if installed else None, import_module, _versions(pipecat)
    )


def _check_session_types(import_module, *, pipecat="0.0.95", pipecatcloud="1.2.0"):
    return pcc_pipecat_compat._check_session_types(import_module, _versions(pipecat, pipecatcloud))


@pytest.fixture(autouse=True)
def _exit_raises(monkeypatch):
    """A refusal ends the process with os._exit; in-process it raises instead."""

    def exit_(code):
        raise SystemExit(code)

    monkeypatch.setattr(pcc_pipecat_compat, "_exit", exit_)


@pytest.fixture
def termination_log(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "termination-log"
    monkeypatch.setenv(pcc_pipecat_compat.TERMINATION_LOG_ENV, str(path))
    return path


def _refusal(termination_log: Path, capfd) -> str:
    line = termination_log.read_text()
    assert "\n" not in line and "[" not in line and "]" not in line
    assert line.startswith("Refusing to start: ")
    assert f"CRITICAL: {line}\n" in capfd.readouterr().err
    return line


# ------------------------------------------------------------
# build()
# ------------------------------------------------------------
class TestBuild:
    def test_a_type_that_takes_body_gets_it_in_the_constructor(self):
        _, agent = _modules(with_body=True)
        args = pcc_pipecat_compat.build(
            agent.DailySessionArguments, session_id="s", room_url="u", token="t", body={"k": 1}
        )
        assert (args.session_id, args.room_url, args.token, args.body) == ("s", "u", "t", {"k": 1})

    def test_a_type_without_body_gets_it_after_construction(self):
        _, agent = _modules(with_body=False)
        with pytest.raises(TypeError):
            agent.PipecatSessionArguments(session_id="s", body={"k": 1})
        args = pcc_pipecat_compat.build(
            agent.PipecatSessionArguments, session_id="s", body={"k": 1}
        )
        assert (args.session_id, args.body) == ("s", {"k": 1})

    def test_body_is_carried_as_given_whatever_it_is(self):
        # The WhatsApp path hands the call object over as the body.
        _, agent = _modules(with_body=False)
        call = object()
        args = pcc_pipecat_compat.build(
            agent.SmallWebRTCSessionArguments, session_id="s", webrtc_connection=None, body=call
        )
        assert args.body is call

    def test_a_body_field_the_constructor_does_not_take_is_set_after(self):
        @dataclasses.dataclass
        class Args:
            session_id: Optional[str]
            body: Any = dataclasses.field(default=None, init=False)

        args = pcc_pipecat_compat.build(Args, session_id="s", body={"k": 1})
        assert args.body == {"k": 1}


# ------------------------------------------------------------
# check_pipecat(): before the bot module
# ------------------------------------------------------------
class TestCheckPipecat:
    def test_a_pipecat_with_runner_types_passes(self, termination_log, capfd):
        runner, _ = _modules()
        _check_pipecat(_importer(runner))
        assert not termination_log.exists()
        assert capfd.readouterr().err == ""

    def test_not_installed_is_refused(self, termination_log, capfd):
        with pytest.raises(SystemExit) as exit_info:
            _check_pipecat(_importer(), pipecat=None, installed=False)
        assert exit_info.value.code == pcc_pipecat_compat.EXIT_CODE
        line = _refusal(termination_log, capfd)
        assert "pipecat-ai is not installed" in line and "0.0.78 or newer" in line

    @pytest.mark.parametrize("missing", ["pipecat.runner", "pipecat.runner.types"])
    def test_no_runner_module_is_too_old(self, termination_log, capfd, missing):
        error = ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(runner_error=error), pipecat="0.0.76")
        line = _refusal(termination_log, capfd)
        assert "pipecat-ai 0.0.76 is too old" in line and "0.0.78 or newer" in line

    def test_a_missing_runner_type_is_too_old(self, termination_log, capfd):
        runner, _ = _modules()
        partial = type("runner_types", (), {"RunnerArguments": runner.RunnerArguments})
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(partial), pipecat="0.0.70")
        assert "pipecat-ai 0.0.70 is too old" in _refusal(termination_log, capfd)

    def test_a_broken_dependency_is_reported_as_such(self, termination_log, capfd):
        error = ModuleNotFoundError("No module named 'numpy'", name="numpy")
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(runner_error=error), pipecat="1.12.0")
        line = _refusal(termination_log, capfd)
        assert "pipecat-ai 1.12.0 failed to import" in line
        assert "No module named 'numpy'" in line
        assert "too old" not in line

    def test_any_other_import_failure_is_reported(self, termination_log, capfd):
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(runner_error=RuntimeError("boom")))
        assert "failed to import (RuntimeError: boom)" in _refusal(termination_log, capfd)

    def test_the_line_stays_one_line_without_brackets(self, termination_log, capfd):
        error = RuntimeError("needs pipecat-ai[daily]\nand [/also] this " + "x" * 1000)
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(runner_error=error))
        line = _refusal(termination_log, capfd)
        assert "pipecat-ai(daily)" in line
        assert len(line.encode()) < 1024

    def test_an_unwritable_termination_log_still_refuses(self, monkeypatch, capfd):
        monkeypatch.setenv(pcc_pipecat_compat.TERMINATION_LOG_ENV, "/nonexistent/dir/log")
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(), pipecat=None, installed=False)
        assert "CRITICAL: Refusing to start: pipecat-ai is not installed" in capfd.readouterr().err

    def test_text_the_termination_log_cannot_encode_is_escaped(self, termination_log, capfd):
        # A lone surrogate, as an undecodable filename in an error text carries.
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(runner_error=RuntimeError("bad \udcff name")))
        assert "bad \\udcff name" in _refusal(termination_log, capfd)

    def test_with_the_capture_on_the_line_goes_to_the_saved_stream(
        self, termination_log, monkeypatch, capfd
    ):
        # Once pcc_structured_logs has captured stderr, fd 2 is its pipe, which
        # the line would die in as the process exits.
        console = io.StringIO()
        monkeypatch.setattr(pcc_structured_logs, "console_stream", lambda: console)
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(), pipecat=None, installed=False)
        assert "CRITICAL: Refusing to start: pipecat-ai is not installed" in console.getvalue()
        assert capfd.readouterr().err == ""

    def test_without_the_capture_the_line_goes_to_the_process_stderr(
        self, termination_log, monkeypatch, capfd
    ):
        # Not to a sys.stderr the bot module has replaced.
        replaced = io.StringIO()
        monkeypatch.setattr(sys, "stderr", replaced)
        with pytest.raises(SystemExit):
            _check_pipecat(_importer(), pipecat=None, installed=False)
        assert replaced.getvalue() == ""
        assert "CRITICAL: Refusing to start: pipecat-ai is not installed" in capfd.readouterr().err

    def test_a_fault_in_the_check_itself_does_not_stop_startup(self, monkeypatch, capfd):
        def broken(*args):
            raise KeyError("a bug")

        monkeypatch.setattr(pcc_pipecat_compat, "_check_pipecat", broken)
        pcc_pipecat_compat.check_pipecat()
        assert "WARNING: The pipecat-ai check did not run" in capfd.readouterr().err


# ------------------------------------------------------------
# check_session_types(): right after the bot module
# ------------------------------------------------------------
class TestCheckSessionTypes:
    def test_current_types_pass_silently(self, termination_log, capfd):
        runner, agent = _modules(with_body=True)
        assert _check_session_types(_importer(runner, agent)) == []
        assert not termination_log.exists()
        assert capfd.readouterr().err == ""

    def test_types_without_body_pass_with_a_deprecation_warning(self, termination_log, capfd):
        runner, agent = _modules(with_body=False)
        [warning] = _check_session_types(_importer(runner, agent), pipecat="0.0.88")
        assert "pipecat-ai 0.0.88 is older than 0.0.91" in warning
        assert "supports pipecat-ai 0.0.78 or newer" in warning
        # Returned for app.py to log once logging is set up, not written here.
        assert capfd.readouterr().err == ""
        assert not termination_log.exists()

    def test_the_warning_reads_well_without_a_version(self):
        runner, agent = _modules(with_body=False)
        [warning] = _check_session_types(_importer(runner, agent), pipecat=None)
        assert warning.startswith("The installed pipecat-ai is older than 0.0.91")

    def test_an_old_pipecatcloud_without_smallwebrtc_passes(self, termination_log):
        # pipecatcloud before 0.2.5; app.py runs such an image without SmallWebRTC.
        runner, agent = _modules(smallwebrtc=False)
        assert _check_session_types(_importer(runner, agent), pipecatcloud="0.2.4") == []
        assert not termination_log.exists()

    def test_an_old_pipecatcloud_with_its_own_types_is_warned_about_not_pipecat(
        self, termination_log
    ):
        # pipecatcloud before 0.2.1 defined its types without pipecat-ai's, and
        # its WebSocketSessionArguments takes no body whatever pipecat-ai is.
        runner, agent = _modules(related=False, smallwebrtc=False)
        [warning] = _check_session_types(_importer(runner, agent), pipecatcloud="0.2.0")
        assert warning.startswith("pipecatcloud 0.2.0's WebSocketSessionArguments does not take")
        assert "pipecatcloud 0.2.1 or newer" in warning
        assert "pipecat-ai" not in warning
        assert not termination_log.exists()

    def test_both_are_warned_about_when_both_are_old(self):
        runner, agent = _modules(with_body=False, related=False, smallwebrtc=False)
        pipecat, pipecatcloud = _check_session_types(
            _importer(runner, agent), pipecat="0.0.88", pipecatcloud="0.2.0"
        )
        assert pipecat.startswith("pipecat-ai 0.0.88 is older than 0.0.91")
        assert pipecatcloud.startswith("pipecatcloud 0.2.0's WebSocketSessionArguments")

    def test_a_missing_required_type_names_pipecatcloud(self, termination_log, capfd):
        runner, agent = _modules()
        partial = type("agent", (), {"DailySessionArguments": agent.DailySessionArguments})
        with pytest.raises(SystemExit):
            _check_session_types(_importer(runner, partial), pipecatcloud="0.1.0")
        line = _refusal(termination_log, capfd)
        assert "pipecatcloud 0.1.0 has no PipecatSessionArguments" in line

    def test_pipecatcloud_failing_to_load_names_both_and_what_it_wraps(
        self, termination_log, capfd
    ):
        # pipecatcloud 1.x's own message names its declared floor (1.0.0); the
        # refusal quotes what it wraps and names this image's floor.
        cause = ImportError("cannot import name 'SomeRunnerArguments'")
        error = ImportError("pipecatcloud's agent session-argument types require pipecat-ai>=1.0.0")
        error.__cause__ = cause
        with pytest.raises(SystemExit):
            _check_session_types(_importer(agent_error=error), pipecat="0.0.95")
        line = _refusal(termination_log, capfd)
        assert "pipecatcloud 1.2.0 cannot load its session types with pipecat-ai 0.0.95" in line
        assert "SomeRunnerArguments" in line and "0.0.78 or newer" in line
        assert "1.0.0" not in line

    def test_a_type_that_does_not_build_is_refused_with_the_error(self, termination_log, capfd):
        # pipecat-ai 0.0.77 under pipecatcloud 0.4.4.
        runner, agent = _modules(
            post_init_error=AttributeError("type object has no attribute '__post_init__'")
        )
        with pytest.raises(SystemExit):
            _check_session_types(_importer(runner, agent), pipecat="0.0.77", pipecatcloud="0.4.4")
        line = _refusal(termination_log, capfd)
        assert (
            "pipecatcloud 0.4.4 with pipecat-ai 0.0.77 cannot build the DailySessionArguments"
            in line
        )
        assert "AttributeError" in line and "0.0.78 or newer" in line

    def test_a_fault_in_the_check_itself_does_not_stop_startup(self, monkeypatch, capfd):
        def broken(*args):
            raise KeyError("a bug")

        monkeypatch.setattr(pcc_pipecat_compat, "_check_session_types", broken)
        assert pcc_pipecat_compat.check_session_types() == []
        assert "WARNING: The session arguments check did not run" in capfd.readouterr().err


class TestInstalledPipecat:
    """The checks against whatever pipecat-ai and pipecatcloud this run has installed."""

    def test_the_installed_pipecat_is_served(self, termination_log):
        from pipecat.runner.types import RunnerArguments

        pcc_pipecat_compat.check_pipecat()
        warnings = pcc_pipecat_compat.check_session_types()
        assert not termination_log.exists()
        if pcc_pipecat_compat.takes_body(RunnerArguments):
            assert warnings == []
        else:
            assert len(warnings) == 1 and "older than 0.0.91" in warnings[0]


# ------------------------------------------------------------
# The process: importing app, as the image's CMD does before serving
# ------------------------------------------------------------
def _stub_pipecat(
    root: Path, version: str, types_source: Optional[str] = None, init_source: str = ""
) -> Path:
    """A pipecat-ai ahead of the real one: no runner types (older than 0.0.77),
    or ``types_source`` as ``pipecat/runner/types.py``; ``init_source`` as its
    ``__init__.py``."""
    (root / "pipecat").mkdir(parents=True)
    (root / "pipecat" / "__init__.py").write_text(textwrap.dedent(init_source))
    if types_source is not None:
        (root / "pipecat" / "runner").mkdir()
        (root / "pipecat" / "runner" / "__init__.py").write_text("")
        (root / "pipecat" / "runner" / "types.py").write_text(types_source)
    dist = root / f"pipecat_ai-{version}.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: pipecat-ai\nVersion: {version}\n")
    return root


def _stub_pipecatcloud(root: Path, version: str, agent_source: str) -> Path:
    """A pipecatcloud ahead of the real one, with ``agent_source`` as its agent module."""
    (root / "pipecatcloud").mkdir(parents=True)
    (root / "pipecatcloud" / "__init__.py").write_text("")
    (root / "pipecatcloud" / "agent.py").write_text(textwrap.dedent(agent_source))
    dist = root / f"pipecatcloud-{version}.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: pipecatcloud\nVersion: {version}\n"
    )
    return root


def _pipecatcloud_without_pipecat_type(tmp_path: Path) -> Path:
    """A pipecatcloud that check_session_types() refuses, after the bot module."""
    return _stub_pipecatcloud(
        tmp_path / "pipecatcloud",
        "0.1.0",
        """
        from dataclasses import dataclass

        @dataclass
        class DailySessionArguments:
            session_id: str
            room_url: str
            token: str
            body: object
        """,
    )


def _bot_module(root: Path, source: Optional[str] = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "bot.py").write_text(
        textwrap.dedent(source)
        if source
        else "print('BOT MODULE IMPORTED', flush=True)\nasync def bot(args): pass\n"
    )
    return root


def _packages_without_pipecat(root: Path) -> Path:
    """This run's site-packages, merged in sys.path order, minus pipecat-ai."""
    root.mkdir()
    for directory in sys.path:
        if not directory.endswith("site-packages") or not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            if entry == "pipecat" or entry.startswith("pipecat_ai"):
                continue
            link = root / entry
            if not link.exists() and not link.is_symlink():
                link.symlink_to(os.path.join(directory, entry))
    return root


def _import_app(
    paths, extra_env=None, *, isolated=False, then="", timeout=60
) -> subprocess.CompletedProcess:
    """Import app in a child interpreter: its whole startup, short of serving."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PCC_LOG_DIR")}
    env.update(extra_env or {})
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in [PIPECAT_BASE_DIR, *paths])
    args = [sys.executable]
    if isolated:
        args.append("-S")  # no site-packages: only what PYTHONPATH names
    args += ["-c", f"import app{then}"]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)


def _lane(log_dir: Path):
    return [json.loads(r) for r in (log_dir / "bot.jsonl").read_text().splitlines()]


class TestProcess:
    def test_a_too_old_pipecat_stops_before_the_bot_module(self, tmp_path):
        termination = tmp_path / "termination-log"
        proc = _import_app(
            [_stub_pipecat(tmp_path / "stub", "0.0.76"), _bot_module(tmp_path / "bot")],
            {pcc_pipecat_compat.TERMINATION_LOG_ENV: str(termination)},
        )
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr
        assert "BOT MODULE IMPORTED" not in proc.stdout + proc.stderr
        line = termination.read_text()
        assert "pipecat-ai 0.0.76 is too old" in line
        assert line in proc.stderr

    def test_with_the_log_lane_on_the_refusal_is_in_stderr_and_bot_jsonl(self, tmp_path):
        termination = tmp_path / "termination-log"
        log_dir = tmp_path / "logs"
        proc = _import_app(
            [_stub_pipecat(tmp_path / "stub", "0.0.76"), _bot_module(tmp_path / "bot")],
            {pcc_pipecat_compat.TERMINATION_LOG_ENV: str(termination), "PCC_LOG_DIR": str(log_dir)},
        )
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr
        line = termination.read_text()
        assert line in proc.stderr
        assert {"stream": "app", "level": "CRITICAL", "line": line}.items() <= _lane(log_dir)[
            -1
        ].items()

    def test_a_pipecat_that_raises_on_import_is_refused_with_the_lane_on(self, tmp_path):
        # pipecatcloud 0.4.4 (the Python 3.10 image's) imports pipecat-ai when
        # preloaded and catches only ImportError; the check still has the say.
        termination = tmp_path / "termination-log"
        stub = _stub_pipecat(tmp_path / "stub", "0.0.95", "raise RuntimeError('broken build')\n")
        proc = _import_app(
            [stub, _bot_module(tmp_path / "bot")],
            {
                pcc_pipecat_compat.TERMINATION_LOG_ENV: str(termination),
                "PCC_LOG_DIR": str(tmp_path / "logs"),
            },
        )
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr
        assert "failed to import (RuntimeError: broken build)" in termination.read_text()

    @pytest.mark.parametrize("lane", [False, True], ids=["lane-off", "lane-on"])
    def test_a_refusal_after_the_bot_module_ends_the_process_whatever_it_left(self, tmp_path, lane):
        # By the second check the bot module has run: here it has left a thread
        # that never ends, which would hold a SystemExit for good, and replaced
        # loguru's handlers, which the refusal does not go through. What it
        # printed, still in the buffer of the sys.stdout it then replaced,
        # reaches stdout.
        bot = _bot_module(
            tmp_path / "bot",
            """
            import io
            import os
            import sys
            import threading
            from loguru import logger

            logger.remove()
            logger.add(os.devnull)
            threading.Thread(target=threading.Event().wait).start()
            print("BOT IMPORT LINE")
            sys.stdout = io.StringIO()

            async def bot(args):
                pass
            """,
        )
        termination = tmp_path / "termination-log"
        env = {pcc_pipecat_compat.TERMINATION_LOG_ENV: str(termination)}
        log_dir = tmp_path / "logs"
        if lane:
            env["PCC_LOG_DIR"] = str(log_dir)
        proc = _import_app([_pipecatcloud_without_pipecat_type(tmp_path), bot], env, timeout=30)
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr[-2000:]
        line = termination.read_text()
        assert "pipecatcloud 0.1.0 has no PipecatSessionArguments" in line
        assert proc.stderr.count(line) == 1
        assert "BOT IMPORT LINE" in proc.stdout
        if lane:
            # Its own record, whatever the bot module did to loguru.
            assert any(
                {"stream": "app", "level": "CRITICAL", "line": line}.items() <= r.items()
                for r in _lane(log_dir)
            )

    def test_with_the_lane_on_what_the_bot_module_printed_is_shipped_before_a_refusal(
        self, tmp_path
    ):
        bot = _bot_module(
            tmp_path / "bot",
            """
            print("BOT IMPORT LINE")  # left in sys.stdout's buffer

            async def bot(args):
                pass
            """,
        )
        log_dir = tmp_path / "logs"
        proc = _import_app(
            [_pipecatcloud_without_pipecat_type(tmp_path), bot],
            {
                pcc_pipecat_compat.TERMINATION_LOG_ENV: str(tmp_path / "termination-log"),
                "PCC_LOG_DIR": str(log_dir),
            },
        )
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr[-2000:]
        assert {"stream": "stdout", "line": "BOT IMPORT LINE"} in [
            {"stream": r["stream"], "line": r["line"]} for r in _lane(log_dir)
        ]

    def test_no_pipecat_at_all_is_refused(self, tmp_path):
        # This run's installed packages without pipecat-ai, and no site-packages,
        # so pipecat is genuinely absent rather than hidden.
        termination = tmp_path / "termination-log"
        proc = _import_app(
            [_packages_without_pipecat(tmp_path / "deps"), _bot_module(tmp_path / "bot")],
            {pcc_pipecat_compat.TERMINATION_LOG_ENV: str(termination)},
            isolated=True,
        )
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr
        assert "BOT MODULE IMPORTED" not in proc.stdout + proc.stderr
        assert "pipecat-ai is not installed" in termination.read_text()

    def test_the_bot_module_finds_logging_as_it_left_it(self, tmp_path):
        # A common bot opening; importing pipecatcloud before the bot module
        # would remove the default handler it removes.
        bot = _bot_module(
            tmp_path / "bot",
            """
            import sys
            from loguru import logger

            logger.remove(0)
            logger.add(sys.stderr, level="INFO")

            async def bot(args):
                pass
            """,
        )
        proc = _import_app([bot])
        assert proc.returncode == 0, proc.stderr[-2000:]

    def test_with_the_lane_unavailable_the_bot_module_finds_logging_as_it_left_it(self, tmp_path):
        # PCC_LOG_DIR is set but cannot be created, so there is no capture, and
        # nothing may touch loguru before the bot module, as with the lane off.
        (tmp_path / "not-a-directory").write_text("")
        bot = _bot_module(
            tmp_path / "bot",
            """
            from loguru import logger

            logger.remove(0)

            async def bot(args):
                pass
            """,
        )
        proc = _import_app([bot], {"PCC_LOG_DIR": str(tmp_path / "not-a-directory" / "logs")})
        assert proc.returncode == 0, proc.stderr[-2000:]

    def test_with_the_lane_on_pipecat_logs_its_import_into_the_lane(self, tmp_path):
        # Whoever imports pipecat-ai first (the check, or pipecatcloud 0.4.4
        # when preloaded), what it logs at import, as its banner, goes through
        # loguru's handlers of the moment: the lane's, which it must reach.
        stub = _stub_pipecat(
            tmp_path / "stub",
            "0.0.76",
            init_source="from loguru import logger\nlogger.info('PIPECAT IMPORT BANNER')\n",
        )
        log_dir = tmp_path / "logs"
        proc = _import_app(
            [stub, _bot_module(tmp_path / "bot")],
            {
                pcc_pipecat_compat.TERMINATION_LOG_ENV: str(tmp_path / "termination-log"),
                "PCC_LOG_DIR": str(log_dir),
            },
        )
        assert proc.returncode == pcc_pipecat_compat.EXIT_CODE, proc.stderr[-2000:]
        assert {"stream": "app", "line": "PIPECAT IMPORT BANNER"} in [
            {"stream": r["stream"], "line": r["line"]} for r in _lane(log_dir)
        ]

    def test_with_the_lane_on_a_bot_importing_pipecatcloud_does_not_flood(self, tmp_path):
        # Importing pipecatcloud puts a loguru handler on sys.stderr. After the
        # capture is installed that is a pipe whose lines are logged again into
        # itself: a 50 ms window of it wrote 43 MB of stderr. The capture
        # imports pipecatcloud before it starts, so the bot's import finds it
        # loaded.
        bot = _bot_module(
            tmp_path / "bot",
            """
            import pipecatcloud.agent  # as a bot typing its arguments does

            for i in range(20):
                print(f"bot import line {i}", flush=True)

            async def bot(args):
                pass
            """,
        )
        log_dir = tmp_path / "logs"
        # The pump threads get a moment to drain what the bot module printed.
        proc = _import_app(
            [bot], {"PCC_LOG_DIR": str(log_dir)}, then="; import time; time.sleep(0.5)"
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert len(proc.stdout) + len(proc.stderr) < 100_000
        printed = [r["line"] for r in _lane(log_dir) if r["stream"] == "stdout"]
        assert printed == [f"bot import line {i}" for i in range(20)]

    def test_the_deprecation_warning_reaches_stderr_and_the_lane(self, tmp_path):
        from pipecat.runner.types import RunnerArguments

        log_dir = tmp_path / "logs"
        proc = _import_app(
            [_bot_module(tmp_path / "bot")],
            {"PCC_LOG_DIR": str(log_dir)},
            then="; import time; time.sleep(0.3)",
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        warnings = [
            r["line"]
            for r in _lane(log_dir)
            if r.get("level") == "WARNING" and "deprecated on this image" in r["line"]
        ]
        if pcc_pipecat_compat.takes_body(RunnerArguments):
            assert warnings == []
        else:
            assert len(warnings) == 1 and warnings[0] in proc.stderr


def test_the_image_copies_the_module():
    dockerfile = (PIPECAT_BASE_DIR / "Dockerfile").read_text()
    copy = next(line for line in dockerfile.splitlines() if line.startswith("COPY ./app.py"))
    assert "./pcc_pipecat_compat.py" in copy
