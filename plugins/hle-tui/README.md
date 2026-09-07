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

## What it shows

Three tables over the same data the CLI reads — there is no second copy of the
API client, the systemd handling or the config lookup in here:

| Tab | Contents |
|---|---|
| Tunnels | subdomain, live/idle, upstream service, auth mode |
| Agents | name, online/offline, host |
| Services | installed units, and which scope each is in |

## Keys

| Key | Action |
|---|---|
| `r` | Refresh now |
| `o` | Open the selected tunnel in a browser |
| `d` | Delete the selected tunnel (asks first) |
| `s` | Restart the selected service |
| `q` | Quit |

Deleting asks for confirmation. A dashboard makes a keystroke cheap, which is
exactly why removing a tunnel and its access rules must not be one.

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
