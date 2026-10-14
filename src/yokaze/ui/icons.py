"""Register bundled app icons with the GTK icon theme."""

from __future__ import annotations

from gi.repository import Gdk, Gtk

from yokaze.lib import paths as app_paths

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


def header_brand() -> Gtk.Widget:
    """App icon + title for HeaderBar start (identity-aware)."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.set_valign(Gtk.Align.CENTER)
    icon = Gtk.Image.new_from_icon_name(app_paths.xdg_name())
    icon.set_pixel_size(28)
    icon.set_valign(Gtk.Align.CENTER)
    title = "Yokaze (Dev)" if app_paths.identity() == app_paths.IDENTITY_DEV else "Yokaze"
    label = Gtk.Label(label=title)
    label.add_css_class("heading")
    label.set_valign(Gtk.Align.CENTER)
    box.append(icon)
    box.append(label)
    return box
