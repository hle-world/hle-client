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

import pytest

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


def _run_ensure_venv(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    source = INSTALL_SH.read_text()
    func = _extract_function(source, "ensure_venv_support")
    body = (
        'info() { printf "INFO %s\\n" "$1"; }\n'
        'error() { printf "ERROR %s\\n" "$1" >&2; }\n'
        'ensure_venv_support "$1"'
    )
    return subprocess.run(  # noqa: S603 — fixed argv, local script + scratch dir
        ["/bin/sh", "-c", f"{func}\n{body}", "install.sh", "python3"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def _make_fake_bin(
    tmp_path: Path,
    *,
    uid: str = "1000",
    apt: bool = True,
    sudo_ok: bool = False,
    venv: bool = False,
    empty_lists: bool = False,
) -> tuple[dict[str, str], Path]:
    """Fake python3/apt-get/sudo/id on a scratch PATH.

    The fake python3 fails `import ensurepip, venv` exactly once, so a
    successful install is observable: the re-check after installing passes.
    Returns the child env and the path to the apt log.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    mark = state / "venv-ok"
    if venv:
        mark.write_text("")
    apt_log = state / "apt.log"

    (bindir / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"import ensurepip, venv"*)\n'
        '    [ -f "$HLE_FAKE_VENV_MARK" ] && exit 0\n'
        '    : > "$HLE_FAKE_VENV_MARK"\n'
        "    exit 1 ;;\n"
        '  *"sys.version_info"*) echo 3.12; exit 0 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    (bindir / "python3").chmod(0o755)

    (bindir / "id").write_text(f'#!/bin/sh\n[ "$1" = "-u" ] && echo "{uid}"\nexit 0\n')
    (bindir / "id").chmod(0o755)

    (bindir / "sudo").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-n" ] && [ "$2" = "true" ]; then\n'
        '  [ "$HLE_FAKE_SUDO_OK" = "1" ] && exit 0\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "-n" ] && [ "$2" = "apt-get" ]; then\n'
        "  shift 2\n"
        '  echo "sudo apt-get $*" >> "$HLE_FAKE_APT_LOG"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    (bindir / "sudo").chmod(0o755)

    if apt:
        # With empty_lists, install fails until `apt-get update` has run, like a
        # fresh cloud image.
        updated = state / "updated"
        if not empty_lists:
            updated.write_text("")
        (bindir / "apt-get").write_text(
            "#!/bin/sh\n"
            'echo "apt-get $*" >> "$HLE_FAKE_APT_LOG"\n'
            f'[ "$1" = "update" ] && : > "{updated}" && exit 0\n'
            f'[ -f "{updated}" ]\n'
        )
        (bindir / "apt-get").chmod(0o755)

    env = {
        "PATH": str(bindir),
        "HLE_FAKE_VENV_MARK": str(mark),
        "HLE_FAKE_APT_LOG": str(apt_log),
        "HLE_FAKE_SUDO_OK": "1" if sudo_ok else "",
    }
    return env, apt_log


class TestEnsureVenvSupport:
    def test_venv_available_makes_no_apt_call(self, tmp_path):
        env, apt_log = _make_fake_bin(tmp_path, venv=True)
        result = _run_ensure_venv(env)
        assert result.returncode == 0, result.stderr
        assert not apt_log.exists()

    def test_root_installs_venv_package(self, tmp_path):
        env, apt_log = _make_fake_bin(tmp_path, uid="0")
        result = _run_ensure_venv(env)
        assert result.returncode == 0, result.stderr
        assert "apt-get install -y python3.12-venv" in apt_log.read_text()

    def test_root_updates_empty_apt_lists_and_retries(self, tmp_path):
        env, apt_log = _make_fake_bin(tmp_path, uid="0", empty_lists=True)
        result = _run_ensure_venv(env)
        assert result.returncode == 0, result.stderr
        assert apt_log.read_text().splitlines() == [
            "apt-get install -y python3.12-venv",
            "apt-get update -qq",
            "apt-get install -y python3.12-venv",
        ]

    def test_passwordless_sudo_installs_venv_package(self, tmp_path):
        env, apt_log = _make_fake_bin(tmp_path, sudo_ok=True)
        result = _run_ensure_venv(env)
        assert result.returncode == 0, result.stderr
        assert "sudo apt-get install -y python3.12-venv" in apt_log.read_text()

    def test_no_sudo_exits_with_the_exact_command(self, tmp_path):
        env, _ = _make_fake_bin(tmp_path)
        result = _run_ensure_venv(env)
        assert result.returncode == 1
        assert "sudo apt install python3.12-venv" in result.stderr

    def test_no_apt_get_exits(self, tmp_path):
        env, _ = _make_fake_bin(tmp_path, apt=False)
        result = _run_ensure_venv(env)
        assert result.returncode == 1
        assert "python venv package" in result.stderr


def _run_parse(*args: str) -> subprocess.CompletedProcess[str]:
    source = INSTALL_SH.read_text()
    funcs = "\n\n".join(
        _extract_function(source, name) for name in ("validate_agent_name", "parse_args")
    )
    body = (
        "usage() { :; }\n"
        "AGENT=0; AGENT_TOKEN=''; AGENT_NAME=''; AGENT_NAME_SET=0\n"
        'parse_args "$@"\n'
        'printf "AGENT=%s NAME=%s SET=%s TOKEN=%s\\n" '
        '"$AGENT" "$AGENT_NAME" "$AGENT_NAME_SET" "$AGENT_TOKEN"'
    )
    return subprocess.run(  # noqa: S603 — fixed argv, local script
        ["sh", "-c", f"{funcs}\n{body}", "install.sh", *args],
        capture_output=True,
        text=True,
        check=False,
    )


class TestAgentNameArg:
    """`--name NAME` parses like the other flags and is validated before use."""

    def test_name_implies_agent(self):
        result = _run_parse("--name", "laptop")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=1 NAME=laptop SET=1 TOKEN=\n"

    def test_name_equals_form(self):
        result = _run_parse("--name=laptop")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=1 NAME=laptop SET=1 TOKEN=\n"

    def test_without_name_nothing_is_set(self):
        result = _run_parse("--agent")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=1 NAME= SET=0 TOKEN=\n"

    @pytest.mark.parametrize("value", ["", "   ", "x" * 101])
    def test_blank_whitespace_or_overlong_names_are_refused(self, value):
        result = _run_parse("--name", value)
        assert result.returncode == 1
        assert "Invalid agent name" in result.stderr

    def test_token_and_name_together_are_refused(self):
        result = _run_parse("--token", "hle_x", "--name", "laptop")
        assert result.returncode == 1
        assert "not both" in result.stderr


def _run_agent_enroll(
    tmp_path: Path, *, name: str = "laptop", enrolled: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "hle.log"
    source = INSTALL_SH.read_text()
    funcs = _extract_function(source, "agent_enroll")
    body = (
        "info() { :; }\n"
        "warn() { :; }\n"
        "has_tty() { return 1; }\n"
        f"LOG='{log}'\n"
        "hle() {\n"
        '    echo "$@" >> "$LOG"\n'
        '    if [ "$1" = "agent" ] && [ "$2" = "status" ]; then\n'
        f"        return {0 if enrolled else 1}\n"
        "    fi\n"
        "    return 0\n"
        "}\n"
        "AGENT_TOKEN=''\n"
        f"AGENT_NAME='{name}'\n"
        "agent_enroll\n"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, local script
        ["sh", "-c", f"{funcs}\n{body}", "install.sh"],
        capture_output=True,
        text=True,
        check=False,
    )
    logged = log.read_text() if log.exists() else ""
    return result, logged


def _run_token_enroll(tmp_path: Path, *, client_has_force: bool) -> tuple[int, str, str]:
    log = tmp_path / "hle.log"
    funcs = _extract_function(INSTALL_SH.read_text(), "agent_enroll")
    help_text = "--name TEXT  --force" if client_has_force else "--name TEXT"
    body = (
        f"LOG='{log}'\n"
        "hle() {\n"
        '    if [ "$3" = "--help" ]; then echo "' + help_text + '"; return 0; fi\n'
        '    echo "$@" >> "$LOG"\n'
        "}\n"
        "AGENT_TOKEN='hle_tok'\nAGENT_NAME=''\nagent_enroll\n"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, local script
        ["sh", "-c", f"{funcs}\n{body}", "install.sh"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stderr, log.read_text().strip()


class TestAgentEnrollToken:
    def test_token_rerun_passes_force_so_it_still_replaces(self, tmp_path):
        code, err, logged = _run_token_enroll(tmp_path, client_has_force=True)
        assert code == 0, err
        assert logged == "agent enroll --force hle_tok"

    def test_an_older_pinned_client_without_force_still_enrolls(self, tmp_path):
        # `--version` can install a client that predates --force; passing it
        # there would fail the whole install with "no such option".
        code, err, logged = _run_token_enroll(tmp_path, client_has_force=False)
        assert code == 0, err
        assert logged == "agent enroll hle_tok"


class TestAgentEnrollName:
    def test_name_runs_enroll_with_the_name(self, tmp_path):
        result, logged = _run_agent_enroll(tmp_path, name="laptop")
        assert result.returncode == 0, result.stderr
        assert "agent enroll --name laptop" in logged
        assert "--token" not in logged

    def test_already_enrolled_keeps_the_token_and_does_not_enroll(self, tmp_path):
        result, logged = _run_agent_enroll(tmp_path, name="laptop", enrolled=True)
        assert result.returncode == 0, result.stderr
        assert logged.strip() == "agent status"
