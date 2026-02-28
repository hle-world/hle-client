# HLE Client

[![PyPI](https://img.shields.io/pypi/v/hle-client?v=2)](https://pypi.org/project/hle-client/)
[![Python](https://img.shields.io/pypi/pyversions/hle-client)](https://pypi.org/project/hle-client/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![CI](https://github.com/hle-world/hle-client/actions/workflows/test.yml/badge.svg?v=2)](https://github.com/hle-world/hle-client/actions/workflows/test.yml)

**Home Lab Everywhere** — Expose homelab services to the internet with built-in SSO authentication and WebSocket support.

One command: `hle expose --service http://localhost:8080`

Your local service gets a public URL like `myapp-x7k.hle.world` with automatic HTTPS and SSO protection.

## Install

### pip (or pipx)

```bash
pip install hle-client
# or
pipx install hle-client
```

### Curl installer

```bash
curl -fsSL https://get.hle.world | sh
```

Installs via pipx (preferred), uv, or pip-in-venv. Supports `--version`:

```bash
curl -fsSL https://get.hle.world | sh -s -- --version 1.7.0
```

### Homebrew

```bash
brew install hle-world/tap/hle-client
```

## Quick Start

1. **Sign up** at [hle.world](https://hle.world) and create an API key in the dashboard.

2. **Save your API key:**

```bash
hle auth login
```

This opens the dashboard in your browser. Copy your key and paste it at the prompt. The key is saved to `~/.config/hle/config.toml`.

3. **Expose a service:**

```bash
hle expose --service http://localhost:8080
```

## CLI Usage

### `hle expose`

Expose a local service to the internet.

```bash
hle expose --service http://localhost:8080              # Basic usage
hle expose --service http://localhost:8080 --label ha   # Custom subdomain label
hle expose --service http://localhost:3000 --auth none  # Disable SSO
hle expose --service http://localhost:8080 --no-websocket  # Disable WS proxying
```

Options:
- `--service` — Local service URL (required)
- `--label` — Service label for the subdomain (e.g. `ha` → `ha-x7k.hle.world`)
- `--auth` — Auth mode: `sso` (default) or `none`
- `--websocket/--no-websocket` — Enable/disable WebSocket proxying (default: enabled)
- `--api-key` — API key (also reads `HLE_API_KEY` env var, then config file)

### `hle auth`

Manage your API key.

```bash
hle auth login                              # Save key (opens dashboard)
hle auth login --api-key hle_xxx            # Save key non-interactively
hle auth status                             # Show current key source
hle auth logout                             # Remove saved key
```

### `hle tunnels`

List your active tunnels.

```bash
hle tunnels
```

### `hle access`

Manage per-tunnel email allow-lists for SSO access.

```bash
hle access list myapp-x7k                            # List access rules
hle access add myapp-x7k friend@example.com           # Allow an email
hle access add myapp-x7k dev@co.com --provider github # Require GitHub SSO
hle access remove myapp-x7k 42                        # Remove rule by ID
```

### `hle pin`

Manage PIN-based access control for tunnels.

```bash
hle pin set myapp-x7k       # Set a PIN (prompts for 4-8 digit PIN)
hle pin status myapp-x7k    # Check PIN status
hle pin remove myapp-x7k    # Remove PIN
```

### `hle share`

Create and manage temporary share links.

```bash
hle share create myapp-x7k                         # 24h link (default)
hle share create myapp-x7k --duration 1h           # 1-hour link
hle share create myapp-x7k --max-uses 5            # Limited uses
hle share list myapp-x7k                           # List share links
hle share revoke myapp-x7k 42                      # Revoke a link
```

### Global Options

```bash
hle --version    # Show version
hle --debug ...  # Enable debug logging
```

## Configuration

The HLE client stores configuration in `~/.config/hle/config.toml`:

```toml
api_key = "hle_your_key_here"
```

API key resolution order:
1. `--api-key` CLI flag
2. `HLE_API_KEY` environment variable
3. `~/.config/hle/config.toml`

## Development

```bash
git clone https://github.com/hle-world/hle-client.git
cd hle-client
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/ tests/
ruff format --check src/ tests/
```

## License

MIT — see [LICENSE](LICENSE).
