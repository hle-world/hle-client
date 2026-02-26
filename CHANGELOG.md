# Changelog

## v1.5.0 — 2026-02-26

<!-- TODO: Fill in release notes before merging -->

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
