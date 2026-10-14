"""Shared Annotate/Preview/History paned chrome: sidebar + toolbar + content."""

from __future__ import annotations

from collections.abc import Iterable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def scrolled_content(scroll: Gtk.ScrolledWindow) -> Gtk.Widget | None:
    """The widget the scrollbar actually moves.

    ScrolledWindow.get_child() is the viewport. Its child is the box we
    packed. Adjustment values are in that box's coordinates.
    """
    viewport = scroll.get_child()
    if viewport is None:
        return None
    return viewport.get_child()


def extent_in_content(
    row: Gtk.Widget, content: Gtk.Widget
) -> tuple[float, float] | None:
    """Y and height of row in the scrolled child's coordinates.

    compute_bounds() to the viewport includes the scroll transform, so a row
    already on screen reports a y that shrinks as the list moves. Walking
    only as far as the scrolled child stays in adjustment coordinates.
    """
    y = 0.0
    height = 0.0
    current: Gtk.Widget | None = row
    measuring_height = True
    while current is not None and current is not content:
        parent = current.get_parent()
        if parent is None:
            return None
        ok, bounds = current.compute_bounds(parent)
        if not ok or bounds.get_height() <= 1:
            return None
        if measuring_height:
            height = bounds.get_height()
            measuring_height = False
        y += bounds.get_y()
        current = parent
    if current is not content or height <= 1:
        return None
    return y, height


def scroll_widget_into_view(scroll: Gtk.ScrolledWindow, row: Gtk.Widget) -> bool:
    """Scroll ``row`` into ``scroll`` once it is laid out. False when it is not measurable yet."""
    if row.get_root() is None:
        return False
    content = scrolled_content(scroll)
    extent = extent_in_content(row, content) if content is not None else None
    if extent is None:
        return False
    top, height = extent
    adj = scroll.get_vadjustment()
    page = adj.get_page_size()
    if page <= 1:
        return False
    bottom = top + height
    view_top = adj.get_value()
    view_bottom = view_top + page
    if top < view_top - 1:
        target = max(adj.get_lower(), top)
    elif bottom > view_bottom + 1:
        target = min(max(adj.get_lower(), adj.get_upper() - page), bottom - page)
    else:
        target = view_top
    adj.set_value(target)
    return True


def place_in_toolbar(
    *,
    toolbar_start: Gtk.Box,
    left_widgets: Iterable[Gtk.Widget],
) -> None:
    """Selectors and captions on the left of a canvas tab toolbar."""
    for widget in left_widgets:
        toolbar_start.append(widget)


def make_switch_row(
    label: str,
    switch: Gtk.Switch,
    *,
    expand_label: bool = True,
    tip: str | None = None,
) -> Gtk.Box:
    """Label + switch. expand_label=True for sidebar rows; False keeps them glued in toolbars."""
    switch.set_valign(Gtk.Align.CENTER)
    switch.set_can_focus(False)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_margin_top(4)
    row.set_margin_bottom(4)
    text = Gtk.Label(label=label, xalign=0, hexpand=expand_label)
    if tip:
        text.set_tooltip_text(tip)
    row.append(text)
    row.append(switch)
    return row


class LabShell(Gtk.Paned):
    """List-on-the-side + viewer-in-the-middle chrome shared by lab tabs."""

    def __init__(self, *, empty_text: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_shrink_start_child(False)
        self.set_resize_start_child(False)

        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.side.add_css_class("sidebar")
        self.side.add_css_class("lab-sidebar")
        self.side.set_size_request(240, -1)

        self.header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.header.set_margin_start(12)
        self.header.set_margin_end(12)
        self.header.set_margin_top(12)
        self.header.set_margin_bottom(4)
        self.side.append(self.header)

        self.header_separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.header_separator.set_margin_start(12)
        self.header_separator.set_margin_end(12)
        self.header_separator.set_margin_top(12)
        self.header_separator.set_margin_bottom(12)
        self.side.append(self.header_separator)

        self.cases_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.cases_box.add_css_class("annotate-cases")

        self.empty_label = Gtk.Label(
            label=empty_text,
            justify=Gtk.Justification.CENTER,
            wrap=True,
        )
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_margin_top(24)
        self.empty_label.set_visible(False)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        list_box.set_margin_start(10)
        list_box.set_margin_end(10)
        list_box.append(self.cases_box)
        list_box.append(self.empty_label)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_child(list_box)
        self.scroll.set_vexpand(True)
        self.side.append(self.scroll)
        self.set_start_child(self.side)

        self.right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.toolbar_bar = Gtk.CenterBox()
        self.toolbar_bar.set_margin_start(8)
        self.toolbar_bar.set_margin_end(8)
        self.toolbar_bar.set_margin_top(8)
        self.toolbar_bar.set_margin_bottom(8)

        # Intra-group spacing is Box.spacing; inter-slot margins keep a gap when
        # CenterBox packs start against center (or center against end) on a
        # narrow window — Stage Split vs rotate is the usual collision.
        _SLOT_GAP = 8
        self.toolbar_start = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toolbar_start.set_margin_end(_SLOT_GAP)
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toolbar.set_margin_end(_SLOT_GAP)
        self.toolbar_end = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toolbar_bar.set_start_widget(self.toolbar_start)
        self.toolbar_bar.set_center_widget(self.toolbar)
        self.toolbar_bar.set_end_widget(self.toolbar_end)
        self.right.append(self.toolbar_bar)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.add_css_class("lab-content")
        self.content.set_hexpand(True)
        self.content.set_vexpand(True)
        self.right.append(self.content)
        self.set_end_child(self.right)
