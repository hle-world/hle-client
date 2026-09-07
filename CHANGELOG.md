# Changelog

## v2609.3 — 2026-09-07

### Changed

- **Firepuncher now reaches your homelab by default, not just the agent's own
  box.** The default allowlist was loopback only, which answered the wrong
  question: an agent exists to reach a network, and this allowed exactly the
  one machine it happened to run on. Meanwhile `hle expose --service
  https://192.168.2.200:8006` — same agent, same LAN, same credential — has
  never been restricted at all. There is no threat model in which one of those
  is safe and the other is not; if anything the tunnel is the more exposed,
  since it publishes a host on the open internet while a forward binds to
  loopback on your own machine.

  Fixed private ranges were the obvious replacement and are wrong in both
  directions: they admit any Docker bridge while refusing Tailscale, whose
  CGNAT range is not private, and refusing a homelab on native IPv6, whose
  addresses are globally routable by design.

  So the default is now the networks the agent is *actually* attached to,
  read from its own interface and route tables — LAN, Docker, Kubernetes, any
  VPN, whatever is really there, updating itself as those come and go. Nothing
  is probed: hop counts are filtered on most homelab gear, forgeable, and cost
  latency on a decision the route table already answers exactly.

  Public addresses stay excluded, so an agent still cannot be used as a
  general-purpose outbound proxy. Reaching one is a rule away.

  Agents older than this release keep working — they receive static private
  ranges alongside the new marker, which is already broader than the loopback
  they have today. The relay must also be new enough to send it; until then
  the static ranges apply.

### Fixed

- **`hle fp` told you to run a command it already knew would fail.** It
  received the agent's allowlist, printed it, and then ignored it —
  announcing the forward and suggesting `ssh -p 9923 ...` for a target that
  allowlist excluded. The refusal only arrived when ssh actually connected,
  as `Connection closed by 127.0.0.1 port 9923`, pointing at nothing:

  ```
  Forwarding 127.0.0.1:9923 → rpi trikala → 192.168.1.101:22
  Agent allows: localhost:*
  Try: ssh -p 9923 <user>@127.0.0.1
  Refused: 192.168.1.101:22 is not allowed. Allowed: localhost:*
  ```

  It now checks before offering anything, and says what to do:

  ```
  Refused: the agent does not allow 192.168.1.101:22.
  It allows: localhost:*
  Add a rule for this target on the agent's Firepuncher settings in the dashboard.
  ```

  Where it cannot judge — a marker only the agent can expand, or a name only
  the agent resolves — it stays quiet and lets the agent decide. A wrong
  refusal here would block a forward that would have worked, and the agent is
  the authority either way. The agent's own refusal now names the networks it
  resolved rather than the raw marker.

### Added

- **Several forwards from one command.** `--to` and `--port` repeat and pair
  in order; ports you leave out are derived, and two targets wanting the same
  local port is refused with the fix in the message.

  ```bash
  hle fp --agent rpi --to 22 --to nas:445 --to 192.168.1.50:5432
  ```

- **Run a command with the forwards up, and tear them down when it exits.**

  ```bash
  hle fp --agent rpi --to 22 -- ssh -p '{port}' me@127.0.0.1
  hle fp --agent rpi --to 22 --to 5432 -- ./backup.sh
  ```

  `{port}` is the first forward's local port and `{port1}`, `{port2}`, ... are
  each one's, so a derived port needn't be guessed; the same values arrive as
  `HLE_FP_PORT` and `HLE_FP_PORT_1`, `HLE_FP_PORT_2`, ... for scripts. The
  exit status is the command's, so it composes in a pipeline or a CI job.

## v2609.2 — 2026-09-07

### Fixed

- **`hle service install` refused a second, unrelated agent.** The guard added
  in v2609.1 rejected any install whose unit name already existed in the other
  systemd scope. That is the right answer for one agent installed twice, and
  the wrong one for two *different* agents on one machine — one per OS user for
  logical separation, or a spare picked up while learning how this works. Both
  are reasonable, and neither is distinguishable from the fault by a filename.

  The enrollment token decides now, and the unit already records where to find
  it. Two units are refused only when they provably share an identity: the same
  token, or the same token file read by the same user. Anything else installs,
  with a note saying the other unit is there and that `--name` keeps the two
  distinguishable.

  Erring toward allowing is deliberate. A false refusal blocks a working setup
  outright and leaves you arguing with your own tooling, while a duplicate that
  slips through is still caught where it can actually be seen — the relay has
  both connections in front of it. Often certainty isn't available anyway: a
  per-user install has no business reading root's token file.

### Added

- **Clients now say which process they are.** A registration described what the
  client wanted but never who was asking, so the relay could not tell one
  client reconnecting from two clients sharing a credential — both arrive as a
  request for the same label. `instance_id` (per process, never persisted, so a
  restart is not mistaken for a duplicate) and `hostname` (shown only back to
  the account's owner) now travel with tunnel registrations and agent hellos.

  The relay uses them to refuse the second of two agents on one token instead
  of letting the pair take every tunnel off each other, and to name the machine
  holding it. Both fields are optional and unknown fields were already ignored,
  so old clients and old relays keep working in every combination.

- **Close codes are now shared vocabulary**, in `hle_common.close_codes`, with
  whether a code may be retried defined alongside it. They were literals on
  both sides, which is how a code meaning "stop" came to be retried: the number
  said one thing where it was sent and another where it was read.

  Two are new. `4010` is sent to a connection turned away because a healthy
  instance already holds its identity — the opposite end from `4009`, and fatal
  for the same reason: if either side retries, the two trade the identity
  forever. `4029` says a client is registering far faster than any healthy one
  needs to and is deliberately *not* fatal — the usual cause is a supervisor
  restarting something, and giving up would turn a restart loop into an outage.

- **The agent stops when the relay says to stop.** A fatal close now ends the
  agent rather than starting the argument again a second later: endpoints are
  torn down, the reason is printed with the machine to go and look at, and it
  exits non-zero — an agent that exits 0 under `Restart=always` just comes back.

## v2609.1 — 2026-09-07

### Fixed

- **The reconnect backoff never started over.** `delay` was set once before the
  reconnect loop and only ever doubled, so it carried every failure a process
  had ever seen. A tunnel up for weeks that had ridden out six unrelated blips
  then waited the full 60-second cap to come back from a routine relay deploy —
  the longer a tunnel had been reliable, the worse it recovered. It now resets
  after any session that got as far as `TUNNEL_ACK`, which is the rule the
  agent's own control loop already used; the two loops finally agree.

- **A tunnel that lost its label kept fighting for it.** When two copies of the
  same label run under one account — a second `hle` left running, or an agent
  and a hand-started tunnel — the relay hands the label to whichever registered
  most recently. Until now nothing told the loser: it held a socket it believed
  was working, received no requests, reported healthy, and never reconnected.

  The relay now closes that socket with code `4009`, and this release treats
  4009 as fatal rather than retrying it. Retrying is what makes it dangerous:
  reconnecting takes the label straight back from whoever just claimed it, and
  two clients end up trading the tunnel between them about once a second — a
  reconnect storm that reads like a network fault and is really a duplicate
  instance. Instead the client stops and says which one to go and find:

  ```
  Tunnel 'mimos-ssh' was taken over by another connection using the same account.
  Another copy of hle (or an hle agent) is running this same label — stop the
  duplicate, or give this one a different --label.
  ```

  Both fixes came out of diagnosing a tunnel that re-registered every 1.4
  seconds for hours. The constant cadence was itself the clue: exponential
  backoff would have shown 1s, 2s, 4s, so a metronomic 1.4s proved a fresh
  connection each cycle rather than one loop backing off.

- **`hle service install` would happily install a second copy of a service that
  was already running.** It wrote its unit and started it without ever looking
  at the other systemd scope, so a `--user` install did not notice a
  system-wide unit of the same name, and a system install did not notice a
  per-user one. Afterwards nothing on the host looked wrong: two units, both
  enabled, both active, neither aware of the other.

  That is what produced the storm above. One host carried
  `/etc/systemd/system/hle-agent.service` from August and
  `~/.config/systemd/user/hle-agent.service` added in September; both agents
  read the same enrollment token, registered the same endpoints, and took the
  tunnel off each other roughly once a second for a day, burning 67 minutes of
  CPU each.

  Installing now stops with the path of the existing unit and the uninstall
  command for the scope it is in. Reinstalling within the same scope is
  unchanged — that is an upgrade, not a duplicate.

  This catches the case on one machine. Preventing it across *different*
  machines needs the relay to tell one agent from another, which is coming
  next.

## v2608.6 — 2026-08-10

### Fixed

- **A failed relay discovery said nothing at all.** On every connection the
  client asks the relay where to connect, and falls back to the default relay
  when that fails. The failure was logged at `debug`, so the fallback was
  completely silent: the tunnel connects, everything looks healthy, and
  discovery has simply stopped working with no symptom to notice.

  Agent-managed tunnels were being turned away here on *every single reconnect*,
  because an agent's token is a credential for carrying traffic that the REST
  API did not accept. No client ever mentioned it. It came to light only from
  the relay's side, as a cluster of rejections in a server error report.

  Discovery failures now log at `warning` with the status code, and say the
  tunnel still works so the message can be read calmly rather than as an
  outage. A 404 stays quiet — that means the relay is older than the endpoint,
  which is expected rather than broken, and warning about it would only teach
  you to ignore the warning that matters.

  The matching relay-side fix shipped in server v2608.23, so the specific
  rejection this exposed is already gone.

## v2608.5 — 2026-08-05

### Fixed

- **Docker discovery offered addresses the agent could not reach.** Every
  discovered container was reported as `http://<container-name>:<port>`, and that
  name only resolves through Docker's embedded DNS at `127.0.0.11` — which exists
  inside a container attached to the same user-defined network. An agent
  installed on the host, via pipx or a venv or on pfSense, has no such resolver,
  so the address was NXDOMAIN and exposing a discovered container produced a
  tunnel to nothing.

  A published container was no better: the port helper sorted published ports
  first but returned the *container-side* number, so `0.0.0.0:32768->8096/tcp`
  still came out as `http://name:8096` — the one address that would have worked
  from the host was found and then discarded.

  Addressing is not a property of the container; it depends on where the agent
  runs. Each container now produces a ladder of candidates — the published host
  port, the container's own bridge address, then the alias or name — and the
  first one that answers a TCP connect is what gets reported. Whether the agent
  is itself containerised decides only the order; the probe decides the answer.

  Containers nothing can reach are still reported, labelled
  `hle.discovery.reachable=false`, rather than dropped — "unreachable from here"
  and "discovery found nothing" are very different problems to debug. The chosen
  rung is recorded in `hle.discovery.address_source`.

- **`hle update` reported success after upgrading nothing.**

  ```
  Latest on PyPI: 2608.4
  $ pipx upgrade hle-client
  hle-client is already at latest version 2608.3
  Updated to 2608.3.
  ```

  `pipx upgrade` and `uv tool upgrade` exit 0 when they decide no upgrade is due,
  and the exit code was the only thing checked — so a green "Updated to 2608.3."
  printed directly under "Latest on PyPI: 2608.4", followed by an offer to
  restart services onto code that had not been replaced.

  The no-op itself was a race rather than a bug, and it will recur every release:
  the version check reads PyPI's JSON API, which updates the moment a release is
  cut, while pipx resolves through the simple index, which trails it. Run
  `hle update` in that window and pip caches the older index page for ten
  minutes.

  So the installed version is now verified against the one requested. A mismatch
  is retried once with the version pinned — `pipx install --force pkg==X` names
  the version outright and cannot no-op — and if it still hasn't moved, that is
  an error naming the cause, the command to force it, and a non-zero exit.
  "Updated to unknown." is gone too: an unreadable version no longer reports
  success.

- **`hle service restart` hid the flag you wanted.** A bare invocation answered
  "--label is required (or pass --agent for the agent service)" and stopped
  there, sending people to look up a label when `--all` was what they needed
  after an upgrade. It now names `--all`, and both this and `uninstall`/`status`
  point at `hle service list`.

## v2608.4 — 2026-08-05

### Fixed

- **`hle` is now on the path on pfSense and OPNsense.** The installer wrote
  `~/.local/bin` into a shell startup file, which pfSense's `tcsh` root login
  never read — so `hle: Command not found.` immediately after an install that
  reported success. When running as root the installer now also symlinks
  `/usr/local/bin/hle`, which is on the default path for every shell on the
  system and survives changing shells. An existing non-symlink at that path is
  left alone rather than clobbered. The startup-file edit remains as the
  fallback for non-root installs, with `--no-modify-path` to skip it.

### Added

- **`hle update` offers to restart what it just made stale.** Upgrading replaces
  the code on disk, not the process already running it: the service kept serving
  the previous release while `hle --version` reported the new one, and nothing
  looked wrong. `update` now lists the installed services and offers to restart
  them, defaulting to yes. Declining prints the command to do it later; a
  restart that fails exits non-zero and says the old version is still serving,
  rather than reporting a successful upgrade.

- **`hle service restart [--all]`**, so restarting doesn't mean knowing the
  platform's own service manager and the generated unit's name.

## v2608.3 — 2026-08-05

### Fixed

- **The agent service now finds its enrollment token.** On FreeBSD and pfSense
  the generated rc.d script set no `HOME`, so the running agent looked for its
  token somewhere other than where enrollment wrote it and failed with
  `No agent token` on every restart — while the file sat on disk. Reported by a
  user on pfSense 26.03, whose service had been restarting every few seconds
  since installation.

  Rather than only setting `HOME`, the token file is now resolved at install
  time — while still running as whoever enrolled — and passed to the service by
  absolute path in `HLE_AGENT_CONFIG`. rc.d, systemd and launchd all carry it.
  Enrollment and the service can run under different environments, so any path
  derived from `HOME` could disagree between them; an explicit path cannot.

- **Nothing reports success over a broken agent any more.** Three separate
  things had to agree the failure above was fine for it to stay invisible:
  `hle agent status` exited 0 with no token, `hle service status --agent`
  reported a healthy pid for a process in a crash loop, and the installer
  installed a service after enrollment had silently failed. All three now say
  what is actually true, and the installer refuses to install a service that
  would restart forever.

- **The PATH prompt named the wrong file on the platform it was written for.**
  It offered to write `~/.profile` on a tcsh box, where that file is never
  read: answering yes would have reported success and changed nothing.
  `$SHELL` is the login shell from `/etc/passwd`, and pfSense's `admin` account
  has `/etc/rc.initial` — its console menu — which execs tcsh without updating
  it. The shell is now taken from the parent process when `$SHELL` isn't a
  shell we recognise.

### Changed

- **`~/.local/bin` is added to PATH without asking**, since every published
  instruction says to run `hle` and the prompt defaulted to No — which made the
  broken answer the likely one. Pass `--no-modify-path` to opt out. Declining
  now prints the exact line for your shell, in its own syntax, and the full
  path to the binary.

## v2608.2 — 2026-08-04

### Changed

- **The client no longer depends on `pydantic`, so it installs without a
  compiler.** `pydantic-core` is compiled Rust and publishes no FreeBSD wheel,
  so `pip install hle-client` tried to build it from source. On a pfSense box
  that meant downloading a Rust toolchain onto a firewall:

  ```
  Collecting pydantic-core==2.46.4
    Downloading pydantic_core-2.46.4.tar.gz (471 kB)
        Unsupported platform: 311
        Rust not found, installing into a temporary directory
  ```

  The remaining four dependencies — `click`, `rich`, `httpx`, `websockets` —
  are pure Python and install there without complaint. This one library was the
  entire reason the agent could not run on pfSense, or on anything else with a
  stock interpreter and no build tools.

  The shared protocol models are now stdlib dataclasses over a small
  serialisation base (`hle_common.wire`). Outside `TunnelRegistration` they were
  plain data containers with no validators, used only for JSON in and out. The
  method names are unchanged, so this is invisible to callers.

  **The wire format is byte-for-byte identical.** A baseline captured while the
  models were still pydantic is asserted against on every test run, covering all
  39 message types: same JSON, same round-trip, same tolerance of unknown fields
  from a newer peer. Old clients and old servers are unaffected.

  `TunnelRegistration` keeps all four of its validators, including the
  `service_label` normalisation. The two `Literal` fields that cross the wire
  keep their checks, since dataclasses ignore `Literal`.

  Two deliberate differences: constructing a model with an unknown keyword now
  raises instead of silently dropping it, and a missing required argument raises
  `TypeError` rather than `ValueError` — parsing from the wire still raises
  `ValueError`.

  On FreeBSD, `pkg install python311` is now the only prerequisite.

## v2608.1 — 2026-08-04

### Added

- **The agent runs as a service on FreeBSD, pfSense and OPNsense.**
  `hle service install --agent` previously knew only systemd and launchd, so on
  a FreeBSD-based firewall the agent had to be started by hand and did not
  survive a reboot. It now generates an rc.d script, enabled through `sysrc`
  and supervised by `daemon(8)`, which restarts it if it exits — the same
  behaviour the systemd unit has. `uninstall`, `status` and `list` work there
  too. rc.d has no per-user services, so `--user` is rejected with an
  explanation rather than failing later on a permission error.
- **The installer handles FreeBSD.** `pydantic` has no FreeBSD wheel on PyPI,
  so a plain `pip install` would try to build Rust on the firewall. On FreeBSD
  the dependencies now come from `pkg` as prebuilt packages, the venv is
  created with `--system-site-packages` to see them, and pip installs the
  client with `--no-deps` — pure Python only, no compiler. Missing packages are
  reported up front with the exact `pkg install` line.

  See the [pfSense guide](https://hle.world/docs/integrations/pfsense/).

### Fixed

- **The release script no longer corrupts shell redirects.** Its version bump
  matched the `2` in `$($PYTHON --version 2>&1)` and rewrote it to
  `--version 2607.8>&1`, which `install.sh` had been shipping since an earlier
  release. The pattern is now anchored to a full CalVer, and the mangled line
  is repaired.

## v2607.8 — 2026-07-25

### Fixed

- **`hle fp` recovers quickly when the agent is briefly offline.** Waiting for
  an agent shared the backoff counter that unreachable-relay retries grow, so
  after a relay deploy it was already at 8s and kept doubling toward 30s — the
  forward stayed dead for up to half a minute after the agent was back.

  Those two situations aren't alike. If the relay answered "that agent isn't
  connected", the network is demonstrably fine and only the agent is missing —
  usually for a few seconds while it reconnects. That now polls on its own
  schedule (2s, capped at 5s), and reaching the relay clears any stale
  connection backoff.

- **Status lines no longer repeat.** The same line every couple of seconds with
  no countdown read like a fault rather than a retry working as intended:

  ```
  Agent 'rpi trikala' is not connected right now. — retrying...
  Agent 'rpi trikala' is not connected right now. — retrying...
  ```

  Each message is printed once and repeated only when the situation changes.
  Recovery is announced with `Reconnected. Forward is live again.`

## v2607.7 — 2026-07-25

### Added

- **`hle agent list`** — shows your agents and whether they're online:

  ```console
  $ hle agent list
                            Agents
  ┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┓
  ┃ Name     ┃ Status  ┃ Endpoints ┃ Version ┃ Last seen ┃
  ┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
  │ trikala  │ online  │         2 │ 2607.7  │ 4s ago    │
  │ nas      │ offline │         1 │ 2607.5  │ 3h ago    │
  └──────────┴─────────┴───────────┴─────────┴───────────┘
  ```

  There was previously no way to see this from the CLI — `hle agent status`
  only inspects the local machine — so finding the name that `hle fp --agent`
  expects meant opening the dashboard. `--json` for scripting.

  It authenticates with your API key rather than the agent token, since it's an
  account-level question. Needs hle-server 2607.12, which is what made
  `/api/agents` reachable with a key at all.

### Fixed

- **The agent's reconnect backoff never reset.** It only reset when a session
  ended cleanly, but a relay restart ends it with an exception, so a session
  that had been healthy for 19 minutes still inherited the delay from an
  earlier unrelated blip:

  ```
  18:46:25  lost -> retry in 1.0s
  18:46:29  502  -> retry in 4.0s
  18:46:34  Agent registered           # healthy for 19 minutes
  19:05:55  lost -> retry in 8.0s      # should have been 1.0s
  ```

  The delay only ever grew over a process's lifetime, so a long-running agent
  drifted toward the 60s ceiling and every routine relay deploy cost up to a
  minute of downtime for no reason. It now resets after any session that got as
  far as registering.

## v2607.6 — 2026-07-25

### Fixed

- **`hle fp` survives losing the relay.** It connected once with no recovery, so
  a relay restart — which sends `1012 service restart`, and happens on every
  deploy — crashed the command with a raw `ConnectionClosedError` traceback and
  the forward was gone.

  It now reconnects with backoff (1s, capped at 30s), the way the agent already
  did. The asymmetry was the actual defect.

  The listener stays bound across reconnects, so the local port doesn't
  disappear: `ssh rpi` works again as soon as the relay returns, without
  restarting anything. That matters most for forwards installed via
  `hle service install --fp`, where the port is expected to simply be there.

  Streams open at the moment of disconnect are closed — the far end is gone and
  they can't be recovered, so existing SSH sessions break either way — but new
  connections work immediately.

  Connections arriving during an outage are refused straight away instead of
  hanging until the dial timeout for a relay we already know is absent.

  Close codes are classified: `4001`/`4003` mean the credential or request was
  rejected, so retrying only repeats it and the command exits with a clear
  message. Restarts, network loss, and a briefly-offline agent are all retried.

  No failure path prints a traceback any more.

## v2607.5 — 2026-07-25

### Added

- **Firepuncher (`hle fp`)** — forward a TCP port that only a remote agent can
  reach to a local port, with no inbound port anywhere:

  ```bash
  hle fp --agent rpi --to 22 --port 9922
  ```

  Then point your SSH client at `localhost:9922`. One session multiplexes many
  connections, so `scp`, `rsync`, and SSH's own forwarding work too.

  Both ends authenticate over WSS and there is never a public TCP port.
  Authorization is enforced twice: the relay proves you own the agent, and the
  agent independently checks the target against an allowlist — so a leaked API
  key can't turn an agent into a pivot into the network behind it. The allowlist
  defaults to loopback only, and an empty rule list denies everything.

- **`hle service install --fp`** — install a forward as a systemd unit or
  launchd job, so a remote port is simply always available locally:

  ```bash
  hle service install --fp --agent-name rpi --to 22 --port 9922
  ```

- **Service discovery** — an agent on a Kubernetes cluster or Docker host
  reports what's running there, so you can expose it from the dashboard instead
  of copying URLs by hand. `hle agent services` lists the same inventory
  locally. Providers self-detect; an agent on a plain VM reports nothing.

  Both providers are read-only and add no dependencies — they use httpx rather
  than the Docker SDK or the Kubernetes client.

### Security

- The Kubernetes discovery provider never skips TLS verification. An earlier
  draft fell back to an unverified connection when the cluster CA was missing,
  which would have sent the ServiceAccount bearer token to an unverified
  endpoint. It now fails closed.

### Fixed

- Agent protocol 1.1: `forward_rules` in `welcome` / `state_sync`, so changing
  what an agent may forward to takes effect within seconds without a restart.

## v2607.4 — 2026-07-25

### Added
- **`hle service install --agent`** — install the dashboard-managed agent as a
  background service, so every endpoint you declare in the dashboard survives
  reboots. Unit is `hle-agent.service` (systemd) / `world.hle.agent` (launchd),
  with `Restart=always`.
  - `hle service uninstall --agent` and `hle service status --agent` target it
    without needing to remember the label.
  - The enrollment token is read at runtime from `~/.config/hle/agent.toml` or
    `HLE_AGENT_TOKEN` — never written into the service file.
- **Service scope auto-detection** — with neither `--user` nor `--system`, root
  installs a system service and a normal user installs a per-user one (with a
  `loginctl enable-linger` hint). Both flags remain available to force it.
- **Installer agent flags** — `--agent`, `--token`, `--no-service`, `--user`,
  `--system`, and `--help`. `--agent` installs the client, enrolls the machine,
  and installs the service in one command; without `--token` it prompts on the
  terminal.

### Fixed
- Installer prompts read from `/dev/tty` instead of stdin. When the installer is
  piped from the network, stdin is the script itself, so the "add ~/.local/bin to
  PATH?" prompt consumed script text rather than the user's answer.

### Changed
- Internal: `service_cmd` now builds `run_args` (was `expose_args`) and takes
  `description` / `restart`, so single-tunnel and agent modes share the systemd
  and launchd backends. No change to existing `hle expose` or
  `hle service install` behaviour.

## v2607.3 — 2026-07-17

### Added
- **`hle update`** — self-upgrade regardless of install method. Detects pipx /
  uv tool / installer venv / pip and runs the right upgrade. `--check` reports
  current vs. latest without changing anything; `--version X` pins a version.
- **`hle service`** — install and manage a background service for a tunnel so
  it survives reboots and restarts on failure, without hand-writing unit files.
  - **Linux** → systemd unit (system or `--user`).
  - **macOS** → launchd plist (LaunchDaemons or `--user` LaunchAgents).
  - Windows is unsupported (use Task Scheduler / NSSM); the command exits with
    a clear message and no side effects.
  - The API key is read at runtime from config/`HLE_API_KEY` — never written
    into the service file.

### Fixed
- **Tunnel flapping under load / streaming** (`1011 keepalive ping timeout`):
  relaxed the control-WebSocket keepalive (`ping_interval=30`, `ping_timeout=120`)
  so a large or continuous tunnel body (e.g. a live video stream) no longer
  starves the ping/pong and severs the tunnel. Pairs with a matching relay-side
  change (needs a relay running the corresponding server release).
- **Reconnect handshake**: a keepalive `PING` arriving before `TUNNEL_ACK`
  (common right after a reconnect) no longer aborts registration — the client
  now responds `PONG` and keeps waiting for the ACK.
- **Log noise**: a `WS_FRAME` arriving just after a local WebSocket failed to
  open or was closed (the browser's queued frames racing teardown) now logs at
  debug instead of `WARNING: WS_FRAME for unknown stream_id=...`.

## v2607.2 — 2026-07-17

### Fixed
- **Proxmox VM/CT consoles (and other WebSocket upstreams) over a tunnel**: a
  service URL with a trailing slash (`--service https://host:8006/`) produced a
  doubled slash in the upstream WebSocket URL (`wss://host:8006//api2/...`),
  which strict upstreams reject — Proxmox's vncwebsocket answers HTTP 500. The
  WS URL is now built without the doubled slash. HTTP was unaffected.

### Added — debug telemetry
When the relay enables debug capture for a tunnel (admin panel), the client now
pushes structured diagnostics back so failures are visible from the dashboard
without reading the client's local log:
- Failed upstream WebSocket connects report the sanitized upstream URL (query
  string / tickets stripped) and upstream HTTP status.
- `service.check` — on enable, the client probes its local service (reachable /
  status / scheme / TLS / redirect).
- `http.upstream_error` — synthetic upstream 502/504s (connect refused /
  timeout / SSL) are surfaced with the exception class.
- `log.line` — the client's own WARNING+ log lines stream to the relay,
  redacted (API keys / tickets / tokens / Authorization) and rate-limited.

All diagnostics are best-effort, gated on the relay opting in, and never touch
the data plane. Requires a relay running v2607.4+ to surface them.

## v2607.1 — 2026-07-16

### Fixed
- **Scheme-less service URLs** (`hle expose --service localhost:9998`) registered the
  tunnel successfully but failed every forwarded request with
  `UnsupportedProtocol` — visitors saw "Bad Gateway: unexpected error" while the
  tunnel looked healthy. Service URLs without a scheme are now normalized to
  `http://` across all entry points (expose, forward, agent endpoints).
- The catch-all 502 response now names the exception class
  (`Bad Gateway: unexpected error (UnsupportedProtocol)`) so unknown failure
  modes are diagnosable from the relay side.
- `install.sh`: repaired a `2>&1` redirect clobbered by an earlier version bump.

## v2606.1 — 2026-06-04

### Added
- **`hle agent`** — run one agent that hosts many tunnels, managed from the dashboard.
  - `hle agent enroll <token>` — save an agent enrollment token (created in the dashboard).
  - `hle agent run` — connect to the server, fetch the desired endpoints, and reconcile a
    pool of tunnels live (add/remove/change endpoints from the dashboard with no restart).
  - `hle agent status` / `hle agent logout`.
  - Shared agent control protocol in `hle_common.agent_protocol`.
- `TunnelConfig.zone` — publish a tunnel under a custom zone (not just the base domain).

Requires a server with the agent control plane enabled (`HLE_AGENTS_ENABLED`).

## v2605.5 — 2026-05-14

- **WebSocket subprotocol negotiation** (`PROTOCOL_VERSION` 1.4 → 1.5): forward `Sec-WebSocket-Protocol` end-to-end so upstreams that require subprotocol negotiation (ttyd, mqtt, graphql-ws, etc.) work through a tunnel. Previously the client stripped the header as if it were hop-by-hop, causing browsers to close the WS with code 1006. New `WS_ACCEPT` message carries the upstream-selected subprotocol back to the relay so it can echo it in the 101 response.
- Requires server protocol 1.5 to take effect; older relays ignore the new message and fall back to no-subprotocol acceptance (no regression for WS flows that don't negotiate one).

## v2604.4 — 2026-04-26

- **Server notices on the CLI** (`PROTOCOL_VERSION` 1.2 → 1.3): the relay can now push informational messages to a connected client, rendered inline with the rest of the `hle expose` output. Wording is server-controlled so new notices do not require a client release. First use case: dashboard "auto-protect" toggles surface immediately on the CLI.
- **`hle config` command group** for declarative tunnel configuration:
  - `hle config show <label>` — full status (auth mode, access rules, PIN, basic-auth, live state) in one call.
  - `hle config auth-mode <label> --set sso|none` — change the SSO gate. Webhook tunnels are always public and rejected.
  - `hle config access <label> --replace [provider:]email ...` — declarative reconcile: rules in the dashboard but not in the flags are removed. Unlike `hle expose --allow`, which only adds.
- **Install docs reordered**: curl one-liner (`curl -fsSL https://get.hle.world | sh`) is now the primary path, followed by `pipx install hle-client` and `brew install hle-world/tap/hle-client`.
- **Fix:** stray `2604.2` version literal in `install.sh`'s `--version 2>&1` invocation, leftover from a previous `sed` pass.

## v2604.2 — 2026-04-16

- **Required `--label` flag**: Both `hle expose` and `hle webhook` now require `--label`. Labels are the stable identity for tunnels — the server uses them to persist subdomain mappings across reconnections.
- **Required `service_label` in protocol**: `TunnelRegistration.service_label` is now a required field. The validator raises on empty or all-invalid-character labels.

## v2604.1 — 2026-04-09

First CalVer release. Switches from SemVer to Calendar Versioning (YYMM.RELEASE).

- **Fix multi-value response headers**: Preserve multiple `Set-Cookie` headers through the tunnel proxy. Previously only the last cookie survived, breaking session persistence for services like Home Assistant.
- **CalVer migration**: Release tooling now validates YYMM.RELEASE format instead of SemVer.

## v1.19.0 — 2026-03-16

Branding: rename "Home Lab Everywhere" to "HomeLab Everywhere" across all user-facing text.

- **Branding consistency:** Standardise on "HomeLab Everywhere" (one word) in all CLI output, package metadata, README, and email templates

## v1.18.0 — 2026-03-12

Enterprise custom domain support preparation.

- **Fix subdomain extraction:** Use the `subdomain` field from the server's `TunnelRegistrationResponse` instead of parsing it from the public URL. The old approach assumed `*.hle.world` URL structure and would break for enterprise custom domain tunnels (e.g. `app.acme.com`).

## v1.17.0 — 2026-03-07

Webhook forwarding support — expose webhook endpoints through HLE tunnels without authentication.

- **`hle webhook` command:** New CLI command with `--path` and `--forward-to` options for forwarding webhooks to local services
- **Path filtering:** Client-side path prefix enforcement with segment boundary checks and `posixpath.normpath` to prevent traversal attacks
- **No-auth mode:** Webhook tunnels bypass authentication, designed for external services (GitHub, Stripe, etc.) that can't provide credentials
- **Validation:** Rejects empty, root, traversal, and oversized (>255 char) webhook paths at both model and CLI level

## v1.16.0 — 2026-03-07

Security hardening, custom zone support, and operator integration.

- **Security audit remediation:** 4MB WebSocket message limit, hardened CI pipelines (pip-audit fix, TruffleHog pinning), removed obsolete protocol test fixtures
- **Custom zones:** Enterprise delegated subdomain support — tunnels can register under custom DNS zones (e.g. `app.example.com`) with zone config persistence in `config.toml`
- **Managed tunnels:** `managed_by` field on `TunnelRegistration` for operator-managed tunnels (locks dashboard edits when set)
- **Operator notifications:** `notify-operator.yml` workflow dispatches to `hle-operator` on new client releases

## v1.15.0 — 2026-03-07

Auto-sanitize service labels and add HTTP_REQUEST_CANCEL protocol support.

- **Label auto-sanitization:** Invalid characters in `--label` (underscores, uppercase, spaces) are now auto-corrected instead of rejected with a validation error
- **HTTP_REQUEST_CANCEL:** Client now handles server cancel messages for orphaned chunked streams — stops streaming when the browser disconnects mid-request
- **Protocol v1.2:** New `HTTP_REQUEST_CANCEL` message type added (backward compatible — old servers simply don't send it)

## v1.14.0 — 2026-03-06

Speed test upload parity and chunk size tracking.

- **Upload speed test:** Added `chunk_size_bytes` to `SpeedTestData` for accurate upload throughput measurement

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
