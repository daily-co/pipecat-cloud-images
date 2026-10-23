"""The weekly refresh's script: .github/scripts/refresh_base_images.py.

It is not part of the image, so it is loaded by path. Its own dependencies,
packaging and PyYAML, are in this environment already (through pytest and
fastapi[standard]).
"""

import importlib.util
import subprocess
import sys
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


# run


def test_run_says_which_command_failed_and_why(capsys):
    script = "import sys; sys.exit('manifest unknown')"
    with pytest.raises(subprocess.CalledProcessError):
        refresh.run(sys.executable, "-c", script)
    assert f"{sys.executable} -c {script} failed:\nmanifest unknown" in capsys.readouterr().err


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


def test_raise_floors_keeps_a_single_quoted_requirement_single_quoted(project):
    tmp, _ = project
    (tmp / "pyproject.toml").write_text("[project]\ndependencies = ['aiohttp>=3.14.3,<4']\n")
    assert refresh.raise_floors({"aiohttp": "3.14.4"}) == ["aiohttp>=3.14.4,<4"]
    assert "['aiohttp>=3.14.4,<4']" in (tmp / "pyproject.toml").read_text()


def test_raise_floors_reports_only_a_floor_it_could_raise(project, capsys):
    tmp, commands = project
    # An escape: tomllib reads "aiohttp>=3.14.3", which is not in the file as written.
    written = '[project]\ndependencies = ["aiohttp\\u003e=3.14.3"]\n'
    (tmp / "pyproject.toml").write_text(written)
    assert refresh.raise_floors({"aiohttp": "3.14.4"}) == []
    assert (tmp / "pyproject.toml").read_text() == written
    assert commands == []
    assert "::warning::Could not find 'aiohttp>=3.14.3'" in capsys.readouterr().out


# probe

PROBED = "3.12.15\n0.13.5\nopenssl 3.5.4-1~deb13u1\nxz-utils 5.8.1-1\n"


def test_probe_reads_python_uv_and_each_debian_source_package(monkeypatch):
    monkeypatch.setattr(refresh, "run", lambda *args, cwd=None: PROBED)
    assert refresh.probe("image") == {
        "Python": "3.12.15",
        "uv": "0.13.5",
        "Debian openssl": "3.5.4-1~deb13u1",
        "Debian xz-utils": "5.8.1-1",
    }


@pytest.mark.parametrize(
    "output",
    ["", "3.12.15\n0.13.5\n", "0.13.5\nopenssl 3.5.4-1~deb13u1\n", "3.12.15\n0.13.5\nodd\n"],
    ids=["empty", "no packages", "a line missing", "an odd package line"],
)
def test_probe_refuses_output_it_does_not_recognise(monkeypatch, output):
    monkeypatch.setattr(refresh, "run", lambda *args, cwd=None: output)
    with pytest.raises(ValueError, match="unexpected output from image"):
        refresh.probe("image")


# refresh_images

OLD = "ghcr.io/astral-sh/uv:python{v}-trixie-slim@sha256:" + "0" * 64
NEW = "ghcr.io/astral-sh/uv:python{v}-trixie-slim@sha256:" + "1" * 64


@pytest.fixture
def repin(tmp_path, monkeypatch):
    """Repin a versions.yaml whose 3.12 and 3.13 tags have both moved."""
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        "base:\n  base_images:\n"
        + "".join(f"    '{v}': {OLD.format(v=v)}\n" for v in ("3.12", "3.13"))
    )
    monkeypatch.setattr(refresh, "VERSIONS", versions)
    monkeypatch.setattr(refresh, "resolve", lambda ref: ref.replace("0" * 64, "1" * 64))

    def setup(probes: dict):
        def probe(ref):
            result = probes[ref]
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(refresh, "probe", probe)
        return refresh.refresh_images()

    return setup


def versions(python: str, openssl: str) -> dict:
    return {"Python": python, "uv": "0.13.5", "Debian openssl": openssl}


def test_refresh_images_says_what_changed_in_which_image(repin):
    [entry], body = repin(
        {
            OLD.format(v="3.12"): versions("3.12.15", "3.5.4-1"),
            NEW.format(v="3.12"): versions("3.12.15", "3.5.5-1"),
            OLD.format(v="3.13"): versions("3.13.16", "3.5.4-1"),
            NEW.format(v="3.13"): versions("3.13.16", "3.5.4-1"),
        }
    )
    assert entry == (
        "Rebuilt on the upstream images' latest builds: "
        "Debian openssl 3.5.4-1 to 3.5.5-1 (the 3.12 image)."
    )
    assert "| 3.12 | `ghcr.io/astral-sh/uv:python3.12-trixie-slim` |" in "\n".join(body)


def test_refresh_images_stops_when_the_new_image_cannot_be_read(repin, monkeypatch):
    # Failed through run, not probe, so that probe() is held to letting it out.
    def run(*args, cwd=None):
        if NEW.format(v="3.12") in args:
            raise subprocess.CalledProcessError(125, args, stderr="manifest unknown")
        return "3.12.15\n0.13.5\nopenssl 3.5.4-1\n"

    monkeypatch.setattr(refresh, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        refresh.refresh_images()


def test_refresh_images_claims_no_change_only_where_it_compared(repin):
    gone = subprocess.CalledProcessError(1, "docker run")
    [entry], _ = repin(
        {
            OLD.format(v="3.12"): gone,
            NEW.format(v="3.12"): versions("3.12.15", "3.5.4-1"),
            OLD.format(v="3.13"): versions("3.13.16", "3.5.4-1"),
            NEW.format(v="3.13"): versions("3.13.16", "3.5.4-1"),
        }
    )
    assert entry == (
        "Rebuilt on the upstream images' latest builds, with no change to Python, uv or "
        "the Debian packages in the 3.13 image. The previous upstream image for the 3.12 "
        "image could no longer be read to compare."
    )


def test_refresh_images_claims_nothing_when_it_compared_nothing(repin):
    gone = ValueError("unexpected output")
    [entry], _ = repin(
        {
            OLD.format(v="3.12"): gone,
            NEW.format(v="3.12"): versions("3.12.15", "3.5.4-1"),
            OLD.format(v="3.13"): gone,
            NEW.format(v="3.13"): versions("3.13.16", "3.5.4-1"),
        }
    )
    assert "no change" not in entry
    assert entry == (
        "Rebuilt on the upstream images' latest builds. The previous upstream image for "
        "the 3.12 and 3.13 images could no longer be read to compare."
    )


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
        "Update dependencies with a known vulnerability to newer releases that the audit "
        "still flags: somelib 1.0.0 to 1.1.0."
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


# main


@pytest.fixture
def release(project, monkeypatch):
    """Run main() on what refresh_images and refresh_packages are stubbed to find."""
    tmp, commands = project

    def run(*args, cwd=None):
        commands.append(args)
        if args[:3] == ("uv", "version", "--bump"):
            pyproject = tmp / "pyproject.toml"
            pyproject.write_text(pyproject.read_text().replace('"0.2.8"', '"0.2.9"'))
        return ""

    monkeypatch.setattr(refresh, "run", run)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp / "output"))
    monkeypatch.setattr(sys, "argv", ["refresh_base_images.py", "--body", str(tmp / "body.md")])

    def setup(images: tuple, packages: tuple, unreleased: str = ""):
        (tmp / "CHANGELOG.md").write_text(CHANGELOG.format(unreleased=unreleased))
        monkeypatch.setattr(refresh, "refresh_images", lambda: images)
        monkeypatch.setattr(refresh, "refresh_packages", lambda: packages)
        refresh.main()
        output = dict(line.split("=", 1) for line in (tmp / "output").read_text().splitlines())
        body = tmp / "body.md"
        return output, body.read_text() if body.exists() else None

    return setup


def test_main_releases_the_next_version_and_lists_what_was_waiting(release):
    output, body = release(
        (["Rebuilt."], ["### Upstream images"]),
        (["Update aiohttp."], [], ["### Locked packages", "", "- aiohttp 3.14.3 to 3.14.4"]),
        unreleased="### Added\n\n- A feature.\n\n",
    )
    assert output == {
        "changed": "true",
        "version": "0.2.9",
        "title": "Release v0.2.9: refresh upstream images and locked packages",
    }
    assert body.startswith("Merging this releases v0.2.9, through `docker-publish.yml`.")
    assert "- aiohttp 3.14.3 to 3.14.4" in body
    assert (
        "### Also released\n\n"
        "Already under `[Unreleased]` in `CHANGELOG.md`, so this releases it too:\n\n"
        "#### Added\n\n- A feature.\n"
    ) in body


def test_main_names_only_what_it_refreshed(release):
    output, body = release(([], []), ([], ["Update pip."], ["### Locked packages"]))
    assert output["title"] == "Release v0.2.9: refresh locked packages"
    assert "### Also released" not in body


def test_main_releases_nothing_in_a_quiet_week(release, project, capsys):
    tmp, commands = project
    stuck = ["### Locked packages", "", "- stuck 1.0, fixed in 2.0"]
    output, body = release(([], []), ([], [], stuck))
    assert output == {"changed": "false"}
    assert body is None
    assert commands == []
    assert (tmp / "pyproject.toml").read_text() == PYPROJECT
    assert capsys.readouterr().out == (
        "Nothing to release: every pinned image is current, and the lock has no upgrade to take.\n"
    )
