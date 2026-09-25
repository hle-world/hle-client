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
      not just "is accepted", but is the credential used. Every leaf resolves
      through `hle_client.context.resolve_api_key`, so the root key is the
      fallback when a leaf was given no flag of its own, and the leaf's own
      flag still wins when it was. (Fifteen of these were `xfail` until plan
      §2.2 gave them one resolver.)
  (c) `--no-input` hoisted to the root is likewise always *accepted* — no
      usage error — on every leaf. What each prompt does under it is
      `tests/unit/test_context.py`'s business.

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
    # One patch point: every command builds its client through
    # `hle_client.context.api`, which looks the class up on the module at
    # call time.
    with patch("hle_client.api.ApiClient", _RecordingApiClient):
        yield _RecordingApiClient


# Every leaf that builds an `ApiClient`. Each resolves its credential through
# `hle_client.context.api`, so the root `--api-key` reaches it when it has no
# flag of its own. This list was split in two — four that honoured the root
# key and fifteen `xfail`s that read their own option directly — until plan
# §2.2 gave them one resolver. Each entry is `path -> (argv, stdin)`.
_HONOURS_ROOT_API_KEY: dict[str, tuple[list[str], str | None]] = {
    "hle tunnel get": (["tunnel", "get", "ha-x7k"], None),
    "hle tunnel list": (["tunnel", "list"], None),
    "hle tunnel delete": (["tunnel", "delete", "ha-x7k", "--yes"], None),
    "hle status": (["status"], None),
    "hle agent list": (["agent", "list"], None),
    "hle tunnel access list": (["tunnel", "access", "list", "ha-x7k"], None),
    "hle tunnel access create": (
        ["tunnel", "access", "create", "ha-x7k", "friend@example.com"],
        None,
    ),
    "hle tunnel access delete": (["tunnel", "access", "delete", "ha-x7k", "1"], None),
    "hle tunnel access replace": (
        ["tunnel", "access", "replace", "ha-x7k", "friend@example.com"],
        None,
    ),
    "hle tunnel set": (["tunnel", "set", "ha-x7k", "--auth", "sso"], None),
    "hle tunnel basic-auth get": (["tunnel", "basic-auth", "get", "ha-x7k"], None),
    "hle tunnel basic-auth delete": (["tunnel", "basic-auth", "delete", "ha-x7k"], None),
    "hle tunnel basic-auth set": (
        ["tunnel", "basic-auth", "set", "ha-x7k"],
        "user\npassword123\npassword123\n",
    ),
    "hle tunnel pin get": (["tunnel", "pin", "get", "ha-x7k"], None),
    "hle tunnel pin delete": (["tunnel", "pin", "delete", "ha-x7k"], None),
    "hle tunnel pin set": (["tunnel", "pin", "set", "ha-x7k"], "1234\n1234\n"),
    "hle tunnel share create": (["tunnel", "share", "create", "ha-x7k"], None),
    "hle tunnel share list": (["tunnel", "share", "list", "ha-x7k"], None),
    "hle tunnel share delete": (["tunnel", "share", "delete", "ha-x7k", "1"], None),
}

# `expose` / `tunnel create`, `webhook` (root and `tunnel webhook`) and
# `forward` resolve the same way but hand the key to `Tunnel` / the forward
# loop rather than to `ApiClient`, so the recorder above cannot see it. Each
# gets its own fake below, capturing at the point the key is handed over.
#
# `auth login`'s `--api-key` names a *different* thing entirely — the key to
# save, not one to authenticate with (see `hoist_global_options`'s docstring)
# — so "does the root key reach it" does not apply; it is excluded from the
# accounting rather than filed as either honouring or ignoring.
_HONOURS_VIA_OWN_MECHANISM = (
    "hle tunnel create",
    "hle expose",
    "hle webhook",
    "hle tunnel webhook",
    "hle forward",
)
_NOT_APPLICABLE = ("hle auth login",)


@pytest.mark.parametrize("path", sorted(_HONOURS_ROOT_API_KEY), ids=sorted(_HONOURS_ROOT_API_KEY))
def test_root_api_key_reaches_command(path: str, recorder: type[_RecordingApiClient]) -> None:
    argv, stdin = _HONOURS_ROOT_API_KEY[path]
    result = CliRunner().invoke(main, ["--api-key", _ROOT_KEY, *argv], input=stdin)
    assert recorder.captured, f"{path} never constructed an ApiClient ({result.output})"
    assert recorder.captured[0] == _ROOT_KEY


@pytest.mark.parametrize("path", sorted(_HONOURS_ROOT_API_KEY), ids=sorted(_HONOURS_ROOT_API_KEY))
def test_leaf_api_key_still_wins_over_root(path: str, recorder: type[_RecordingApiClient]) -> None:
    """The leaf's own --api-key is the most specific thing said, so it wins."""
    if path == "hle status":
        pytest.skip("status has no --api-key of its own")
    argv, stdin = _HONOURS_ROOT_API_KEY[path]
    leaf_key = "hle_" + "c" * 32
    result = CliRunner().invoke(
        main, ["--api-key", _ROOT_KEY, *argv, "--api-key", leaf_key], input=stdin
    )
    assert recorder.captured, f"{path} never constructed an ApiClient ({result.output})"
    assert recorder.captured[0] == leaf_key


# The spellings the verbs above replaced (plan §2.5). They resolve to the same
# command objects through hidden aliases, so the root key must reach them too.
_OLD_SPELLINGS: dict[str, tuple[list[str], str | None]] = {
    "access add": (["tunnel", "access", "add", "ha-x7k", "friend@example.com"], None),
    "access remove": (["tunnel", "access", "remove", "ha-x7k", "1"], None),
    "auth-mode --set": (["tunnel", "auth-mode", "ha-x7k", "--set", "sso"], None),
    "basic-auth status": (["tunnel", "basic-auth", "status", "ha-x7k"], None),
    "basic-auth remove": (["tunnel", "basic-auth", "remove", "ha-x7k"], None),
    "pin status": (["tunnel", "pin", "status", "ha-x7k"], None),
    "pin remove": (["tunnel", "pin", "remove", "ha-x7k"], None),
    "share revoke": (["tunnel", "share", "revoke", "ha-x7k", "1"], None),
    "share create --label": (["tunnel", "share", "create", "ha-x7k", "--label", "x"], None),
}


@pytest.mark.parametrize("path", sorted(_OLD_SPELLINGS), ids=sorted(_OLD_SPELLINGS))
def test_root_api_key_reaches_old_spellings(path: str, recorder: type[_RecordingApiClient]) -> None:
    argv, stdin = _OLD_SPELLINGS[path]
    result = CliRunner().invoke(main, ["--api-key", _ROOT_KEY, *argv], input=stdin)
    assert recorder.captured, f"{path} never constructed an ApiClient ({result.output})"
    assert recorder.captured[0] == _ROOT_KEY
    # Hidden aliases are quiet: only the four renamed top-level names print a note.
    assert "note:" not in result.stderr


_TUNNEL_BASED: dict[str, list[str]] = {
    "hle tunnel create": ["tunnel", "create", "ha", "http://localhost:8080"],
    "hle expose": ["expose", "--service", "http://localhost:8080", "--label", "ha"],
    "hle webhook": [
        "webhook",
        "--path",
        "/hook",
        "--forward-to",
        "http://localhost:1",
        "--label",
        "gh",
    ],
    "hle tunnel webhook": [
        "tunnel",
        "webhook",
        "--path",
        "/hook",
        "--forward-to",
        "http://localhost:1",
        "--label",
        "gh",
    ],
}


@pytest.mark.parametrize("path", sorted(_TUNNEL_BASED), ids=sorted(_TUNNEL_BASED))
def test_root_api_key_reaches_the_tunnel_config(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """These never touch `ApiClient` — they hand the key to `TunnelConfig`
    and connect. Captured at `Tunnel(...)` construction instead, with the
    actual connection short-circuited.
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
        result = CliRunner().invoke(main, ["--api-key", _ROOT_KEY, *_TUNNEL_BASED[path]])
    assert captured, result.output
    assert captured[0] == _ROOT_KEY


def test_root_api_key_reaches_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    """`forward` hands the key to its relay loop; capture it there."""
    captured: list[str] = []

    async def _fake_run_all(*, api_key: str, **_kw: Any) -> int:
        captured.append(api_key)
        return 0

    monkeypatch.delenv("HLE_API_KEY", raising=False)
    with (
        patch("hle_client.fp_cmd._run_all", _fake_run_all),
        patch("hle_client.fp_cmd.shutdown.run", lambda coro: __import__("asyncio").run(coro)),
    ):
        result = CliRunner().invoke(main, ["--api-key", _ROOT_KEY, "forward", "rpi", "22"])
    assert captured, result.output
    assert captured[0] == _ROOT_KEY


def test_every_api_key_leaf_is_accounted_for() -> None:
    """The lists above must add up to every leaf with an --api-key story.

    Guards the register itself: a new command added with its own --api-key
    option and not recorded as honouring the root key would otherwise fall
    through unnoticed.
    """
    accounted = set(_HONOURS_ROOT_API_KEY) | set(_HONOURS_VIA_OWN_MECHANISM) | set(_NOT_APPLICABLE)
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
