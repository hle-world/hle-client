"""Let separately installed packages add commands to ``hle``.

The core client has to stay small enough for a pfSense or OpenWrt box, and a
terminal dashboard is exactly the kind of thing such a box will never run. So
the extension point is a packaging boundary, not a build flag: install
``hle-tui`` and ``hle tui`` appears; do not install it and nothing about the
core changes.

A plugin declares an entry point in the ``hle_client.plugins`` group pointing
at a ``click.Command`` (or a list of them, or a callable returning either)::

    [project.entry-points."hle_client.plugins"]
    tui = "hle_tui.plugin:tui"

Failures are reported, never raised. A broken third-party plugin must not be
able to stop ``hle expose`` from running — the tunnel is the product.
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import click

logger = logging.getLogger(__name__)

GROUP = "hle_client.plugins"


def _commands_from(loaded: object) -> list[click.Command]:
    """Normalise what an entry point resolved to into a list of commands."""
    import click

    if callable(loaded) and not isinstance(loaded, click.Command):
        loaded = loaded()
    if isinstance(loaded, click.Command):
        return [loaded]
    if isinstance(loaded, (list, tuple)):
        return [c for c in loaded if isinstance(c, click.Command)]
    return []


def discover() -> list[click.Command]:
    """Every command contributed by an installed plugin.

    Set ``HLE_NO_PLUGINS`` to skip discovery — the escape hatch when a plugin
    misbehaves and the user still needs the CLI.
    """
    if os.environ.get("HLE_NO_PLUGINS"):
        return []

    found: list[click.Command] = []
    try:
        eps = entry_points(group=GROUP)
    except Exception:  # noqa: BLE001 — a broken environment is not fatal here
        logger.debug("Plugin discovery failed", exc_info=True)
        return []

    for ep in eps:
        try:
            found.extend(_commands_from(ep.load()))
        except Exception:  # noqa: BLE001 — one bad plugin must not break the CLI
            logger.warning("Could not load the %r plugin; skipping it.", ep.name, exc_info=True)
    return found


def register(group: click.Group) -> None:
    """Attach discovered plugin commands to *group*.

    A plugin may not replace a built-in: shadowing ``expose`` or ``auth`` would
    turn installing a package into a way to redefine what the tool does.
    """
    for command in discover():
        name = command.name or ""
        if not name:
            continue
        if name in group.commands:
            logger.warning("A plugin tried to replace the built-in %r command; ignoring it.", name)
            continue
        group.add_command(command)
