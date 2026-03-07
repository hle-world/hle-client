# HLE Client — Security Audit Report

**Date:** 2026-03-07
**Branch:** main (v1.15.0)
**Scope:** All source files in `src/hle_client/` and `src/hle_common/`

---

## File Map & Function Inventory

### `src/hle_client/__init__.py`
- `__version__` — Version string "1.15.0"

### `src/hle_common/__init__.py`
- `__version__` — Version string "0.2.0"

### `src/hle_common/protocol.py`
- `PROTOCOL_VERSION` — Wire protocol version "1.2"
- `MessageType(StrEnum)` — All message types (handshake, tunnel, HTTP, WS, speed test, control)
- `ProtocolMessage(BaseModel)` — Base wire message: type, tunnel_id, request_id, payload
- `TunnelOpenRequest` — Legacy tunnel open request (unused in current flow)
- `TunnelOpenResponse` — Legacy tunnel open response (unused in current flow)
- `HttpRequest` — Legacy HTTP request model (unused — ProxiedHttpRequest used instead)
- `HttpResponse` — Legacy HTTP response model (unused — ProxiedHttpResponse used instead)
- `WsFrame` — Legacy WS frame model (unused — WsStreamFrame used instead)
- `ErrorPayload` — Error message payload

### `src/hle_common/models.py`
- `CAPABILITY_CHUNKED_RESPONSE` — Capability token string
- `_SERVICE_LABEL_RE` — Regex for valid service labels
- `TunnelRegistration(BaseModel)` — Client→server registration payload (service_url, api_key, etc.)
  - `validate_service_label()` — Auto-sanitize label: lowercase, replace separators, strip invalid chars, max 63
- `TunnelRegistrationResponse(BaseModel)` — Server→client ack (tunnel_id, subdomain, public_url, etc.)
- `RelayDiscoveryResponse(BaseModel)` — Discovery endpoint response (relay_url, region, ttl, fallbacks)
- `ProxiedHttpRequest(BaseModel)` — HTTP request forwarded through tunnel
- `ProxiedHttpResponse(BaseModel)` — HTTP response forwarded back
- `HttpResponseStart(BaseModel)` — First frame of chunked response (status + headers)
- `HttpResponseChunk(BaseModel)` — Body chunk (base64 data + index)
- `HttpResponseEnd(BaseModel)` — Terminal frame (optional error)
- `WsStreamOpen(BaseModel)` — Open WS stream (stream_id, path, headers)
- `WsStreamFrame(BaseModel)` — WS frame (stream_id, data, is_binary)
- `WsStreamClose(BaseModel)` — Close WS stream (stream_id, code, reason)
- `SpeedTestData(BaseModel)` — Speed test chunk payload
- `SpeedTestResult(BaseModel)` — Speed test result payload

### `src/hle_client/tunnel.py`
- `TunnelFatalError(Exception)` — Non-retryable server rejection
- `_CONFIG_DIR`, `_CONFIG_FILE` — Config paths (~/.config/hle/config.toml)
- `_load_api_key()` — Load api_key from TOML config
- `_save_api_key(api_key)` — Persist api_key to config (0o600 perms)
- `_remove_api_key()` — Remove api_key line from config
- `TunnelConfig` — Dataclass: all tunnel settings (service_url, relay_host, api_key, etc.)
- `MAX_WS_STREAMS = 100` — Hard limit on concurrent WS streams
- `MAX_SPEED_TEST_CHUNKS = 100` — Hard limit on speed test chunks
- `Tunnel` — Main tunnel class:
  - `connect()` — Reconnection loop with exponential backoff
  - `disconnect()` — Graceful shutdown
  - `is_connected` / `public_url` — Properties
  - `_discover_relay_uri(api_key)` — Call discovery endpoint, fallback to config
  - `_connect_once()` — Single connection: discover, register, receive loop
  - `_receive_loop(ws)` — Message dispatch loop (HTTP, WS, PING, speed test, cancel)
  - `_handle_http_request(ws, msg)` — Route to chunked or buffered handler
  - `_handle_http_request_buffered(ws, msg)` — Single-message HTTP response
  - `_handle_http_request_chunked(ws, msg)` — Streaming chunked HTTP response
  - `_handle_ws_open(ws, msg)` — Open local WS, bridge frames
  - `_ws_local_reader(relay_ws, local_ws, stream_id)` — Background local→relay frame forwarding
  - `_handle_ws_frame(msg)` — Relay→local frame forwarding
  - `_handle_ws_close(msg)` — Close local WS stream
  - `_handle_speed_test_data(ws, msg)` — Download timing / upload chunk generation
  - `_spawn(coro)` — Fire-and-forget task with cleanup tracking
  - `_cleanup()` — Close all streams, cancel tasks, stop proxy

### `src/hle_client/proxy.py`
- `ProxyConfig` — Dataclass: target_url, timeout, verify_ssl, upstream_basic_auth, forward_host
- `LocalProxy` — HTTP proxy to local services:
  - `start()` — Initialize httpx.AsyncClient with base_url, limits
  - `stop()` — Close HTTP client
  - `_should_forward_host` — Property: check if Host header should be forwarded
  - `_build_forwarded_headers(headers)` — Strip hop-by-hop, inject upstream Basic Auth
  - `forward_http(method, path, headers, body, query_string)` — Full-buffer HTTP proxy with SSRF guard + sticky Host auto-detection
  - `stream_http(method, path, headers, body, query_string)` — Streaming HTTP proxy with SSRF guard

### `src/hle_client/api.py`
- `ApiClientConfig` — Dataclass: api_key
- `ApiClient` — REST client for hle.world:
  - `discover_relay()` — GET /api/v1/connect (relay discovery)
  - `list_tunnels()` — GET /api/tunnels
  - `list_access_rules(subdomain)` — GET /api/tunnels/{subdomain}/access
  - `add_access_rule(subdomain, email, provider)` — POST /api/tunnels/{subdomain}/access
  - `delete_access_rule(subdomain, rule_id)` — DELETE /api/tunnels/{subdomain}/access/{rule_id}
  - `get_tunnel_pin_status(subdomain)` — GET /api/tunnels/{subdomain}/pin
  - `set_tunnel_pin(subdomain, pin)` — PUT /api/tunnels/{subdomain}/pin
  - `remove_tunnel_pin(subdomain)` — DELETE /api/tunnels/{subdomain}/pin
  - `create_share_link(subdomain, duration, label, max_uses)` — POST /api/tunnels/{subdomain}/share-links
  - `list_share_links(subdomain)` — GET /api/tunnels/{subdomain}/share-links
  - `delete_share_link(subdomain, link_id)` — DELETE /api/tunnels/{subdomain}/share-links/{link_id}
  - `get_tunnel_basic_auth_status(subdomain)` — GET /api/tunnels/{subdomain}/basic-auth
  - `set_tunnel_basic_auth(subdomain, username, password)` — PUT /api/tunnels/{subdomain}/basic-auth
  - `remove_tunnel_basic_auth(subdomain)` — DELETE /api/tunnels/{subdomain}/basic-auth

### `src/hle_client/cli.py`
- `_resolve_api_key(api_key)` — Resolve key from flag/env/config, exit if missing
- `main()` — Click group: --debug flag, logging setup
- `_VALID_AUTH_PROVIDERS` — Set of valid SSO providers
- `_parse_auth_spec(spec)` — Parse "[provider:]email" spec
- `expose(...)` — Main command: configure tunnel, connect with reconnection
- `_API_KEY_PATTERN` — Regex for valid API key format
- `auth` group: `login`, `auth_status`, `logout`
- `webhook(path, forward_to)` — TODO: not implemented
- `tunnels(api_key)` — List active tunnels via API
- `_warn_if_basic_auth_active(client, subdomain)` — Conflict warning
- `_warn_if_pin_or_rules_exist(client, subdomain)` — Conflict warning
- `access` group: `access_list`, `access_add`, `access_remove`
- `pin` group: `pin_set`, `pin_remove`, `pin_status`
- `share` group: `share_create`, `share_list`, `share_revoke`
- `basic_auth` group: `basic_auth_set`, `basic_auth_remove`, `basic_auth_status`
- `_handle_api_error(exc)` — Map HTTP errors to user-friendly messages

---

## SECURITY FINDINGS

### [SEC-01] CRITICAL — API Key Sent in Plaintext Over WebSocket Registration
**File:** `tunnel.py:297-306`
**Risk:** HIGH

The API key is sent as a field inside the `TunnelRegistration` payload over the WebSocket.
While the WebSocket connection uses WSS (TLS), the API key is:
1. Included in the JSON payload (not as a header)
2. Persisted in the `ProtocolMessage.payload` dict which could be logged server-side
3. Sent on every reconnection (not a session token — the long-lived secret itself)

```python
registration = TunnelRegistration(
    ...
    api_key=api_key,  # <-- raw API key in payload
    ...
)
```

**Recommendation:** Consider a challenge-response or short-lived session token exchange instead of sending the raw API key on every connection. At minimum, ensure the server never logs the full registration payload.

---

### [SEC-02] HIGH — Config File TOCTOU Race Condition in _save_api_key / _save_zone
**File:** `tunnel.py:72-100`
**Risk:** MEDIUM

The config save functions read the file, modify in memory, then write back. Between read and write, another process (or concurrent call) could modify the file, and those changes would be silently overwritten.

```python
# Read phase
with open(_CONFIG_FILE) as f:
    for line in f:
        ...
# Time gap — file could change here
# Write phase
fd = os.open(_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
```

**Impact:** Low in practice (single-user CLI tool), but could cause data loss if multiple `hle` instances write simultaneously.

**Recommendation:** Use file locking (`fcntl.flock`) or atomic write (write to temp file + `os.rename`).

---

### [SEC-03] HIGH — Relay Server Can Trigger Arbitrary Local HTTP Requests (SSRF Mitigation Incomplete)
**File:** `proxy.py:133-143`, `tunnel.py:548-560`
**Risk:** HIGH

The SSRF guard only checks for non-relative paths:
```python
if not path.startswith("/") or path.startswith("//"):
```

This blocks absolute URLs and protocol-relative URLs, BUT:
1. **No blocklist for internal metadata endpoints** — A compromised relay can request `/.well-known/`, `/server-status`, `/env`, `/debug/pprof`, etc. on the local service.
2. **No restriction on the local service target** — If `--service` points to `http://localhost:8080`, the relay can access ANY path on that service, including admin endpoints.
3. **Query string is relay-controlled** — `query_string` is passed through unvalidated. A crafted query could exploit vulnerable local services.

**This is by design** (the client proxies to a user-specified local service), but the threat model should be clearly documented: **the relay server is a trusted party that can access any endpoint on the local service**.

**Recommendation:**
- Document the trust model explicitly (relay = trusted)
- Consider an optional `--restrict-paths` flag for security-conscious users
- Log all proxied requests at DEBUG level for forensic review

---

### [SEC-04] HIGH — Relay Discovery URL Not Validated — Open Redirect to Malicious WebSocket
**File:** `tunnel.py:253-279`
**Risk:** HIGH

The `_discover_relay_uri` method trusts whatever URL the server returns from `/api/v1/connect`:
```python
discovery = await client.discover_relay()
...
return discovery.relay_url  # <-- completely untrusted URL
```

If an attacker compromises the discovery endpoint (or MITM's it), they can redirect the client to a malicious WebSocket server. The client would then:
1. Send the API key to the attacker
2. Execute any HTTP/WS requests the attacker sends

**Impact:** Full account compromise + SSRF against local services.

**Recommendation:**
- Validate that `relay_url` scheme is `wss://` (never `ws://`)
- Validate that the hostname is within `*.hle.world` or a known allowlist
- Pin the TLS certificate or verify it against expected CA

---

### [SEC-05] HIGH — No WebSocket Origin/TLS Validation on Relay Connection
**File:** `tunnel.py:293`
**Risk:** MEDIUM-HIGH

```python
async with websockets.connect(relay_uri) as ws:
```

The `websockets.connect()` call uses default SSL verification (Python's default CA bundle), which is good. However:
1. No certificate pinning for `hle.world`
2. The fallback URL construction (`tunnel.py:278-279`) uses `ws://` for non-443 ports — **unencrypted**:
```python
scheme = "wss" if self.config.relay_port == 443 else "ws"
```

A non-443 port would send the API key in plaintext over an unencrypted WebSocket.

**Recommendation:** Always use `wss://`. Remove the `ws://` fallback or restrict it to explicit `--insecure` flag.

---

### [SEC-06] MEDIUM — API Key Visible in Process Listing via --api-key Flag
**File:** `cli.py:76-81`, `cli.py:176-181`
**Risk:** MEDIUM

The `--api-key` CLI flag is passed as a command-line argument, which is visible to all users via `ps aux`:
```
hle expose --service http://localhost:8080 --api-key hle_abc123...
```

There IS a warning displayed (`cli.py:177`), but the flag still works. Any user on the system can harvest the key.

**Recommendation:** Consider reading from stdin when `--api-key -` is passed, or remove the flag entirely and require env var / config file only.

---

### [SEC-07] MEDIUM — No Rate Limiting on Reconnection Loop
**File:** `tunnel.py:192-231`
**Risk:** MEDIUM

The reconnection loop uses exponential backoff (1s → 60s max), but:
1. **No jitter** — multiple clients reconnecting simultaneously will thundering-herd
2. **Backoff resets on success** — not shown in code, but `delay` starts fresh each `connect()` call
3. **No maximum retry count** — the client will retry forever, which could be used to amplify a DDoS against the relay if many clients are simultaneously disconnected

**Recommendation:** Add jitter to backoff delay. Consider a maximum retry count with a "give up" message.

---

### [SEC-08] MEDIUM — Speed Test Allows Server to Force Client Memory Allocation
**File:** `tunnel.py:724-801`
**Risk:** MEDIUM

The speed test handler has limits (`MAX_SPEED_TEST_CHUNKS = 100`), but:
1. `chunk_size_bytes` is server-controlled with no upper bound:
```python
upload_chunk_size = data.chunk_size_bytes or 65536
```
A malicious relay could set `chunk_size_bytes` to 1GB, causing the client to allocate 1GB of random data.

2. `_speed_test_state` dict grows without cleanup for incomplete tests — if the server sends download chunks but never reaches `total_chunks`, the state leaks.

3. `len(data.data)` counts base64 characters, not decoded bytes — throughput calculation is wrong (overcounts by ~33%).

**Recommendation:**
- Cap `chunk_size_bytes` (e.g., max 1MB)
- Add timeout/cleanup for stale speed test state
- Use decoded byte length for throughput calculation

---

### [SEC-09] MEDIUM — Config File Line-Based Parsing is Fragile
**File:** `tunnel.py:72-123`
**Risk:** MEDIUM

The config save/remove functions use naive line-based string matching:
```python
if line.startswith("api_key"):
```

This matches `api_key_backup = "..."` or `api_keys = [...]` — any line starting with "api_key". Similarly for removal. A TOML file with sections (e.g., `[server]\napi_key = ...`) would cause the wrong line to be matched.

More critically, the save function doesn't use a proper TOML writer — it does string replacement on raw lines. This breaks if:
- The value contains a quote: `api_key = "hle_abc\"def"` (injection)
- The file uses TOML sections, inline tables, or multi-line strings

**Recommendation:** Use `tomli_w` (or `tomllib` for read + manual TOML-safe write) instead of raw string manipulation. At minimum, use `line.startswith("api_key ")` or `line.startswith("api_key=")` with exact prefix matching.

---

### [SEC-10] MEDIUM — Subdomain Used Without Sanitization in API URLs
**File:** `api.py:64-213`
**Risk:** MEDIUM

All API methods interpolate `subdomain` directly into URL paths:
```python
f"{self._base_url}/api/tunnels/{subdomain}/access"
```

If `subdomain` contains path traversal characters (e.g., `../admin`), the request goes to an unintended endpoint. While httpx should handle URL encoding, a value like `foo%2F..%2Fadmin` could potentially bypass path-based authorization on the server.

**Recommendation:** Validate subdomain format client-side (e.g., `^[a-z0-9-]+$`) before interpolating into URLs. Use `urllib.parse.quote(subdomain, safe='')` as defense-in-depth.

---

### [SEC-11] MEDIUM — No TLS Certificate Verification for Local Service by Default
**File:** `proxy.py:51-57`, `tunnel.py:137`
**Risk:** MEDIUM

`verify_ssl` defaults to `False`:
```python
verify_ssl: bool = False
```

This means the client accepts self-signed/expired/wrong-hostname certificates from the local service by default. While pragmatic for homelab use, it means a MITM between the client and local service is trivially achievable on the local network.

**Recommendation:** This is a known trade-off (documented in `--verify-ssl` help text). Consider warning at startup when verify_ssl is False and the target is not localhost/127.0.0.1.

---

### [SEC-12] LOW — WebSocket Local Connection Has No TLS Verification
**File:** `tunnel.py:620-624`
**Risk:** LOW

```python
local_ws = await websockets.connect(
    local_ws_url,
    additional_headers=clean_headers,
)
```

The local WebSocket connection doesn't pass `ssl=` context, so it uses Python defaults. If the local service URL is `wss://`, it will verify certificates (unlike HTTP which explicitly sets `verify=False`). This is inconsistent — HTTP allows self-signed, WS doesn't.

**Recommendation:** If `verify_ssl=False`, create an `ssl.SSLContext` with verification disabled and pass it to `websockets.connect(ssl=ctx)`.

---

### [SEC-13] LOW — Unused/Dead Protocol Models
**File:** `protocol.py:69-120`
**Risk:** LOW (code hygiene)

`TunnelOpenRequest`, `TunnelOpenResponse`, `HttpRequest`, `HttpResponse`, `WsFrame`, `ErrorPayload` are all defined but never used anywhere in the client codebase. They appear to be legacy models from an earlier protocol version.

**Recommendation:** Remove dead code. It increases attack surface review burden and could confuse contributors.

---

### [SEC-14] LOW — No Timeout on WebSocket Registration Ack
**File:** `tunnel.py:314`
**Risk:** LOW

```python
ack_raw = await ws.recv()  # blocks indefinitely
```

After sending the registration message, the client waits forever for an ACK. A malicious/buggy relay could hang the client indefinitely at this point.

**Recommendation:** Wrap with `asyncio.wait_for(ws.recv(), timeout=30)`.

---

### [SEC-15] LOW — _handle_api_error Leaks Server Response Body
**File:** `cli.py:881`
**Risk:** LOW

```python
msg = messages.get(status, f"Server error ({status}): {exc.response.text}")
```

For unexpected status codes, the full server response body is printed to the user's terminal. If the server returns an error page with internal details (stack traces, DB info), this would leak to the user.

**Recommendation:** Truncate `exc.response.text` to a reasonable length (e.g., first 200 chars).

---

---

## OPTIMIZATION FINDINGS

### [OPT-01] ApiClient Creates a New httpx.AsyncClient Per Request
**File:** `api.py:39-213`

Every API method creates and tears down an `httpx.AsyncClient`:
```python
async with httpx.AsyncClient() as client:
    resp = await client.get(...)
```

This means every API call does a fresh TCP handshake + TLS negotiation. For sequential calls (e.g., `_warn_if_basic_auth_active` followed by `add_access_rule`), this is wasteful.

**Recommendation:** Initialize the `httpx.AsyncClient` once in `__init__` (or use `async with ApiClient(...) as client:` pattern) and reuse it across calls. Include `timeout=10.0` and the auth headers in the shared client.

---

### [OPT-02] Speed Test Throughput Counts Base64 Length, Not Raw Bytes
**File:** `tunnel.py:753`

```python
state["bytes"] += len(data.data)  # base64 string length, not decoded bytes
```

Base64 encoding inflates size by ~33%. The throughput calculation at line 759 uses this inflated count, so reported speeds are ~33% higher than actual.

**Recommendation:** Use `len(base64.b64decode(data.data))` or `len(data.data) * 3 // 4` for accurate measurement.

---

### [OPT-03] _ws_hop_by_hop Set Recreated Per WebSocket Open
**File:** `tunnel.py:589-601`

The `_ws_hop_by_hop` set is defined inside `_handle_ws_open` and recreated for every WebSocket stream open. It should be a module-level constant.

**Recommendation:** Move to module level as `_WS_HOP_BY_HOP_HEADERS = frozenset({...})`.

---

### [OPT-04] proxy.py Skip Set Recreated Per Request
**File:** `proxy.py:90`

```python
skip = {"transfer-encoding", "connection", "upgrade", "accept-encoding"}
```

This set is created on every call to `_build_forwarded_headers`. Should be a class or module constant.

---

### [OPT-05] Unnecessary `import os as _os` in Speed Test
**File:** `tunnel.py:780`

```python
import os as _os
upload_chunk_size = data.chunk_size_bytes or 65536
chunk_data = base64.b64encode(_os.urandom(upload_chunk_size)).decode("ascii")
```

`os` is already imported at the module level (line 9). The `import os as _os` is redundant.

---

### [OPT-06] pydantic-settings and pyjwt/cryptography Are Unused Dependencies
**File:** `pyproject.toml:30-33`

```toml
"pydantic-settings>=2.0",
"cryptography>=43.0",
"pyjwt>=2.9",
```

Grep shows no imports of `pydantic_settings`, `jwt`, or `cryptography` anywhere in the client source code. These are likely server-side dependencies that were copied over, or needed by hle_common on the server side but not the client.

**Recommendation:** Remove unused dependencies to reduce install size and supply chain attack surface. If they're needed by hle_common on the server, split the dependency list.

---

## SUMMARY

| ID | Severity | Category | Description |
|----|----------|----------|-------------|
| SEC-01 | CRITICAL | Auth | API key sent in WS payload on every reconnect |
| SEC-02 | HIGH | File I/O | TOCTOU race in config file read/write |
| SEC-03 | HIGH | SSRF | Relay can hit any path on local service |
| SEC-04 | HIGH | Auth/Redirect | Discovery URL not validated — open redirect |
| SEC-05 | HIGH | Transport | ws:// fallback sends API key unencrypted |
| SEC-06 | MEDIUM | Auth | API key visible in process listing |
| SEC-07 | MEDIUM | DoS | No jitter/max retries in reconnection |
| SEC-08 | MEDIUM | DoS | Speed test allows unbounded memory allocation |
| SEC-09 | MEDIUM | Config | Fragile line-based TOML parsing |
| SEC-10 | MEDIUM | Injection | Subdomain not sanitized in API URLs |
| SEC-11 | MEDIUM | Transport | No TLS verification for local service |
| SEC-12 | LOW | Transport | Inconsistent TLS verify between HTTP/WS |
| SEC-13 | LOW | Hygiene | Unused protocol models |
| SEC-14 | LOW | DoS | No timeout on registration ack |
| SEC-15 | LOW | Info Leak | Server error body printed to terminal |
| OPT-01 | — | Perf | New HTTP client per API call |
| OPT-02 | — | Bug | Speed test measures base64 not raw bytes |
| OPT-03 | — | Perf | WS hop-by-hop set recreated per call |
| OPT-04 | — | Perf | Proxy skip set recreated per call |
| OPT-05 | — | Hygiene | Redundant os import in speed test |
| OPT-06 | — | Supply Chain | Unused deps (pydantic-settings, cryptography, pyjwt) |

---

## CI/CD WORKFLOW FINDINGS

### [CI-01] CRITICAL — Shell Injection via CHANGELOG in auto-release.yml
**File:** `.github/workflows/auto-release.yml:73,82`

`NOTES` is populated from `CHANGELOG.md` via sed, then interpolated into a Python heredoc as `'''$NOTES'''`. If the changelog contains `'''`, the Python string literal breaks — potential code execution on the runner.

**Fix:** Pass `NOTES` as env var, read via `os.environ["NOTES"]` in Python.

---

### [CI-02] CRITICAL — TruffleHog Installed from Unpinned @main via curl|sh
**File:** `.github/workflows/security.yml:91-92`

```yaml
curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s
```

Fetches and executes an unpinned script from the trufflehog repo's `main` branch. A compromise of that repo = arbitrary code on the self-hosted runner. The secret scanner itself is installed unsafely.

**Fix:** Pin to a commit SHA or use the official action pinned to SHA. Verify binary checksum.

---

### [CI-03] HIGH — All Actions Pinned to Mutable Version Tags, Not SHAs
**Files:** All 6 workflow files

`actions/checkout@v4.1.7` can be force-pushed to point to different code. Only `peter-evans/repository-dispatch` in `notify-server.yml` is correctly SHA-pinned.

**Fix:** Pin all actions to full commit SHAs.

---

### [CI-04] HIGH — Fork PRs Run on Self-Hosted Runner (security.yml)
**File:** `.github/workflows/security.yml:7`

Triggered on `pull_request` which includes fork PRs. Combined with the curl|sh TruffleHog install (CI-02), this gives untrusted code execution on the self-hosted `arc-runner-hle-client`.

**Fix:** Use GitHub-hosted runners for PR-triggered security scans, or gate fork PRs.

---

### [CI-05] HIGH — RELEASE_TAG Interpolated Unsafely in curl JSON (publish.yml)
**File:** `.github/workflows/publish.yml:61,74,82,95`

`RELEASE_TAG` from `github.event.release.tag_name` is interpolated directly into a `-d` JSON body. Shell/JSON metacharacters in tag name could break or inject into the dispatch payload.

**Fix:** Use `jq` to construct JSON payloads.

---

### [CI-06] HIGH — PyPI Package Installed on Self-Hosted Runner (homebrew.yml)
**File:** `.github/workflows/homebrew.yml:45`

`pip install "hle-client==${VERSION}"` runs on the self-hosted runner. A compromised PyPI package executes code during install.

**Fix:** Extract metadata from PyPI JSON API without installing. Use `uv` per project convention.

---

### [CI-07] MEDIUM — No Deny-All Permissions Baseline (auto-release.yml, homebrew.yml)
**Files:** `auto-release.yml:11-12`, `homebrew.yml` (missing entirely)

Workflows should set `permissions: {}` at workflow level, then grant per-job.

---

### [CI-08] MEDIUM — Security Tool Failures Suppressed (security.yml)
**File:** `.github/workflows/security.yml:34,75`

Both `bandit` and `pip-audit` use `|| true`. Vulnerable dependencies don't block CI.

**Fix:** Remove `|| true` from `pip-audit`. Set appropriate severity thresholds.

---

## CI SUMMARY TABLE

| ID | Severity | File | Issue |
|----|----------|------|-------|
| CI-01 | CRITICAL | auto-release.yml | CHANGELOG shell injection into Python -c |
| CI-02 | CRITICAL | security.yml | TruffleHog curl\|sh from @main |
| CI-03 | HIGH | All workflows | Actions pinned to mutable tags |
| CI-04 | HIGH | security.yml | Fork PRs on self-hosted runner |
| CI-05 | HIGH | publish.yml | Unsafe RELEASE_TAG in curl JSON |
| CI-06 | HIGH | homebrew.yml | pip install on self-hosted runner |
| CI-07 | MEDIUM | auto-release, homebrew | Missing deny-all permissions baseline |
| CI-08 | MEDIUM | security.yml | Security tools suppressed with \|\| true |
