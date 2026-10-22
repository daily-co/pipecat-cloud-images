"""The weekly refresh's script: .github/scripts/refresh_base_images.py.

It is not part of the image, so it is loaded by path. Its own dependencies,
packaging and PyYAML, are in this environment already (through pytest and
fastapi[standard]).
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "refresh_base_images.py"
_spec = importlib.util.spec_from_file_location("refresh_base_images", SCRIPT)
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)

PYPROJECT = """\
[project]
name = "pipecat-base"
version = "0.2.8"
dependencies = [
    "fastapi[standard]>=0.115.6,<1",
    "loguru~=0.7.3",
    # A comment is kept.
    "aiohttp>=3.14.3,<4",
    "nltk>=3.10.3,<4",
]
"""

CHANGELOG = """\
# Changelog

## [Unreleased]

{unreleased}## [0.2.8] - 2026-10-22

### Security

- An earlier entry.
"""


def vuln(id: str, *fixes: str) -> dict:
    return {"id": id, "fix_versions": list(fixes)}


def dependency(name: str, version: str, *vulns: dict) -> dict:
    return {"name": name, "version": version, "vulns": list(vulns)}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """pyproject.toml and CHANGELOG.md in a scratch directory, and no commands run."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    monkeypatch.setattr(refresh, "PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(refresh, "CHANGELOG", tmp_path / "CHANGELOG.md")
    commands = []
    monkeypatch.setattr(refresh, "run", lambda *args, cwd=None: commands.append(args) or "")
    return tmp_path, commands


# first_fix


def test_first_fix_is_the_lowest_release_that_fixes_every_vulnerability():
    d = dependency("x", "1.0", vuln("A", "1.2", "2.0"), vuln("B", "1.1", "1.3"))
    assert refresh.first_fix(d) == "1.2"


def test_first_fix_ignores_releases_at_or_below_the_current_one():
    d = dependency("x", "1.5", vuln("A", "1.2", "1.5", "1.6"))
    assert refresh.first_fix(d) == "1.6"


def test_first_fix_is_none_when_a_vulnerability_names_no_fix():
    d = dependency("x", "1.0", vuln("A", "1.2"), vuln("B"))
    assert refresh.first_fix(d) is None


# raise_floors


def test_raise_floors_replaces_the_floor_and_keeps_the_rest(project):
    tmp, commands = project
    raised = refresh.raise_floors({"aiohttp": "3.14.4", "nltk": "3.10.4"})
    assert raised == ["aiohttp>=3.14.4,<4", "nltk>=3.10.4,<4"]
    text = (tmp / "pyproject.toml").read_text()
    assert '"aiohttp>=3.14.4,<4"' in text and '"nltk>=3.10.4,<4"' in text
    assert "# A comment is kept." in text
    assert commands == [("uv", "lock")]


def test_raise_floors_adds_a_floor_beside_other_specifiers(project):
    assert refresh.raise_floors({"loguru": "0.7.5"}) == ["loguru~=0.7.3,>=0.7.5"]


def test_raise_floors_keeps_extras_and_matches_names_canonically(project):
    tmp, _ = project
    (tmp / "pyproject.toml").write_text(
        '[project]\ndependencies = ["FastAPI[standard]>=0.115.6,<1"]\n'
    )
    assert refresh.raise_floors({"fastapi": "0.120.0"}) == ["FastAPI[standard]>=0.120.0,<1"]


def test_raise_floors_leaves_transitive_packages_and_the_lock_alone(project):
    tmp, commands = project
    assert refresh.raise_floors({"multidict": "6.9.1"}) == []
    assert (tmp / "pyproject.toml").read_text() == PYPROJECT
    assert commands == []


# refresh_packages


@pytest.fixture
def packages(project, monkeypatch):
    """Drive refresh_packages: the audits it runs, and the lock before and after."""

    def setup(audits: list[list[dict]], before: dict, after: dict):
        audits = iter(audits)
        locks = iter([before, after])
        monkeypatch.setattr(refresh, "audit", lambda: next(audits))
        monkeypatch.setattr(refresh, "locked", lambda: next(locks))
        return refresh.refresh_packages()

    return setup


def test_a_fix_is_a_security_entry_and_raises_a_direct_dependencys_floor(packages, project):
    tmp, _ = project
    aiohttp = dependency("aiohttp", "3.14.3", vuln("CVE-1", "3.14.4"))
    security, changed, body = packages(
        [[aiohttp], []], {"aiohttp": "3.14.3"}, {"aiohttp": "3.14.4"}
    )
    assert security == [
        "Update dependencies to resolve security vulnerabilities: aiohttp 3.14.3 to 3.14.4."
    ]
    assert changed == []
    assert "- aiohttp 3.14.3 to 3.14.4: CVE-1" in body
    assert '"aiohttp>=3.14.4,<4"' in (tmp / "pyproject.toml").read_text()


def test_an_upgrade_the_audit_still_flags_is_not_called_a_fix(packages, project, capsys):
    tmp, _ = project
    somelib = dependency("somelib", "1.0.0", vuln("GHSA-1"))
    still = dependency("somelib", "1.1.0", vuln("GHSA-1"))
    security, changed, body = packages(
        [[somelib], [still]], {"somelib": "1.0.0"}, {"somelib": "1.1.0"}
    )
    assert security == []
    assert changed == [
        "Update dependencies with a known vulnerability to their latest releases, which do "
        "not resolve it yet: somelib 1.0.0 to 1.1.0."
    ]
    assert "- somelib 1.0.0 to 1.1.0, **still vulnerable**: GHSA-1" in body
    assert "- somelib 1.1.0, fixed in no release yet" in body
    assert "::warning::Still vulnerable: somelib 1.1.0" in capsys.readouterr().out
    assert (tmp / "pyproject.toml").read_text() == PYPROJECT


def test_a_vulnerability_the_lock_cannot_fix_is_reported(packages, capsys):
    stuck = dependency("stuck", "1.0", vuln("CVE-2", "2.0"))
    security, changed, body = packages([[stuck]], {"stuck": "1.0"}, {"stuck": "1.0"})
    assert (security, changed) == ([], [])
    assert body[0] == "### Locked packages"
    assert "- stuck 1.0, fixed in 2.0" in body
    assert "::warning::Still vulnerable: stuck 1.0, fixed in 2.0" in capsys.readouterr().out


def test_nothing_vulnerable_and_nothing_changed_says_nothing(packages):
    assert packages([[]], {"pip": "26.2.2"}, {"pip": "26.2.2"}) == ([], [], [])


def test_a_vulnerable_package_the_upgrade_drops_from_the_lock(packages):
    gone = dependency("gone", "1.0", vuln("CVE-3", "1.1"))
    security, _, body = packages([[gone], []], {"gone": "1.0", "parent": "1.0"}, {"parent": "2.0"})
    assert security == [
        "Update dependencies to resolve security vulnerabilities: gone 1.0 (removed)."
    ]
    assert "- gone 1.0 (removed): CVE-3" in body


def test_the_fallback_takes_the_latest_when_a_first_fix_cannot_be_locked(
    project, packages, monkeypatch
):
    _, commands = project

    def run(*args, cwd=None):
        commands.append(args)
        if any("==" in a for a in args):
            raise subprocess.CalledProcessError(1, args, stderr="no solution")
        return ""

    monkeypatch.setattr(refresh, "run", run)
    x = dependency("x", "1.0", vuln("CVE-4", "1.1"))
    packages([[x], []], {"x": "1.0"}, {"x": "1.2"})
    first, fallback = commands[0], commands[1]
    assert "x==1.1" in first
    assert "x==1.1" not in fallback and "x" in fallback


# release_changelog


def released(tmp: Path, unreleased: str, sections: dict) -> tuple[str, str]:
    (tmp / "CHANGELOG.md").write_text(CHANGELOG.format(unreleased=unreleased))
    carried = refresh.release_changelog("0.2.9", sections)
    return carried, (tmp / "CHANGELOG.md").read_text()


def test_release_changelog_releases_the_new_entries_under_a_fresh_unreleased(project):
    tmp, _ = project
    carried, text = released(tmp, "", {"Changed": ["Rebuilt."], "Security": []})
    assert carried == ""
    assert "## [Unreleased]\n\n## [0.2.9] - " in text
    assert "### Changed\n\n- Rebuilt.\n\n## [0.2.8] - 2026-10-22" in text
    assert "### Security\n\n- An earlier entry." in text


def test_release_changelog_keeps_and_returns_what_was_waiting(project):
    tmp, _ = project
    waiting = "### Fixed\n\n- A fix.\n\n### Added\n\n- A feature.\n\n"
    carried, text = released(tmp, waiting, {"Security": ["Update aiohttp."]})
    assert carried == waiting.strip()
    release = text.split("## [0.2.9]")[1].split("## [0.2.8]")[0]
    # Keep a Changelog's order, whatever order they were written in.
    assert release.index("### Added") < release.index("### Fixed") < release.index("### Security")
    assert "- A fix." in release and "- A feature." in release


def test_release_changelog_keeps_both_entries_under_a_repeated_heading(project):
    tmp, _ = project
    waiting = "### Fixed\n\n- First.\n\n### Fixed\n\n- Second.\n\n"
    _, text = released(tmp, waiting, {})
    release = text.split("## [0.2.9]")[1].split("## [0.2.8]")[0]
    assert release.count("### Fixed") == 1
    assert "- First." in release and "- Second." in release


def test_release_changelog_stops_on_an_entry_with_no_heading(project):
    tmp, _ = project
    with pytest.raises(SystemExit, match="no ### heading"):
        released(tmp, "- An orphan.\n\n", {"Changed": ["Rebuilt."]})
    assert "- An orphan." in (tmp / "CHANGELOG.md").read_text()
    assert "## [0.2.9]" not in (tmp / "CHANGELOG.md").read_text()
