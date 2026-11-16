"""Exercise the container check's log bounds without requiring Docker."""

import subprocess
import sys
import textwrap
from types import SimpleNamespace

import container_signal_check as check
import pytest


@pytest.fixture
def fake_docker(monkeypatch):
    """Replace Docker with real child processes, keeping their actual file I/O."""
    popen = subprocess.Popen
    temporary_file = check.tempfile.TemporaryFile
    children = []
    files = []

    def open_file(*args, **kwargs):
        file = temporary_file(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(check.tempfile, "TemporaryFile", open_file)

    def install(source, *, already_exited=False):
        def start(command, **kwargs):
            assert command == ["docker", "logs", "fixture-container"]
            proc = popen([sys.executable, "-c", textwrap.dedent(source)], **kwargs)
            children.append(proc)
            if already_exited:
                proc.wait(timeout=5)
            return proc

        monkeypatch.setattr(check.subprocess, "Popen", start)
        return SimpleNamespace(children=children, files=files)

    yield install

    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert all(file.closed for file in files), "log capture leaked a temporary file"


def test_more_than_a_thousand_lines_are_returned_without_losing_early_import_logs(fake_docker):
    fake_docker(
        """
        import sys

        for number in range(200):
            print(f"customer import log {number}", file=sys.stderr)
        for number in range(1100):
            print(f"later startup log {number}")
        """
    )
    expected_stdout = "".join(f"later startup log {number}\n" for number in range(1100))
    expected_stderr = "".join(f"customer import log {number}\n" for number in range(200))
    assert check.logs("fixture-container") == expected_stdout + expected_stderr


def test_stdout_and_stderr_share_a_byte_limit_and_overflowing_child_is_reaped(
    fake_docker, monkeypatch
):
    monkeypatch.setattr(check, "MAX_LOG_BYTES", 100)
    fixture = fake_docker(
        """
        import os
        import time

        # Each stream is only 20 characters / 80 bytes: the combined bytes
        # exceed the cap, while each stream and the combined character count do not.
        os.write(1, ("\\U0001f916" * 20).encode())
        os.write(2, ("\\U0001f916" * 20).encode())
        time.sleep(60)
        """
    )
    with pytest.raises(AssertionError, match="docker logs exceeded 100 bytes"):
        check.logs("fixture-container")
    assert fixture.children[0].returncode < 0


def test_fast_exiting_child_cannot_bypass_the_byte_limit(fake_docker, monkeypatch):
    monkeypatch.setattr(check, "MAX_LOG_BYTES", 100)
    fixture = fake_docker("import os; os.write(1, b'x' * 101)", already_exited=True)
    with pytest.raises(AssertionError, match="docker logs exceeded 100 bytes"):
        check.logs("fixture-container")
    assert fixture.children[0].returncode == 0


def test_output_at_the_byte_limit_is_returned_whole(fake_docker, monkeypatch):
    monkeypatch.setattr(check, "MAX_LOG_BYTES", 64)
    fake_docker("import os; os.write(1, b'x' * 32); os.write(2, b'y' * 32)")
    assert check.logs("fixture-container") == "x" * 32 + "y" * 32


def test_failed_docker_command_reports_its_exit_code_and_stderr(fake_docker):
    fixture = fake_docker(
        """
        import sys

        print("partial stdout")
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        sys.exit(2)
        """
    )
    with pytest.raises(AssertionError, match="docker logs failed") as error:
        check.logs("fixture-container")
    assert "rc=2" in str(error.value)
    assert "partial stdout" in str(error.value)
    assert "Cannot connect to the Docker daemon" in str(error.value)
    assert fixture.children[0].returncode == 2


def test_timeout_kills_and_reaps_the_docker_command(fake_docker):
    fixture = fake_docker("import time; time.sleep(60)")
    with pytest.raises(AssertionError, match="docker logs timed out.*after 0.1s"):
        check.logs("fixture-container", timeout=0.1)
    assert fixture.children[0].returncode < 0
