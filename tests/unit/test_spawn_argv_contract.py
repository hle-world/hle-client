"""The argv a service file execs must still parse against the real commands.

`hle daemon install tunnel` builds argv with `build_expose_args` / a spec
dict, writes it into a unit/plist/rc.d script as an ``hle-spec:`` comment
(`service_cmd.spec_comment` / `parse_service_spec`), and execs it as
``hle expose ...`` on every boot. If a tunnel option is ever added to the
`expose`/`tunnel create` command without also reaching the argv builder, the
service file silently cannot carry it — the option works interactively and
vanishes the moment someone daemonises the tunnel. This is finding §1
"option drift" in the audit: `--upstream-basic-auth` already is that option,
missing from `build_expose_args` and from both `daemon install` variants.

Each case here builds argv the way the real code paths do, then feeds it to
the real Click command via ``make_context`` — which parses and validates
without invoking the callback, so nothing here opens a socket or touches the
filesystem outside of what the parse itself needs (nothing, for these
commands). A case that cannot be built or cannot parse is `xfail`, not
skipped: it is debt, and skipping would make it invisible again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from hle_client.cli import expose, main
from hle_client.fp_cmd import fp
from hle_client.service_cmd import (
    build_expose_args,
    build_fp_args,
    parse_service_spec,
    spec_comment,
)

if TYPE_CHECKING:
    import click

_daemon = cast("click.Group", main.commands["daemon"])
_install_group = cast("click.Group", _daemon.commands["install"])
_install_tunnel = _install_group.commands["tunnel"]


def _parses(cmd: click.Command, argv: list[str]) -> click.Context:
    """Parse ``argv`` against ``cmd`` without running its callback."""
    return cmd.make_context(cmd.name or "cmd", list(argv))


# --------------------------------------------------------------------------- #
# build_expose_args -> `hle expose` (what a unit file execs)
# --------------------------------------------------------------------------- #


def test_build_expose_args_round_trips_through_expose() -> None:
    argv = build_expose_args(
        service="http://localhost:8123",
        label="ha",
        zone="t00t.us",
        apex=False,
        auth="sso",
        websocket=True,
        verify_ssl=True,
        forward_host=True,
        allow=("alice@example.com", "google:bob@example.com"),
        options=("key=value",),
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


@pytest.mark.xfail(
    reason=(
        "audit §1 'option drift': build_expose_args() has no upstream_basic_auth "
        "parameter, so a tunnel using --upstream-basic-auth cannot be daemonised — "
        "the argv silently drops it. Plan §2.1 rec. 7 (one TunnelSpec/options "
        "decorator) is meant to close this."
    ),
    strict=True,
)
def test_build_expose_args_cannot_carry_upstream_basic_auth() -> None:
    argv = build_expose_args(  # type: ignore[call-arg]
        service="http://localhost:8123",
        label="ha",
        upstream_basic_auth="user:pass",
    )
    ctx = _parses(expose, argv[1:])
    assert ctx.params["upstream_basic_auth"] == ("user", "pass")


def test_expose_hle_spec_round_trips_through_the_unit_comment() -> None:
    """What `daemon install tunnel` actually writes and reads back on `refresh`."""
    argv = build_expose_args(service="http://localhost:8123", label="ha", verify_ssl=True)
    spec = {"version": "0.0.0", "label": "ha", "run_args": argv}
    comment = spec_comment(spec)
    assert comment is not None
    parsed = parse_service_spec(f"ExecStart=/usr/bin/hle {' '.join(argv)}\n{comment}\n")
    assert parsed is not None
    assert parsed["run_args"][0] == "expose"
    ctx = _parses(expose, parsed["run_args"][1:])
    assert ctx.params["verify_ssl"] is True


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
# `daemon install tunnel` — the other copy of the tunnel option set
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
        "--user",
        "--start",
    ]
    ctx = _parses(_install_tunnel, argv)
    assert ctx.params["label"] == "ha"
    assert ctx.params["url"] == "http://localhost:8123"
    assert ctx.params["verify_ssl"] is True


@pytest.mark.xfail(
    reason=(
        "audit §1 'option drift': `daemon install tunnel` has no "
        "--upstream-basic-auth option either — the same tunnel cannot be "
        "daemonised through this path, not just through build_expose_args."
    ),
    strict=True,
)
def test_daemon_install_tunnel_cannot_carry_upstream_basic_auth() -> None:
    argv = ["ha", "http://localhost:8123", "--upstream-basic-auth", "user:pass"]
    _parses(_install_tunnel, argv)


def test_webhooks_cannot_be_daemonised_at_all() -> None:
    """audit §1: 'Webhooks cannot be daemonised at all.' No verb, no builder.

    Locked as a plain assertion, not xfail — there is no argv to attempt
    building, so there is nothing to mark as failing. If a `daemon install
    webhook` verb is added, this test should be replaced with one exercising
    it the way the tunnel/forward/agent cases above are.
    """
    assert "webhook" not in _install_group.commands
