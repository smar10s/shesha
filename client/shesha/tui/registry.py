"""Canonical app registry — the ordered set of apps the shell exposes.

Membership and order live here, once.  Each app owns its own ``name`` and
``description``; consumers read those attributes.  Add, remove, or reorder
an app here and the shell, the status overview, and the headless snapshot
all follow.
"""
from .status import StatusApp
from .viz import VizApp
from .wifi import WifiApp
from .fm import FmApp

APP_CLASSES = (StatusApp, VizApp, WifiApp, FmApp)
