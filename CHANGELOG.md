# Changelog

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
