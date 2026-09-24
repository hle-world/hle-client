"""The one error path: exit codes, which stream, and the JSON shape.

Every command raises an ``HleError``; the root group renders it. These tests
pin the contract a script relies on — a fixed code per kind, nothing on
stdout, and under ``-o json`` one object on stderr — without caring which
command raised.
"""

from __future__ import annotations

import json
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from hle_client.cli import main
from hle_client.errors import (
    AbortedError,
    ApiError,
    AuthError,
    ConflictError,
    HleError,
    NotFoundError,
    UnreachableError,
    UsageError,
)


def _invoke_raising(exc: Exception, *root_args: str) -> Any:
    """Run a throwaway leaf under the real root group that raises *exc*."""

    @main.command("_boom", hidden=True)
    def _boom() -> None:
        raise exc

    try:
        return CliRunner().invoke(main, [*root_args, "_boom"])
    finally:
        del main.commands["_boom"]


class TestExitCodes:
    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (HleError("plain"), 1),
            (UsageError("usage"), 2),
            (AuthError("auth"), 3),
            (NotFoundError("missing"), 4),
            (UnreachableError("down"), 5),
            (ConflictError("clash"), 1),
            (AbortedError(), 1),
            (HleError("child said so", exit_code=127), 127),
        ],
    )
    def test_each_kind_has_its_code(self, exc: HleError, code: int) -> None:
        result = _invoke_raising(exc)
        assert result.exit_code == code, result.output

    def test_exit_code_override_beats_the_class_default(self) -> None:
        assert AuthError("x", exit_code=9).exit_code == 9

    def test_aborted_has_a_fixed_message(self) -> None:
        assert str(AbortedError()) == "Aborted."


class TestStreams:
    def test_message_and_hint_go_to_stderr_not_stdout(self) -> None:
        result = _invoke_raising(HleError("it broke", hint="try again"))
        assert "it broke" in result.stderr
        assert "try again" in result.stderr
        assert result.stdout == ""

    def test_the_error_line_is_labelled(self) -> None:
        result = _invoke_raising(HleError("it broke"))
        assert "Error: it broke" in result.stderr

    def test_quiet_does_not_silence_errors(self) -> None:
        result = _invoke_raising(HleError("still said"), "--quiet")
        assert "still said" in result.stderr

    def test_no_color_strips_markup(self) -> None:
        result = _invoke_raising(HleError("[cyan]plain[/cyan]"), "--no-color")
        assert "plain" in result.stderr
        assert "[cyan]" not in result.stderr


class TestJsonShape:
    def test_json_mode_emits_one_object_on_stderr(self) -> None:
        result = _invoke_raising(NotFoundError("no such tunnel", hint="list them"), "-o", "json")
        assert result.stdout == ""
        payload = json.loads(result.stderr)
        assert payload == {"error": "no such tunnel", "code": 4, "hint": "list them"}

    def test_status_is_included_when_the_relay_answered(self) -> None:
        err = AuthError("refused", status=401)
        assert err.as_dict() == {"error": "refused", "code": 3, "status": 401}

    def test_hint_is_omitted_when_there_is_none(self) -> None:
        assert "hint" not in HleError("x").as_dict()


def _status_error(code: int, *, json_body: Any = None, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://hle.world/api/x")
    if json_body is not None:
        response = httpx.Response(code, json=json_body, request=request)
    else:
        response = httpx.Response(code, text=text, request=request)
    return httpx.HTTPStatusError(str(code), request=request, response=response)


class TestApiErrorMapping:
    """One mapper, replacing three: what each relay answer becomes."""

    def test_401_is_auth_and_prefers_the_relays_detail(self) -> None:
        err = ApiError.from_http(_status_error(401, json_body={"detail": "wrong kind of token"}))
        assert isinstance(err, AuthError)
        assert err.message == "wrong kind of token"
        assert err.status == 401

    def test_401_without_detail_has_a_fallback(self) -> None:
        err = ApiError.from_http(_status_error(401, text="nope"))
        assert err.message == "Invalid or missing API key."

    def test_403_names_the_tunnel_when_known(self) -> None:
        err = ApiError.from_http(_status_error(403), subdomain="ha-x7k")
        assert isinstance(err, AuthError)
        assert "ha-x7k" in err.message

    def test_404_is_not_found(self) -> None:
        err = ApiError.from_http(_status_error(404), subdomain="ha-x7k")
        assert isinstance(err, NotFoundError)
        assert err.message == "Tunnel 'ha-x7k' not found."
        assert ApiError.from_http(_status_error(404)).message == "Resource not found."

    def test_409_is_a_conflict(self) -> None:
        assert isinstance(ApiError.from_http(_status_error(409)), ConflictError)

    def test_429_is_a_plain_error(self) -> None:
        err = ApiError.from_http(_status_error(429))
        assert type(err) is HleError
        assert "Rate limited" in err.message

    def test_anything_else_quotes_the_status_and_body(self) -> None:
        err = ApiError.from_http(_status_error(502, text="bad gateway"))
        assert isinstance(err, ApiError)
        assert err.message == "Server returned 502: bad gateway"
        assert err.exit_code == 1

    def test_a_transport_failure_is_unreachable(self) -> None:
        exc = httpx.ConnectError("refused", request=httpx.Request("GET", "https://hle.world"))
        err = ApiError.from_http(exc)
        assert isinstance(err, UnreachableError)
        assert err.exit_code == 5

    def test_from_exception_passes_hle_errors_through(self) -> None:
        original = NotFoundError("gone")
        assert ApiError.from_exception(original) is original

    def test_from_exception_wraps_anything_else(self) -> None:
        err = ApiError.from_exception(RuntimeError("odd"))
        assert type(err) is HleError
        assert err.message == "odd"


class TestClickErrorsAreUntouched:
    """Argument parsing is still Click's: its usage errors keep exit 2."""

    def test_an_unknown_option_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(main, ["tunnel", "list", "--bogus"])
        assert result.exit_code == 2
        assert "Usage:" in result.output

    def test_click_exceptions_are_not_swallowed(self) -> None:
        result = _invoke_raising(click.ClickException("from click"))
        assert result.exit_code == 1
        assert "from click" in result.output
