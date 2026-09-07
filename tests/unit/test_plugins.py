"""Plugins extend the CLI without being in it.

The core has to stay small enough for a pfSense or OpenWrt box, and a terminal
dashboard is exactly what such a box will never run. So the extension point is
a packaging boundary: install `hle-tui` and `hle tui` appears, don't and
nothing about the core changes.

The rules that matter are the failure ones. A third-party plugin must not be
able to stop `hle expose` from running, and must not be able to redefine what
a built-in command does.
"""

from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

from hle_client import plugins


@click.command("demo")
def demo_command() -> None:
    """A plugin command."""


def _ep(name, loader):
    return SimpleNamespace(name=name, load=loader)


@pytest.fixture
def group():
    @click.group()
    def g() -> None:
        pass

    @g.command("expose")
    def _expose() -> None:
        pass

    return g


class TestDiscovery:
    def test_a_command_is_found(self, monkeypatch):
        monkeypatch.setattr(
            plugins, "entry_points", lambda **_: [_ep("demo", lambda: demo_command)]
        )
        assert plugins.discover() == [demo_command]

    def test_a_list_of_commands_is_found(self, monkeypatch):
        monkeypatch.setattr(
            plugins, "entry_points", lambda **_: [_ep("demo", lambda: [demo_command])]
        )
        assert plugins.discover() == [demo_command]

    def test_a_factory_is_called(self, monkeypatch):
        monkeypatch.setattr(
            plugins, "entry_points", lambda **_: [_ep("demo", lambda: lambda: demo_command)]
        )
        assert plugins.discover() == [demo_command]

    def test_a_plugin_that_raises_on_import_is_skipped(self, monkeypatch):
        def _boom():
            raise ImportError("no textual installed")

        monkeypatch.setattr(plugins, "entry_points", lambda **_: [_ep("broken", _boom)])
        # The tunnel is the product; a broken plugin cannot take the CLI down.
        assert plugins.discover() == []

    def test_one_broken_plugin_does_not_hide_a_working_one(self, monkeypatch):
        def _boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(
            plugins,
            "entry_points",
            lambda **_: [_ep("broken", _boom), _ep("demo", lambda: demo_command)],
        )
        assert plugins.discover() == [demo_command]

    def test_discovery_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("HLE_NO_PLUGINS", "1")
        monkeypatch.setattr(
            plugins, "entry_points", lambda **_: [_ep("demo", lambda: demo_command)]
        )
        assert plugins.discover() == []


class TestRegistration:
    def test_a_plugin_command_is_added(self, monkeypatch, group):
        monkeypatch.setattr(plugins, "discover", lambda: [demo_command])
        plugins.register(group)
        assert "demo" in group.commands

    def test_a_plugin_cannot_replace_a_built_in(self, monkeypatch, group):
        """Installing a package must not be a way to redefine `hle expose`."""

        @click.command("expose")
        def hijack() -> None:
            pass

        original = group.commands["expose"]
        monkeypatch.setattr(plugins, "discover", lambda: [hijack])
        plugins.register(group)
        assert group.commands["expose"] is original
