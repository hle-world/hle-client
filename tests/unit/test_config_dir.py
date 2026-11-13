"""Config directory resolution and failing loudly when it cannot be written."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hle_client import config
from hle_client.cli import main
from hle_client.errors import HleError

_KEY = "hle_" + "c" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HLE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


class TestResolveConfigDir:
    def test_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert config.resolve_config_dir() == tmp_path / ".config" / "hle"

    def test_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config.resolve_config_dir() == tmp_path / "xdg" / "hle"

    def test_xdg_set_only_legacy_dir_exists_keeps_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".config" / "hle").mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config.resolve_config_dir() == tmp_path / ".config" / "hle"

    def test_xdg_set_and_xdg_dir_exists_uses_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".config" / "hle").mkdir(parents=True)
        (tmp_path / "xdg" / "hle").mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config.resolve_config_dir() == tmp_path / "xdg" / "hle"

    def test_xdg_set_and_neither_exists_uses_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config.resolve_config_dir() == tmp_path / "xdg" / "hle"

    def test_relative_xdg_is_ignored(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
        assert config.resolve_config_dir() == tmp_path / ".config" / "hle"

    def test_hle_config_dir_beats_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("HLE_CONFIG_DIR", str(tmp_path / "explicit"))
        assert config.resolve_config_dir() == tmp_path / "explicit"

    def test_agent_config_override_keeps_precedence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HLE_CONFIG_DIR", str(tmp_path / "explicit"))
        monkeypatch.setenv("HLE_AGENT_CONFIG", str(tmp_path / "tok.toml"))
        assert config.agent_config_path() == tmp_path / "tok.toml"


class TestUnwritableDir:
    def test_save_api_key_raises_naming_path_and_hint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory")
        target = blocker / "hle"
        monkeypatch.setattr(config, "CONFIG_DIR", target)
        monkeypatch.setattr(config, "CONFIG_FILE", target / "config.toml")

        with pytest.raises(HleError) as info:
            config.save_api_key(_KEY)

        assert str(target / "config.toml") in info.value.message
        assert "HLE_CONFIG_DIR" in (info.value.hint or "")

    def test_dropped_write_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "config.toml"
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(config, "CONFIG_FILE", cfg)

        def drop(src: Path, dst: Path) -> None:
            Path(src).unlink()  # accepted, then discarded

        monkeypatch.setattr(config.os, "replace", drop)
        with pytest.raises(HleError):
            config.save_api_key(_KEY)

    def test_save_agent_token_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setenv("HLE_AGENT_CONFIG", str(blocker / "agent.toml"))
        with pytest.raises(HleError):
            config.save_agent_token(_KEY)

    def test_login_fails_instead_of_reporting_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        target = blocker / "hle"
        monkeypatch.setattr(config, "CONFIG_DIR", target)
        monkeypatch.setattr(config, "CONFIG_FILE", target / "config.toml")

        from unittest.mock import AsyncMock

        client = AsyncMock()
        client.get_credential = AsyncMock(return_value={"kind": "api_key"})
        with patch("hle_client.api.ApiClient", return_value=client):
            result = CliRunner().invoke(main, ["auth", "login", "--api-key", _KEY])

        assert result.exit_code != 0
        assert "Saved" not in result.output
        assert "HLE_CONFIG_DIR" in result.output + (result.stderr_bytes or b"").decode()
