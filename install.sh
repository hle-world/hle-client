#!/bin/sh
# HLE Client installer
#
# Usage:
#   curl -fsSL https://get.hle.world | sh
#   curl -fsSL https://get.hle.world | sh -s -- --version 2608.4
#
#   # Install the agent and run it as a service (prompts for the token):
#   curl -fsSL https://get.hle.world | sh -s -- --agent
#
#   # Fully unattended (CI, cloud-init, Ansible):
#   curl -fsSL https://get.hle.world | sh -s -- --agent --token hlea_xxxxx
set -e

PACKAGE="hle-client"
VERSION=""
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11

AGENT=0
AGENT_TOKEN=""
INSTALL_SERVICE=1
SERVICE_SCOPE=""   # "", "--user", or "--system"; empty lets the CLI auto-detect
SYSTEM_LINK=""     # set when hle was linked into a directory already on PATH
MODIFY_PATH=1      # every published instruction says to run `hle`; make it work

usage() {
    cat <<'EOF'
HLE Client installer

Options:
  --version <v>     Install a specific hle-client version
  --agent           Enroll this machine as an HLE agent and run it as a service
  --token <token>   Agent enrollment token (hlea_...); implies --agent.
                    Without it, --agent prompts on the terminal.
  --no-service      With --agent: enroll only, don't install a service
  --no-modify-path  Don't add ~/.local/bin to your shell's PATH
  --user            Install a per-user service (no sudo; needs linger on Linux)
  --system          Install a system-wide service (starts at boot; needs sudo)
  -h, --help        Show this help

Without --agent this installs the CLI only, exactly as before.
EOF
}

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --version=*) VERSION="${1#*=}"; shift ;;
        --agent) AGENT=1; shift ;;
        --token) AGENT_TOKEN="$2"; AGENT=1; shift 2 ;;
        --token=*) AGENT_TOKEN="${1#*=}"; AGENT=1; shift ;;
        --no-service) INSTALL_SERVICE=0; shift ;;
        --no-modify-path) MODIFY_PATH=0; shift ;;
        --user) SERVICE_SCOPE="--user"; shift ;;
        --system) SERVICE_SCOPE="--system"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ -n "$VERSION" ]; then
    INSTALL_SPEC="${PACKAGE}==${VERSION}"
else
    INSTALL_SPEC="${PACKAGE}"
fi

# --- Helpers ---

info() { printf '\033[0;34m[hle]\033[0m %s\n' "$1"; }
success() { printf '\033[0;32m[hle]\033[0m %s\n' "$1"; }
error() { printf '\033[0;31m[hle]\033[0m %s\n' "$1" >&2; }
warn() { printf '\033[0;33m[hle]\033[0m %s\n' "$1" >&2; }

# True when we can talk to a human. Under `curl ... | sh` stdin is the script
# itself, so every prompt must read from /dev/tty instead.
has_tty() { [ -r /dev/tty ] && [ -w /dev/tty ]; }

prompt_yn() {
    has_tty || return 1
    printf '\033[0;34m[hle]\033[0m %s [y/N] ' "$1" > /dev/tty
    read -r answer < /dev/tty
    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# Detect OS
detect_os() {
    case "$(uname -s)" in
        Linux*) echo "linux" ;;
        Darwin*) echo "macos" ;;
        FreeBSD*) echo "freebsd" ;;
        *) error "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
}

# Any 3.11+ interpreter will do, and which one is available differs by
# platform: pfSense 2.7 carries python311 as a dependency of unbound, while
# OPNsense ships python313 and has no python311 package at all. So never name a
# version — suggest the meta-package and a search, or the advice is wrong on
# somebody's box. Every dependency is pure Python as of 2608.2, so pip needs no
# compiler; the interpreter is the only prerequisite.
FREEBSD_PKGS="python3"

# Find Python 3.11+
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge "$MIN_PYTHON_MAJOR" ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

# Ensure ~/.local/bin is in PATH
ensure_local_bin() {
    mkdir -p "$HOME/.local/bin"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            # Already reachable from a system directory, so there is nothing to
            # add and no shell to guess at.
            if [ -n "${SYSTEM_LINK:-}" ]; then
                export PATH="$HOME/.local/bin:$PATH"
                return 0
            fi
            SHELL_NAME=$(basename "${SHELL:-}" 2>/dev/null || echo "")
            # $SHELL is the login shell from /etc/passwd, which is not always
            # the shell you are typing into. pfSense's admin account has
            # /etc/rc.initial — its console menu — and picking the shell option
            # execs tcsh without updating $SHELL, so this used to resolve to
            # "rc.initial", fall through to .profile, and write a file tcsh
            # never reads. The install then reported success and changed
            # nothing. Ask the parent process what it actually is whenever
            # $SHELL names something that isn't a shell we know.
            case "$SHELL_NAME" in
                zsh|bash|fish|csh|tcsh|sh|ksh|dash) ;;
                *)
                    PARENT=$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d ' ') || PARENT=""
                    # A login shell shows up as "-tcsh"; the dash is not part
                    # of the name.
                    PARENT=${PARENT#-}
                    [ -n "$PARENT" ] && SHELL_NAME=$(basename "$PARENT")
                    ;;
            esac

            # csh/tcsh use a different syntax and never read .profile.
            PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
            RELOAD_HINT="restart your shell or run: . $HOME/.profile"
            case "$SHELL_NAME" in
                zsh) RC_FILE="$HOME/.zshrc"; RELOAD_HINT="restart your shell" ;;
                bash) RC_FILE="$HOME/.bashrc"; RELOAD_HINT="restart your shell" ;;
                fish)
                    RC_FILE="$HOME/.config/fish/config.fish"
                    PATH_LINE='set -gx PATH $HOME/.local/bin $PATH'
                    RELOAD_HINT="restart your shell"
                    ;;
                csh|tcsh)
                    RC_FILE="$HOME/.cshrc"
                    PATH_LINE='set path = ( $HOME/.local/bin $path )'
                    # tcsh caches what is on the path and will keep saying
                    # "Command not found." until told to look again.
                    RELOAD_HINT="run: rehash"
                    ;;
                *) RC_FILE="$HOME/.profile" ;;
            esac
            # Written without asking. Every instruction we publish says to run
            # `hle`, so an install that leaves it off PATH has not finished the
            # job — and the prompt defaulted to No, which meant the common
            # answer was the broken one. --no-modify-path opts out.
            if [ "$MODIFY_PATH" -eq 1 ] && [ -n "$RC_FILE" ]; then
                if [ -f "$RC_FILE" ] && grep -qF '.local/bin' "$RC_FILE" 2>/dev/null; then
                    info "PATH already set up in $RC_FILE"
                elif echo "$PATH_LINE" >> "$RC_FILE" 2>/dev/null; then
                    info "Added ~/.local/bin to PATH in $RC_FILE"
                    info "  For this shell: $RELOAD_HINT"
                else
                    warn "Could not write $RC_FILE. Add this line yourself:"
                    warn "    $PATH_LINE"
                fi
            elif [ "$MODIFY_PATH" -eq 0 ]; then
                # Say exactly what to run, in the right syntax. "Add it
                # yourself" leaves csh users to discover that the obvious
                # export line does nothing.
                info "Left PATH alone (--no-modify-path). Use $HOME/.local/bin/hle, or add:"
                info "    $PATH_LINE"
                info "  to $RC_FILE"
            fi
            export PATH="$HOME/.local/bin:$PATH"
            ;;
    esac
}

# Verify installed package integrity
verify_install() {
    PYTHON="$1"
    info "Verifying package integrity..."
    # Check the installed package metadata matches PyPI
    INSTALLED_VERSION=$("$PYTHON" -c "import hle_client; print(hle_client.__version__)" 2>/dev/null) || return 0
    if [ -n "$VERSION" ] && [ "$INSTALLED_VERSION" != "$VERSION" ]; then
        error "Version mismatch: expected $VERSION, got $INSTALLED_VERSION"
        exit 1
    fi
    info "Verified: hle-client==$INSTALLED_VERSION"
}

# --- Installation methods ---

install_with_pipx() {
    if pipx list --short 2>/dev/null | grep -q "^${PACKAGE} "; then
        info "Upgrading with pipx..."
        pipx upgrade "$PACKAGE"
    else
        info "Installing with pipx..."
        pipx install "$INSTALL_SPEC"
    fi
}

install_with_uv() {
    if uv tool list 2>/dev/null | grep -q "^${PACKAGE} "; then
        info "Upgrading with uv tool..."
        uv tool upgrade "$PACKAGE"
    else
        info "Installing with uv tool..."
        uv tool install "$INSTALL_SPEC"
    fi
}

install_with_venv() {
    PYTHON="$1"
    VENV_DIR="$HOME/.local/share/hle/venv"

    info "Installing in isolated venv at $VENV_DIR..."
    rm -rf "$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet "$INSTALL_SPEC"

    # Verify before symlinking
    verify_install "$VENV_DIR/bin/python"

    # Symlink the hle binary
    mkdir -p "$HOME/.local/bin"
    ln -sf "$VENV_DIR/bin/hle" "$HOME/.local/bin/hle"
    link_into_system_path "$VENV_DIR/bin/hle"
    ensure_local_bin
}

# Put hle somewhere already on PATH, rather than trusting a shell rc file.
#
# Editing an rc file is a guess about which shell will read it, and on FreeBSD
# firewalls the guess was wrong: pfSense and OPNsense give root csh/tcsh, which
# never reads .profile, so the install reported success and `hle` was still not
# found. A symlink in a directory that is already on the default PATH needs no
# guess and works in every shell, including the console menu's.
link_into_system_path() {
    target="$1"
    # Root only: writing outside $HOME as a normal user is not ours to do.
    [ "$(id -u)" -eq 0 ] || return 0
    for d in /usr/local/bin /usr/bin; do
        case ":$PATH:" in
            *":$d:"*) ;;
            *) continue ;;
        esac
        [ -d "$d" ] && [ -w "$d" ] || continue
        # Never clobber something we did not put there.
        if [ -e "$d/hle" ] && [ ! -L "$d/hle" ]; then
            warn "$d/hle exists and is not a symlink — leaving it alone."
            return 0
        fi
        if ln -sf "$target" "$d/hle" 2>/dev/null; then
            info "Linked $d/hle — available in every shell, no PATH change needed"
            SYSTEM_LINK="$d/hle"
            return 0
        fi
    done
    return 0
}

# --- Agent setup ---

# Enroll this machine as an agent. The token is passed to `hle agent enroll`,
# which validates the hlea_ prefix and writes ~/.config/hle/agent.toml (0600).
# It is never written to disk by this script, nor echoed back.
agent_enroll() {
    if [ -n "$AGENT_TOKEN" ]; then
        hle agent enroll "$AGENT_TOKEN" || return 1
        return 0
    fi

    if ! has_tty; then
        warn "No agent token and no terminal to prompt on."
        warn "Re-run with: --agent --token hlea_xxxxx"
        return 1
    fi

    info "Create an agent at https://hle.world/dashboard and copy its token."
    info "The token is shown only once."
    # `hle agent enroll` prompts (with echo off) and validates; bind its stdin
    # to the terminal so it works under `curl ... | sh`.
    hle agent enroll < /dev/tty || return 1
}

agent_install_service() {
    if [ "$INSTALL_SERVICE" -eq 0 ]; then
        info "Skipping service install (--no-service). Start it with: hle agent run"
        return 0
    fi

    # Scope is auto-detected by the CLI (root -> system, otherwise per-user)
    # unless the caller pinned it with --user / --system.
    # shellcheck disable=SC2086 -- SERVICE_SCOPE is intentionally unquoted (may be empty)
    if hle service install --agent $SERVICE_SCOPE; then
        return 0
    fi

    warn "Could not install the service automatically."
    warn "Install it manually with: sudo hle service install --agent --system"
    warn "Or just run the agent in the foreground: hle agent run"
    return 1
}

setup_agent() {
    if ! command -v hle >/dev/null 2>&1; then
        error "hle is not on PATH yet — cannot set up the agent."
        error "Restart your shell, then run: hle agent enroll && hle service install --agent"
        exit 1
    fi

    if ! agent_enroll; then
        error "Agent enrollment failed. The client is installed; you can retry with:"
        error "  hle agent enroll"
        error "  hle service install --agent"
        exit 1
    fi

    # Confirm a token actually landed before installing anything that depends
    # on one. A service started without a token does not fail — it respawns
    # forever, logging "No agent token" while `service ... status` reports a
    # healthy pid, so the install looks successful and the agent never
    # connects. Better to stop here and say so.
    if ! hle agent status >/dev/null 2>&1; then
        error "Enrollment reported success but no agent token is readable."
        error "Not installing the service: it would restart forever without one."
        error "Retry with:  hle agent enroll"
        exit 1
    fi
    success "Agent enrolled."

    agent_install_service || exit 1

    success "Agent is set up."
    info "Add endpoints at https://hle.world/dashboard — the agent picks them up in seconds."
    info "Check on it with: hle service status --agent"
}

# --- Main ---

main() {
    OS=$(detect_os)
    info "Detected OS: $OS"

    if [ "$OS" = "freebsd" ]; then
        PYTHON=$(find_python) || {
            error "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required but not found."
            echo ""
            echo "    pkg install $FREEBSD_PKGS"
            echo ""
            echo "  If that package does not exist on this system, find one that does:"
            echo ""
            echo "    pkg search '^python3'"
            echo ""
            exit 1
        }
        info "Found Python: $PYTHON ($($PYTHON --version 2>&1))"
        # pipx and uv are rarely packaged here; the venv path needs nothing but
        # the interpreter, and every dependency is pure Python.
        install_with_venv "$PYTHON"
    else
        PYTHON=$(find_python) || {
            error "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required but not found."
            error "Install Python from https://python.org or via your package manager."
            exit 1
        }
        info "Found Python: $PYTHON ($($PYTHON --version 2>&1))"

        # Try install methods in order of preference
        if command -v pipx >/dev/null 2>&1; then
            install_with_pipx
        elif command -v uv >/dev/null 2>&1; then
            install_with_uv
        else
            install_with_venv "$PYTHON"
        fi
    fi

    # Verify installation
    if command -v hle >/dev/null 2>&1; then
        success "HLE client installed successfully!"
        info "Version: $(hle --version)"
        # csh/tcsh cache the executables on PATH and keep reporting a new one
        # as missing until told to look again. pfSense and OPNsense both give
        # root tcsh, so this is the common case on a firewall.
        case "$(basename "${SHELL:-}" 2>/dev/null)" in
            csh|tcsh) info "In this shell, run 'rehash' first so it finds hle." ;;
        esac
    else
        success "HLE client installed. Restart your shell or run:"
        info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        if [ "$AGENT" -eq 1 ]; then
            warn "Then finish agent setup with:"
            warn "  hle agent enroll && hle service install --agent"
        fi
        exit 0
    fi

    if [ "$AGENT" -eq 1 ]; then
        setup_agent
    else
        info "Run 'hle expose --service http://localhost:8080' to expose one service,"
        info "or re-run this installer with --agent to manage many from the dashboard."
    fi
}

main
