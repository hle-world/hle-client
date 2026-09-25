# hle-tui

An interactive terminal dashboard for [HLE](https://hle.world), shipped as a
plugin for `hle-client`.

```bash
pip install hle-tui
hle tui
```

Installing the package is the whole integration: `hle tui` appears in `hle
--help`, and uninstalling it makes the command go away again. The core client
is not modified either way and never imports textual — which is the point.
`hle-client` has to stay small enough for a pfSense or OpenWrt box, and those
machines want the tunnel, not a rendering library.

## What it does

Three tabs over the same service layer the CLI uses (`hle_client.ops`). There
is no second copy of the API client, the systemd handling or the config lookup
in here, and every edit shows the `hle …` command it stands for in the status
bar, so anything done here can be scripted afterwards.

| Tab | Table | Detail |
|---|---|---|
| Tunnels | subdomain, live/idle, upstream service, auth mode | **Enter** opens the tunnel: public URL, state, and editing for its access rules, gate mode, PIN, basic auth and share links |
| Agents | name, online/offline/disabled, endpoints, version, last seen | the highlighted agent's version, host, last seen, and whatever else the relay reports |
| Daemons | installed services, scope, kind, state | **l** tails the log |

The tunnel pane covers:

| Section | Actions | Same as |
|---|---|---|
| Access rules | add `[provider:]email`, remove the selected rule | `hle tunnel access add/remove` |
| Gate mode | SSO or public (asks first) | `hle tunnel auth-mode --set` |
| PIN | set, remove (asks first) | `hle tunnel pin set/remove` |
| Basic auth | set user and password, remove (asks first) | `hle tunnel basic-auth set/remove` |
| Share links | create for 1h/24h/7d with a label, list, revoke | `hle tunnel share create/list/revoke` |

A new share link's URL is shown once, and copied to the clipboard where the
terminal allows it. The relay keeps only a prefix, so that is the only time the
full link exists.

On a terminal narrower than 120 columns the tunnel pane opens as its own
screen instead of beside the table.

## Keys

| Key | Where | Action |
|---|---|---|
| `Enter` | Tunnels | Open the tunnel pane |
| `o` | Tunnels | Open the tunnel in a browser |
| `d` | Tunnels | Delete the tunnel record (asks first) |
| `n` | Tunnels | Show the CLI command to create a tunnel |
| `s` | Daemons | Restart the daemon (asks first) |
| `l` | Daemons | Tail the daemon's log (last 200 lines, re-read every 2s) |
| `r` | anywhere | Refresh now |
| `Esc` | pane or screen | Close it |
| `q` | anywhere | Quit |

Anything that deletes, restarts or opens a gate asks first. A dashboard makes
a keystroke cheap, which is exactly why those must not be one.

The cursor follows the row, not the position: after a refresh that reorders
the list it is still on the tunnel, agent or daemon it was on.

Errors never close the dashboard. A refused key says so and says what to run;
"Could not reach the relay" means exactly that.

## Still CLI-only

- Creating a tunnel (`hle tunnel create`), webhooks and preflight.
- Installing, uninstalling and refreshing daemons, and following a log live
  (`hle daemon logs -f`).
- Agent enrolment, `hle agent services`, forwards, `hle update`, `hle auth`.
- The per-agent endpoint list, install method and platform: the relay's agent
  list does not return them yet. The pane shows them the day it does.

## Options

```
hle tui --refresh 30     # poll the relay every 30s (0 disables polling)
hle tui --api-key hle_…  # otherwise read from HLE_API_KEY or the config file
```

## Writing another plugin

The mechanism is a `hle_client.plugins` entry point pointing at a
`click.Command`:

```toml
[project.entry-points."hle_client.plugins"]
myplugin = "my_package.plugin:command"
```

A plugin that fails to import is skipped with a warning rather than taking the
CLI down, and one that tries to shadow a built-in command is refused. Set
`HLE_NO_PLUGINS=1` to skip discovery entirely.

MIT licensed, same as `hle-client`.
