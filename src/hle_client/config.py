"""Where this machine keeps its credentials, and how to read and write them.

Two files, one place that knows about both:

* ``~/.config/hle/config.toml`` holds the API key (``hle_`` + 32 hex).
* ``~/.config/hle/agent.toml`` holds an agent enrollment token (``hlea_...``),
  at a path ``HLE_AGENT_CONFIG`` can override so a service unit can point at
  the file it was enrolled with rather than whatever HOME it was started under.

This used to live in ``tunnel.py`` (behind underscored names that four other
modules imported anyway) and ``agent.py``. Neither module needs the file I/O
to do its job; the CLI does. ``tunnel._load_api_key`` stays as an alias
because hle-operator and hle-tui import it.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "hle"
CONFIG_FILE = CONFIG_DIR / "config.toml"

# Separate from the API-key file so the two cannot clash.
AGENT_CONFIG_PATH = CONFIG_DIR / "agent.toml"
# Set by `hle daemon install agent` to the file the token was actually found
# in, so the service reads the same file the enrolling user wrote.
AGENT_CONFIG_ENV = "HLE_AGENT_CONFIG"

# An API key is "hle_" plus 32 hex characters. The relay checks this exact
# length before it will even hash a key, so a value with a stray quote or a
# trailing newline is refused there while still working elsewhere.
API_KEY_PATTERN = re.compile(r"^hle_[0-9a-f]{32}$")

# Agent enrollment tokens share the "hle" stem, which is precisely why they get
# put in variables named for API keys.
AGENT_TOKEN_PREFIX = "hlea_"

API_KEY_ENV = "HLE_API_KEY"
AGENT_TOKEN_ENV = "HLE_AGENT_TOKEN"

# Sources, as reported to people and to `-o json`.
SOURCE_API_KEY_ENV = "HLE_API_KEY"
SOURCE_API_KEY_FILE = "~/.config/hle/config.toml"
SOURCE_AGENT_TOKEN_ENV = "HLE_AGENT_TOKEN"
SOURCE_AGENT_TOKEN_FILE = "~/.config/hle/agent.toml"


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
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

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

        # 0o600 (owner-only read/write) to protect the API key.
        fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(existing_lines)

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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(f'token = "{token}"\n')
    path.chmod(0o600)


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
