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
curl -fsSL https://get.hle.world | sh -s -- --version 2610.5
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
apply: `list`, `get`, `create`, `set`, `delete`. Older spellings (`hle expose`, <!-- docs-guard: allow cli-pre-noun-verb -->
`hle config`, `hle service`, `hle fp`, and verbs such as `access add`, <!-- docs-guard: allow cli-pre-noun-verb -->
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
hle agent status --ready            # Exit 0 only while this agent is connected
hle agent list                      # Agents on your account, and whether they are online
hle agent services                  # Services this machine can see and could expose
hle agent logout                    # Remove the saved token
```

`hle agent status --ready` is meant for supervisors: it exits 0 only while the
running agent has a live control connection (the recorded process must still be
alive), and exits 1 with a one-line reason otherwise. A Kubernetes
`readinessProbe` uses it.

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

Some installs are owned by something outside the client, and `hle update`
prints how to upgrade those instead of guessing:

- **Homebrew** — `brew upgrade hle-client`
- **Docker** — pull the new image: `docker compose pull && docker compose up -d`
- **Home Assistant add-on** — update it from Settings → Add-ons
- **Kubernetes** — `helm upgrade` the hle-operator chart
- **Editable / source checkout** — this is a development install; update it with
  `git pull`

```bash
hle update            # upgrade to the latest release
hle update --check    # just report current vs. latest, don't change anything
hle update --version 2610.5   # pin an exact version
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

### Kubernetes agents

An agent running inside a cluster only tunnels to Kubernetes Services; a raw
URL could otherwise publish the API server, the cloud metadata service or a
node. The agent targets `<svc>`, `<svc>.<ns>`, `<svc>.<ns>.svc` and
`<svc>.<ns>.svc.<cluster-domain>` names. A bare or two-label name is rewritten
to its absolute in-cluster FQDN (with a trailing dot, so the pod's DNS search
domains are never consulted) before it is resolved, so an ordinary public
domain is never reached through the search path. The agent refuses the
Kubernetes API by name — including a hostname-form `KUBERNETES_SERVICE_HOST`,
compared lower-cased, dot-stripped and IDNA-normalised — and by the addresses
that the API Service name and a hostname-form service host resolve to. That
resolved address set is seeded from the literals in `KUBERNETES_SERVICE_HOST`
and `KUBERNETES_PORT_443_TCP_ADDR` when they are IPs, then refreshed in the
background; the agent waits briefly (bounded) for the first refresh before it
starts any endpoint, and a failed or partial refresh is retried with a short
backoff and never clears the by-name refusal (a partial refresh keeps the
answers that did resolve). The agent also refuses link-local and cloud metadata
addresses (`169.254.0.0/16`,
`100.100.100.200`, `192.0.0.192`, `168.63.129.16`, `fd00:ec2::254`), the
unspecified address and `0.0.0.0/8`, loopback and any node address, whatever
the settings say. IPv6 addresses that wrap an IPv4 address (`::ffff:0:0/96`,
6to4, NAT64, Teredo and IPv4-compatible `::a.b.c.d`) are unwrapped and the
embedded address checked too; an IPv6 literal carrying a zone/scope id
(`fd00::10%1`) is refused outright. A hostname must be an ASCII `[a-z0-9.-]`
name: unicode (there is no legitimate use after IDNA) and SRV-style `_…` labels
are refused. When the guard canonicalises a name, the authority the dashboard
asked for is kept as the upstream `Host` header, so host-allowlisting services
are unaffected. TLS connections to a canonicalised name verify the certificate
against that original host (without the trailing dot canonicalisation added),
since Python's TLS stack does not strip the dot. Environment proxies are not
used for in-cluster targets: a cluster Service must never be carried by an
`HTTP(S)_PROXY`, which cannot reach the canonical name. Upstream TLS
verification still honours `SSL_CERT_FILE` and `SSL_CERT_DIR` even though proxy
settings are ignored, so a private in-cluster CA can be trusted. Firepuncher is
disabled on Kubernetes agents unless `HLE_FIREPUNCHER_ENABLED` is truthy; when
it is on, every forward target is checked by the same guard and dialled at the
exact address that was checked.

These settings are read from the environment, with safe defaults:

- `KUBERNETES_SERVICE_HOST` — its presence marks the process as in-cluster.
- `KUBERNETES_PORT_443_TCP_ADDR` — the API Service's ClusterIP as the kubelet
  exports it; when it is an IP it seeds the always-refused set. Some managed
  clusters set it (and `KUBERNETES_SERVICE_HOST`) to a hostname instead; then
  the API is covered by name and by the addresses that hostname resolves to,
  which the agent waits for briefly before its first reconcile.
- `HLE_INSTALL_METHOD` — set to `kubernetes` to declare a cluster agent where
  the variable above is absent; it also overrides how the install is
  classified for the dashboard.
- `HLE_CLUSTER_DOMAIN` — the cluster DNS domain (default `cluster.local`).
- `HLE_ALLOW_RAW_URLS` — `true` to also allow raw URLs (IP literals and
  non-cluster hostnames). The always-refused targets above stay refused.
- `HLE_NODE_IP` — the node's address, from the downward API; honoured when set.
- `HLE_POD_NAMESPACE` — the pod's namespace, used to expand a bare `<svc>`.
  Falls back to the service-account namespace file; if neither is available a
  bare name is refused.
- `HLE_FIREPUNCHER_ENABLED` — `true` to allow firepuncher on a cluster agent.
- `HLE_DISCOVERY_EXCLUDE_NAMESPACES` — comma-separated namespaces to hide from
  discovery and refuse as endpoint targets, on top of the built-in skips
  (`kube-system`, `kube-public`, `kube-node-lease`). The check runs on the
  canonical Service name, so `<svc>`, `<svc>.<ns>`, `<svc>.<ns>.svc` and the
  full cluster-domain form are all refused alike.

**This guard is defence in depth, not the boundary.** It validates at connect
time, so:

- DNS answers can change later. A name that resolved to an allowed address when
  the endpoint started can rebind to a refused one, and an `ExternalName`
  Service follows whatever its target domain says. The transport resolves the
  in-cluster name again at request time.
- In-cluster names are only as trustworthy as the cluster's DNS. A malicious
  or compromised DNS entry can point an allowed Service name at any address.
- `HLE_NODE_IP` covers the node only when it is set; the API server is covered
  by name (including the hostname form of `KUBERNETES_SERVICE_HOST`), by the
  literals in `KUBERNETES_SERVICE_HOST` and `KUBERNETES_PORT_443_TCP_ADDR`
  when they are IPs, and by the addresses `kubernetes.default.svc.<cluster-domain>`
  and a hostname-form service host resolve to — not by every address it may be
  reachable on.
- On managed clusters `KUBERNETES_SERVICE_HOST` is sometimes a hostname (AKS),
  not an IP. The guard refuses that hostname by name and resolves it and the
  API Service name in the background (at most every five minutes, retrying with
  backoff if DNS is unavailable), adding the answers to the always-refused set.
  Before the first endpoint starts the agent waits briefly for that first
  refresh, and the set is seeded from `KUBERNETES_PORT_443_TCP_ADDR` when it is
  an IP. If cluster DNS is unavailable the by-name and seeded-IP checks still
  hold, but an address-only route to the API is not covered.

Make the network the real boundary with an egress `NetworkPolicy` that denies
the agent pod the addresses it must never reach:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: hle-agent-egress
  namespace: hle
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: hle-agent
  policyTypes: [Egress]
  egress:
    # DNS. If NodeLocal DNSCache is installed its link-local VIP answers here
    # too; it is outside the cluster DNS pods, so allow it separately.
    - to:
      - namespaceSelector: {}
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
    - to:
      - ipBlock:
          cidr: 169.254.20.10/32   # NodeLocal DNSCache (when present)
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
    # Service endpoints in this namespace only.
    - to:
      - podSelector: {}
    # The relay over HTTPS. On a managed cluster with a public API endpoint the
    # API address is outside the cluster CIDRs below; list the real endpoint(s)
    # from `kubectl get endpoints kubernetes -o wide` (or pin the relay's own
    # address) so 443 to the API is denied too.
    - to:
      - ipBlock:
          cidr: 0.0.0.0/0
          except:
            - 169.254.0.0/16        # cloud metadata
            - 100.100.100.200/32    # Alibaba metadata
            - 192.0.0.192/32        # Oracle metadata
            - 168.63.129.16/32      # Azure WireServer
            - 203.0.113.10/32       # API server endpoint (kubectl get endpoints kubernetes -o wide)
            - 10.0.0.0/8            # node + API server CIDR (adjust)
            - 172.16.0.0/12
            - 192.168.0.0/16
      ports:
        - { protocol: TCP, port: 443 }
```

Adjust the cluster CIDRs and the API endpoint address to your own node, service
and API ranges. `kubectl get endpoints kubernetes -o wide` and
`kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes`
both print the API server's endpoint addresses — the guard does not read
Endpoints or EndpointSlices, it resolves the
`kubernetes.default.svc.<cluster-domain>` Service name (and a hostname-form
`KUBERNETES_SERVICE_HOST`) and refuses the answers, so use those endpoint
addresses to choose the `except` entries above; the `203.0.113.10/32` line above
is a placeholder. On clusters that expose the API on a public address that is
not inside the cluster CIDRs, this explicit except is what keeps 443 to the API
closed. Denying the metadata, node and API ranges at the network layer holds
even if the guard is bypassed or its background resolution fails.


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
