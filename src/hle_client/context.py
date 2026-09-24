"""What one CLI invocation carries: credential, output, and whether it may ask.

Every leaf used to work these out for itself — its own ``--api-key`` read
directly, its own ``Console()``, its own ``click.confirm``. The root group's
``--api-key``, ``-o json`` and ``--no-input`` therefore reached the handful of
commands that happened to look, and silently did nothing on the rest.

Now the root group populates ``ctx.obj`` once and the helpers here read it.
A leaf may still declare an ``--api-key`` of its own (the option is part of
the public grammar); it hands the value to ``resolve_api_key`` and the same
precedence applies everywhere:

    leaf --api-key  >  root --api-key  >  HLE_API_KEY  >  ~/.config/hle/config.toml

Everything here tolerates a context the root never populated — a command
invoked directly in a test, or through ``ctx.invoke`` — by falling back to
the environment and the file.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import click

from hle_client import api as api_module
from hle_client import config
from hle_client.errors import NO_API_KEY, AbortedError, AuthError, UsageError
from hle_client.output import Output, from_ctx

if TYPE_CHECKING:
    from hle_client.api import ApiClient

_CLIENT_KEY = "_api_client"


def _obj(ctx: click.Context | None) -> dict[str, Any] | None:
    obj = getattr(ctx, "obj", None)
    return obj if isinstance(obj, dict) else None


def out(ctx: click.Context | None) -> Output:
    """The invocation's ``Output``."""
    return from_ctx(ctx)


def is_json(ctx: click.Context | None) -> bool:
    """Whether ``-o json`` was given: stdout is a payload, nothing else."""
    return out(ctx).json_mode


def no_input(ctx: click.Context | None) -> bool:
    """Whether ``--no-input`` forbids prompting."""
    obj = _obj(ctx)
    return bool(obj.get("no_input")) if obj else False


def resolve_api_key(ctx: click.Context | None, explicit: str | None = None) -> str | None:
    """The credential to use, or None when there is none anywhere.

    ``explicit`` is a leaf's own ``--api-key`` value. The root already folded
    ``HLE_API_KEY`` into its own option, but the environment is consulted
    again here for contexts the root never populated.
    """
    if explicit:
        return explicit
    obj = _obj(ctx)
    if obj:
        inherited = obj.get("api_key")
        if isinstance(inherited, str) and inherited:
            return inherited
    return os.environ.get(config.API_KEY_ENV) or config.load_api_key()


def require_api_key(ctx: click.Context | None, explicit: str | None = None) -> str:
    """``resolve_api_key``, or an ``AuthError`` saying where a key can come from."""
    key = resolve_api_key(ctx, explicit)
    if not key:
        raise AuthError(NO_API_KEY)
    return key


def api(ctx: click.Context | None, explicit: str | None = None) -> ApiClient:
    """The invocation's ``ApiClient`` — built once, reused by every call.

    Looked up on the module at call time rather than bound at import, so a
    test that patches ``hle_client.api.ApiClient`` sees every command go
    through its fake.
    """
    obj = _obj(ctx)
    if obj is not None:
        cached = obj.get(_CLIENT_KEY)
        if cached is not None and obj.get("_api_client_key") == (explicit or None):
            return cached  # type: ignore[no-any-return]
    key = require_api_key(ctx, explicit)
    client = api_module.ApiClient(api_module.ApiClientConfig(api_key=key))
    if obj is not None:
        obj[_CLIENT_KEY] = client
        obj["_api_client_key"] = explicit or None
    return client


def confirm(ctx: click.Context | None, prompt: str, *, default: bool = False) -> bool:
    """Ask a yes/no question, unless ``--no-input`` says not to.

    Under ``--no-input`` the answer is ``default``: a question whose safe
    answer is "no" is answered no, and the caller decides what that means.
    """
    if no_input(ctx):
        return default
    return bool(click.confirm(prompt, default=default))


def confirm_or_abort(ctx: click.Context | None, prompt: str, *, default: bool = False) -> None:
    """``confirm``, where "no" ends the command with "Aborted." and exit 1.

    Under ``--no-input`` a default of "no" aborts with a hint naming the flag
    that would have said yes, rather than proceeding on a question nobody
    answered.
    """
    if no_input(ctx):
        if default:
            return
        raise AbortedError(hint="--no-input forbids prompting; pass --yes to confirm.")
    if not click.confirm(prompt, default=default):
        raise AbortedError()


def prompt(ctx: click.Context | None, text: str, **kwargs: Any) -> Any:
    """``click.prompt``, refused under ``--no-input``.

    A prompt with no terminal to answer it is where unattended runs hang, so
    say what was wanted and how to pass it instead.
    """
    if no_input(ctx):
        raise UsageError(f"{text} is needed and --no-input forbids prompting for it.")
    return click.prompt(text, **kwargs)
