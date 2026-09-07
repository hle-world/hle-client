"""The entry point hle-client discovers.

This module is imported during plugin discovery, on every `hle` invocation on
a machine where the package is installed. So it imports nothing expensive:
textual is loaded inside the command, when someone actually asks for a
dashboard. A CLI that got slower for everyone who installed a plugin would be
a bad trade.
"""

from __future__ import annotations

import click


@click.command("tui")
@click.option(
    "--api-key",
    default=None,
    envvar="HLE_API_KEY",
    help="API key. Falls back to ~/.config/hle/config.toml.",
)
@click.option(
    "--refresh",
    default=10,
    show_default=True,
    metavar="SECONDS",
    help="How often to re-poll the relay. 0 disables polling.",
)
def tui(api_key: str | None, refresh: int) -> None:
    """Interactive dashboard for tunnels, agents and services.

    \b
    Keys:
      r  refresh now        d  delete the selected tunnel
      o  open in a browser  s  restart the selected service
      q  quit
    """
    try:
        from hle_tui.app import HleApp
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise click.ClickException(
            f"The dashboard needs textual, which is missing ({exc}). "
            "Reinstall it with: pip install --upgrade hle-tui"
        ) from None

    HleApp(api_key=api_key, refresh_seconds=refresh).run()
