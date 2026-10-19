"""Where this machine keeps its credentials, and how to read and write them.

Two files, one place that knows about both:

* ``~/.config/hle/config.toml`` holds the API key (``hle_`` + 32 hex).
* ``~/.config/hle/agent.toml`` holds the agent credential (``hle_...``, or a legacy ``hlea_...``),
  at a path ``HLE_AGENT_CONFIG`` can override so a service unit can point at
  the file it was enrolled with rather than whatever HOME it was started under.

This used to live in ``tunnel.py`` (behind underscored names that four other
modules imported anyway) and ``agent.py``. Neither module needs the file I/O
to do its job; the CLI does. ``tunnel._load_api_key`` stays as an alias
because hle-operator and hle-tui import it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from hle_common import credentials

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "hle"
CONFIG_FILE = CONFIG_DIR / "config.toml"

# Separate from the API-key file so the two cannot clash.
AGENT_CONFIG_PATH = CONFIG_DIR / "agent.toml"
# Set by `hle daemon install agent` to the file the token was actually found
# in, so the service reads the same file the enrolling user wrote.
AGENT_CONFIG_ENV = "HLE_AGENT_CONFIG"

# The credential shapes are defined once, in hle_common, shared with the relay.
# An API key is "hle_" plus 32 hex characters. Re-exported here so existing
# `config.API_KEY_PATTERN` users keep working.
API_KEY_PATTERN = credentials.API_KEY_PATTERN

# Legacy agent enrollment tokens. Agents created since server v2609.2 get an
# ordinary API_KEY_PATTERN key instead; these keep working. They share the
# "hle" stem, which is precisely why they get put in variables named for API keys.
AGENT_TOKEN_PREFIX = credentials.LEGACY_AGENT_TOKEN_PREFIX

API_KEY_ENV = "HLE_API_KEY"
AGENT_TOKEN_ENV = "HLE_AGENT_TOKEN"

# Agent-side self-update policy. Read from the environment first, then from
# agent.toml next to the token, so an operator can refuse remote updates on the
# host itself rather than trusting the relay to.
REMOTE_UPDATE_ENV = "HLE_REMOTE_UPDATE"
ALLOW_DOWNGRADE_ENV = "HLE_ALLOW_DOWNGRADE"
UPDATE_MANIFEST_ENV = "HLE_UPDATE_MANIFEST"

_FALSEY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Sources, as reported to people and to `-o json`.
SOURCE_API_KEY_ENV = "HLE_API_KEY"
SOURCE_API_KEY_FILE = "~/.config/hle/config.toml"
SOURCE_AGENT_TOKEN_ENV = "HLE_AGENT_TOKEN"
SOURCE_AGENT_TOKEN_FILE = "~/.config/hle/agent.toml"


# ---------------------------------------------------------------------------
# Private file writes
# ---------------------------------------------------------------------------

# Windows has no O_NOFOLLOW; there the flag is simply absent.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` 0700, tightening it if it was looser.

    Only HLE's own credential directory is chmodded. An ``HLE_AGENT_CONFIG``
    override can name a shared directory (``/tmp``, someone's home), and
    chmodding that would break every other user of it, so there we only warn.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = stat.S_IMODE(directory.stat().st_mode)
    except OSError:
        return
    if not mode & 0o077:
        return
    if directory != CONFIG_DIR:
        logger.warning("Credential directory %s has mode %o, not 0700", directory, mode)
        return
    logger.warning("Credential directory %s had mode %o; tightening to 0700", directory, mode)
    try:
        directory.chmod(0o700)
    except OSError:
        logger.warning("Could not tighten %s to 0700", directory, exc_info=True)


def _write_private_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` at 0600 without ever exposing it wider.

    A sibling temporary file, opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` and forced
    to 0600 before anything is written, is fsynced and atomically renamed over
    the target. The target is therefore either the old file or a complete 0600
    file, never a world-readable work-in-progress.
    """
    _ensure_private_dir(path.parent)

    try:
        loose = bool(stat.S_IMODE(path.stat().st_mode) & 0o077)
    except OSError:
        loose = False
    if loose:
        logger.warning("Credential file %s had loose permissions; rewriting 0600", path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
    for _ in range(100):
        tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"could not create a temporary file next to {path}")

    closed = False
    try:
        os.fchmod(fd, 0o600)
        data = content.encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        if not closed:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------


def load_api_key() -> str | None:
    """The API key saved in the config file, or None."""
    if not CONFIG_FILE.exists():
        return None
    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        value = data.get("api_key")
        return str(value) if value else None
    except Exception:
        # nosemgrep: python-logger-credential-disclosure
        logger.debug("Failed to load API key from %s", CONFIG_FILE)
        return None


def save_api_key(api_key: str) -> None:
    """Persist the API key to the config file with restrictive permissions.

    Rewrites only the ``api_key`` line, so anything else someone put in the
    file survives.
    """
    try:
        existing_lines: list[str] = []
        found = False
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                for line in f:
                    if line.startswith("api_key ") or line.startswith("api_key="):
                        existing_lines.append(f'api_key = "{api_key}"\n')
                        found = True
                    else:
                        existing_lines.append(line)

        if not found:
            existing_lines.append(f'api_key = "{api_key}"\n')

        # 0600 (owner-only read/write) to protect the API key; the temp file is
        # created at that mode and renamed into place, so an existing 0644 file
        # cannot leak the key.
        _write_private_file(CONFIG_FILE, "".join(existing_lines))

        # nosemgrep: python-logger-credential-disclosure
        logger.info("API key saved to %s", CONFIG_FILE)
    except Exception:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning("Failed to save API key to %s", CONFIG_FILE, exc_info=True)


def remove_api_key() -> bool:
    """Remove the API key from the config file. True if one was removed."""
    if not CONFIG_FILE.exists():
        return False
    try:
        with open(CONFIG_FILE) as f:
            lines = f.readlines()

        new_lines = [
            line
            for line in lines
            if not (line.startswith("api_key ") or line.startswith("api_key="))
        ]
        if len(new_lines) == len(lines):
            return False

        fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(new_lines)

        # Both of these log the config path, never the key itself. The rule
        # fires on the words "API key" appearing in the message.
        logger.info("API key removed from %s", CONFIG_FILE)  # nosemgrep
        return True
    except Exception:
        logger.warning(  # nosemgrep
            "Failed to remove API key from %s", CONFIG_FILE, exc_info=True
        )
        return False


# ---------------------------------------------------------------------------
# Agent token
# ---------------------------------------------------------------------------


def agent_config_path() -> Path:
    """The token file to read or write, honouring an explicit override."""
    override = os.environ.get(AGENT_CONFIG_ENV)
    return Path(override).expanduser() if override else AGENT_CONFIG_PATH


def save_agent_token(token: str) -> None:
    """Persist the agent enrollment token (0600)."""
    path = agent_config_path()
    _write_private_file(path, f'token = "{token}"\n')


def load_agent_token() -> str | None:
    """The saved agent token, or None."""
    path = agent_config_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            value = tomllib.load(f).get("token")
        return str(value) if value else None
    except (OSError, ValueError):
        # Logs the path, never the token. The rule fires on the word "token"
        # appearing in the message.
        logger.debug("Failed to read agent token from %s", path)  # nosemgrep
        return None


def remove_agent_token() -> bool:
    """Delete the saved agent token. True if there was one."""
    path = agent_config_path()
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Agent self-update policy
# ---------------------------------------------------------------------------


def load_agent_settings() -> dict[str, object]:
    """The agent.toml document, or {} when it is missing or unreadable.

    The same file that holds the token; a malformed file degrades to defaults
    rather than stopping the agent, exactly as the token reader does.
    """
    path = agent_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        logger.debug("Failed to read agent config from %s", path)
        return {}
    return {str(k): v for k, v in data.items()}


def _env_flag(value: str | None) -> bool | None:
    """True/False for a recognised flag spelling, None when unset or unknown."""
    if value is None:
        return None
    token = value.strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSEY:
        return False
    return None


def load_remote_update() -> bool:
    """Whether this agent accepts remote self-update requests. Default on.

    ``HLE_REMOTE_UPDATE=off`` (or ``remote_update = false`` in agent.toml)
    refuses every update request, whatever the relay sends.
    """
    env = _env_flag(os.environ.get(REMOTE_UPDATE_ENV))
    if env is not None:
        return env
    value = load_agent_settings().get("remote_update")
    if isinstance(value, bool):
        return value
    return True


def load_allow_downgrade() -> bool:
    """Whether a remote update may move back to an older release. Default off."""
    env = _env_flag(os.environ.get(ALLOW_DOWNGRADE_ENV))
    if env is not None:
        return env
    value = load_agent_settings().get("allow_downgrade")
    if isinstance(value, bool):
        return value
    return False


def load_update_manifest() -> dict[str, str] | None:
    """Pinned wheel digests, version -> sha256, or None when not configured.

    A configured manifest that cannot be read is an error, not a silent
    fall-back to unpinned installs: the operator asked for verification.
    """
    raw = os.environ.get(UPDATE_MANIFEST_ENV, "").strip()
    if not raw:
        value = load_agent_settings().get("update_manifest")
        raw = str(value).strip() if value else ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read update manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"update manifest {path} is not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# Both at once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    """What this machine holds, and where each piece came from.

    Both are reported, not just the first one found. A host can hold an API
    key and an agent token at once, and knowing only about one of them is how
    "this machine plainly is set up" turns into "no API key".
    """

    api_key: str | None
    api_key_source: str | None
    agent_token: str | None
    agent_token_source: str | None


def load_credentials() -> Credentials:
    """Resolve both credentials: environment first, then the saved file."""
    env_key = os.environ.get(API_KEY_ENV) or None
    env_token = os.environ.get(AGENT_TOKEN_ENV) or None

    api_key, key_source = env_key, SOURCE_API_KEY_ENV if env_key else None
    if api_key is None:
        api_key = load_api_key()
        key_source = SOURCE_API_KEY_FILE if api_key else None

    token, token_source = env_token, SOURCE_AGENT_TOKEN_ENV if env_token else None
    if token is None:
        token = load_agent_token()
        token_source = SOURCE_AGENT_TOKEN_FILE if token else None

    return Credentials(
        api_key=api_key,
        api_key_source=key_source,
        agent_token=token,
        agent_token_source=token_source,
    )
