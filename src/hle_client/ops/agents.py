"""Agents: the account's, and the one this machine may be enrolled as."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from hle_client import config
from hle_client.errors import ApiError, AuthError, HleError, server_detail
from hle_client.ops.models import Agent
from hle_common.credentials import is_agent_credential

if TYPE_CHECKING:
    from hle_client.api import ApiClient


async def list_agents(api: ApiClient) -> list[Agent]:
    """Every agent on the account, and whether each is connected."""
    try:
        rows = await api.list_agents()
    except Exception as exc:
        err = ApiError.from_exception(exc)
        if err.status == 404:
            # The endpoint 404s (rather than 403s) when the feature flag is off.
            raise HleError("Agents are not enabled on this server.") from None
        raise err from None
    return [Agent.from_api(row) for row in rows]


@dataclass(frozen=True, slots=True)
class Credential:
    """What the relay says a credential is: an account key, or an agent's.

    ``name`` and ``active`` describe an agent credential; an API key has
    neither and is always active.
    """

    kind: str  # "api_key" | "agent"
    name: str | None = None
    active: bool = True


async def classify_credential(api: ApiClient) -> Credential | None:
    """Ask the relay whether a credential is an account key or an agent token.

    Returns ``None`` when the relay cannot say — it is unreachable, or too old
    to know the endpoint — so the caller falls back to the credential's prefix.
    A credential the relay refuses (401) raises instead: nothing was saved, and
    the string the person pasted was not recognised.
    """
    try:
        data = await api.get_credential()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (404, 405) or code >= 500:
            return None
        if code == 401:
            raise AuthError(
                "The server does not recognise this credential. Check you copied all of it.",
                status=code,
            ) from None
        raise ApiError.from_exception(exc) from None
    except httpx.HTTPError:
        return None
    except ValueError:  # a body that is not JSON: not the endpoint we asked for
        return None

    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("api_key", "agent"):
        return None
    nested: dict[str, Any] = {}
    if isinstance(data.get("agent"), dict):
        nested = data["agent"]
    name = data.get("name") or nested.get("name")
    active = data.get("active")
    if active is None:
        active = nested.get("active", True)
    return Credential(
        kind=kind,
        name=str(name) if name else None,
        active=bool(active),
    )


@dataclass(frozen=True, slots=True)
class LocalAgent:
    """Whether this machine holds an agent token, and where it came from.

    Local state only. Whether the agent is *running* is the service manager's
    business (:mod:`hle_client.ops.daemon`); whether it is *online* is the
    relay's (:func:`list_agents`).
    """

    enrolled: bool
    source: str | None
    token_prefix: str | None


def local_agent_status() -> LocalAgent:
    creds = config.load_credentials()
    token = creds.agent_token
    return LocalAgent(
        enrolled=token is not None,
        source=creds.agent_token_source,
        token_prefix=(token[:9] + "…") if token else None,
    )


def enroll(token: str) -> str:
    """Save an enrollment token. Returns the path it was written to.

    Agents created since server v2609.2 get an ordinary ``hle_`` key scoped
    to tunnels; older ones hold an ``hlea_`` token, which still works. Only
    the shape is checked here: whether it is an agent's is the relay's call.
    """
    token = token.strip()
    if not is_agent_credential(token):
        raise HleError(
            "Invalid agent token. Copy it from Connections → Agents → New in the "
            "dashboard; it starts with 'hle_'."
        )
    config.save_agent_token(token)
    return str(config.agent_config_path())


@dataclass(frozen=True, slots=True)
class EnrolledAgent:
    """The agent the relay just created for this machine."""

    agent_id: int
    name: str


async def enroll_named(api: ApiClient, name: str) -> EnrolledAgent:
    """Enroll this machine from the account key and save the token returned.

    Unlike :func:`enroll`, no dashboard trip is needed: the account's own API
    key authorises the relay to mint a tunnel-scoped token for a fresh agent.
    The token is written straight to the machine's agent credential and never
    returned to the caller, so it cannot be logged or echoed.
    """
    try:
        payload = await api.enroll_agent(name)
    except Exception as exc:
        raise _enroll_error(exc) from None
    config.save_agent_token(str(payload["token"]))
    return EnrolledAgent(agent_id=int(payload["agent_id"]), name=str(payload["name"]))


def _enroll_error(exc: Exception) -> HleError:
    """The typed error an enrollment failure means.

    404 is a server too old for the endpoint; 422 is FastAPI's validation list
    (blank, whitespace-only or over-long name); 429 carries how long to wait.
    Everything else keeps the relay's own ``detail`` through :class:`ApiError`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 404:
            return HleError(
                "This HLE server doesn't support --name yet — use a token from "
                "Connections → Agents → New",
                status=code,
            )
        if code == 422:
            return HleError(f"Invalid agent name: {_first_validation_message(exc)}", status=code)
        if code == 429:
            message = server_detail(exc) or "Too many agent enrollments — try again later"
            retry = exc.response.headers.get("Retry-After")
            if retry:
                message = f"{message} (retry in {retry}s)"
            return HleError(message, status=code)
    return ApiError.from_exception(exc)


def _first_validation_message(exc: httpx.HTTPStatusError) -> str:
    """FastAPI's first validation message, or a plain fallback."""
    try:
        detail = exc.response.json().get("detail")
    except Exception:  # noqa: BLE001 — an unparseable body is just "no detail"
        return "name is required"
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        message = detail[0].get("msg")
        if message:
            return str(message)
    return "name is required"
