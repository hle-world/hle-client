# Structured events (`--events jsonl`)

For programs that run `hle` as a child process and need to know what it is
doing: hle-webapp, the Home Assistant add-on, hle-docker, a systemd wrapper,
your own supervisor. Parse these instead of the human output. The human output
is for people; its glyphs, colours and wording change without notice.

## Turning it on

```bash
hle tunnel create ha http://localhost:8123 --events jsonl
hle tunnel webhook --path /hook --forward-to http://localhost:3000 --label gh --events jsonl
hle agent run --events jsonl
```

The legacy spellings `hle expose ... --events jsonl` and
`hle webhook ... --events jsonl` accept it too.

With `--events jsonl`:

- **stdout** carries events and nothing else: one JSON object per line, UTF-8,
  newline-terminated, flushed after every line.
- **stderr** carries everything meant for a person: the startup banner, log
  lines, notices rendered with glyphs, and the final error message.

Read the two streams separately. Merging stderr into stdout, as
`stderr=subprocess.STDOUT` does, puts human text back among the events.

## Event object

Every line has the same keys. A key with nothing to say is `null`, never missing.

| Key          | Type             | Meaning |
|--------------|------------------|---------|
| `ts`         | string           | UTC time the event was written, ISO 8601 with milliseconds and `Z`, e.g. `2026-09-25T10:00:00.123Z`. |
| `event`      | string           | What happened. See the table below. |
| `level`      | string           | `info`, `success`, `warning` or `error`. For `notice` it is the level the relay sent. |
| `source`     | string           | `tunnel` for one tunnel's own connection. `agent` for the agent's control connection under `hle agent run`. |
| `label`      | string or null   | The tunnel label. `null` for an apex tunnel and for `agent` events. |
| `subdomain`  | string or null   | The subdomain the relay assigned. Known from `registered` onwards. |
| `public_url` | string or null   | The public URL the relay reported. Known from `registered` onwards. Use it as it is and do not build it yourself. |
| `message`    | string           | Human-readable text. Wording can change, so do not match on it. |
| `code`       | string or null   | A machine-readable code, always a string. For `notice` it is the relay's notice code. For `disconnected`, `error` and `fatal` it is the WebSocket close code when there was one, e.g. `"4003"`. |

## Events

| `event`        | When | Level |
|----------------|------|-------|
| `connected`    | The WebSocket to the relay is open. The tunnel is not serving yet. | `info` |
| `registered`   | The relay accepted the tunnel and it is live. `subdomain` and `public_url` are set. Under `hle agent run`, an `agent` event means the control connection was welcomed. | `success` |
| `notice`       | The relay pushed an informational message, e.g. auto-protect added a rule. | the relay's level |
| `disconnected` | A live session ended. The client reconnects on its own, and `connected` and `registered` follow when it is back. | `warning` |
| `error`        | A connection attempt failed before registering, and the client retries. Under `hle agent run` it also covers a dashboard endpoint that could not start, with `label` set. | `error` |
| `fatal`        | The relay closed with a code that must not be retried, e.g. an invalid key, a label taken, or a duplicate instance. The process exits non-zero right after. | `error` |

A supervisor should ignore any `event` name it does not know. New names may be
added in a minor release. The keys above are not removed or renamed.

Under `hle agent run`, events from every endpoint tunnel the agent runs arrive
on the same stream with `source: "tunnel"` and their own `label`.

## Example

```text
{"ts":"2026-09-25T10:00:00.101Z","event":"connected","level":"info","source":"tunnel","label":"ha","subdomain":null,"public_url":null,"message":"Connected to relay at wss://hle.world:443/_hle/tunnel","code":null}
{"ts":"2026-09-25T10:00:00.340Z","event":"registered","level":"success","source":"tunnel","label":"ha","subdomain":"ha-x7k","public_url":"https://ha-x7k.hle.world","message":"Tunnel registered","code":null}
{"ts":"2026-09-25T10:00:00.512Z","event":"notice","level":"success","source":"tunnel","label":"ha","subdomain":"ha-x7k","public_url":"https://ha-x7k.hle.world","message":"Auto-protect added you@example.com via Google SSO","code":"auto_protect"}
{"ts":"2026-09-25T11:12:03.004Z","event":"disconnected","level":"warning","source":"tunnel","label":"ha","subdomain":"ha-x7k","public_url":"https://ha-x7k.hle.world","message":"received 1012 (service restart); then sent 1012 (service restart)","code":"1012"}
```

## Replacing glyph scraping

hle-webapp's `backend/tunnel_manager.py` maps the leading `ℹ ✓ ⚠ ✗` of each
stdout line to a notice level. With events enabled:

1. Add `--events jsonl` to the spawned argv. Spawn with separate stdout and
   stderr pipes.
2. For each stdout line, `json.loads` it and switch on `event`:
   - `notice`: record `level` and `message`, keyed by `code` if useful.
   - `registered`: the tunnel is up. `subdomain` and `public_url` are
     authoritative.
   - `disconnected` and `error`: show as degraded. The client is retrying.
   - `fatal`: show as failed with `message`. The process is about to exit.
3. Keep writing stderr to the log file for people to read.

`--events` is new in the hle-client release after 2609.9. An older client rejects the option
with exit code 2 before it connects. A consumer that must support older clients
can fall back to glyph scraping when that happens.
