"""The argv a service file execs must still parse against the real commands.

`hle daemon install tunnel` builds argv with `build_expose_args(TunnelSpec)`,
writes it and the spec into a unit/plist/rc.d script as an ``hle-spec:``
comment (`service_cmd.spec_comment` / `parse_service_spec`), and execs it as
``hle tunnel create ...`` on every boot (``hle expose ...`` before plan §2.6a;
old units keep that until ``daemon refresh``). If a tunnel option is ever added to the
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
from typing import Any, cast

import click
import pytest

from hle_client.cli import expose, main, tunnel_create
from hle_client.fp_cmd import _split_positionals, fp
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


def _tunnel_params(argv: list[str]) -> dict[str, Any]:
    """Parse a tunnel unit's argv the way `hle` would, as `expose`-shaped params.

    Units exec `tunnel create [LABEL] URL ...` now and `expose --service URL
    --label L ...` before; both must parse, since an old unit keeps its argv
    until `daemon refresh` rewrites it. Returned with `expose`'s names
    (`service`, `service_label`) so the assertions read the same for both.
    """
    if argv[:2] == ["tunnel", "create"]:
        params = dict(_parses(tunnel_create, argv[2:]).params)
        first, second = params.pop("first"), params.pop("second")
        url, label = (second, first) if second is not None else (first, None)
        params.update(service=url, service_label=label)
        return params
    assert argv[0] == "expose", argv
    return dict(_parses(expose, argv[1:]).params)


def _spec_from_params(params: dict[str, Any]) -> TunnelSpec:
    params = dict(params)
    params.pop("api_key", None)
    params.pop("allow", None)
    params.pop("events", None)
    return spec_from_params(
        service_url=params.pop("service"), label=params.pop("service_label"), **params
    )


# --------------------------------------------------------------------------- #
# build_expose_args -> `hle tunnel create` (what a unit file execs)
# --------------------------------------------------------------------------- #


def test_build_expose_args_emits_tunnel_create() -> None:
    """Plan §2.6a: units stop depending on the legacy `expose` name."""
    argv = build_expose_args(TunnelSpec(service_url="http://localhost:8123", label="ha"))
    assert argv[:4] == ["tunnel", "create", "ha", "http://localhost:8123"]
    assert "expose" not in argv
    assert "--service" not in argv and "--label" not in argv


def test_the_emitted_argv_resolves_through_the_root_group() -> None:
    """What `hle` itself does with the unit's argv: `tunnel` -> `create`."""
    argv = build_expose_args(TunnelSpec(service_url="http://localhost:8123", label="ha"))
    ctx = click.Context(main)
    group = main.get_command(ctx, argv[0])
    assert isinstance(group, click.Group)
    assert group.get_command(ctx, argv[1]) is tunnel_create


def test_build_expose_args_round_trips_through_tunnel_create() -> None:
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
    params = _tunnel_params(argv)
    assert params["service"] == "http://localhost:8123"
    assert params["service_label"] == "ha"
    assert params["verify_ssl"] is True
    assert params["forward_host"] is True
    assert params["options"] == ("key=value",)
    assert params["allow"] == ("alice@example.com", "google:bob@example.com")


def test_an_apex_tunnel_has_only_the_url_positional() -> None:
    argv = build_expose_args(TunnelSpec(service_url="http://x", zone="t00t.us", apex=True))
    params = _tunnel_params(argv)
    assert params["service"] == "http://x"
    assert params["service_label"] is None
    assert params["apex"] is True


def test_every_spec_field_survives_argv_plus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec -> (argv, env) -> tunnel create's parse -> spec is the identity.

    This is the test that makes option drift fail loudly: a new TunnelSpec
    field with a flag that build_expose_args forgets comes back as its default.
    """
    argv = build_expose_args(_FULL)
    for key, value in expose_env(_FULL).items():
        monkeypatch.setenv(key, value)
    back = _spec_from_params(_tunnel_params(argv))
    # managed_by has no flag; the daemon never sets it.
    assert back == dataclasses.replace(_FULL, managed_by=None)


def test_build_expose_args_carries_upstream_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Was a strict xfail (audit §1): the builder had no way to carry it."""
    spec = TunnelSpec(service_url="http://localhost:8123", label="ha", upstream_basic_auth="u:p")
    argv = build_expose_args(spec)
    assert "u:p" not in argv  # never on a command line
    for key, value in expose_env(spec).items():
        monkeypatch.setenv(key, value)
    assert _tunnel_params(argv)["upstream_basic_auth"] == "u:p"


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
    assert parsed["run_args"][:2] == ["tunnel", "create"]
    assert _tunnel_params(parsed["run_args"])["verify_ssl"] is True
    assert stamped_tunnel_spec(parsed) == _FULL


def test_a_legacy_expose_argv_still_parses() -> None:
    """Units written before this keep exec'ing `expose` until refreshed."""
    params = _tunnel_params(["expose", "--service", "http://x", "--label", "ha", "--verify-ssl"])
    assert params["service"] == "http://x"
    assert params["service_label"] == "ha"
    assert params["verify_ssl"] is True


def test_old_stamp_without_tunnel_key_is_still_read() -> None:
    old = {"version": "2609.1", "label": "ha", "run_args": ["expose", "--service", "x"]}
    parsed = parse_service_spec(spec_comment(old) or "")
    assert parsed == old
    assert stamped_tunnel_spec(parsed) is None


# --------------------------------------------------------------------------- #
# build_fp_args -> `hle forward AGENT TARGET`
# --------------------------------------------------------------------------- #


def test_build_fp_args_round_trips_through_forward() -> None:
    argv = build_fp_args(
        agent="rpi",
        target="192.168.1.50:22",
        bind_port=9922,
        bind_host="0.0.0.0",
        relay_host="relay.example.com",
        relay_port=8443,
    )
    # The current grammar: positionals, under the name `forward`.
    assert argv[:3] == ["forward", "rpi", "192.168.1.50:22"]
    assert main.get_command(click.Context(main), argv[0]) is fp
    ctx = _parses(fp, argv[1:])
    # `forward` sorts positionals out in its callback; the parse keeps them raw.
    agent, targets, _command = _split_positionals(
        ctx.params["agent"], ctx.params["targets"], ctx.params["command"]
    )
    assert agent == "rpi"
    assert targets == ("192.168.1.50:22",)
    assert ctx.params["bind_ports"] == (9922,)
    assert ctx.params["bind_host"] == "0.0.0.0"
    assert ctx.params["relay_host"] == "relay.example.com"
    assert ctx.params["relay_port"] == 8443


def test_a_legacy_fp_argv_still_parses() -> None:
    ctx = _parses(fp, ["--agent", "rpi", "--to", "22", "--port", "9922"])
    assert ctx.params["agent"] == "rpi"
    assert ctx.params["targets"] == ("22",)


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
