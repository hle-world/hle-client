"""Which HLE server this client talks to.

One setting, ``HLE_SERVER_URL``, selects the server for everything: the REST
API, the relay the tunnels and agents connect to, and every dashboard link the
client prints. It defaults to ``https://hle.world``, so behaviour is unchanged
when it is unset.

Resolution order is the ``HLE_SERVER_URL`` environment variable, then
``server_url`` in ``~/.config/hle/config.toml`` (the file ``hle auth login``
writes), then the default. The value is validated here, once, so a bad setting
fails with one clear message naming the variable rather than as a confusing
error somewhere later.
"""

from __future__ import annotations

import os
import tomllib
from urllib.parse import urlparse, urlunparse

import click

from hle_client import config as hle_config

DEFAULT_SERVER_URL = "https://hle.world"
SERVER_URL_ENV = "HLE_SERVER_URL"

# Cleartext is only allowed on loopback, where it cannot leave the machine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _from_config_file() -> str | None:
    """The ``server_url`` saved in the config file, or None.

    Reads the same file as ``config.load_api_key``; a missing or unreadable
    file simply falls through to the default rather than failing the command.
    """
    path = hle_config.CONFIG_FILE
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            value = tomllib.load(f).get("server_url")
    except Exception:
        return None
    return str(value) if value else None


def _invalid(raw: str, reason: str) -> click.UsageError:
    return click.UsageError(
        f"Invalid {SERVER_URL_ENV} {raw!r}: {reason}. Expected an https:// URL "
        "(http:// only for localhost or 127.0.0.1), with no path, query or fragment."
    )


def _normalize(raw: str) -> str:
    """Validate a server URL and return it without a trailing slash."""
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http"):
        raise _invalid(raw, "the scheme must be https:// (or http:// for localhost)")
    if not parsed.hostname:
        raise _invalid(raw, "no host was given")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise _invalid(raw, "http:// is only allowed for localhost or 127.0.0.1")
    if parsed.path not in ("", "/"):
        raise _invalid(raw, "it must not include a path")
    if parsed.query:
        raise _invalid(raw, "it must not include a query string")
    if parsed.fragment:
        raise _invalid(raw, "it must not include a fragment")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def server_url() -> str:
    """The base URL of the HLE server, without a trailing slash."""
    raw = os.environ.get(SERVER_URL_ENV) or _from_config_file() or DEFAULT_SERVER_URL
    return _normalize(raw)


def server_host() -> str:
    """The relay host for the configured server, as ``host[:port]``.

    The port is omitted for https on 443, so the default is just ``hle.world``.
    """
    parsed = urlparse(server_url())
    host = parsed.hostname or ""
    port = parsed.port
    if (
        port is None
        or (parsed.scheme == "https" and port == 443)
        or (parsed.scheme == "http" and port == 80)
    ):
        return host
    return f"{host}:{port}"
