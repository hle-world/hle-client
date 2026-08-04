#!/bin/sh
# HLE Client installer
#
# Usage:
#   curl -fsSL https://get.hle.world | sh
#   curl -fsSL https://get.hle.world | sh -s -- --version 2607.8
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

usage() {
    cat <<'EOF'
HLE Client installer

Options:
  --version <v>     Install a specific hle-client version
  --agent           Enroll this machine as an HLE agent and run it as a service
  --token <token>   Agent enrollment token (hlea_...); implies --agent.
                    Without it, --agent prompts on the terminal.
  --no-service      With --agent: enroll only, don't install a service
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

# FreeBSD (and therefore pfSense/OPNsense) has no prebuilt pydantic-core wheel
# on PyPI, so a plain `pip install` would try to compile Rust on the firewall.
# The dependencies are installed from pkg instead and the venv is given access
# to them; pip then only has to place pure-Python code.
FREEBSD_PKGS="python311 py311-pydantic2 py311-httpx py311-websockets py311-click py311-rich"

freebsd_preflight() {
    PYTHON="$1"
    missing=""
    for mod in pydantic httpx websockets click rich; do
        "$PYTHON" -c "import $mod" >/dev/null 2>&1 || missing="$missing $mod"
    done
    [ -z "$missing" ] && return 0

    error "Missing Python modules from pkg:$missing"
    echo ""
    echo "  Install them first (they ship as prebuilt packages, no compiler needed):"
    echo ""
    echo "    pkg install $FREEBSD_PKGS"
    echo ""
    echo "  If pkg reports any of these as unavailable, check your repo with:"
    echo ""
    echo "    pkg search py311-pydantic2"
    echo ""
    return 1
}

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
            SHELL_NAME=$(basename "$SHELL" 2>/dev/null || echo "sh")
            case "$SHELL_NAME" in
                zsh) RC_FILE="$HOME/.zshrc" ;;
                bash) RC_FILE="$HOME/.bashrc" ;;
                fish) RC_FILE="$HOME/.config/fish/config.fish" ;;
                *) RC_FILE="$HOME/.profile" ;;
            esac
            if [ -n "$RC_FILE" ]; then
                if prompt_yn "Add ~/.local/bin to PATH in $RC_FILE?"; then
                    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC_FILE"
                    info "Added to $RC_FILE — restart your shell or run: source $RC_FILE"
                else
                    info "Skipped. You may need to add ~/.local/bin to your PATH manually."
                fi
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
    if [ "$(detect_os)" = "freebsd" ]; then
        # Reuse the pkg-installed dependencies rather than rebuilding them.
        "$PYTHON" -m venv --system-site-packages "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        "$VENV_DIR/bin/pip" install --quiet --no-deps "$INSTALL_SPEC"
    else
        "$PYTHON" -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        "$VENV_DIR/bin/pip" install --quiet "$INSTALL_SPEC"
    fi

    # Verify before symlinking
    verify_install "$VENV_DIR/bin/python"

    # Symlink the hle binary
    ensure_local_bin
    ln -sf "$VENV_DIR/bin/hle" "$HOME/.local/bin/hle"
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
            exit 1
        }
        info "Found Python: $PYTHON ($($PYTHON --version 2>&1))"
        freebsd_preflight "$PYTHON" || exit 1
        # pipx/uv would each rebuild the dependency tree from PyPI, which is the
        # thing that needs a Rust toolchain here. Always take the venv path.
        install_with_venv "$PYTHON"
    else
        PYTHON=$(find_python) || {
            error "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required but not found."
            error "Install Python from https://python.org or via your package manager."
            exit 1
        }
        info "Found Python: $PYTHON ($($PYTHON --version 2607.8>&1))"

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
