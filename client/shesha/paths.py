"""Filesystem locations for user state."""
from __future__ import annotations

import os


def config_dir() -> str:
    """Directory holding user preferences.

    ``SHESHA_CONFIG_DIR`` wins outright, for tests and unusual setups.
    Otherwise follow XDG: an absolute ``XDG_CONFIG_HOME`` or ``~/.config``.
    """
    override = os.environ.get("SHESHA_CONFIG_DIR")
    if override:
        return override
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "shesha")


def default_config_path() -> str:
    return os.path.join(config_dir(), "config.json")
