"""Platform probes that change how a command is allowed to run.

The interesting case is iOS (a-Shell, iSH): it has no fork/exec, so a child
process is emulated by running the command inside the same app process. That is
fine for most tools, but starting a second Python interpreter on top of the
running one aborts the runtime. See ``hle_client.fp_cmd._run_command``.
"""

from __future__ import annotations

import os
import platform
import re
import sys

# Every iOS app bundle lives under this root; the binary and the environment it
# runs in both sit below it (``.../Application/<uuid>/<App>.app/...``).
_IOS_APP_ROOT = "/private/var/containers/Bundle/Application/"

# ``python``, ``python3`` and a versioned ``python3.13`` / ``python3.13.1``.
_PYTHON = re.compile(r"python(?:3(?:\.\d+)*)?")


def _is_ios_app_path(path: str) -> bool:
    return bool(path) and ".app" in path and _IOS_APP_ROOT in path


def is_ios() -> bool:
    """Whether this is an iOS shell rather than a desktop OS.

    a-Shell reports ``sys.platform == "ios"``; on other builds the machine name
    (``iPad...`` / ``iPhone...``) or a bundle path for the interpreter is the
    signal. Any one is enough.
    """
    if sys.platform == "ios":
        return True
    if platform.machine().startswith(("iPad", "iPhone")):
        return True
    return any(_is_ios_app_path(path) for path in (sys.executable, sys.prefix))


def is_python_command(executable: str) -> bool:
    """Whether *executable* names a Python interpreter.

    The basename is what matters — ``/usr/bin/python3`` and a bare ``python3``
    are the same interpreter — plus the running interpreter however it is
    spelled.
    """
    if not executable:
        return False
    if executable == sys.executable:
        return True
    return _PYTHON.fullmatch(os.path.basename(executable)) is not None
