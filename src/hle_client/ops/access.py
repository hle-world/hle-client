"""The SSO allow-list: read it, add to it, remove from it, or reconcile it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from hle_client.errors import server_detail
from hle_client.ops.models import AccessDiff, AccessRule
from hle_client.ops.tunnels import fail, resolve_subdomain

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hle_client.api import ApiClient

VALID_PROVIDERS = frozenset({"any", "google", "github", "hle"})


def parse_spec(spec: str) -> AccessRule:
    """``[provider:]email`` → rule. An unknown prefix is part of the address."""
    if ":" in spec:
        prefix, _, rest = spec.partition(":")
        if prefix in VALID_PROVIDERS:
            return AccessRule(email=rest, provider=prefix)
    return AccessRule(email=spec, provider="any")


async def get_access(api: ApiClient, name: str) -> list[AccessRule]:
    subdomain = await resolve_subdomain(api, name)
    try:
        rows = await api.list_access_rules(subdomain)
    except Exception as exc:
        fail(exc, subdomain)
    return [AccessRule.from_api(r) for r in rows]


async def add_rule(api: ApiClient, name: str, email: str, provider: str = "any") -> AccessRule:
    subdomain = await resolve_subdomain(api, name)
    try:
        row = await api.add_access_rule(subdomain, email, provider)
    except Exception as exc:
        fail(exc, subdomain)
    created = AccessRule.from_api(row)
    if not created.email:
        # An older relay answering ``{"message": "ok"}``: report what was sent.
        created = AccessRule(email=email, provider=provider, raw=row)
    return created


async def remove_rule(api: ApiClient, name: str, rule_id: int) -> None:
    subdomain = await resolve_subdomain(api, name)
    try:
        await api.delete_access_rule(subdomain, rule_id)
    except Exception as exc:
        fail(exc, subdomain)


async def set_access(api: ApiClient, name: str, desired: Iterable[AccessRule]) -> AccessDiff:
    """Make the allow-list exactly ``desired``: add what is missing, remove the rest.

    Rules are matched on case-folded address plus provider. One rule the relay
    refuses does not stop the others; each refusal is reported in ``failed``
    with the relay's status, and the caller decides whether that is fatal.
    """
    subdomain = await resolve_subdomain(api, name)
    wanted = {rule.key: rule for rule in desired}
    try:
        existing = [AccessRule.from_api(r) for r in await api.list_access_rules(subdomain)]
    except Exception as exc:
        fail(exc, subdomain)
    present = {rule.key: rule for rule in existing}

    added: list[AccessRule] = []
    removed: list[AccessRule] = []
    failed: list[tuple[AccessRule, str, int | None]] = []

    for key in sorted(wanted.keys() - present.keys()):
        rule = wanted[key]
        try:
            await api.add_access_rule(subdomain, rule.email.lower(), rule.provider)
            added.append(AccessRule(email=rule.email.lower(), provider=rule.provider))
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            failed.append((rule, server_detail(exc) or str(code), code))
        except Exception as exc:
            fail(exc, subdomain)

    for key in sorted(present.keys() - wanted.keys()):
        rule = present[key]
        if rule.id is None:
            failed.append((rule, "the relay returned no id for this rule", None))
            continue
        try:
            await api.delete_access_rule(subdomain, rule.id)
            removed.append(rule)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            failed.append((rule, server_detail(exc) or str(code), code))
        except Exception as exc:
            fail(exc, subdomain)

    kept = tuple(present[key] for key in sorted(wanted.keys() & present.keys()))
    return AccessDiff(added=tuple(added), removed=tuple(removed), kept=kept, failed=tuple(failed))
