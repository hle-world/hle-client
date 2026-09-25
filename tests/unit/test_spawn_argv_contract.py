"""The argv a service file execs must still parse against the real commands.

`hle daemon install tunnel` builds argv with `build_expose_args(TunnelSpec)`,
writes it and the spec into a unit/plist/rc.d script as an ``hle-spec:``
comment (`service_cmd.spec_comment` / `parse_service_spec`), and execs it as
``hle expose ...`` on every boot. If a tunnel option is ever added to the
`expose`/`tunnel create` command without also reaching the argv builder, the
service file silently cannot carry it — the option works interactively and
vanishes the moment someone daemonises the tunnel. That was finding §1
"option drift" in the audit, with `--upstream-basic-auth` as the live case.

Since TunnelSpec the builder is driven by the same model the options are
generated from, and the cases below that used to be strict xfails — that
debt — now pass. Secrets never go into argv: `--upstream-basic-auth` reaches
`expose` through its envvar, which the service file sets.

Each case builds argv the way the real code paths do, then feeds it to the
real Click command via ``make_context`` — which parses and validates without
invoking the callback, so nothing here opens a socket or touches the
filesystem.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

import pytest

from hle_client.cli import expose, main
from hle_client.fp_cmd import fp
from hle_client.service_cmd import (
    build_expose_args,
    build_fp_args,
    expose_env,
    parse_service_spec,
    spec_comment,
    stamped_tunnel_spec,
)
from hle_client.tunnel_options import spec_from_params
from hle_common.tunnel_spec import TunnelSpec

if TYPE_CHECKING:
    import click

_daemon = cast("click.Group", main.commands["daemon"])
_install_group = cast("click.Group", _daemon.commands["install"])
_install_tunnel = _install_group.commands["tunnel"]

_FULL = TunnelSpec(
    label="ha",
    service_url="http://localhost:8123",
    zone="t00t.us",
    auth_mode="none",
    websocket_enabled=False,
    verify_ssl=True,
    forward_host=True,
    upstream_basic_auth="user:pa:ss",
    apex=False,
    options={"key": "value", "other": "a=b"},
    response_timeout=300,
)


def _parses(cmd: click.Command, argv: list[str]) -> click.Context:
    """Parse ``argv`` against ``cmd`` without running its callback."""
    return cmd.make_context(cmd.name or "cmd", list(argv))


def _spec_from_expose(ctx: click.Context) -> TunnelSpec:
    params = dict(ctx.params)
    return spec_from_params(
        service_url=params.pop("service"), label=params.pop("service_label"), **params
    )


# --------------------------------------------------------------------------- #
# build_expose_args -> `hle expose` (what a unit file execs)
# --------------------------------------------------------------------------- #


def test_build_expose_args_round_trips_through_expose() -> None:
    argv = build_expose_args(
        TunnelSpec(
            service_url="http://localhost:8123",
            label="ha",
            zone="t00t.us",
            verify_ssl=True,
            forward_host=True,
            options={"key": "value"},
        ),
        allow=("alice@example.com", "google:bob@example.com"),
    )
    # build_expose_args (like build_fp_args) emits the leading process-name
    # token a unit execs (`hle expose ...`); it is not one of expose's options.
    assert argv[0] == "expose"
    ctx = _parses(expose, argv[1:])
    assert ctx.params["service"] == "http://localhost:8123"
    assert ctx.params["service_label"] == "ha"
    assert ctx.params["verify_ssl"] is True
    assert ctx.params["forward_host"] is True
    assert ctx.params["options"] == ("key=value",)
    assert ctx.params["allow"] == ("alice@example.com", "google:bob@example.com")


def test_every_spec_field_survives_argv_plus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec -> (argv, env) -> expose's parse -> spec is the identity.

    This is the test that makes option drift fail loudly: a new TunnelSpec
    field with a flag that build_expose_args forgets comes back as its default.
    """
    argv = build_expose_args(_FULL)
    for key, value in expose_env(_FULL).items():
        monkeypatch.setenv(key, value)
    back = _spec_from_expose(_parses(expose, argv[1:]))
    # managed_by has no flag; the daemon never sets it.
    assert back == dataclasses.replace(_FULL, managed_by=None)


def test_build_expose_args_carries_upstream_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Was a strict xfail (audit §1): the builder had no way to carry it."""
    spec = TunnelSpec(service_url="http://localhost:8123", label="ha", upstream_basic_auth="u:p")
    argv = build_expose_args(spec)
    assert "u:p" not in argv  # never on a command line
    for key, value in expose_env(spec).items():
        monkeypatch.setenv(key, value)
    ctx = _parses(expose, argv[1:])
    assert ctx.params["upstream_basic_auth"] == "u:p"


def test_expose_hle_spec_round_trips_through_the_unit_comment() -> None:
    """What `daemon install tunnel` actually writes and reads back on `refresh`."""
    argv = build_expose_args(_FULL, allow=("a@x.com",))
    stamp = {
        "version": "0.0.0",
        "label": "ha",
        "run_args": argv,
        "tunnel": _FULL.model_dump(),
        "allow": ["a@x.com"],
    }
    comment = spec_comment(stamp)
    assert comment is not None
    parsed = parse_service_spec(f"ExecStart=/usr/bin/hle {' '.join(argv)}\n{comment}\n")
    assert parsed is not None
    assert parsed["run_args"][0] == "expose"
    ctx = _parses(expose, parsed["run_args"][1:])
    assert ctx.params["verify_ssl"] is True
    assert stamped_tunnel_spec(parsed) == _FULL


def test_old_stamp_without_tunnel_key_is_still_read() -> None:
    old = {"version": "2609.1", "label": "ha", "run_args": ["expose", "--service", "x"]}
    parsed = parse_service_spec(spec_comment(old) or "")
    assert parsed == old
    assert stamped_tunnel_spec(parsed) is None


# --------------------------------------------------------------------------- #
# build_fp_args -> `hle forward` (leaf command, legacy name `fp` in its argv)
# --------------------------------------------------------------------------- #


def test_build_fp_args_round_trips_through_fp() -> None:
    argv = build_fp_args(
        agent="rpi",
        target="192.168.1.50:22",
        bind_port=9922,
        bind_host="0.0.0.0",
        relay_host="relay.example.com",
        relay_port=8443,
    )
    # build_fp_args emits the legacy `fp ...` argv a unit execs; the leading
    # `fp` token names the process, not an option `fp` itself parses.
    assert argv[0] == "fp"
    ctx = _parses(fp, argv[1:])
    assert ctx.params["agent"] == "rpi"
    assert ctx.params["targets"] == ("192.168.1.50:22",)
    assert ctx.params["bind_ports"] == (9922,)
    assert ctx.params["bind_host"] == "0.0.0.0"
    assert ctx.params["relay_host"] == "relay.example.com"
    assert ctx.params["relay_port"] == 8443


# --------------------------------------------------------------------------- #
# `daemon install tunnel` — generated from the same TunnelSpec metadata
# --------------------------------------------------------------------------- #


def test_daemon_install_tunnel_parses_the_full_tunnel_option_set() -> None:
    argv = [
        "ha",
        "http://localhost:8123",
        "--zone",
        "t00t.us",
        "--auth",
        "sso",
        "--no-websocket",
        "--verify-ssl",
        "--forward-host",
        "--allow",
        "alice@example.com",
        "--option",
        "key=value",
        "--response-timeout",
        "60",
        "--user",
        "--start",
    ]
    ctx = _parses(_install_tunnel, argv)
    assert ctx.params["label"] == "ha"
    assert ctx.params["url"] == "http://localhost:8123"
    assert ctx.params["verify_ssl"] is True
    assert ctx.params["response_timeout"] == 60


def test_daemon_install_tunnel_carries_upstream_basic_auth() -> None:
    """Was a strict xfail (audit §1): the option did not exist here."""
    argv = ["ha", "http://localhost:8123", "--upstream-basic-auth", "user:pass"]
    ctx = _parses(_install_tunnel, argv)
    assert ctx.params["upstream_basic_auth"] == "user:pass"


def test_webhooks_cannot_be_daemonised_at_all() -> None:
    """audit §1: 'Webhooks cannot be daemonised at all.' Still true.

    TunnelSpec carries `webhook_path`, but webhooks run through `tunnel
    webhook`, not `expose`, and build_expose_args refuses a webhook spec
    rather than write an argv that would silently serve the whole service.
    A `daemon install webhook` verb is follow-up work; when it lands this
    should become a round-trip case like the ones above.
    """
    assert "webhook" not in _install_group.commands
    with pytest.raises(ValueError):
        build_expose_args(TunnelSpec(service_url="http://x", label="gh", webhook_path="/h"))
