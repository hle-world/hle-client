"""Platform probes: iOS detection and Python-interpreter recognition."""

from __future__ import annotations

import platform
import sys

import pytest

from hle_client import platform_info


class TestIsIos:
    @pytest.fixture(autouse=True)
    def _desktop_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
        monkeypatch.setattr(sys, "prefix", "/usr/local")

    def test_sys_platform_ios_is_the_first_signal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "ios")
        assert platform_info.is_ios()

    @pytest.mark.parametrize("machine", ["iPad13,1", "iPhone14,2"])
    def test_apple_machine_names_are_a_signal(self, machine, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(platform, "machine", lambda: machine)
        assert platform_info.is_ios()

    def test_executable_inside_an_app_bundle_is_a_signal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            sys,
            "executable",
            "/private/var/containers/Bundle/Application/AB12/a-Shell.app/a-shell",
        )
        assert platform_info.is_ios()

    def test_prefix_inside_an_app_bundle_is_a_signal(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            sys,
            "prefix",
            "/private/var/containers/Bundle/Application/AB12/Python.app/3.13",
        )
        assert platform_info.is_ios()

    def test_an_app_path_outside_the_ios_container_is_not_ios(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "executable", "/Applications/Python.app/python3")
        assert not platform_info.is_ios()

    def test_a_desktop_is_not_ios(self):
        assert not platform_info.is_ios()


class TestIsPythonCommand:
    @pytest.mark.parametrize(
        "executable",
        [
            "python",
            "python3",
            "python3.13",
            "python3.13.1",
            "/usr/bin/python3",
            "/usr/local/bin/python3.12",
        ],
    )
    def test_python_names_match_by_basename(self, executable: str):
        assert platform_info.is_python_command(executable)

    def test_the_running_interpreter_counts_however_it_is_spelled(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "executable", "/opt/weird/thing")
        assert platform_info.is_python_command("/opt/weird/thing")

    @pytest.mark.parametrize(
        "executable",
        ["", "ssh", "pythonw", "python2", "my-python3", "/usr/bin/pypy3"],
    )
    def test_other_programs_are_not_python(self, executable: str):
        assert not platform_info.is_python_command(executable)
