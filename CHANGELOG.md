# Changelog

## v1.13.1 — 2026-03-04

Fix tunnel limit/auth error UX and CI reliability.

- **Fatal tunnel errors:** Tunnel limit (4003) and invalid API key (4001) now show a clear error message and exit immediately instead of retrying forever
- **CI secret fixes:** Workflow files referenced non-existent `HLE_PAT` secret — reverted to actual per-workflow secret names (`RELEASE_TOKEN`, `HOMEBREW_TAP_TOKEN`, `HA_ADDON_DISPATCH_TOKEN`, `HLE_DOCKER_DISPATCH_TOKEN`)
- **Pre-commit hook:** Added `scripts/pre-commit` (ruff check + format) to catch lint errors before they reach CI
- **Lint fix:** `raise SystemExit(1) from None` for ruff B904 compliance

## v1.13.0 — 2026-03-03

Documentation cross-check and missing CLI flags.

- **Docs:** Added missing `--verify-ssl`, `--forward-host` flags and `hle auth login/status/logout` commands to website docs
- **README:** Added `--verify-ssl`, `--upstream-basic-auth`, `--forward-host`, `hle basic-auth`, and `--label` for share create

## v1.12.0 — 2026-03-02

Relay discovery handshake — prepare for future multi-server support.

- **Relay discovery:** Client now calls `GET /api/v1/connect` before establishing the WebSocket tunnel. The server can return the optimal relay URL based on geolocation, latency, load balancing, or per-user policy. Falls back gracefully to `hle.world` when the endpoint is unavailable.
- **New shared model:** `RelayDiscoveryResponse` in `hle_common` with `relay_url`, `relay_region`, `ttl`, `fallback_urls`, and `metadata` fields. Only `relay_url` is required.
- **Type safety:** Fixed all 43 pre-existing mypy errors across `api.py`, `tunnel.py`, and `cli.py` — proper dict type parameters, updated websockets v16 types, corrected function signatures.

## v1.11.0 — 2026-03-01

Sticky Host header auto-detection — detect once, apply for the session.

- **Sticky detection:** Instead of retrying with/without Host on every 502 response, detect the correct behavior on the first request and lock it in for the entire session. Zero retry overhead after the first request.
- Logs which mode was selected at INFO level: `"Forwarding browser Host header resolved 502 — locked in for this session"` or `"Host header stripping confirmed working"`

<details>
<summary>Technical details</summary>

- `proxy.py`: `_detected_forward_host: bool | None` on `LocalProxy` — `None` = undetermined, `True` = forward Host, `False` = strip Host
- `_should_forward_host` property checks `--forward-host` flag first, then sticky detection, then defaults to strip
- `_build_forwarded_headers()` accepts `include_host: bool | None` override for the retry path
- First non-502 response locks in "strip Host"; first 502 triggers retry, outcome locks in the winner

</details>

## v1.10.0 — 2026-03-01

Fix Host header handling for services behind reverse proxies (Traefik, nginx, Caddy).

- **Fix 502 errors for proxied services:** Strip the browser's Host header by default so httpx sets it from the target URL. Services behind virtual-host reverse proxies route by Host and returned 502 when they saw the HLE public hostname (e.g. `j-ian.hle.world`) instead of the target hostname.
- **Auto-detection:** If the target returns 502, automatically retry with the browser's original Host header forwarded. Logs the detection and result at INFO level.
- **New `--forward-host` flag:** Explicitly forward the browser's Host header to the local service. Use for services like Home Assistant that validate the Host header against `external_url`. Skips auto-detection when set.

<details>
<summary>Technical details</summary>

- `proxy.py`: New `_build_forwarded_headers()` helper centralizes header filtering and Basic Auth injection for both `forward_http()` and `stream_http()`
- `proxy.py`: `forward_http()` auto-retries on 502 with Host included, logs outcome
- `proxy.py`: `ProxyConfig.forward_host: bool` controls behavior
- `tunnel.py`: `TunnelConfig.forward_host: bool` threaded through to `ProxyConfig`
- `cli.py`: `--forward-host` flag on `hle expose`

</details>

## v1.9.0 — 2026-03-01

Chunked HTTP response streaming — fixes 504 Gateway Timeout for video streaming and large file downloads.

- Stream large HTTP responses in 512KB chunks over the WebSocket tunnel instead of buffering the entire body in memory
- Bump wire protocol to 1.1 with 3 new message types: `HTTP_RESPONSE_START`, `HTTP_RESPONSE_CHUNK`, `HTTP_RESPONSE_END`
- Capability negotiation (`chunked_response`) ensures full backward compatibility with older servers
- New `stream_http()` async generator on `LocalProxy` using `httpx.stream()` with configurable chunk size (`HLE_HTTP_CHUNK_SIZE` env var, default 512KB)
- Inject upstream Basic Auth credentials on streaming path (consistency with buffered path)

<details>
<summary>Technical details</summary>

- `hle_common/protocol.py`: `PROTOCOL_VERSION` bumped from `"1.0"` to `"1.1"`, 3 new `MessageType` values
- `hle_common/models.py`: `CAPABILITY_CHUNKED_RESPONSE` constant, `capabilities` on `TunnelRegistration`, `server_capabilities` on `TunnelRegistrationResponse`, `HttpResponseStart`, `HttpResponseChunk`, `HttpResponseEnd` models
- `hle_client/proxy.py`: `stream_http()` async generator — first yield is `(status, headers, None)`, subsequent yields are `(None, None, chunk_bytes)`
- `hle_client/tunnel.py`: `_handle_http_request` branches to chunked path when server advertises `chunked_response`; sends START/CHUNK/END messages over the WebSocket
- 512KB binary → ~700KB base64 → well under WebSocket 2MB default `max_size`

</details>

## v1.8.0 — 2026-02-28

Upstream Basic Auth support and CLI auth conflict warnings.

- Add `--upstream-basic-auth USER:PASS` flag to inject HTTP Basic Auth toward the local service
- CLI warns when auth methods conflict (e.g. setting Basic Auth when PIN is active)

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
