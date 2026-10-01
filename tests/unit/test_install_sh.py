"""install.sh: linking hle into the system PATH must not clobber brew's link.

Homebrew installs /usr/local/bin/hle (or /opt/homebrew/bin/hle) as a symlink
into its Cellar, so the installer's "never overwrite a non-symlink" guard waved
it through and replaced a brew-owned link with one of ours. These tests pin the
detection and the refusal, without touching the real /usr/local/bin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"


def _extract_function(source: str, name: str) -> str:
    lines = source.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == f"{name}() {{"),
        None,
    )
    assert start is not None, f"{name}() not found in install.sh"
    end = next((j for j in range(start + 1, len(lines)) if lines[j] == "}"), None)
    assert end is not None, f"closing brace for {name}() not found"
    return "\n".join(lines[start : end + 1])


def _run(
    body: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    source = INSTALL_SH.read_text()
    funcs = "\n\n".join(
        _extract_function(source, name) for name in ("is_brew_symlink", "link_into_system_path")
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, local script + paths
        ["sh", "-c", f"{funcs}\n{body}", "install.sh", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    return result


def _make_symlink(tmp_path: Path, target: str) -> Path:
    link = tmp_path / "hle"
    link.symlink_to(target)
    return link


class TestIsBrewSymlink:
    def test_absolute_cellar_link(self, tmp_path):
        link = _make_symlink(tmp_path, "/usr/local/Cellar/hle-client/1.0/bin/hle")
        result = _run('is_brew_symlink "$1"', str(link))
        assert result.returncode == 0

    def test_relative_cellar_link(self, tmp_path):
        # brew's links are normally relative, e.g. ../Cellar/hle-client/...
        (tmp_path / "Cellar/hle-client/1.0/bin").mkdir(parents=True)
        (tmp_path / "Cellar/hle-client/1.0/bin/hle").touch()
        (tmp_path / "bin").mkdir()
        link = tmp_path / "bin/hle"
        link.symlink_to("../Cellar/hle-client/1.0/bin/hle")
        result = _run('is_brew_symlink "$1"', str(link))
        assert result.returncode == 0

    def test_apple_silicon_prefix_link(self, tmp_path):
        link = _make_symlink(tmp_path, "/opt/homebrew/opt/hle-client/bin/hle")
        result = _run('is_brew_symlink "$1"', str(link))
        assert result.returncode == 0

    def test_installer_link_is_not_brew(self, tmp_path):
        link = _make_symlink(tmp_path, str(tmp_path / "current/bin/hle"))
        result = _run('is_brew_symlink "$1"', str(link))
        assert result.returncode != 0

    def test_regular_file_is_not_brew(self, tmp_path):
        target = tmp_path / "hle"
        target.write_text("#!/bin/sh\n")
        result = _run('is_brew_symlink "$1"', str(target))
        assert result.returncode != 0

    def test_missing_path_is_not_brew(self, tmp_path):
        result = _run('is_brew_symlink "$1"', str(tmp_path / "absent"))
        assert result.returncode != 0


class TestLinkIntoSystemPath:
    def test_refuses_brew_link_and_keeps_it(self, tmp_path):
        system_bin = tmp_path / "system-bin"
        system_bin.mkdir()
        link = system_bin / "hle"
        link.symlink_to("/usr/local/Cellar/hle-client/1.0/bin/hle")
        new_target = tmp_path / "current/bin/hle"
        new_target.parent.mkdir(parents=True)
        new_target.touch()

        result = _run(
            "id() { echo 0; }\n"
            'info() { printf "INFO %s\\n" "$1"; }\n'
            'warn() { printf "WARN %s\\n" "$1" >&2; }\n'
            'link_into_system_path "$1"',
            str(new_target),
            env={"PATH": f"{system_bin}:{os.environ['PATH']}", "SYSTEM_PATH_DIRS": str(system_bin)},
        )

        assert result.returncode == 0, result.stderr
        assert "Homebrew" in result.stderr
        assert "brew upgrade hle-client" in result.stderr
        assert os.readlink(link) == "/usr/local/Cellar/hle-client/1.0/bin/hle"

    def test_replaces_our_own_older_link_with_the_new_target(self, tmp_path):
        """A non-brew symlink is ours to replace; the brew check must not
        clobber the ``target`` the new link points at."""
        system_bin = tmp_path / "system-bin"
        system_bin.mkdir()
        (system_bin / "hle").symlink_to(tmp_path / "old/bin/hle")
        new_target = tmp_path / "current/bin/hle"
        new_target.parent.mkdir(parents=True)
        new_target.touch()

        result = _run(
            "id() { echo 0; }\n"
            'info() { printf "INFO %s\\n" "$1"; }\n'
            'warn() { printf "WARN %s\\n" "$1" >&2; }\n'
            'link_into_system_path "$1"',
            str(new_target),
            env={"PATH": f"{system_bin}:{os.environ['PATH']}", "SYSTEM_PATH_DIRS": str(system_bin)},
        )

        assert result.returncode == 0, result.stderr
        assert os.readlink(system_bin / "hle") == str(new_target)

    def test_links_when_path_is_free(self, tmp_path):
        system_bin = tmp_path / "system-bin"
        system_bin.mkdir()
        new_target = tmp_path / "current/bin/hle"
        new_target.parent.mkdir(parents=True)
        new_target.touch()

        result = _run(
            "id() { echo 0; }\n"
            'info() { printf "INFO %s\\n" "$1"; }\n'
            'warn() { printf "WARN %s\\n" "$1" >&2; }\n'
            'link_into_system_path "$1"',
            str(new_target),
            env={"PATH": f"{system_bin}:{os.environ['PATH']}", "SYSTEM_PATH_DIRS": str(system_bin)},
        )

        assert result.returncode == 0, result.stderr
        assert os.readlink(system_bin / "hle") == str(new_target)
