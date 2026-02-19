# Changelog

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
