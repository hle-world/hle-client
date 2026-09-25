# HLE Client

[![PyPI](https://img.shields.io/pypi/v/hle-client?v=2)](https://pypi.org/project/hle-client/)
[![Python](https://img.shields.io/pypi/pyversions/hle-client)](https://pypi.org/project/hle-client/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![CI](https://github.com/hle-world/hle-client/actions/workflows/test.yml/badge.svg?v=2)](https://github.com/hle-world/hle-client/actions/workflows/test.yml)

**HomeLab Everywhere** — Expose homelab services to the internet with built-in SSO authentication, WebSocket support, and webhook forwarding.

One command: `hle tunnel create myapp http://localhost:8080`

Your local service gets a public URL like `myapp-x7k.hle.world` with automatic HTTPS and SSO protection.

## Install

### Curl installer (recommended)

```bash
curl -fsSL https://get.hle.world | sh
```

Installs via pipx (preferred), uv, or pip-in-venv. Supports `--version`:

```bash
curl -fsSL https://get.hle.world | sh -s -- --version 2609.10
```

### pipx

```bash
pipx install hle-client
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
hle tunnel create myapp http://localhost:8080

# Or forward webhooks from GitHub/Stripe:
hle tunnel webhook --path /hook/github --forward-to http://localhost:3000 --label github-hook
```

4. **See what this machine is doing:**

```bash
hle status
```

## CLI Usage

The CLI has one shape: `hle <noun> <verb>`. The nouns are `tunnel`, `agent`,
`daemon`, `forward` and `auth`, and the verbs are the same wherever they
apply: `list`, `get`, `create`, `set`, `delete`. Older spellings (`hle expose`,
`hle config`, `hle service`, `hle fp`, and verbs such as `access add`,
`pin status`, `share revoke`, `auth-mode --set`, `daemon uninstall --label`,
`agent enroll`) still work but are not listed in `--help`.

Every command that reads a resource accepts `-o json`.

### `hle tunnel create`

Expose a local service to the internet. The label names the tunnel; the URL is
the local service.

```bash
hle tunnel create ha http://localhost:8123                       # ha-x7k.hle.world
hle tunnel create app http://localhost:3000 --auth none          # Disable SSO
hle tunnel create app http://localhost:8080 --no-websocket       # Disable WS proxying
hle tunnel create app http://localhost:8080 --allow user@gmail.com
hle tunnel create app http://localhost:8080 --allow google:user@gmail.com --allow github:dev@co.com
hle tunnel create --apex --zone t00t.us http://localhost:3000    # Serve at a custom zone root
```

Options:
- `--auth` — Auth mode: `sso` (default) or `none`
- `--allow` — Allow an email to access the tunnel (repeatable). Format: `email` or `provider:email`
- `--zone` — Custom zone to publish under (e.g. `t00t.us`)
- `--apex` — Serve at the bare zone root instead of a subdomain. Requires `--zone`
- `--websocket/--no-websocket` — Enable/disable WebSocket proxying (default: enabled)
- `--verify-ssl` — Verify the local service's TLS certificate (default: off, accepts self-signed)
- `--upstream-basic-auth USER:PASS` — Inject Basic Auth into requests forwarded to the local service (also reads `HLE_UPSTREAM_BASIC_AUTH`)
- `--forward-host` — Forward the browser's Host header to the local service
- `--option KEY=VALUE` — Server-interpreted parameter, passed through verbatim (repeatable)
- `--response-timeout SECONDS` — How long the relay waits for the local service to respond (default 30, max 1200)
- `--api-key` — API key (also reads `HLE_API_KEY` env var, then config file)

`hle daemon install tunnel` takes the same options, so any tunnel you can run
you can also install as a service. `--upstream-basic-auth` is written to the
service's environment, never its command line, and the service file is then
readable by its owner only.

### `hle tunnel preflight`

Check whether a service will work as a tunnel before creating one. Probes it
the way a tunnel would and reports what would break. Changes nothing.

```bash
hle tunnel preflight http://localhost:8123
hle tunnel preflight https://192.168.1.10:8006 --forward-host
```

### `hle tunnel webhook`

Forward incoming webhooks to a local service.

```bash
hle tunnel webhook --path /hook/github --forward-to http://localhost:3000 --label github-hook
hle tunnel webhook --path /hook/stripe --forward-to http://localhost:4000/stripe --label stripe-hook
```

Options:
- `--path` — Webhook path prefix, e.g. `/webhook/github` (required). Cannot be `/`
- `--forward-to` — Local URL to forward webhooks to (required)
- `--label` — Webhook label, e.g. `github-hook` (required)
- `--response-timeout SECONDS` — How long the relay waits for the local service to respond (default 120 for webhooks, max 1200)
- `--api-key` — API key (also reads `HLE_API_KEY` env var, then config file)

Webhook tunnels bypass SSO so external services (GitHub, Stripe, etc.) can deliver payloads without authentication.

### Inspecting and securing tunnels

Tunnel subcommands take a label (resolved to `<label>-<user_code>`) or a full
subdomain. Labels may contain hyphens (`home-assistant`).

```bash
hle tunnel list                       # List your active tunnels
hle tunnel get ha                     # Full status for one tunnel (auth, rules, PIN, …)
hle tunnel delete ha                  # Remove a tunnel's record
```

#### `hle tunnel set`

```bash
hle tunnel set ha --auth sso          # SSO gate on
hle tunnel set ha --auth none         # Tunnel becomes public
```

#### `hle tunnel access` — SSO email allow-list

```bash
hle tunnel access list ha                                # List rules
hle tunnel access create ha friend@example.com           # Allow an email
hle tunnel access create ha dev@co.com --provider github # Require GitHub SSO
hle tunnel access delete ha 42                           # Remove rule by ID
hle tunnel access replace ha google:alice@x.com github:bob@y.com   # Declarative — adds + prunes
hle tunnel access replace ha --clear                     # Remove all rules
```

`replace` is declarative: rules on the server but not in the args are removed.
`hle tunnel create --allow` remains additive (never prunes) for ad-hoc sessions.

#### `hle tunnel pin`

```bash
hle tunnel pin set ha          # Set a PIN (prompts for 4-8 digits)
hle tunnel pin get ha          # Check PIN status
hle tunnel pin delete ha       # Remove PIN
```

#### `hle tunnel basic-auth`

```bash
hle tunnel basic-auth set ha          # Prompts for username + password (min 8 chars)
hle tunnel basic-auth get ha          # Check Basic Auth status
hle tunnel basic-auth delete ha       # Remove Basic Auth
```

#### `hle tunnel share` — temporary share links

```bash
hle tunnel share create ha                        # 24h link (default)
hle tunnel share create ha --duration 1h          # 1-hour link
hle tunnel share create ha --max-uses 5           # Limited uses
hle tunnel share create ha --name "demo"          # Name it for reference
hle tunnel share list ha                          # List share links
hle tunnel share delete ha 42                     # Revoke a link
```

### `hle agent`

One process, many tunnels, managed from the dashboard. Create an agent at
[hle.world/dashboard](https://hle.world/dashboard), copy its token, and enrol
this machine. Endpoints added or removed in the dashboard take effect without
a restart.

```bash
hle auth login --agent-token        # Paste the token at the prompt
hle agent run                       # Run in the foreground
hle agent status                    # Is a token configured here?
hle agent list                      # Agents on your account, and whether they are online
hle agent services                  # Services this machine can see and could expose
hle agent logout                    # Remove the saved token
```

For an agent that survives reboots, install it as a service instead:
`hle daemon install agent`.

### `hle forward`

Forward TCP ports from a remote agent to this machine, e.g. SSH into a box
that has no public address.

```bash
hle forward rpi 22 --port 9922        # then: ssh -p 9922 root@localhost
hle forward nas 192.168.1.50:5432     # Postgres on the agent's LAN
hle forward rpi 22 nas:445            # Two forwards, one command
hle forward rpi 22 -- ssh -p '{port}' me@127.0.0.1   # Run a command, tear down on exit
```

The first argument is the agent name (see `hle agent list`); each target is a
port or `host:port` as the agent sees it. The agent must allow each target;
adjust per agent in the dashboard.

### `hle daemon`

Install and manage a background service, so a tunnel, agent or forward
survives reboots and restarts on failure. Uses **systemd on Linux**, **launchd
on macOS** and **rc.d on FreeBSD/pfSense** (Windows is unsupported). The API
key is read at runtime from `~/.config/hle/config.toml` (or `HLE_API_KEY`) and
is never written into the service file.

```bash
# One always-on tunnel
#   Linux → /etc/systemd/system/hle-tv.service (or ~/.config/systemd/user/ with --user)
#   macOS → /Library/LaunchDaemons/world.hle.tv.plist (or ~/Library/LaunchAgents/ with --user)
sudo hle daemon install tunnel tv http://localhost:9998
hle daemon install tunnel tv http://localhost:9998 --user           # per-user, no sudo
sudo hle daemon install tunnel prox https://192.168.2.200:8006 --zone pr.t00t.us

# The dashboard-driven agent
sudo hle daemon install agent

# A permanent forward
hle daemon install forward rpi 22 --port 9922 --user

hle daemon list                     # Installed hle services, in both scopes
hle daemon status tv                # One service's status
hle daemon logs agent               # Its log (-f to follow, -n 200 for more)
hle daemon restart tv               # Restart one, or --all
hle daemon refresh --all            # Rebuild service files after an upgrade
hle daemon delete tv                # Stop, disable, remove
```

### `hle update`

Update the client to the latest version, regardless of how it was installed
(pipx, uv tool, the installer's venv, or pip). It detects the install method
and runs the right upgrade, then offers to restart any installed services.
Homebrew installs are told to run `brew upgrade hle-client` instead.

```bash
hle update            # upgrade to the latest release
hle update --check    # just report current vs. latest, don't change anything
hle update --version 2609.10   # pin an exact version
```

### `hle auth`

Manage the credentials saved on this machine.

```bash
hle auth login                              # Save an API key (opens dashboard)
hle auth login --api-key <KEY>              # Save key non-interactively
hle auth login --agent-token <TOKEN>        # Enrol this machine as an agent
hle auth status                             # Which credentials exist, and where from
hle auth logout                             # Remove saved key
```

### `hle status`

Everything this machine is set up to do, on one screen: credentials, installed
services, published tunnels, and agents.

```bash
hle status
hle -o json status
```

### Server notices

While a tunnel is connected, the relay can push informational messages that the
client prints (e.g. `✓ Auto-protect added you@example.com via Google SSO`).
Wording is server-controlled so new notices do not require a client release.

### Structured events for supervisors

A program that runs `hle` as a child process should not parse that text. Pass
`--events jsonl` to `hle tunnel create`, `hle tunnel webhook` or `hle agent run`.
stdout then carries one JSON object per line: `connected`, `registered`,
`disconnected`, `notice`, `error` and `fatal`. Everything meant for a person
goes to stderr. The schema is in [docs/events.md](docs/events.md).

### Global Options

Declared once at the root and accepted anywhere on the line:

```bash
hle --version          # Show version
hle --debug ...        # Enable debug logging
hle -o json ...        # Machine-readable output (also accepted after the command)
hle --quiet ...        # Only print errors
hle --no-input ...     # Never prompt; fail instead (for scripts)
```

## Configuration

The HLE client stores configuration in `~/.config/hle/config.toml`, written by
`hle auth login`. It holds a single `api_key` entry.

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
