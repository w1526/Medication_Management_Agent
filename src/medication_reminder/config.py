"""Small process-local configuration helpers.

The MVP intentionally does not depend on a settings framework.  This module
keeps the existing ``.env`` convenience in one place so the HTTP device
adapter and the optional Harness adapter read the same environment.
"""

import os
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_local_env(path=None):
    """Load simple ``KEY=VALUE`` entries without overriding process env.

    The file is only read into the current process.  It is never written and
    existing environment variables always have precedence.
    """

    env_path = Path(path or PROJECT_ROOT / ".env")
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("export "):
            item = item[7:].lstrip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)

