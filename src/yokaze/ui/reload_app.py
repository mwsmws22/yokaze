"""Restart the running app to pick up code and data changes."""

from __future__ import annotations

import os
import sys

from . import ROOT
from .scan_view import STATE_IDLE


def restart_app(
    *,
    tab: str | None,
    scan_state: str,
    width: int = 0,
    height: int = 0,
    stage_select: str | None = None,
) -> str | None:
    """Replace this process with a fresh app instance. Returns an error message."""
    if scan_state != STATE_IDLE:
        return "Finish or cancel the current scan before reloading"

    argv = [sys.executable, "-m", "yokaze.ui"]
    if tab:
        argv.extend(["--tab", tab])

    env = os.environ.copy()
    root = str(ROOT)
    src = str(ROOT / "src")
    env["YOKAZE_REPO_DIR"] = root
    # Keep workbench vs release identity across F5.
    env.setdefault("YOKAZE_IDENTITY", os.environ.get("YOKAZE_IDENTITY", "user"))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not existing else f"{src}:{existing}"
    if width > 0 and height > 0:
        env["YOKAZE_WINDOW_WIDTH"] = str(width)
        env["YOKAZE_WINDOW_HEIGHT"] = str(height)
    env.pop("YOKAZE_STAGE_TRAY", None)
    if stage_select:
        env["YOKAZE_STAGE_SELECT"] = stage_select
    else:
        env.pop("YOKAZE_STAGE_SELECT", None)

    os.execvpe(sys.executable, argv, env)
    return None
