"""The tunnel option set as click options, generated from ``TunnelSpec``.

``expose``, ``tunnel create``, ``daemon install tunnel`` and the legacy
``daemon install _flags`` all take the same tunnel options. They used to each
declare them by hand, and the copies drifted: different help, and
``--upstream-basic-auth`` missing from both daemon variants, so a tunnel that
needed it could be run but not installed. Now there is one declaration — the
field metadata on :class:`hle_common.tunnel_spec.TunnelSpec` — and every
command gets the same options from it.

What each command still declares itself is how it names the tunnel and the
service (``--label``/``--service`` flags, or positional ``LABEL URL``), because
those are spelled differently on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

import click

from hle_client.errors import UsageError
from hle_common.tunnel_spec import TunnelSpec, cli_fields, parse_basic_auth

F = TypeVar("F", bound=Callable[..., Any])

# Not a TunnelSpec field — SSO allow rules are applied through the API after
# the tunnel registers — but every command that creates a tunnel takes it, so
# it is declared here with the rest instead of four more times.
ALLOW_OPTION: dict[str, Any] = {
    "opts": ("--allow",),
    "dest": "allow",
    "multiple": True,
    "metavar": "[PROVIDER:]EMAIL",
    "help": "Allow an email to access this tunnel via SSO. "
    "Format: 'email' or 'provider:email'. "
    "Providers: any (default), google, github, hle. Repeatable.",
}


def _make_option(meta: dict[str, Any]) -> Callable[[F], F]:
    kwargs: dict[str, Any] = {"help": meta["help"]}
    if meta.get("is_flag"):
        kwargs["is_flag"] = True
    if meta.get("multiple"):
        kwargs["multiple"] = True
    if "choices" in meta:
        kwargs["type"] = click.Choice(list(meta["choices"]))
    if "int_range" in meta:
        lo, hi = meta["int_range"]
        kwargs["type"] = click.IntRange(lo, hi)
    if "metavar" in meta:
        kwargs["metavar"] = meta["metavar"]
    if "envvar" in meta:
        kwargs["envvar"] = meta["envvar"]
    if "default" in meta:
        kwargs["default"] = meta["default"]
    elif not meta.get("multiple"):
        # Explicit, as every hand-written copy had it: "not given" is None.
        kwargs["default"] = None
    decorator: Callable[[F], F] = click.option(*meta["opts"], meta["dest"], **kwargs)
    return decorator


def tunnel_option(field_name: str) -> Callable[[F], F]:
    """The click option for one ``TunnelSpec`` field, for commands that take a subset."""
    for name, meta in cli_fields():
        if name == field_name:
            return _make_option(meta)
    raise KeyError(f"TunnelSpec.{field_name} has no CLI option")


def tunnel_options(f: F) -> F:
    """Add the full tunnel option set (every flagged ``TunnelSpec`` field, plus ``--allow``).

    Options appear in ``--help`` in ``TunnelSpec`` field order, then ``--allow``.
    """
    metas = [meta for _, meta in cli_fields()] + [ALLOW_OPTION]
    # click lists options in the reverse of decoration order.
    for meta in reversed(metas):
        f = _make_option(meta)(f)
    return f


def tunnel_param_names() -> list[str]:
    """The click parameter names ``@tunnel_options`` adds, in order."""
    return [meta["dest"] for _, meta in cli_fields()] + [ALLOW_OPTION["dest"]]


def parse_option_pairs(pairs: Iterable[str]) -> dict[str, str]:
    """``("k=v", ...)`` from ``--option`` into a dict. Raises UsageError on a bad pair."""
    out: dict[str, str] = {}
    for opt in pairs:
        key, sep, val = opt.partition("=")
        if not sep or not key:
            raise UsageError(f"--option must be KEY=VALUE (got '{opt}').")
        out[key.strip()] = val
    return out


def spec_from_params(
    *,
    service_url: str,
    label: str | None,
    **params: Any,
) -> TunnelSpec:
    """Build a ``TunnelSpec`` from what ``@tunnel_options`` parsed.

    ``params`` is the command's keyword arguments; anything that is not a
    tunnel option (``allow``, ``api_key``, scope flags, ...) is ignored, so a
    command can pass its ``**kwargs`` straight through. Validates the two
    free-form values up front, where the error can still name the flag.
    """
    by_dest = {meta["dest"]: name for name, meta in cli_fields()}
    fields: dict[str, Any] = {}
    for dest, name in by_dest.items():
        if dest in params:
            fields[name] = params[dest]

    raw_options = fields.pop("options", None) or ()
    if isinstance(raw_options, dict):
        fields["options"] = dict(raw_options)
    else:
        fields["options"] = parse_option_pairs(raw_options)

    auth = fields.get("upstream_basic_auth")
    if auth is not None:
        try:
            parse_basic_auth(auth)
        except ValueError:
            raise UsageError("--upstream-basic-auth must be in USER:PASS format.") from None
        if not auth:
            fields["upstream_basic_auth"] = None

    return TunnelSpec(label=label, service_url=service_url, **fields)


def validate_label_and_apex(spec: TunnelSpec) -> None:
    """The label/apex/zone rule, checked before anything connects or is installed."""
    if spec.apex and not spec.zone:
        raise UsageError("--apex requires --zone (e.g. --zone t00t.us).")
    if not spec.apex and not spec.label:
        # Names the form being taught, not the flag it replaced.
        raise UsageError(
            "a label is required — it names the tunnel.",
            hint=(
                "[cyan]hle tunnel create <label> <url>[/cyan]  "
                "(or use --apex with --zone to serve a bare zone root)."
            ),
        )
