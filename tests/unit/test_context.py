"""What one invocation carries: credential precedence and the prompting policy.

`hle_client.context` is the only place a leaf asks "which key?", "may I
prompt?" and "which Output?". These tests pin the answers directly, without
going through any particular command.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from hle_client import context
from hle_client.cli import main
from hle_client.errors import AbortedError, AuthError, UsageError
from hle_client.output import Output

_FLAG = "hle_" + "f" * 32
_ENV = "hle_" + "e" * 32
_FILE = "hle_" + "c" * 32
_LEAF = "hle_" + "1" * 32


def _ctx(**obj: Any) -> click.Context:
    """A context the root group would have populated."""
    ctx = click.Context(click.Command("x"))
    ctx.obj = obj
    return ctx


@pytest.fixture()
def saved_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("hle_client.config.load_api_key", lambda: _FILE)
    return _FILE


class TestResolutionOrder:
    """flag > HLE_API_KEY > ~/.config/hle/config.toml, with the leaf's own flag first."""

    def test_leaf_flag_beats_everything(self, monkeypatch, saved_key) -> None:
        monkeypatch.setenv("HLE_API_KEY", _ENV)
        assert context.resolve_api_key(_ctx(api_key=_FLAG), _LEAF) == _LEAF

    def test_root_flag_beats_env_and_file(self, monkeypatch, saved_key) -> None:
        monkeypatch.setenv("HLE_API_KEY", _ENV)
        assert context.resolve_api_key(_ctx(api_key=_FLAG)) == _FLAG

    def test_env_beats_file(self, monkeypatch, saved_key) -> None:
        monkeypatch.setenv("HLE_API_KEY", _ENV)
        assert context.resolve_api_key(_ctx(api_key=None)) == _ENV

    def test_file_is_the_last_resort(self, monkeypatch, saved_key) -> None:
        monkeypatch.delenv("HLE_API_KEY", raising=False)
        assert context.resolve_api_key(_ctx(api_key=None)) == _FILE

    def test_nothing_anywhere_is_none(self, monkeypatch) -> None:
        monkeypatch.delenv("HLE_API_KEY", raising=False)
        monkeypatch.setattr("hle_client.config.load_api_key", lambda: None)
        assert context.resolve_api_key(_ctx(api_key=None)) is None

    def test_a_context_the_root_never_populated_still_resolves(self, monkeypatch, saved_key):
        """`ctx.invoke` and direct calls in tests arrive with no ctx.obj."""
        monkeypatch.delenv("HLE_API_KEY", raising=False)
        bare = click.Context(click.Command("x"))
        assert context.resolve_api_key(bare) == _FILE
        assert context.resolve_api_key(None) == _FILE

    def test_the_real_root_group_feeds_the_same_resolver(self, monkeypatch, saved_key) -> None:
        """End to end: the root's --api-key lands in ctx.obj and wins over the file."""
        monkeypatch.delenv("HLE_API_KEY", raising=False)
        seen: list[str | None] = []

        @main.command("_peek", hidden=True)
        @click.pass_context
        def _peek(ctx: click.Context) -> None:
            seen.append(context.resolve_api_key(ctx))

        try:
            CliRunner().invoke(main, ["--api-key", _FLAG, "_peek"])
        finally:
            del main.commands["_peek"]
        assert seen == [_FLAG]


class TestRequireAndApi:
    def test_require_raises_auth_error_when_there_is_no_key(self, monkeypatch) -> None:
        monkeypatch.delenv("HLE_API_KEY", raising=False)
        monkeypatch.setattr("hle_client.config.load_api_key", lambda: None)
        with pytest.raises(AuthError) as excinfo:
            context.require_api_key(_ctx(api_key=None))
        assert excinfo.value.exit_code == 3
        assert "hle auth login" in excinfo.value.message

    def test_api_builds_one_client_per_invocation(self, monkeypatch, saved_key) -> None:
        monkeypatch.delenv("HLE_API_KEY", raising=False)
        built: list[str] = []

        class _Fake:
            def __init__(self, config: Any) -> None:
                built.append(config.api_key)

        ctx = _ctx(api_key=_FLAG)
        with patch("hle_client.api.ApiClient", _Fake):
            first = context.api(ctx)
            second = context.api(ctx)
        assert first is second
        assert built == [_FLAG]

    def test_api_looks_the_class_up_at_call_time(self, monkeypatch, saved_key) -> None:
        """So a test patching `hle_client.api.ApiClient` sees every command."""
        monkeypatch.delenv("HLE_API_KEY", raising=False)

        class _Fake:
            def __init__(self, config: Any) -> None:
                self.key = config.api_key

        with patch("hle_client.api.ApiClient", _Fake):
            client = context.api(_ctx(api_key=_FLAG))
        assert isinstance(client, _Fake)
        assert client.key == _FLAG


class TestOutput:
    def test_out_returns_the_roots_output(self) -> None:
        o = Output(fmt="json")
        assert context.out(_ctx(output=o)) is o
        assert context.is_json(_ctx(output=o)) is True

    def test_out_falls_back_to_a_default(self) -> None:
        o = context.out(None)
        assert isinstance(o, Output)
        assert context.is_json(None) is False


class TestNoInput:
    """`--no-input` means never prompt: answer with the default, or fail."""

    def test_confirm_prompts_when_allowed(self, monkeypatch) -> None:
        monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
        assert context.confirm(_ctx(no_input=False), "Sure?") is True

    def test_confirm_returns_the_default_under_no_input(self, monkeypatch) -> None:
        def _never(*a: Any, **k: Any) -> bool:
            raise AssertionError("prompted under --no-input")

        monkeypatch.setattr(click, "confirm", _never)
        assert context.confirm(_ctx(no_input=True), "Sure?", default=False) is False
        assert context.confirm(_ctx(no_input=True), "Sure?", default=True) is True

    def test_confirm_or_abort_declined_is_aborted(self, monkeypatch) -> None:
        monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
        with pytest.raises(AbortedError) as excinfo:
            context.confirm_or_abort(_ctx(no_input=False), "Delete?")
        assert excinfo.value.message == "Aborted."
        assert excinfo.value.exit_code == 1

    def test_confirm_or_abort_accepted_returns(self, monkeypatch) -> None:
        monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
        context.confirm_or_abort(_ctx(no_input=False), "Delete?")

    def test_confirm_or_abort_under_no_input_aborts_with_a_hint(self, monkeypatch) -> None:
        def _never(*a: Any, **k: Any) -> bool:
            raise AssertionError("prompted under --no-input")

        monkeypatch.setattr(click, "confirm", _never)
        with pytest.raises(AbortedError) as excinfo:
            context.confirm_or_abort(_ctx(no_input=True), "Delete?", default=False)
        assert "--yes" in (excinfo.value.hint or "")

    def test_confirm_or_abort_under_no_input_with_a_yes_default_proceeds(self, monkeypatch):
        def _never(*a: Any, **k: Any) -> bool:
            raise AssertionError("prompted under --no-input")

        monkeypatch.setattr(click, "confirm", _never)
        context.confirm_or_abort(_ctx(no_input=True), "Restart?", default=True)

    def test_prompt_is_refused_under_no_input(self, monkeypatch) -> None:
        def _never(*a: Any, **k: Any) -> str:
            raise AssertionError("prompted under --no-input")

        monkeypatch.setattr(click, "prompt", _never)
        with pytest.raises(UsageError) as excinfo:
            context.prompt(_ctx(no_input=True), "API key")
        assert excinfo.value.exit_code == 2
        assert "API key" in excinfo.value.message

    def test_prompt_passes_through_otherwise(self, monkeypatch) -> None:
        monkeypatch.setattr(click, "prompt", lambda text, **k: f"answered {text}")
        assert context.prompt(_ctx(no_input=False), "PIN") == "answered PIN"


class TestNoInputEndToEnd:
    """The flag on the real root, reaching real prompts."""

    def test_tunnel_delete_without_yes_aborts_instead_of_asking(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.delenv("HLE_API_KEY", raising=False)
        client = AsyncMock()
        client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        client.list_tunnels = AsyncMock(
            return_value=[{"subdomain": "ha-x7k", "tunnel_id": "t1", "is_active": False}]
        )
        with patch("hle_client.api.ApiClient", return_value=client):
            result = CliRunner().invoke(
                main, ["--no-input", "--api-key", _FLAG, "tunnel", "delete", "ha"]
            )
        assert result.exit_code == 1
        assert "Aborted." in result.stderr
        client.delete_tunnel_record.assert_not_called()

    def test_auth_login_will_not_prompt_for_a_key(self, monkeypatch) -> None:
        with patch("hle_client.cli.webbrowser.open"):
            result = CliRunner().invoke(main, ["--no-input", "auth", "login"])
        assert result.exit_code == 2
        assert "--no-input" in result.stderr

    def test_declining_a_delete_says_aborted(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.delenv("HLE_API_KEY", raising=False)
        client = AsyncMock()
        client.get_me = AsyncMock(return_value={"user_code": "x7k"})
        client.list_tunnels = AsyncMock(
            return_value=[{"subdomain": "ha-x7k", "tunnel_id": "t1", "is_active": False}]
        )
        with patch("hle_client.api.ApiClient", return_value=client):
            result = CliRunner().invoke(
                main, ["--api-key", _FLAG, "tunnel", "delete", "ha"], input="n\n"
            )
        assert result.exit_code == 1
        assert "Aborted." in result.stderr
        client.delete_tunnel_record.assert_not_called()
