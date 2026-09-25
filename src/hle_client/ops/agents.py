"""Agents: the account's, and the one this machine may be enrolled as."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hle_client import config
from hle_client.errors import ApiError, HleError
from hle_client.ops.models import Agent

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
    """Save an enrollment token. Returns the path it was written to."""
    if not token.startswith(config.AGENT_TOKEN_PREFIX):
        raise HleError(
            f"Invalid agent token. Expected one starting with '{config.AGENT_TOKEN_PREFIX}'."
        )
    config.save_agent_token(token)
    return str(config.agent_config_path())
