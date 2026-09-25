"""One way to fail.

Errors used to leave the CLI three ways: ``console.print("[red]Error")`` to
stdout followed by ``SystemExit(1)``, ``click.ClickException`` to stderr with
exit 1, and ``click.UsageError`` with exit 2. A script could not tell "no such
tunnel" from "the relay is down", and ``-o json`` consumers got red text in
their data stream.

Now a command raises one of these and the root group renders it: message and
hint to stderr, or a JSON object under ``-o json``, and a fixed exit code per
kind. Nothing below the root prints an error or exits.

Exit codes:

  1  a plain error (the default; also a declined confirmation)
  2  bad arguments (kept in step with Click's own usage errors)
  3  no credential, or one the relay refused
  4  the thing named does not exist
  5  the relay could not be reached
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx


class HleError(Exception):
    """A failure the CLI reports and exits on.

    ``message`` is what went wrong; ``hint`` is what to do about it, printed
    on its own line. Markup in either is fine — the renderer strips it when
    colour is off.
    """

    exit_code: int = 1

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        hint: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if exit_code is not None:
            self.exit_code = exit_code
        self.hint = hint
        # The HTTP status this came from, when it came from the relay.
        self.status = status

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        """The shape ``-o json`` emits on stderr."""
        payload: dict[str, Any] = {"error": self.message, "code": self.exit_code}
        if self.hint:
            payload["hint"] = self.hint
        if self.status is not None:
            payload["status"] = self.status
        return payload


class UsageError(HleError):
    """The arguments do not make sense together."""

    exit_code = 2


class AuthError(HleError):
    """No credential, or one the relay would not accept."""

    exit_code = 3


class NotFoundError(HleError):
    """The tunnel, rule or link named does not exist."""

    exit_code = 4


class UnreachableError(HleError):
    """The relay did not answer at all."""

    exit_code = 5


class ConflictError(HleError):
    """It already exists, or the state does not allow the change."""

    exit_code = 1


class AbortedError(HleError):
    """The user declined a confirmation, or ``--no-input`` forbade one."""

    exit_code = 1

    def __init__(self, hint: str | None = None) -> None:
        super().__init__("Aborted.", hint=hint)


NO_API_KEY = "No API key found. Run 'hle auth login', set HLE_API_KEY, or pass --api-key."
NO_AGENT_TOKEN = "No agent token. Run 'hle auth login --agent-token' first."


def server_detail(exc: Any) -> str | None:
    """The relay's own explanation for a failed request, if it gave one.

    Preferring it to a message composed here keeps the reasoning on the side
    that can be updated: the relay knows which credential arrived and why it
    was refused, and a client installed months ago gets the better wording the
    moment the relay is deployed.
    """
    try:
        detail = exc.response.json().get("detail")
    except Exception:  # noqa: BLE001 — an unparseable body is just "no detail"
        return None
    return str(detail) if detail else None


class ApiError(HleError):
    """A response the relay sent that this client has no better name for."""

    @classmethod
    def from_http(cls, exc: httpx.HTTPError, subdomain: str | None = None) -> HleError:
        """Turn an httpx failure into the typed error it means.

        The one place a status code becomes words, replacing the three
        mappings the commands used to carry each. ``subdomain`` names the
        tunnel in 403/404 messages when the caller has one.
        """
        import httpx

        if not isinstance(exc, httpx.HTTPStatusError):
            # ConnectError, timeouts, TLS trouble: the relay never answered.
            return UnreachableError(
                f"Could not reach the relay server ({exc.__class__.__name__}: {exc})."
            )

        code = exc.response.status_code
        detail = server_detail(exc)
        if code == 401:
            # What the relay said, rather than a guess made here. It knows
            # which credential arrived and why it was refused — an agent
            # token is a valid credential of the wrong kind, and "run hle
            # auth login" is advice that cannot help there.
            return AuthError(detail or "Invalid or missing API key.", status=code)
        if code == 403:
            message = f"You do not own {subdomain!r}." if subdomain else "Forbidden."
            return AuthError(detail or message, status=code)
        if code == 404:
            message = f"Tunnel {subdomain!r} not found." if subdomain else "Resource not found."
            return NotFoundError(message, status=code)
        if code == 409:
            return ConflictError(detail or "Email already in access list.", status=code)
        if code == 429:
            return HleError("Rate limited — try again shortly.", status=code)
        text = detail or exc.response.text[:200]
        return cls(f"Server returned {code}: {text}", status=code)

    @classmethod
    def from_exception(cls, exc: Exception, subdomain: str | None = None) -> HleError:
        """``from_http`` for anything, so a bare ``except Exception`` can use it."""
        import httpx

        if isinstance(exc, HleError):
            return exc
        if isinstance(exc, httpx.HTTPError):
            return cls.from_http(exc, subdomain)
        return HleError(str(exc) or exc.__class__.__name__)
