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


def _run_parse(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
        env={**os.environ, **(env or {})},
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

    def test_token_can_come_from_the_environment(self):
        # The form curl|sh and cloud-init are told to use: the token never
        # reaches this script's argv, so `ps` cannot see it.
        result = _run_parse("--agent", env={"HLE_AGENT_TOKEN": "hle_env"})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=1 NAME= SET=0 TOKEN=hle_env\n"

    def test_the_environment_alone_does_not_turn_on_enrollment(self):
        # HLE_AGENT_TOKEN is also set on machines that already run an agent; a
        # plain install or upgrade there must not re-enroll.
        result = _run_parse(env={"HLE_AGENT_TOKEN": "hle_env"})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=0 NAME= SET=0 TOKEN=\n"

    def test_name_ignores_an_inherited_environment_token(self):
        result = _run_parse("--name", "laptop", env={"HLE_AGENT_TOKEN": "hle_env"})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=1 NAME=laptop SET=1 TOKEN=\n"

    def test_an_explicit_token_wins_over_the_environment(self):
        result = _run_parse("--token", "hle_arg", env={"HLE_AGENT_TOKEN": "hle_env"})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "AGENT=1 NAME= SET=0 TOKEN=hle_arg\n"


def _run_version_ge(a: str, b: str) -> int:
    source = INSTALL_SH.read_text()
    func = _extract_function(source, "version_ge")
    result = subprocess.run(  # noqa: S603 — fixed argv, local script
        ["sh", "-c", f'{func}\nversion_ge "$1" "$2"', "install.sh", a, b],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


class TestVersionFloor:
    """install.sh refuses a version older than MIN_VERSION (2610.13)."""

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("2609.9", "2609.9", 0),
            ("2610.19", "2609.9", 0),
            ("2609.10", "2609.9", 0),
            ("2610.0", "2609.9", 0),
            ("2609.8", "2609.9", 1),
            ("2609.0", "2609.9", 1),
            ("2608.99", "2609.9", 1),
        ],
    )
    def test_version_ge(self, a, b, expected):
        assert _run_version_ge(a, b) == expected

    def test_the_floor_is_the_first_stdin_enroll_release(self):
        source = INSTALL_SH.read_text()
        assert 'MIN_VERSION="2610.13"' in source


def _run_agent_enroll(
    tmp_path: Path, *, name: str = "laptop", enrolled: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "hle.log"
    source = INSTALL_SH.read_text()
    funcs = "\n\n".join(_extract_function(source, f) for f in ("agent_is_enrolled", "agent_enroll"))
    status = "return 0" if enrolled else 'echo "No agent token configured"; return 1'
    body = (
        "info() { :; }\n"
        "warn() { :; }\n"
        "has_tty() { return 1; }\n"
        f"LOG='{log}'\n"
        "hle() {\n"
        '    echo "$@" >> "$LOG"\n'
        '    if [ "$1" = "agent" ] && [ "$2" = "status" ]; then\n'
        f"        {status}\n"
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


def _run_token_enroll(tmp_path: Path) -> tuple[int, str, str, str]:
    """Run agent_enroll with a token, and report how the token was passed.

    Returns (exit code, stderr, logged argv, text delivered on stdin).
    """
    log = tmp_path / "hle.log"
    stdin_log = tmp_path / "stdin.log"
    funcs = _extract_function(INSTALL_SH.read_text(), "agent_enroll")
    body = (
        f"LOG='{log}'\nSTDIN_LOG='{stdin_log}'\n"
        "hle() {\n"
        '    echo "$@" >> "$LOG"\n'
        '    case " $* " in *" --stdin "*) cat >> "$STDIN_LOG" ;; esac\n'
        "}\n"
        "AGENT_TOKEN='hle_tok'\nAGENT_NAME=''\nagent_enroll\n"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, local script
        ["sh", "-c", f"{funcs}\n{body}", "install.sh"],
        capture_output=True,
        text=True,
        check=False,
    )
    logged = log.read_text().strip() if log.exists() else ""
    piped = stdin_log.read_text() if stdin_log.exists() else ""
    return result.returncode, result.stderr, logged, piped


class TestAgentEnrollToken:
    def test_token_is_piped_not_put_in_argv(self, tmp_path):
        code, err, logged, piped = _run_token_enroll(tmp_path)
        assert code == 0, err
        assert logged == "agent enroll --stdin --force"
        assert "hle_tok" not in logged
        assert piped == "hle_tok"

    def test_there_is_no_argv_fallback(self):
        # The floor (2610.13) guarantees --stdin, so the token must never be
        # passed as an argument, where `ps` could read it.
        source = _extract_function(INSTALL_SH.read_text(), "agent_enroll")
        assert '"$AGENT_TOKEN" ||' not in source
        assert "agent_enroll_supports" not in INSTALL_SH.read_text()


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


def _run_agent_enroll_no_name(
    tmp_path: Path, *, status_output: str, status_code: int
) -> subprocess.CompletedProcess[str]:
    """Run agent_enroll with no token, no name and no terminal.

    `hle agent status` exits 0 whether or not a token exists, so the output is
    the only thing that tells the two apart. The stub prints *status_output*.
    """
    funcs = "\n\n".join(
        _extract_function(INSTALL_SH.read_text(), f) for f in ("agent_is_enrolled", "agent_enroll")
    )
    body = (
        'info() { printf "INFO %s\\n" "$1"; }\n'
        'warn() { printf "WARN %s\\n" "$1" >&2; }\n'
        "has_tty() { return 1; }\n"
        "hle() {\n"
        '    if [ "$1" = "agent" ] && [ "$2" = "status" ]; then\n'
        f"        printf '%s\\n' '{status_output}'\n"
        f"        return {status_code}\n"
        "    fi\n"
        "    return 0\n"
        "}\n"
        "AGENT_TOKEN=''\nAGENT_NAME=''\nagent_enroll\n"
    )
    return subprocess.run(  # noqa: S603 — fixed argv, local script
        ["sh", "-c", f"{funcs}\n{body}", "install.sh"],
        capture_output=True,
        text=True,
        check=False,
    )


class TestAgentEnrollDetectsAMissingToken:
    def test_no_token_output_is_not_enrolled(self, tmp_path):
        """Exits 0 with no token; the installer must not read that as enrolled."""
        result = _run_agent_enroll_no_name(
            tmp_path, status_output="No agent token configured", status_code=0
        )
        assert result.returncode != 0
        assert "already enrolled" not in result.stdout + result.stderr
        assert "no terminal to prompt on" in result.stderr

    def test_a_configured_token_is_enrolled(self, tmp_path):
        result = _run_agent_enroll_no_name(
            tmp_path, status_output="Token: hlea_xxxx", status_code=0
        )
        assert result.returncode == 0
        assert "already enrolled" in result.stdout

    def test_an_older_client_exiting_1_is_still_not_enrolled(self, tmp_path):
        """Older clients printed the same line and exited 1."""
        result = _run_agent_enroll_no_name(
            tmp_path, status_output="No agent token configured", status_code=1
        )
        assert result.returncode != 0
        assert "already enrolled" not in result.stdout + result.stderr


def _run_agent_install_service(
    tmp_path: Path,
    *,
    scope: str,
    uid: str,
    sudo_installed: bool,
    sudo_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run agent_install_service with fake hle/id/sudo on a scratch PATH.

    Returns (result, log) where log records each `hle`/`sudo` invocation.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"

    (bindir / "hle").write_text(f'#!/bin/sh\necho "hle $*" >> "{log}"\nexit 0\n')
    (bindir / "hle").chmod(0o755)
    (bindir / "id").write_text(f'#!/bin/sh\necho {uid}\n')
    (bindir / "id").chmod(0o755)
    if sudo_installed:
        (bindir / "sudo").write_text(f'#!/bin/sh\necho "sudo $*" >> "{log}"\nexit {sudo_exit}\n')
        (bindir / "sudo").chmod(0o755)

    func = _extract_function(INSTALL_SH.read_text(), "agent_install_service")
    body = (
        "info() { :; }\n"
        'warn() { printf "WARN %s\\n" "$1" >&2; }\n'
        "INSTALL_SERVICE=1\n"
        f'SERVICE_SCOPE="{scope}"\n'
        "agent_install_service\n"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, local script + scratch dir
        ["/bin/sh", "-c", f"{func}\n{body}", "install.sh"],
        capture_output=True,
        text=True,
        check=False,
        # Only the scratch bin: no real sudo can leak in and change the branch.
        env={**os.environ, "PATH": str(bindir)},
    )
    logged = log.read_text() if log.exists() else ""
    return result, logged


class TestAgentServiceEscalates:
    """`--agent --system` as a normal user escalates by itself.

    It used to fail with "Permission denied writing /etc/systemd/system/…" and
    tell the user to run `sudo hle …`, which sudo cannot find: its secure_path
    drops ~/.local/bin. The installer now resolves hle first and runs it through
    sudo by absolute path.
    """

    def test_system_scope_as_a_normal_user_escalates(self, tmp_path):
        result, logged = _run_agent_install_service(
            tmp_path, scope="--system", uid="1000", sudo_installed=True
        )
        assert result.returncode == 0, result.stderr
        hle = tmp_path / "bin" / "hle"
        assert logged.strip() == f"sudo {hle} daemon install agent --system"

    def test_root_does_not_escalate(self, tmp_path):
        result, logged = _run_agent_install_service(
            tmp_path, scope="--system", uid="0", sudo_installed=True
        )
        assert result.returncode == 0, result.stderr
        assert logged.strip() == "hle daemon install agent --system"

    def test_user_scope_is_not_escalated(self, tmp_path):
        result, logged = _run_agent_install_service(
            tmp_path, scope="--user", uid="1000", sudo_installed=True
        )
        assert result.returncode == 0, result.stderr
        assert logged.strip() == "hle daemon install agent --user"

    def test_auto_scope_is_not_escalated(self, tmp_path):
        result, logged = _run_agent_install_service(
            tmp_path, scope="", uid="1000", sudo_installed=True
        )
        assert result.returncode == 0, result.stderr
        assert logged.strip() == "hle daemon install agent"

    def test_no_sudo_falls_through_to_the_manual_hint(self, tmp_path):
        result, logged = _run_agent_install_service(
            tmp_path, scope="--system", uid="1000", sudo_installed=False
        )
        assert result.returncode == 1
        assert logged == ""
        hle = tmp_path / "bin" / "hle"
        assert f"sudo {hle} daemon install agent --system" in result.stderr

    def test_the_hint_names_the_absolute_path(self, tmp_path):
        result, _ = _run_agent_install_service(
            tmp_path, scope="--system", uid="1000", sudo_installed=True, sudo_exit=1
        )
        assert result.returncode == 1
        hle = tmp_path / "bin" / "hle"
        assert f"sudo {hle} daemon install agent --system" in result.stderr
        assert "sudo hle " not in result.stderr

