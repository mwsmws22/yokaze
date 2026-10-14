"""Paperless tab — browse deliveries, Restage into Stage."""

from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from .archive_thumbs import THUMB_MAX_EDGE
from .confirm import confirm_action
from .deliveries import Delivery, load_deliveries
from .lab_shell import LabShell, scroll_widget_into_view
from .staging_tray import StagingTray, restage_delivery

_THUMB_MAX_EDGE = THUMB_MAX_EDGE
# Up to four columns; cell box fits the visible canvas (width and height).
_THUMB_COLUMNS = 4
_THUMB_GAP = 8
_THUMB_MIN_EDGE = 64
_THUMB_LABEL_H = 22


def paperless_cell_box(
    avail_width: int,
    avail_height: int,
    page_count: int,
    *,
    columns: int = _THUMB_COLUMNS,
    gap: int = _THUMB_GAP,
    label_h: int = _THUMB_LABEL_H,
) -> tuple[int, int]:
    """CSS-px (cell_w, cell_h) so up to a 4×4 grid fits in the canvas."""
    show = min(max(1, page_count), columns * columns)
    cols = max(1, min(columns, show))
    rows = max(1, (show + cols - 1) // cols)
    usable_w = max(0, int(avail_width) - gap * (cols - 1) - 24)
    usable_h = max(0, int(avail_height) - gap * (rows - 1) - label_h * rows - 8)
    cell_w = max(_THUMB_MIN_EDGE, usable_w // cols)
    cell_h = max(_THUMB_MIN_EDGE, usable_h // rows)
    return cell_w, cell_h


def thumb_fit_box(
    pix_w: int, pix_h: int, *, box_w: int, box_h: int
) -> tuple[int, int]:
    """Scale a thumb to fit inside ``box_w``×``box_h`` (aspect preserved)."""
    if pix_w <= 0 or pix_h <= 0:
        return max(1, box_w), max(1, box_h)
    scale = min(box_w / float(pix_w), box_h / float(pix_h), 1.0)
    return max(1, int(round(pix_w * scale))), max(1, int(round(pix_h * scale)))


def pixbuf_for_thumb_display(
    pix: GdkPixbuf.Pixbuf, *, box_w: int, box_h: int
) -> GdkPixbuf.Pixbuf:
    """Shrink a dense archive thumb to the cell box so FlowBox can pack columns.

    ``Gtk.Picture`` reports preferred size from the pixbuf. ``set_size_request`` only
    raises the minimum, so an unscaled 1200px thumb still forces overflow/clipping.
    """
    tw, th = thumb_fit_box(
        pix.get_width(), pix.get_height(), box_w=box_w, box_h=box_h
    )
    if pix.get_width() == tw and pix.get_height() == th:
        return pix
    scaled = pix.scale_simple(tw, th, GdkPixbuf.InterpType.BILINEAR)
    return scaled if scaled is not None else pix


class PaperlessView(LabShell):
    def __init__(
        self,
        toast_overlay: Adw.ToastOverlay,
        tray: StagingTray,
        *,
        on_restaged: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(empty_text="No Paperless deliveries yet.\nSend a scan to Paperless.")
        self._toast = toast_overlay
        self._tray = tray
        self._on_restaged = on_restaged
        self._deliveries: list[Delivery] = []
        self._selected: Delivery | None = None
        self._row_buttons: dict[str, Gtk.Button] = {}
        self._thumb_gen = 0
        self._thumb_src: list[GdkPixbuf.Pixbuf] = []
        self._thumb_box: tuple[int, int] = (0, 0)
        self._status = Gtk.Label(label="", xalign=0)
        self._status.set_selectable(True)
        self._status.add_css_class("dim-label")

        self._count_label = Gtk.Label(label="0 deliveries", xalign=0)
        self._count_label.add_css_class("heading")
        self.header.append(self._count_label)

        self._restage_btn = Gtk.Button(label="Restage")
        self._restage_btn.add_css_class("suggested-action")
        self._restage_btn.set_sensitive(False)
        self._restage_btn.connect("clicked", self._on_restage)
        self._restage_btn.set_can_focus(False)
        self.toolbar_start.append(self._restage_btn)
        self.toolbar_start.append(self._status)

        self._thumbs = Gtk.FlowBox()
        self._thumbs.set_valign(Gtk.Align.START)
        self._thumbs.set_halign(Gtk.Align.FILL)
        self._thumbs.set_hexpand(True)
        self._thumbs.set_min_children_per_line(1)
        self._thumbs.set_max_children_per_line(_THUMB_COLUMNS)
        self._thumbs.set_column_spacing(_THUMB_GAP)
        self._thumbs.set_row_spacing(_THUMB_GAP)
        self._thumbs.set_selection_mode(Gtk.SelectionMode.NONE)
        self._thumbs.set_homogeneous(True)
        self._thumb_scroll = Gtk.ScrolledWindow()
        self._thumb_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._thumb_scroll.set_vexpand(True)
        self._thumb_scroll.set_hexpand(True)
        self._thumb_scroll.set_child(self._thumbs)
        self._thumb_scroll.connect("notify::width", self._on_thumb_area_size)
        self._thumb_scroll.connect("notify::height", self._on_thumb_area_size)
        self.content.append(self._thumb_scroll)
        self._stop_sidebar_focus_scroll()

        self.refresh()

    def _stop_sidebar_focus_scroll(self) -> None:
        """The delivery list scrolls itself. A focused row must not scroll it again."""
        content = self.scroll.get_child()
        viewport = content.get_parent() if content is not None else None
        if isinstance(viewport, Gtk.Viewport):
            viewport.set_scroll_to_focus(False)

    def on_show(self) -> None:
        self.refresh(
            select=self._selected.task_uuid if self._selected else None
        )

    def refresh(self, *, select: str | None = None) -> None:
        keep = select
        while True:
            child = self.cases_box.get_first_child()
            if child is None:
                break
            self.cases_box.remove(child)
        self._row_buttons.clear()

        self._deliveries = load_deliveries()
        n = len(self._deliveries)
        self._count_label.set_text(
            f"{n} delivery" if n == 1 else f"{n} deliveries"
        )
        self.empty_label.set_visible(n == 0)

        for delivery in self._deliveries:
            btn = Gtk.Button(label=delivery.label)
            btn.set_halign(Gtk.Align.FILL)
            btn.add_css_class("flat")
            btn.set_can_focus(False)
            btn.connect(
                "clicked",
                lambda _b, d=delivery: self._select(d),
            )
            self.cases_box.append(btn)
            self._row_buttons[delivery.task_uuid] = btn

        chosen = None
        if keep:
            chosen = next(
                (d for d in self._deliveries if d.task_uuid == keep), None
            )
        if chosen is None and self._deliveries:
            chosen = self._deliveries[0]
        if chosen is not None:
            self._select(chosen)
        else:
            self._selected = None
            self._clear_thumbs()
            self._restage_btn.set_sensitive(False)
            self._status.set_text("")

    def _select(self, delivery: Delivery) -> None:
        self._selected = delivery
        chosen = None
        for uuid, btn in self._row_buttons.items():
            if uuid == delivery.task_uuid:
                btn.add_css_class("lab-case-selected")
                chosen = btn
            else:
                btn.remove_css_class("lab-case-selected")
        if chosen is not None:
            self._reveal_row(chosen)
        self._restage_btn.set_sensitive(True)
        self._fill_thumbs(delivery)

    def _reveal_row(self, row: Gtk.Widget) -> None:
        """Keep the highlighted delivery on screen, including after a wrap."""
        if self._scroll_row_into_view(row):
            return
        GLib.idle_add(self._reveal_row_later, row)

    def _reveal_row_later(self, row: Gtk.Widget) -> bool:
        self._scroll_row_into_view(row)
        return False

    def _scroll_row_into_view(self, row: Gtk.Widget) -> bool:
        return scroll_widget_into_view(self.scroll, row)

    def _clear_thumbs(self) -> None:
        self._thumb_src = []
        self._thumb_box = (0, 0)
        while True:
            child = self._thumbs.get_first_child()
            if child is None:
                break
            self._thumbs.remove(child)

    def _thumb_canvas_size(self) -> tuple[int, int]:
        """CSS pixels of the visible thumbnail canvas (scrolled viewport)."""
        width = self._thumb_scroll.get_width()
        height = self._thumb_scroll.get_height()
        if width < _THUMB_MIN_EDGE * 2:
            width = self.content.get_width() or 800
        if height < _THUMB_MIN_EDGE * 2:
            height = self.content.get_height() or 600
        return width, height

    def _on_thumb_area_size(self, *_a) -> None:
        if self._thumb_src:
            self._apply_thumb_pixbufs()

    def _fill_thumbs(self, delivery: Delivery) -> None:
        self._clear_thumbs()
        self._thumb_gen += 1
        gen = self._thumb_gen
        canvas_w, canvas_h = self._thumb_canvas_size()
        cell_w, cell_h = paperless_cell_box(
            canvas_w, canvas_h, len(delivery.pages)
        )
        for i, _page in enumerate(delivery.pages, start=1):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_halign(Gtk.Align.CENTER)
            box.set_hexpand(False)
            box.set_vexpand(False)
            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_halign(Gtk.Align.CENTER)
            pic.set_valign(Gtk.Align.CENTER)
            pic.set_hexpand(False)
            pic.set_vexpand(False)
            pic.set_size_request(cell_w, cell_h)
            label = Gtk.Label(label=str(i), xalign=0.5)
            label.add_css_class("dim-label")
            box.append(pic)
            box.append(label)
            self._thumbs.append(box)

        self._status.set_text("Building thumbnails…")

        def work() -> None:
            from yokaze.ui.archive_thumbs import bgr_to_rgb_bytes, thumb_bgr

            try:
                frames: list[tuple[bytes, int, int]] = []
                for entry in delivery.pages:
                    bgr = thumb_bgr(
                        entry.archive,
                        entry.page,
                        entry.crop,
                        entry.rotate,
                        max_edge=_THUMB_MAX_EDGE,
                    )
                    frames.append(bgr_to_rgb_bytes(bgr))
                GLib.idle_add(self._on_thumbs_ready, gen, frames, "")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_thumbs_ready, gen, [], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_thumbs_ready(
        self,
        gen: int,
        frames: list[tuple[bytes, int, int]],
        error: str,
    ) -> bool:
        if gen != self._thumb_gen:
            return False
        if error:
            self._status.set_text(error)
            from .error_ui import error_toast

            self._toast.add_toast(error_toast(error, timeout=6))
            return False
        self._status.set_text("")
        src: list[GdkPixbuf.Pixbuf] = []
        for rgb, width, height in frames:
            src.append(
                GdkPixbuf.Pixbuf.new_from_bytes(
                    GLib.Bytes.new(rgb),
                    GdkPixbuf.Colorspace.RGB,
                    False,
                    8,
                    width,
                    height,
                    width * 3,
                )
            )
        self._thumb_src = src
        self._thumb_box = (0, 0)
        self._apply_thumb_pixbufs()
        return False

    def _apply_thumb_pixbufs(self) -> None:
        if not self._thumb_src:
            return
        canvas_w, canvas_h = self._thumb_canvas_size()
        cell_w, cell_h = paperless_cell_box(
            canvas_w, canvas_h, len(self._thumb_src)
        )
        if (cell_w, cell_h) == self._thumb_box:
            return
        self._thumb_box = (cell_w, cell_h)
        child = self._thumbs.get_first_child()
        for pix in self._thumb_src:
            if child is None:
                break
            inner = child.get_child()
            pic = inner.get_first_child() if inner is not None else None
            if isinstance(pic, Gtk.Picture):
                shown = pixbuf_for_thumb_display(
                    pix, box_w=cell_w, box_h=cell_h
                )
                pic.set_size_request(cell_w, cell_h)
                pic.set_pixbuf(shown)
            child = child.get_next_sibling()

    def _on_restage(self, *_a) -> None:
        if self._selected is None:
            return
        delivery = self._selected
        if self._tray.pages:
            self._confirm(
                "Stage already has pages",
                "Append this delivery’s pages to the current Stage tray?",
                on_yes=lambda: self._confirm_pendings_then_restage(delivery),
            )
            return
        self._confirm_pendings_then_restage(delivery)

    def _confirm_pendings_then_restage(self, delivery: Delivery) -> None:
        if self._tray.replace_pending:
            listing = "\n".join(f"• {u}" for u in self._tray.replace_pending)
            self._confirm(
                "Stage already has replace targets",
                "Send will also delete these Paperless docs:\n"
                f"{listing}\n\nAdd another replace target from this delivery?",
                on_yes=lambda: self._run_restage(delivery),
            )
            return
        self._run_restage(delivery)

    def _run_restage(self, delivery: Delivery) -> None:
        self._restage_btn.set_sensitive(False)

        def work() -> None:
            try:
                added = restage_delivery(delivery, self._tray)
                GLib.idle_add(self._on_restage_done, True, f"Restaged {added} page(s)")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_restage_done, False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_restage_done(self, ok: bool, message: str) -> None:
        self._restage_btn.set_sensitive(self._selected is not None)
        if ok:
            self._toast.add_toast(Adw.Toast(title=message, timeout=3))
            if self._on_restaged:
                self._on_restaged()
        else:
            from .error_ui import present_error

            root = self.get_root()
            parent = root if isinstance(root, Gtk.Window) else None
            present_error(parent, "Restage failed", message)

    def _confirm(self, heading: str, body: str, *, on_yes: Callable[[], None]) -> None:
        confirm_action(self, heading, body, on_yes=on_yes)

    def handle_nav_key(self, keyval: int) -> bool:
        if not self._deliveries:
            return False
        uuids = [d.task_uuid for d in self._deliveries]
        cur = self._selected.task_uuid if self._selected else uuids[0]
        if cur not in uuids:
            self._select(self._deliveries[0])
            return True
        idx = uuids.index(cur)
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Right):
            self._select(self._deliveries[(idx + 1) % len(self._deliveries)])
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Left):
            self._select(self._deliveries[(idx - 1) % len(self._deliveries)])
            return True
        return False
