# /// script
# requires-python = ">=3.12"
# dependencies = ["packaging>=24", "pyyaml>=6,<7"]
# ///
"""Prepare a pipecat-base release from what upstream has fixed since the last.

Moves each Python's pinned upstream image in versions.yaml to what its tag
points to now, upgrades each locked package with a known vulnerability to the
first release that fixes it, raising the floor in pyproject.toml to that
release when the package is a direct dependency, and takes the latest pip and
certifi. When any of that changes anything, it bumps the patch version, writes
the release's changelog entry from what actually changed (the upstream images'
Python, uv and Debian packages, and the locked packages), and writes a pull
request body. Nothing changed leaves every file alone.

Run by .github/workflows/refresh-base-images.yml; locally, from anywhere:

    uv run .github/scripts/refresh_base_images.py --body /tmp/body.md

It needs docker, to resolve tags and to read each image's versions.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import tomllib
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "versions.yaml"
BASE = ROOT / "pipecat-base"
PYPROJECT = BASE / "pyproject.toml"
CHANGELOG = BASE / "CHANGELOG.md"
LOCK = BASE / "uv.lock"

PIP_AUDIT = "pip-audit==2.10.1"

# Taken at their latest whether or not an advisory names them: pip installs
# every agent image's requirements, and certifi is the CA bundle httpx trusts.
# An advisory can lag the fix, as none named pip 26.2.1 when 26.2.2 distrusted
# the eMudhra/emSign CAs.
ALWAYS_LATEST = ["certifi", "pip"]

# One line each for Python and uv, then one per Debian source package, so that
# openssl's several binary packages read as the one upgrade they are. Each
# command runs only if the one before succeeded, and probe() checks the shape:
# a line gone missing would otherwise shift every version after it.
PROBE = (
    'python -c "import platform; print(platform.python_version())" && '
    "uv --version | cut -d' ' -f2 && "
    "dpkg-query -W -f '${source:Package} ${source:Version}\\n' | sort -u"
)

SECTION_ORDER = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]


def run(*args: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        # The output is captured, so the error alone would give only the exit
        # status; nobody watches a scheduled run, so its log must say why.
        print(f"{' '.join(args)} failed:\n{e.stderr}", file=sys.stderr)
        raise


def resolve(ref: str) -> str:
    """The ref's tag, pinned to the digest the tag points to now."""
    tag = ref.split("@", 1)[0]
    manifest = run(
        "docker", "buildx", "imagetools", "inspect", tag, "--format", "{{json .Manifest}}"
    )
    return f"{tag}@{json.loads(manifest)['digest']}"


def probe(ref: str) -> dict[str, str]:
    """Python, uv and each Debian source package's version in the image.

    Raises CalledProcessError when the image cannot be pulled or run, and
    ValueError when what it printed is not what PROBE prints.
    """
    lines = run("docker", "run", "--rm", ref, "sh", "-c", PROBE).splitlines()
    version = r"\d+\.\d+\.\d+\S*"
    if not (
        len(lines) > 2
        and re.fullmatch(version, lines[0])
        and re.fullmatch(version, lines[1])
        and all(re.fullmatch(r"\S+ \S+", line) for line in lines[2:])
    ):
        raise ValueError(f"unexpected output from {ref}:\n" + "\n".join(lines[:5]))
    python, uv, *packages = lines
    versions = {"Python": python, "uv": uv}
    for line in packages:
        package, package_version = line.split(" ")
        versions[f"Debian {package}"] = package_version
    return versions


def diff(old: dict[str, str], new: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {
        name: (old.get(name), new.get(name))
        for name in sorted(old.keys() | new.keys())
        if old.get(name) != new.get(name)
    }


def describe(name: str, change: tuple[str | None, str | None]) -> str:
    before, after = change
    if before is None:
        return f"{name} {after} (new)"
    if after is None:
        return f"{name} {before} (removed)"
    return f"{name} {before} to {after}"


def refresh_images() -> tuple[list[str], list[str]]:
    """Repin versions.yaml; the changelog's lines for it, and the PR body's."""
    text = VERSIONS.read_text()
    pins = yaml.safe_load(text)["base"]["base_images"]
    # A change, as described, mapped to the Pythons whose image it is in, so
    # one that every image shares (uv, Debian) is said once.
    changes: dict[str, list[str]] = {}
    table = ["| Python | Image | Before | After |", "|---|---|---|---|"]
    rebuilt, unreadable = [], []
    for python, old in pins.items():
        new = resolve(old)
        if new == old:
            continue
        rebuilt.append(python)
        text = text.replace(old, new)
        image, before_digest = old.split("@")
        table.append(
            f"| {python} | `{image}` | `{before_digest[:19]}` | `{new.split('@')[1][:19]}` |"
        )
        # The new image is the one this releases, so one that cannot be read
        # stops the run; an old pin's may no longer be pullable.
        after = probe(new)
        try:
            before = probe(old)
        except (subprocess.CalledProcessError, ValueError):
            unreadable.append(python)
            continue
        for name, change in diff(before, after).items():
            changes.setdefault(describe(name, change), []).append(python)
    if not rebuilt:
        return [], []
    VERSIONS.write_text(text)

    # A change in only some of the rebuilt images says which; a Python version
    # already does.
    parts = []
    for change, pythons in changes.items():
        if len(pythons) < len(rebuilt) and not change.startswith("Python "):
            change += f" ({images(pythons)})"
        parts.append(change)
    if len(rebuilt) == len(pins):
        entry = "Rebuilt on the upstream images' latest builds"
    elif len(rebuilt) == 1:
        entry = f"Rebuilt {images(rebuilt)} on its upstream image's latest build"
    else:
        entry = f"Rebuilt {images(rebuilt)} on their upstream images' latest builds"
    compared = [p for p in rebuilt if p not in unreadable]
    if parts:
        entry += ": " + ", ".join(parts) + "."
    elif compared and unreadable:
        entry += f", with no change to Python, uv or the Debian packages in {images(compared)}."
    elif compared:
        entry += ", with no change to Python, uv or the Debian packages."
    else:
        entry += "."
    if unreadable:
        entry += f" The previous upstream image for {images(unreadable)} could no longer be read to compare."
    body = ["### Upstream images", "", *table, "", *(f"- {part}" for part in parts)]
    return [entry], body


def images(pythons: list[str]) -> str:
    if len(pythons) == 1:
        return f"the {pythons[0]} image"
    return f"the {', '.join(pythons[:-1])} and {pythons[-1]} images"


def locked() -> dict[str, str]:
    versions: dict[str, set[str]] = {}
    for package in tomllib.loads(LOCK.read_text())["package"]:
        if package["name"] != "pipecat-base":
            versions.setdefault(package["name"], set()).add(package["version"])
    return {name: ", ".join(sorted(v)) for name, v in versions.items()}


def audit() -> list[dict]:
    """The locked packages with a known vulnerability, as pip-audit reports them."""
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        run(
            "uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-header",
            "--format", "requirements-txt", "--output-file", str(requirements), cwd=BASE,
        )  # fmt: skip
        result = subprocess.run(
            # OSV carries GitHub's advisories as well as PyPI's.
            [
                "uvx", PIP_AUDIT, "--requirement", str(requirements), "--disable-pip",
                "--no-deps", "--vulnerability-service", "osv", "--format", "json",
                "--progress-spinner", "off",
            ],
            capture_output=True,
            text=True,
        )  # fmt: skip
    # pip-audit exits 1 when it finds a vulnerability; anything else failed.
    if result.returncode not in (0, 1):
        sys.exit(f"pip-audit failed:\n{result.stderr}")
    return [d for d in json.loads(result.stdout)["dependencies"] if d.get("vulns")]


def first_fix(dependency: dict) -> str | None:
    """The lowest version that fixes every one of the dependency's vulnerabilities.

    None when a vulnerability names no fixed version.
    """
    current = Version(dependency["version"])
    needed = []
    for vuln in dependency["vulns"]:
        fixes = [Version(f) for f in vuln.get("fix_versions", []) if Version(f) > current]
        if not fixes:
            return None
        needed.append(min(fixes))
    return str(max(needed))


def raise_floors(fixes: dict[str, str]) -> list[str]:
    """Raise each direct dependency's floor to the release that fixed it.

    pyproject.toml's floors are the image's security minimums, so one left at a
    version the audit rejects would let a later lock go back to it. A transitive
    package is left to the lock and next week's audit.
    """
    text = PYPROJECT.read_text()
    raised = []
    for dependency in tomllib.loads(text)["project"]["dependencies"]:
        requirement = Requirement(dependency)
        fix = fixes.get(canonicalize_name(requirement.name))
        if fix is None:
            continue
        spec, semicolon, marker = dependency.partition(";")
        if re.search(r">=\s*[^,\s]+", spec):
            spec = re.sub(r">=\s*[^,\s]+", f">={fix}", spec, count=1)
        else:
            spec = spec.rstrip() + ("," if requirement.specifier else "") + f">={fix}"
        new = spec + semicolon + marker
        # As written in the file: either TOML string quote, and not one whose
        # escapes make it read differently from what tomllib returned.
        quoted = [f"{q}{dependency}{q}" for q in "\"'" if f"{q}{dependency}{q}" in text]
        if not quoted:
            print(f"::warning::Could not find {dependency!r} in pyproject.toml to raise its floor")
            continue
        quote = quoted[0][0]
        text = text.replace(quoted[0], f"{quote}{new}{quote}", 1)
        raised.append(new)
    if raised:
        PYPROJECT.write_text(text)
        # Records the new floors in the lock, which uv sync --locked checks.
        run("uv", "lock", cwd=BASE)
    return raised


def refresh_packages() -> tuple[list[str], list[str], list[str]]:
    """Upgrade vulnerable packages, and pip and certifi; changelog Security and Changed lines, and the PR body's."""
    vulnerable = {d["name"]: d for d in audit()}
    # The first fixing release rather than the latest, so a fix does not bring
    # a new major version of a package along with it.
    upgrades = {name: first_fix(d) for name, d in vulnerable.items()}
    upgrades |= {name: None for name in ALWAYS_LATEST}
    before = locked()
    args = [
        a
        for name, fix in upgrades.items()
        for a in ("--upgrade-package", f"{name}=={fix}" if fix else name)
    ]
    try:
        run("uv", "lock", *args, cwd=BASE)
    except subprocess.CalledProcessError:
        # A first fix the other locked packages cannot take: let the resolver
        # choose each package's version instead.
        print("uv lock could not take the first fixes, so takes the latest instead")
        run("uv", "lock", *(a for name in upgrades for a in ("--upgrade-package", name)), cwd=BASE)
    after = locked()
    changes = diff(before, after)
    if not changes:
        lines = still_vulnerable(list(vulnerable.values()))
        return [], [], ["### Locked packages", *lines] if lines else []
    remaining = audit()
    still = {d["name"] for d in remaining}
    # The ones the audit now accepts. An advisory may name no fixed release,
    # and then the floor is the one the lock moved to.
    resolved = [n for n in vulnerable if n in changes and n not in still]
    raised = raise_floors(
        {
            name: upgrades[name] or after[name]
            for name in resolved
            if name in after and ", " not in after[name]
        }
    )

    fixed = [describe(n, changes[n]) for n in resolved]
    # Upgraded to a release the audit still flags: said as an upgrade, not a fix.
    unresolved = [describe(n, changes[n]) for n in vulnerable if n in changes and n in still]
    latest = [
        describe(n, c) for n, c in changes.items() if n in ALWAYS_LATEST and n not in vulnerable
    ]
    alongside = [
        describe(n, c) for n, c in changes.items() if n not in vulnerable and n not in ALWAYS_LATEST
    ]
    security = []
    if fixed:
        security.append(
            "Update dependencies to resolve security vulnerabilities: " + ", ".join(fixed) + "."
        )
    changed = []
    if unresolved:
        changed.append(
            "Update dependencies with a known vulnerability to their latest releases, "
            "which do not resolve it yet: " + ", ".join(unresolved) + "."
        )
    if latest:
        changed.append(
            "Update pip and certifi, which the image keeps at their latest: "
            + ", ".join(latest)
            + "."
        )
    if alongside:
        changed.append("Update the packages those updates required: " + ", ".join(alongside) + ".")

    body = ["### Locked packages", ""]
    for name, d in vulnerable.items():
        ids = ", ".join(sorted({v["id"] for v in d["vulns"]}))
        status = describe(name, changes[name]) if name in changes else "**not fixed**"
        if name in still and name in changes:
            status += ", **still vulnerable**"
        body.append(f"- {status}: {ids}")
    body += [f"- {line}" for line in latest]
    body += [f"- {line}, alongside" for line in alongside]
    if raised:
        body.append(
            "- Raised the floor in `pyproject.toml`: " + ", ".join(f"`{r}`" for r in raised)
        )
    body += still_vulnerable(remaining)
    return security, changed, body


def still_vulnerable(dependencies: list[dict]) -> list[str]:
    """The PR body's lines for packages left vulnerable, each also a warning on the run."""
    if not dependencies:
        return []
    lines = [
        "",
        "Still vulnerable after the upgrade; a fix may be unreleased, or outside "
        "what `pyproject.toml` allows:",
        "",
    ]
    for d in dependencies:
        fixes = sorted({f for v in d["vulns"] for f in v.get("fix_versions", [])})
        line = f"{d['name']} {d['version']}, fixed in {', '.join(fixes) or 'no release yet'}"
        print(f"::warning::Still vulnerable: {line}")
        lines.append(f"- {line}")
    return lines


def bullet(text: str) -> str:
    return textwrap.fill(
        text, width=79, initial_indent="- ", subsequent_indent="  ", break_on_hyphens=False
    )


def release_changelog(version: str, sections: dict[str, list[str]]) -> str:
    """Add the entries under [Unreleased], then release it as the version.

    Returns what was already under [Unreleased], which ships in this release
    too.
    """
    text = CHANGELOG.read_text()
    unreleased = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[)", text, re.M | re.S)
    if not unreleased:
        sys.exit("CHANGELOG.md has no [Unreleased] section followed by a release")
    parts = re.split(r"^### (.+)\n", unreleased.group(1), flags=re.M)
    # Rewritten from the headings below, so an entry above them would be lost.
    if parts[0].strip():
        sys.exit("CHANGELOG.md has an entry under [Unreleased] with no ### heading above it")
    entries: dict[str, list[str]] = {}
    for name, body in zip(parts[1::2], parts[2::2]):
        entries.setdefault(name, []).append(body.strip())
    for name, lines in sections.items():
        if lines:
            entries.setdefault(name, []).extend(bullet(line) for line in lines)
    names = sorted(
        entries, key=lambda n: SECTION_ORDER.index(n) if n in SECTION_ORDER else len(SECTION_ORDER)
    )
    date = datetime.datetime.now(datetime.UTC).date().isoformat()
    released = "".join(
        f"### {name}\n\n" + "\n\n".join(e for e in entries[name] if e) + "\n\n" for name in names
    )
    section = f"## [Unreleased]\n\n## [{version}] - {date}\n\n{released}"
    CHANGELOG.write_text(text[: unreleased.start()] + section + text[unreleased.end() :])
    return unreleased.group(1).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--body", type=Path, required=True, help="where to write the pull request body"
    )
    args = parser.parse_args()

    image_lines, image_body = refresh_images()
    security, changed, package_body = refresh_packages()
    if not (image_lines or security or changed):
        print(
            "Nothing to release: every pinned image is current and no locked package is vulnerable."
        )
        outputs = {"changed": "false"}
    else:
        run("uv", "version", "--bump", "patch", "--no-sync", cwd=BASE)
        version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
        carried = release_changelog(
            version, {"Changed": image_lines + changed, "Security": security}
        )
        if carried:
            # One level down, under this body's own headings.
            carried = re.sub(r"^### ", "#### ", carried, flags=re.M)
            package_body += [
                "",
                "### Also released",
                "",
                "Already under `[Unreleased]` in `CHANGELOG.md`, so this releases it too:",
                "",
                carried,
            ]
        what = " and ".join(
            w
            for w, did in (
                ("upstream images", image_lines),
                ("locked packages", security or changed),
            )
            if did
        )
        title = f"Release v{version}: refresh {what}"
        args.body.write_text(
            "\n".join(
                [
                    f"Merging this releases v{version}, through `docker-publish.yml`.",
                    "",
                    *image_body,
                    "",
                    *package_body,
                    "",
                    "Generated by `.github/workflows/refresh-base-images.yml`; its next run "
                    "replaces this branch, so a change made here belongs in a separate pull request.",
                ]
            )
            + "\n"
        )
        print(title)
        outputs = {"changed": "true", "version": version, "title": title}
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in outputs.items())


if __name__ == "__main__":
    main()
