"""Keyboard shortcuts shared by the Annotate and Stage crop canvases."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402


PAGE_CANVAS_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Space", "Fit the page; solo: flip white/black"),
    ("Ctrl+Z", "Undo last crop edit"),
    ("A", "Deskew on / off"),
    ("Ctrl+scroll", "Skew by 0.05°"),
    ("Tab", "Top-left corner"),
    ("Q", "Top-right corner"),
    ("W", "Bottom-right corner"),
    ("E", "Bottom-left corner"),
    ("S", "Stage: split / add guide; deskew: tip ↔ live"),
    ("Enter", "Stage: confirm split (when guides are shown)"),
    ("D", "Solo crop on/off (white); deskew: grid"),
    ("C", "Grid color: red, blue, green (deskew)"),
    ("Drag", "Move the grid (deskew)"),
    ("Ctrl+click", "Lock the nearest line (deskew)"),
    ("Ctrl+drag", "Change density against the lock (deskew)"),
)


def shortcut_legend() -> Gtk.Widget:
    grid = Gtk.Grid(column_spacing=16, row_spacing=6)
    grid.set_margin_top(10)
    grid.set_margin_bottom(10)
    grid.set_margin_start(12)
    grid.set_margin_end(12)
    for row, (key, desc) in enumerate(PAGE_CANVAS_SHORTCUTS):
        key_label = Gtk.Label(label=key, xalign=0)
        key_label.add_css_class("heading")
        desc_label = Gtk.Label(label=desc, xalign=0)
        grid.attach(key_label, 0, row, 1, 1)
        grid.attach(desc_label, 1, row, 1, 1)
    return grid


def crop_corner_for_key(keyval: int) -> int | None:
    """Tab top-left, Q top-right, W bottom-right, E bottom-left."""
    return {
        Gdk.KEY_Tab: 1,
        Gdk.KEY_q: 2,
        Gdk.KEY_Q: 2,
        Gdk.KEY_w: 3,
        Gdk.KEY_W: 3,
        Gdk.KEY_e: 4,
        Gdk.KEY_E: 4,
    }.get(keyval)
