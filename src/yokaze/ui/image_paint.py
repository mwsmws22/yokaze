"""Ways to paint a page into a canvas: a file, a file built on demand, pixels.

Shared by every lab tab so image loading behaves identically everywhere —
including the background rebuild and the "is this still the page the user is
looking at?" guard — and so ``ViewIntent`` is translated in one place.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from .canvas import FitImageCanvas
from .compare_stage import ViewIntent


def paint_file(
    pane: FitImageCanvas, path: Path | str | None, intent: ViewIntent
) -> None:
    """Draw a page that already exists on disk; blank the pane if it doesn't."""
    if path and Path(path).is_file():
        pane.set_image_file(str(path), keep_view=intent.keep_view)
    else:
        pane.clear_image()


def paint_pixels(
    pane: FitImageCanvas, pixbuf: GdkPixbuf.Pixbuf, intent: ViewIntent
) -> None:
    """Draw pixels held in memory (live tuning) with no disk round-trip."""
    pane.set_image_pixbuf(pixbuf, keep_view=intent.keep_view)


def paint_file_async(
    pane: FitImageCanvas,
    *,
    cached: Path | None,
    build: Callable[[], Path | None],
    intent: ViewIntent,
    still_current: Callable[[], bool] | None = None,
) -> None:
    """Draw ``cached`` when present, otherwise build it off the GTK thread.

    ``still_current`` keeps a slow build from painting over the canvas after
    the user has already moved to another page.
    """
    if cached and Path(cached).is_file():
        paint_file(pane, cached, intent)
        return

    def run() -> None:
        try:
            built = build()
        except Exception:  # noqa: BLE001 — background thread; never raise into GTK
            built = None

        def apply() -> bool:
            if still_current is None or still_current():
                paint_file(pane, built, intent)
            return False

        GLib.idle_add(apply)

    threading.Thread(target=run, daemon=True).start()
