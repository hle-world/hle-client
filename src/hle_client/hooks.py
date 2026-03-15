"""Programmatic hook support — execute user scripts at tunnel lifecycle events."""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook registry — known hook names and the arguments they provide.
# ---------------------------------------------------------------------------

HOOK_DEFINITIONS: dict[str, list[str]] = {
    "tunnel_established": ["subdomain", "public_url", "tunnel_id"],
    "tunnel_dismantled": ["subdomain", "public_url", "tunnel_id"],
}
"""Mapping of hook name → list of positional argument names passed to the script."""


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


def parse_hooks(raw: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Parse ``--hook`` values of the form ``name=/path/to/script`` into a dict.

    Raises ``SystemExit`` on invalid input:

    * Duplicate hook names are rejected.
    * Unknown hook names are rejected.
    """
    hooks: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"Invalid --hook format: {item!r}. Expected hook_name=/path/to/script")
        name, _, script = item.partition("=")
        name = name.strip()
        script = script.strip()
        if not name or not script:
            raise SystemExit(
                f"Invalid --hook format: {item!r}. Both hook name and script path are required."
            )
        if name not in HOOK_DEFINITIONS:
            valid = ", ".join(sorted(HOOK_DEFINITIONS))
            raise SystemExit(f"Unknown hook name: {name!r}. Valid hooks: {valid}")
        if name in hooks:
            raise SystemExit(f"Duplicate hook: {name!r} specified more than once.")
        hooks[name] = script
    return hooks


# ---------------------------------------------------------------------------
# Hook manager
# ---------------------------------------------------------------------------


@dataclass
class HookRunner:
    """Stores parsed hooks and executes them at the appropriate lifecycle points."""

    hooks: dict[str, str] = field(default_factory=dict)

    async def fire(self, hook_name: str, **kwargs: str) -> None:
        """Execute the script registered for *hook_name*, if any.

        Keyword arguments are passed as positional arguments to the script in
        the order defined by ``HOOK_DEFINITIONS[hook_name]``.
        """
        script = self.hooks.get(hook_name)
        if not script:
            return

        arg_names = HOOK_DEFINITIONS.get(hook_name, [])
        args = [kwargs.get(name, "") for name in arg_names]

        cmd = shlex.split(script) + args
        logger.info("Firing hook %s: %s", hook_name, cmd)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logger.warning(
                    "Hook %s exited with code %d: %s",
                    hook_name,
                    process.returncode,
                    stderr.decode(errors="replace").strip(),
                )
            else:
                logger.debug("Hook %s completed successfully", hook_name)
        except FileNotFoundError:
            logger.error("Hook %s: script not found: %s", hook_name, cmd[0])
        except PermissionError:
            logger.error("Hook %s: permission denied: %s", hook_name, cmd[0])
        except Exception:
            logger.exception("Hook %s failed unexpectedly", hook_name)
