"""Three conventions the CLI claims for every leaf command, checked for real.

Root help (`cli.py:103`) says "Every command that reads a resource accepts
-o json." The audit (docs/audits-2026-09-23/cli-audit.md §1, "Output formats" /
"Root flags honoured unevenly") found that claim false for all but five
commands, and `--no-input` honoured by exactly one. These tests parametrise
over every leaf command in the tree and check three things:

  (a) `-o json` hoisted to the root, followed by the leaf's own `--help`,
      never produces a usage error — the parser always accepts the flag, even
      on leaves that go on to ignore it.
  (b) root `--api-key` actually *reaches* the commands that call the API —
      not just "is accepted", but is the credential used. The signal is
      exactly the one the source uses: `hle_client.output.api_key_from_ctx`.
      Commands that call it fall back to the root key when they have no flag
      of their own; commands that read their own `--api-key` (or its envvar)
      directly do not. That second group is this file's xfail list — the
      debt item is tracked as plan §2.1 recommendation 3 ("One context for
      credentials, output, prompts").
  (c) `--no-input` hoisted to the root is likewise always *accepted* — no
      usage error — on every leaf, independent of whether that leaf's own
      prompts (§2.1 recommendation 3 again) pay attention to it.

None of this hits the network: (a) and (c) never get past `--help`, and (b)
patches `ApiClient` with a small recorder instead of a mock tied to any one
command's return shape, so it works unmodified across commands with entirely
different response bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Iterator

from hle_client.cli import main

from ._cli_introspect import leaf_paths

_ALL_LEAVES = leaf_paths(main)


def _words(path: str) -> list[str]:
    """Drop the leading "hle" root name Click never sees on argv."""
    return path.split()[1:]


# --------------------------------------------------------------------------- #
# (a) / (c): global flags never break a leaf's own --help
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _ALL_LEAVES, ids=_ALL_LEAVES)
def test_output_json_hoists_cleanly_to_every_leaf(path: str) -> None:
    result = CliRunner().invoke(main, ["-o", "json", *_words(path), "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


@pytest.mark.parametrize("path", _ALL_LEAVES, ids=_ALL_LEAVES)
def test_no_input_hoists_cleanly_to_every_leaf(path: str) -> None:
    result = CliRunner().invoke(main, ["--no-input", *_words(path), "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


# --------------------------------------------------------------------------- #
# (b): root --api-key actually reaching the commands that call the API
# --------------------------------------------------------------------------- #

_ROOT_KEY = "hle_" + "a" * 32
_LOCAL_KEY = "hle_" + "b" * 32  # what ~/.config/hle/config.toml would hold


class _RecordingApiClient:
    """Stands in for ``ApiClient``: records the key it was built with.

    Every attribute access returns an async no-op that answers with an empty
    (falsy) result, so any command — whatever it goes on to call, whatever
    shape it expects back — gets *something* it can render or fail past,
    without this file needing to know its response schema. What matters is
    only ever the key ``ApiClientConfig`` was constructed with.
    """

    captured: list[str | None] = []

    def __init__(self, config: Any) -> None:
        type(self).captured.append(config.api_key)

    def __getattr__(self, _name: str) -> Any:
        async def _any(*_a: Any, **_kw: Any) -> dict[str, Any]:
            return {}

        return _any


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_RecordingApiClient]]:
    _RecordingApiClient.captured = []
    monkeypatch.delenv("HLE_API_KEY", raising=False)
    # Simulates "no key saved locally" — a command that falls back to it
    # instead of the root key is instantly visible: the fallback is _LOCAL_KEY.
    monkeypatch.setattr("hle_client.config.load_api_key", lambda: _LOCAL_KEY)
    with (
        patch("hle_client.config_cmd.ApiClient", _RecordingApiClient),
        patch("hle_client.api.ApiClient", _RecordingApiClient),
    ):
        yield _RecordingApiClient


# Commands that resolve their credential through `api_key_from_ctx`, so the
# root `--api-key` reaches them when they have no flag of their own. Verified
# against the source: this is the complete set — `grep -rn api_key_from_ctx
# src/hle_client/*.py` names exactly these four call sites plus `status_cmd`.
_HONOURS_ROOT_API_KEY: dict[str, tuple[list[str], str | None]] = {
    "hle tunnel get": (["tunnel", "get", "ha-x7k"], None),
    "hle tunnel list": (["tunnel", "list"], None),
    "hle tunnel delete": (["tunnel", "delete", "ha-x7k", "--yes"], None),
    "hle status": (["status"], None),
}

# The rest of the API-calling leaves: each reads its own `--api-key` option
# (falling back to its own envvar / the config file) and never asks the
# context for the root's. This is the debt the audit calls out by name
# (§1 "Root flags honoured unevenly") and plan §2.1 recommendation 3 is meant
# to retire by replacing every one of these with `api_key_from_ctx`. Each
# entry is `path -> (argv, stdin)`.
_IGNORES_ROOT_API_KEY: dict[str, tuple[list[str], str | None]] = {
    "hle agent list": (["agent", "list"], None),
    "hle tunnel access list": (["tunnel", "access", "list", "ha-x7k"], None),
    "hle tunnel access add": (["tunnel", "access", "add", "ha-x7k", "friend@example.com"], None),
    "hle tunnel access remove": (["tunnel", "access", "remove", "ha-x7k", "1"], None),
    "hle tunnel access replace": (
        ["tunnel", "access", "replace", "ha-x7k", "friend@example.com"],
        None,
    ),
    "hle tunnel auth-mode": (["tunnel", "auth-mode", "ha-x7k", "--set", "sso"], None),
    "hle tunnel basic-auth status": (["tunnel", "basic-auth", "status", "ha-x7k"], None),
    "hle tunnel basic-auth remove": (["tunnel", "basic-auth", "remove", "ha-x7k"], None),
    "hle tunnel basic-auth set": (
        ["tunnel", "basic-auth", "set", "ha-x7k"],
        "user\npassword123\npassword123\n",
    ),
    "hle tunnel pin status": (["tunnel", "pin", "status", "ha-x7k"], None),
    "hle tunnel pin remove": (["tunnel", "pin", "remove", "ha-x7k"], None),
    "hle tunnel pin set": (["tunnel", "pin", "set", "ha-x7k"], "1234\n1234\n"),
    "hle tunnel share create": (["tunnel", "share", "create", "ha-x7k"], None),
    "hle tunnel share list": (["tunnel", "share", "list", "ha-x7k"], None),
    "hle tunnel share revoke": (["tunnel", "share", "revoke", "ha-x7k", "1"], None),
}

# Not exercised here at all: `expose`, `webhook` (root and `tunnel webhook`),
# and `forward` resolve their credential inside `Tunnel`/`fp` at *connection*
# time, not through `_client()`/`ApiClient` — the recorder above can't see it
# without also faking the relay connection. Per the same audit line they also
# ignore the root key (`forward` and `webhook` are named explicitly; `expose`
# reads its own `--api-key`/envvar the same way). Tracked as the same debt,
# just not asserted by this test.
#
# `tunnel create` is accounted for separately, by
# `test_tunnel_create_forwards_root_api_key_into_the_tunnel_config` below — it
# does honour the root key (it's the fourth `api_key_from_ctx` call site), but
# via `Tunnel`, not `ApiClient`, so it needs its own fake.
#
# `auth login`'s `--api-key` names a *different* thing entirely — the key to
# save, not one to authenticate with (see `hoist_global_options`'s docstring)
# — so "does the root key reach it" does not apply; it is excluded from the
# accounting rather than filed as either honouring or ignoring.
_NOT_EXERCISED_TUNNEL_BASED = ("hle expose", "hle webhook", "hle tunnel webhook", "hle forward")
_HONOURS_VIA_OWN_MECHANISM = ("hle tunnel create",)
_NOT_APPLICABLE = ("hle auth login",)


@pytest.mark.parametrize("path", sorted(_HONOURS_ROOT_API_KEY), ids=sorted(_HONOURS_ROOT_API_KEY))
def test_root_api_key_reaches_command(path: str, recorder: type[_RecordingApiClient]) -> None:
    argv, stdin = _HONOURS_ROOT_API_KEY[path]
    result = CliRunner().invoke(main, ["--api-key", _ROOT_KEY, *argv], input=stdin)
    assert recorder.captured, f"{path} never constructed an ApiClient ({result.output})"
    assert recorder.captured[0] == _ROOT_KEY


@pytest.mark.parametrize("path", sorted(_IGNORES_ROOT_API_KEY), ids=sorted(_IGNORES_ROOT_API_KEY))
@pytest.mark.xfail(
    reason=(
        "audit §1 'Root flags honoured unevenly': this command reads its own "
        "--api-key/envvar directly instead of hle_client.output.api_key_from_ctx, "
        "so the root --api-key is silently ignored. Fixed by plan §2.1 rec. 3."
    ),
    strict=True,
)
def test_root_api_key_should_reach_command_but_does_not_yet(
    path: str, recorder: type[_RecordingApiClient]
) -> None:
    argv, stdin = _IGNORES_ROOT_API_KEY[path]
    result = CliRunner().invoke(main, ["--api-key", _ROOT_KEY, *argv], input=stdin)
    assert recorder.captured, f"{path} never constructed an ApiClient ({result.output})"
    assert recorder.captured[0] == _ROOT_KEY


def test_tunnel_create_forwards_root_api_key_into_the_tunnel_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tunnel create` is the one command that resolves its key through
    `api_key_from_ctx` (`cli.py:554`) but never touches `ApiClient` directly —
    it hands the key to `TunnelConfig` and connects. Captured at `Tunnel(...)`
    construction instead, with the actual connection short-circuited.
    """
    captured: list[str | None] = []

    class _FakeTunnel:
        def __init__(self, *, config: Any, **_kw: Any) -> None:
            captured.append(config.api_key)

        async def connect(self) -> None:
            return None

    monkeypatch.delenv("HLE_API_KEY", raising=False)
    with (
        patch("hle_client.cli.Tunnel", _FakeTunnel),
        patch("hle_client.cli.shutdown.run", lambda coro: coro.close()),
    ):
        result = CliRunner().invoke(
            main,
            ["--api-key", _ROOT_KEY, "tunnel", "create", "ha", "http://localhost:8080"],
        )
    assert captured, result.output
    assert captured[0] == _ROOT_KEY


def test_every_api_key_leaf_is_accounted_for() -> None:
    """The three lists above must add up to every leaf with an --api-key story.

    Guards the debt register itself: a new command added with its own
    --api-key option, and neither honoured nor recorded as ignoring the root
    key, would otherwise fall through unnoticed.
    """
    accounted = (
        set(_HONOURS_ROOT_API_KEY)
        | set(_IGNORES_ROOT_API_KEY)
        | set(_NOT_EXERCISED_TUNNEL_BASED)
        | set(_HONOURS_VIA_OWN_MECHANISM)
        | set(_NOT_APPLICABLE)
    )
    from ._cli_introspect import walk

    api_key_leaves = {
        n.path
        for n in walk(main)
        if n.kind == "command" and any("--api-key" in p.opts for p in n.params)
    }
    # `status` has no --api-key option of its own (root-only) but does call
    # the API with the resolved key, so it belongs in the accounted set too.
    api_key_leaves.add("hle status")
    assert api_key_leaves == accounted, api_key_leaves.symmetric_difference(accounted)
