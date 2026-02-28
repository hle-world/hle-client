# Changelog

## v1.7.1 — 2026-02-28

Add CLI warnings when auth methods conflict.

- `hle basic-auth set` warns if the tunnel already has a PIN or email rules configured (they will be bypassed)
- `hle pin set` warns if Basic Auth is currently active (PIN won't be checked)
- `hle access add` warns if Basic Auth is currently active (email rules won't be checked)
- All warnings prompt for confirmation before proceeding; network errors during the check are silently ignored

<details>
<summary>Technical details</summary>

- Two async helpers `_warn_if_basic_auth_active` and `_warn_if_pin_or_rules_exist` added to cli.py
- Helpers call the respective status/list endpoints before the primary action, consuming no additional round-trips since clients already have the API connection open
- `SystemExit` is re-raised so "Continue? N" exits cleanly with code 0
- Test updated to mock `get_tunnel_pin_status` and `list_access_rules` returning no-conflict state

</details>

## v1.7.0 — 2026-02-28

<!-- TODO: Fill in release notes before merging -->

## v1.7.0 — 2026-02-28

Add HTTP Basic Auth support — both for protecting tunnel URLs and for forwarding credentials to local services.

- **`hle basic-auth set <subdomain>`** — Set username/password on a tunnel (prompts securely, validates length and no `:` in username)
- **`hle basic-auth status <subdomain>`** — Show whether Basic Auth is active and the configured username
- **`hle basic-auth remove <subdomain>`** — Remove Basic Auth from a tunnel
- **`hle expose --upstream-basic-auth USER:PASS`** — Inject `Authorization: Basic` into every request forwarded to the local service (e.g. for Home Assistant requiring credentials)
- 7 new CLI unit tests covering set/status/remove including validation edge cases

<details>
<summary>Technical details</summary>

- `api.py`: Added `get_tunnel_basic_auth_status`, `set_tunnel_basic_auth`, `remove_tunnel_basic_auth` to `ApiClient`
- `proxy.py`: `ProxyConfig.upstream_basic_auth: tuple[str, str] | None` — if set, overrides any `Authorization` header from the browser before forwarding to the local service
- `tunnel.py`: `TunnelConfig.upstream_basic_auth` threaded through to `ProxyConfig` and also injected in the WebSocket connection path
- CLI command group registered as `hle basic-auth` (with hyphen) matching the `hle pin` / `hle access` / `hle share` pattern

</details>

## v1.6.0 — 2026-02-26

Forward the original `Host` header to local services instead of stripping it.

- **Fix Host header forwarding:** The local proxy previously stripped the `Host` header, causing httpx to set it from the `base_url` (e.g. `homeassistant.local.hass.io:8123`). Services like Home Assistant (2023.6+) validate the `Host` header and reject requests that don't match their configured `external_url`. The original `Host` from the browser (e.g. `ha-ian.hle.world`) is now forwarded — matching standard reverse-proxy behaviour.

## v1.5.0 — 2026-02-26

Fix auto-release pipeline so GitHub releases trigger PyPI publish.

- **Fix release token:** Switch auto-release workflow from `GITHUB_TOKEN` to a PAT (`RELEASE_TOKEN`) so release events trigger the publish workflow. GitHub's anti-infinite-loop protection blocks `GITHUB_TOKEN`-created events from cascading.

## v1.4.0 — 2026-02-26

Automated release pipeline and PyPI publish fix.

- **Auto-release on PR merge:** Merging a `chore/release-*` PR now automatically creates the GitHub release (which triggers PyPI + Homebrew). No manual `--tag` step needed.
- **Fix PyPI publish:** Switch from OIDC token exchange (broken on ARC runners) to `PYPI_API_TOKEN` secret for reliable uploads.
- **Release script improvements:** `scripts/release.sh` updated to document the fully automated flow; `--tag` kept as manual fallback.

## v1.3.0 — 2026-02-26

Fix PyPI publishing on ARC runners and add automated release tooling.

- **Fix PyPI publish workflow:** Replace `pypa/gh-action-pypi-publish` Docker action with direct `twine` upload using OIDC token exchange (`python -m id`). Docker container actions don't work inside ARC runner container jobs.
- **Fix ha-addon dispatch:** Replace `peter-evans/repository-dispatch` action with `curl` for ARC runner compatibility.
- **Add `scripts/release.sh`:** Automates the full release workflow — bumps version in all files (`pyproject.toml`, `__init__.py`, `README.md`, `install.sh`), adds CHANGELOG stub, creates branch/PR, and creates GitHub release.
- **Fix stale version references:** README.md and install.sh now show the current version instead of outdated `1.1.0` / `1.0.1`.

## v1.2.0 — 2026-02-26

Accept self-signed SSL certificates by default, simplify CLI by removing internal relay flags, and add protocol versioning.

- **Self-signed SSL support:** SSL certificate verification is now disabled by default — homelab services (Proxmox, Unraid, TrueNAS, etc.) almost always use self-signed certs. Use `--verify-ssl` to opt in to strict checking.
- **Better error messages:** `ConnectError` now distinguishes SSL failures from TCP connection refused, showing a clear hint instead of a misleading "connection refused" message.
- **Remove `--relay-host` / `--relay-port`** from all CLI commands — HLE is a hosted service; the relay is always `hle.world`.
- Add `PROTOCOL_VERSION = "1.0"` to `hle_common/protocol.py` for wire-format version negotiation
- Add `protocol_version` field to `TunnelRegistration` (optional, backward compatible with older servers)
- Bump `hle_common` version to `0.2.0`
- Add `security.yml` workflow (Bandit SAST, pip-audit, TruffleHog secret scanning)
- Switch all CI workflows to ARC self-hosted runners with job containers

## v1.1.2 — 2026-02-21

Fix README badges: use static license badge (repo is private), bust GitHub camo cache for PyPI version badge.

## v1.1.1 — 2026-02-21

Fix outdated version in README curl installer example (`0.4.0` → `1.1.1`).

## v1.1.0 — 2026-02-21

Add `hle auth` command for explicit API key management.

- `hle auth login` — Opens dashboard in browser and prompts for API key paste (hidden input), or accepts `--api-key` flag for headless/CI use
- `hle auth status` — Shows current API key source (env var, config file, or none) with masked key
- `hle auth logout` — Removes saved API key from config file
- `--api-key` flag on `hle expose` is now purely ephemeral (never auto-saved to config)
- Updated error messages to suggest `hle auth login`

<details>
<summary>Technical details</summary>

- Removed auto-save block from `tunnel.py:_connect_once()` — API keys are only persisted via `hle auth login`
- Added `_remove_api_key()` to `tunnel.py` for config file cleanup
- API key format validation: `hle_` prefix + 32 hex chars (36 total)
- Interactive login uses `click.prompt(hide_input=True)` to prevent shoulder surfing

</details>

## v1.0.2 — 2026-02-21

- Fix API key config file permissions: `~/.config/hle/config.toml` now created with `0600` (owner-only), config directory with `0700`

## v1.0.1 — 2026-02-21

Security hardening release.

- Cap concurrent WebSocket streams at 100 to prevent resource exhaustion
- Cap speed test chunks at 100 (~6.4 MB) to prevent bandwidth exhaustion
- Warn when API key is passed via --api-key flag (visible in process listings)
- Stop printing partial API key to console
- Install script now prompts before modifying shell RC files
- Install script verifies package version after installation

## v0.4.0 — 2026-02-19

Initial public release of the HLE client, extracted from the monorepo as a standalone package.

- First PyPI release with `pip install hle-client`
- Curl installer script at `https://get.hle.world`
- Homebrew tap at `hle-world/tap/hle-client`
- Fixed race condition in WebSocket stream handling (`_ws_streams` now protected by `asyncio.Lock`)
- Fixed empty body handling: `is not None` checks instead of truthiness for base64 bodies
- CLI commands: `expose`, `tunnels`, `access` (list/add/remove), `pin` (set/remove/status), `share` (create/list/revoke), `webhook` (placeholder)
- API key resolution: `--api-key` flag > `HLE_API_KEY` env var > `~/.config/hle/config.toml`
- WebSocket multiplexing with automatic reconnection and exponential backoff
- CI with Python 3.11/3.12/3.13 matrix testing
