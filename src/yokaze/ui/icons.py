"""Register bundled app icons with the GTK icon theme."""

from __future__ import annotations

from gi.repository import Gdk, Gtk

from . import ASSETS

_ICON_ROOT = ASSETS / "icons"


def register_bundled_icons(display: Gdk.Display | None = None) -> None:
    """Add ``assets/icons`` so names like ``paperless-ngx`` resolve."""
    disp = display or Gdk.Display.get_default()
    if disp is None:
        return
    theme = Gtk.IconTheme.get_for_display(disp)
    path = str(_ICON_ROOT)
    if path not in theme.get_search_path():
        theme.add_search_path(path)
