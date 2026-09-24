"""ops.access — the allow-list, and the reconcile that used to live in a leaf."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from hle_client.errors import ConflictError, UnreachableError
from hle_client.ops import access
from hle_client.ops.models import AccessRule
from tests.unit.test_ops_tunnels import FakeApi, _http_error

EXISTING = [
    {"id": 1, "allowed_email": "alice@example.com", "provider": "google"},
    {"id": 2, "allowed_email": "bob@example.com", "provider": "github"},
]


def api(**overrides: Any) -> FakeApi:
    fixtures = {"get_me": {"user_code": "x7k"}, "list_access_rules": EXISTING}
    fixtures.update(overrides)
    return FakeApi(**fixtures)


class TestParseSpec:
    def test_provider_prefix(self):
        assert access.parse_spec("google:a@x.com") == AccessRule(email="a@x.com", provider="google")

    def test_no_prefix_is_any(self):
        assert access.parse_spec("a@x.com") == AccessRule(email="a@x.com", provider="any")

    def test_unknown_prefix_is_part_of_the_address(self):
        assert access.parse_spec("mailto:a@x.com").email == "mailto:a@x.com"


class TestGetAddRemove:
    async def test_get_maps_rules(self):
        rules = await access.get_access(api(), "ha")
        assert [(r.id, r.email, r.provider) for r in rules] == [
            (1, "alice@example.com", "google"),
            (2, "bob@example.com", "github"),
        ]

    async def test_add_returns_what_the_relay_made(self):
        a = api(add_access_rule={"id": 9, "allowed_email": "c@x.com", "provider": "hle"})
        rule = await access.add_rule(a, "ha", "c@x.com", "hle")
        assert (rule.id, rule.email, rule.provider) == (9, "c@x.com", "hle")
        assert ("add_access_rule", ("ha-x7k", "c@x.com", "hle")) in a.calls

    async def test_add_on_an_old_relay_reports_what_was_sent(self):
        rule = await access.add_rule(api(add_access_rule={"message": "ok"}), "ha", "c@x.com")
        assert (rule.email, rule.provider) == ("c@x.com", "any")

    async def test_a_duplicate_is_a_conflict(self):
        a = api()
        a.errors["add_access_rule"] = _http_error(409)
        with pytest.raises(ConflictError):
            await access.add_rule(a, "ha", "alice@example.com")

    async def test_remove(self):
        a = api()
        await access.remove_rule(a, "ha", 2)
        assert ("delete_access_rule", ("ha-x7k", 2)) in a.calls


class TestSetAccess:
    async def test_adds_missing_and_removes_extra(self):
        a = api()
        diff = await access.set_access(
            a,
            "ha",
            [
                access.parse_spec("google:alice@example.com"),
                access.parse_spec("github:carol@x.com"),
            ],
        )
        assert [r.email for r in diff.added] == ["carol@x.com"]
        assert [r.email for r in diff.removed] == ["bob@example.com"]
        assert [r.email for r in diff.kept] == ["alice@example.com"]
        assert diff.failed == ()
        assert not diff.in_sync
        assert ("add_access_rule", ("ha-x7k", "carol@x.com", "github")) in a.calls
        assert ("delete_access_rule", ("ha-x7k", 2)) in a.calls

    async def test_already_in_sync_touches_nothing(self):
        a = api()
        diff = await access.set_access(
            a,
            "ha",
            [
                access.parse_spec("google:alice@example.com"),
                access.parse_spec("github:bob@example.com"),
            ],
        )
        assert diff.in_sync
        assert len(diff.kept) == 2
        assert not [c for c, _ in a.calls if c in ("add_access_rule", "delete_access_rule")]

    async def test_matching_is_case_insensitive_on_the_address(self):
        diff = await access.set_access(api(), "ha", [access.parse_spec("google:ALICE@example.com")])
        assert [r.email for r in diff.kept] == ["alice@example.com"]
        assert [r.email for r in diff.removed] == ["bob@example.com"]

    async def test_provider_is_part_of_the_identity(self):
        diff = await access.set_access(api(), "ha", [access.parse_spec("hle:alice@example.com")])
        assert [(r.email, r.provider) for r in diff.added] == [("alice@example.com", "hle")]
        assert [r.email for r in diff.removed] == ["alice@example.com", "bob@example.com"]

    async def test_clear_removes_everything(self):
        diff = await access.set_access(api(), "ha", [])
        assert [r.id for r in diff.removed] == [1, 2]

    async def test_one_refusal_does_not_stop_the_rest(self):
        a = api()
        a.errors["add_access_rule"] = _http_error(422, json={"detail": "bad address"})
        diff = await access.set_access(a, "ha", [access.parse_spec("nope")])
        assert [(r.email, why, code) for r, why, code in diff.failed] == [
            ("nope", "bad address", 422)
        ]
        # The removals still happened.
        assert [r.id for r in diff.removed] == [1, 2]
        assert not diff.in_sync

    async def test_a_rule_without_an_id_cannot_be_removed(self):
        a = api(list_access_rules=[{"allowed_email": "x@y.z", "provider": "any"}])
        diff = await access.set_access(a, "ha", [])
        assert diff.removed == ()
        assert diff.failed[0][2] is None

    async def test_an_unreachable_relay_is_fatal(self):
        a = api()
        a.errors["list_access_rules"] = httpx.ConnectError("down")
        with pytest.raises(UnreachableError):
            await access.set_access(a, "ha", [])
