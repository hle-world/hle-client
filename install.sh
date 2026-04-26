#!/bin/sh
# HLE Client installer
# Usage: curl -fsSL https://get.hle.world | sh
#        curl -fsSL https://get.hle.world | sh -s -- --version 2604.2
set -e

PACKAGE="hle-client"
VERSION=""
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --version=*) VERSION="${1#*=}"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
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
prompt_yn() {
    printf '\033[0;34m[hle]\033[0m %s [y/N] ' "$1"
    read -r answer
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
        *) error "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
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
    info "Installing with pipx..."
    pipx install "$INSTALL_SPEC"
}

install_with_uv() {
    info "Installing with uv tool..."
    uv tool install "$INSTALL_SPEC"
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
    ensure_local_bin
    ln -sf "$VENV_DIR/bin/hle" "$HOME/.local/bin/hle"
}

# --- Main ---

main() {
    OS=$(detect_os)
    info "Detected OS: $OS"

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

    # Verify installation
    if command -v hle >/dev/null 2>&1; then
        success "HLE client installed successfully!"
        info "Version: $(hle --version)"
        info "Run 'hle expose --service http://localhost:8080' to get started."
    else
        success "HLE client installed. Restart your shell or run:"
        info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

main
