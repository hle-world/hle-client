"""Credential files must never be group- or world-readable, even briefly."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hle_client import config

_KEY = "hle_" + "a" * 32


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestSaveApiKeyPermissions:
    def test_tightens_a_preexisting_0644_file(self, tmp_path: Path, monkeypatch) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('api_key = "hle_' + "b" * 32 + '"\nother = "keep"\n')
        config_file.chmod(0o644)
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(config, "CONFIG_FILE", config_file)

        config.save_api_key(_KEY)

        assert _mode(config_file) == 0o600
        content = config_file.read_text()
        assert _KEY in content
        assert 'other = "keep"' in content

    def test_tightens_a_loose_config_dir(self, tmp_path: Path, monkeypatch) -> None:
        creds = tmp_path / "hle"
        creds.mkdir()
        creds.chmod(0o755)
        monkeypatch.setattr(config, "CONFIG_DIR", creds)
        monkeypatch.setattr(config, "CONFIG_FILE", creds / "config.toml")

        config.save_api_key(_KEY)

        assert _mode(creds) == 0o700


class TestSaveAgentTokenPermissions:
    def test_creates_the_file_0600_under_a_loose_umask(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HLE_AGENT_CONFIG", str(tmp_path / "agent.toml"))

        seen: list[int] = []
        real_replace = os.replace

        def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            seen.append(_mode(Path(src)))
            real_replace(src, dst)

        monkeypatch.setattr(config.os, "replace", spy_replace)
        old_umask = os.umask(0o022)
        try:
            config.save_agent_token(_KEY)
        finally:
            os.umask(old_umask)

        path = tmp_path / "agent.toml"
        assert _mode(path) == 0o600
        # The temporary file is 0600 before it becomes visible at the real path.
        assert seen == [0o600]

    def test_override_directory_is_not_chmodded(self, tmp_path: Path, monkeypatch) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        monkeypatch.setenv("HLE_AGENT_CONFIG", str(shared / "agent.toml"))

        config.save_agent_token(_KEY)

        assert _mode(shared / "agent.toml") == 0o600
        assert _mode(shared) == 0o755
