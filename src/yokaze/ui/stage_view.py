"""Stage tab — ephemeral tray: thumbnails, crop/rotate overlays, Send to Paperless."""

from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GdkPixbuf, GLib, GObject, Gtk  # noqa: E402

from .canvas import CropCanvas
from .confirm import confirm_action
from .lab_shell import LabShell
from .models import CropRect
from .page_canvas_shortcuts import crop_corner_for_key, shortcut_legend
from .page_split import (
    SplitAxis,
    auto_split_axis,
    split_crop_along_guides,
)
from .rotate_geom import step_rotate
from .staging_tray import (
    ReplaceDocumentGone,
    StagingTray,
    block_insert_index,
    send_staging_tray,
)


def send_page_ids(page_ids: list[str], selected_ids: list[str]) -> list[str]:
    """Tray order of the pages Send should upload.

    Several selected pages, with at least one left unselected, go alone.
    One focused page, an empty selection, or every page selected sends the tray.
    """
    chosen = [page_id for page_id in page_ids if page_id in set(selected_ids)]
    if len(chosen) > 1 and len(chosen) < len(page_ids):
        return chosen
    return list(page_ids)


# Adaptive FlowBox: cell width drives column count as the pane is resized.
_STAGE_SIDE_WIDTH = 360
_STAGE_THUMB_CELL = 96


class StageView(LabShell):
    def __init__(
        self,
        toast_overlay: Adw.ToastOverlay,
        tray: StagingTray,
        *,
        on_tray_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(empty_text="Stage is empty.\nScan with Send to Stage.")
        self._toast = toast_overlay
        self._tray = tray
        self._on_tray_changed = on_tray_changed
        self._selected_id: str | None = None
        self._selected_ids: list[str] = []
        self._drag_ids: list[str] = []
        self._drag_active = False
        self._thumb_buttons: dict[str, Gtk.Widget] = {}
        self._syncing = False
        self._temp_deskew: dict[str, float] = {}
        self._deskew_modified_deg: float | None = None
        self._deskew_showing_tip: bool = False
        self._deskew_suppress_edit_track: bool = False
        self._crop_before_deskew: CropRect | None = None

        # Wider pane + adaptive FlowBox grid (Annotate keeps the vertical list).
        self.side.set_size_request(_STAGE_SIDE_WIDTH, -1)
        self._install_thumb_grid()

        self._count_label = Gtk.Label(label="0 pages", xalign=0)
        self._count_label.add_css_class("heading")
        self.header.append(self._count_label)

        self._send_btn = Gtk.Button(label="Send")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.set_tooltip_text(
            "Send the selected pages when several are selected and some are left out. "
            "Otherwise send every page."
        )
        self._send_btn.connect("clicked", self._on_send)
        self._clear_btn = Gtk.Button(label="Clear")
        self._clear_btn.add_css_class("destructive-action")
        self._clear_btn.connect("clicked", self._on_clear)
        self._delete_btn = Gtk.Button(label="Delete")
        self._delete_btn.connect("clicked", self._on_delete)
        self._up_btn = Gtk.Button(label="↑")
        self._up_btn.set_tooltip_text("Move selected pages up")
        self._up_btn.connect("clicked", lambda *_: self._move(-1))
        self._down_btn = Gtk.Button(label="↓")
        self._down_btn.set_tooltip_text("Move selected pages down")
        self._down_btn.connect("clicked", lambda *_: self._move(1))

        for w in (
            self._send_btn,
            self._clear_btn,
            self._delete_btn,
            self._up_btn,
            self._down_btn,
        ):
            w.set_can_focus(False)
            self.toolbar_start.append(w)

        split_menu = Gio.Menu()
        split_menu.append("Horizontal", "stage.split-horizontal")
        split_menu.append("Vertical", "stage.split-vertical")
        self._split_menu_btn = Gtk.MenuButton(label="Split", menu_model=split_menu)
        self._split_menu_btn.set_always_show_arrow(True)
        self._split_menu_btn.set_can_focus(False)
        self._split_confirm_btn = Gtk.Button(label="Split")
        self._split_confirm_btn.add_css_class("suggested-action")
        self._split_confirm_btn.connect("clicked", self._on_split_confirm)
        self._split_confirm_btn.set_can_focus(False)
        self._split_cancel_btn = Gtk.Button(label="Cancel")
        self._split_cancel_btn.connect("clicked", self._on_split_cancel)
        self._split_cancel_btn.set_can_focus(False)
        self.toolbar_start.append(self._split_menu_btn)
        self.toolbar_start.append(self._split_confirm_btn)
        self.toolbar_start.append(self._split_cancel_btn)
        self._split_confirm_btn.set_visible(False)
        self._split_cancel_btn.set_visible(False)

        actions = Gio.SimpleActionGroup()
        for name, axis in (
            ("split-horizontal", "horizontal"),
            ("split-vertical", "vertical"),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect(
                "activate",
                lambda _a, _p, ax=axis: self._enter_split(ax),  # type: ignore[misc]
            )
            actions.add_action(act)
        self.insert_action_group("stage", actions)

        full_btn = Gtk.Button(label="Full page")
        full_btn.connect("clicked", self._on_full_page)
        zoom_out = Gtk.Button(label="−")
        zoom_out.connect("clicked", lambda *_: self._canvas.zoom_out())
        zoom_in = Gtk.Button(label="+")
        zoom_in.connect("clicked", lambda *_: self._canvas.zoom_in())
        zoom_fit = Gtk.Button(label="Fit")
        zoom_fit.set_tooltip_text("Fit the page (Space)")
        zoom_fit.connect("clicked", lambda *_: self._canvas.zoom_fit())

        self._rot_ccw = Gtk.Button(label="↺")
        self._rot_ccw.connect("clicked", lambda *_: self._nudge_rotate(-90))
        self._rot_cw = Gtk.Button(label="↻")
        self._rot_cw.connect("clicked", lambda *_: self._nudge_rotate(90))
        self._rotate_label = Gtk.Label(label="0°")
        self._rotate_label.add_css_class("lab-muted")

        self._deskew_btn = Gtk.ToggleButton(label="Deskew")
        self._deskew_btn.set_tooltip_text(
            "Deskew (A): S detected↔live, D grid, C color; drag pans; "
            "Ctrl+hover/click lock line; Ctrl+drag other line = density "
            "(Ctrl+scroll skew in crop or deskew; not saved yet)"
        )
        self._deskew_btn.set_can_focus(False)
        self._deskew_btn.connect("toggled", self._on_deskew_toggled)
        self._deskew_label = Gtk.Label(label="")
        self._deskew_label.add_css_class("lab-muted")
        self._deskew_label.set_tooltip_text(
            "Current deskew: live adjustment, or the detected angle"
        )
        self._deskew_label.set_visible(False)

        for w in (
            self._rot_ccw,
            self._rot_cw,
            self._rotate_label,
            self._deskew_btn,
            self._deskew_label,
        ):
            if isinstance(w, Gtk.Button):
                w.set_can_focus(False)
            self.toolbar.append(w)
        for w in (full_btn, zoom_out, zoom_in, zoom_fit):
            w.set_can_focus(False)
            self.toolbar.append(w)

        legend_btn = Gtk.Button(label="?")
        legend_btn.add_css_class("flat")
        legend_btn.set_tooltip_text("Shortcuts")
        legend_btn.set_can_focus(False)
        legend = Gtk.Popover()
        legend.set_child(shortcut_legend())
        legend.set_parent(legend_btn)
        legend_btn.connect("clicked", lambda *_a: legend.popup())
        self.toolbar_end.append(legend_btn)

        self._canvas = CropCanvas()
        self._canvas._placeholder = "Select a staged page"
        self._canvas._deskew_changed = self._on_deskew_canvas_changed
        self.content.append(self._canvas)

        self.refresh()

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    def prepare_reload(self) -> None:
        """Flush canvas overlays and persist before F5 / quit."""
        self._flush_canvas_to_page()

    def on_show(self) -> None:
        self.refresh(select=self._selected_id)

    def refresh(self, *, select: str | None = None) -> None:
        current = select if select is not None else self._selected_id
        chosen = list(self._selected_ids)
        self._clear_thumb_rows()

        n = len(self._tray)
        self._count_label.set_text(f"{n} page{'s' if n != 1 else ''}")
        self.empty_label.set_visible(n == 0)
        self._send_btn.set_sensitive(n > 0)
        self._clear_btn.set_sensitive(n > 0)

        for index, page in enumerate(self._tray.pages):
            self._add_thumb(page, index)
        self._restore_selection(current, chosen)

        if self._on_tray_changed:
            self._on_tray_changed()

    def _install_thumb_grid(self) -> None:
        """Swap the vertical cases list for an adaptive FlowBox (Stage only)."""
        parent = self.cases_box.get_parent()
        if parent is None:
            return
        parent.remove(self.cases_box)
        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_homogeneous(True)
        grid.set_min_children_per_line(1)
        grid.set_max_children_per_line(12)
        grid.set_column_spacing(4)
        grid.set_row_spacing(4)
        grid.set_valign(Gtk.Align.START)
        grid.add_css_class("annotate-cases")
        grid.add_css_class("stage-thumb-grid")
        # Insert above the empty-state label.
        parent.insert_child_after(grid, None)
        self.cases_box = grid

    def _clear_thumb_rows(self) -> None:
        while True:
            child = self.cases_box.get_first_child()
            if child is None:
                break
            self.cases_box.remove(child)
        self._thumb_buttons.clear()

    def _add_thumb(self, page, index: int) -> None:
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        cell.add_css_class("stage-thumb")
        cell.set_size_request(_STAGE_THUMB_CELL, -1)
        cell.set_margin_top(2)
        cell.set_margin_bottom(2)
        cell.set_margin_start(2)
        cell.set_margin_end(2)
        cell.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        cell.set_tooltip_text("Drag to reorder. Ctrl-click to select more than one page.")
        cell.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"Page {index + 1}"],
        )
        thumb = Gtk.Picture()
        thumb.set_can_shrink(True)
        thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
        thumb.set_halign(Gtk.Align.CENTER)
        pix = _stage_thumb_pixbuf(page)
        if pix is not None:
            tw, th = self._thumb_display_size(pix.get_width(), pix.get_height())
            thumb.set_size_request(tw, th)
            thumb.set_pixbuf(pix)
        label = Gtk.Label(label=f"{index + 1}", xalign=0.5)
        label.add_css_class("dimmed")
        label.add_css_class("caption")
        cell.append(thumb)
        cell.append(label)
        self._bind_thumb_input(cell, page.id)
        self.cases_box.append(cell)
        self._thumb_buttons[page.id] = cell

    def _bind_thumb_input(self, row: Gtk.Widget, page_id: str) -> None:
        click = Gtk.GestureClick()
        click.set_button(1)
        click.set_exclusive(False)
        click.connect(
            "released",
            lambda gesture, _n, _x, _y, pid=page_id: self._on_thumb_released(
                gesture, pid
            ),
        )
        row.add_controller(click)

        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect(
            "prepare",
            lambda _source, _x, _y, pid=page_id: self._prepare_page_drag(pid),
        )
        source.connect("drag-begin", lambda *_a: self._on_drag_begin())
        source.connect("drag-end", lambda *_a: self._on_drag_end())
        source.connect("drag-cancel", lambda *_a: self._on_drag_cancel())
        row.add_controller(source)

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect(
            "motion",
            lambda _target, x, y, pid=page_id: self._on_thumb_drag_motion(pid, x, y),
        )
        drop.connect("leave", lambda *_a: self._clear_drop_indicator())
        drop.connect(
            "drop",
            lambda _target, _value, x, y, pid=page_id: self._on_thumb_drop(
                pid, x, y
            ),
        )
        row.add_controller(drop)

    def _restore_selection(self, current: str | None, chosen: list[str]) -> None:
        order = [page.id for page in self._tray.pages]
        alive = set(order)
        chosen_ids = set(chosen)
        self._selected_ids = [page_id for page_id in order if page_id in chosen_ids]
        if current in alive:
            if current not in self._selected_ids:
                self._selected_ids = [
                    page_id
                    for page_id in order
                    if page_id == current or page_id in set(self._selected_ids)
                ]
            self._show_page(current)
            return
        if order:
            self._select(order[0])
            return
        self._selected_ids = []
        self._selected_id = None
        if self._canvas.split_active:
            self._canvas.exit_split_mode()
        self._canvas.clear_image()
        self._set_page_actions(False)

    def _thumb_display_size(self, pix_w: int, pix_h: int) -> tuple[int, int]:
        """Fit thumb into a fixed cell; height follows image aspect ratio."""
        avail = _STAGE_THUMB_CELL - 8
        if pix_w <= 0 or pix_h <= 0:
            return avail, avail
        # Cap tall pages so the grid stays dense.
        th = max(1, int(round(avail * pix_h / pix_w)))
        th = min(th, int(avail * 1.35))
        tw = max(1, int(round(th * pix_w / pix_h))) if th else avail
        tw = min(tw, avail)
        return tw, th

    def _select(self, page_id: str) -> None:
        if self._tray.get(page_id) is None:
            return
        self._selected_ids = [page_id]
        self._show_page(page_id)

    def _show_page(self, page_id: str) -> None:
        page = self._tray.get(page_id)
        if page is None:
            return
        self._flush_canvas_to_page()
        if self._canvas.split_active:
            self._canvas.exit_split_mode()
        self._selected_id = page_id
        self._tray.selected_id = page_id
        if page_id not in self._selected_ids:
            self._selected_ids.append(page_id)
        if self._tray.durable:
            self._tray.persist()
        self._paint_selection()
        self._syncing = True
        try:
            self._canvas.set_image_file(str(page.path))
            self._canvas.set_crop(page.crop)
            self._canvas.set_view_rotate(page.rotate)
            self._rotate_label.set_text(f"{page.rotate}°")
        except GLib.Error:
            self._canvas.clear_image()
        finally:
            self._syncing = False
        self._set_page_actions(True)

    def _paint_selection(self) -> None:
        chosen = set(self._selected_ids)
        multi = len(chosen) > 1
        for page_id, row in self._thumb_buttons.items():
            if page_id in chosen:
                row.add_css_class("lab-case-selected")
            else:
                row.remove_css_class("lab-case-selected")
            if multi and page_id == self._selected_id:
                row.add_css_class("stage-page-current")
            else:
                row.remove_css_class("stage-page-current")

    def _on_thumb_released(self, gesture: Gtk.Gesture, page_id: str) -> None:
        if self._drag_active:
            return
        self._on_thumb_activate(page_id, ctrl=_event_has_control(gesture))

    def _on_thumb_activate(self, page_id: str, *, ctrl: bool) -> None:
        if self._tray.get(page_id) is None:
            return
        if not ctrl or self._canvas.split_active:
            self._select(page_id)
            return
        self._toggle_selected(page_id)

    def _toggle_selected(self, page_id: str) -> None:
        if page_id not in self._selected_ids:
            order = [page.id for page in self._tray.pages]
            chosen = set(self._selected_ids)
            chosen.add(page_id)
            self._selected_ids = [pid for pid in order if pid in chosen]
            self._show_page(page_id)
            return
        if len(self._selected_ids) == 1:
            return
        self._selected_ids = [pid for pid in self._selected_ids if pid != page_id]
        if self._selected_id != page_id:
            self._paint_selection()
            return
        nxt = self._fallback_selected(page_id)
        if nxt is not None:
            self._show_page(nxt)

    def _fallback_selected(self, removed: str) -> str | None:
        order = [page.id for page in self._tray.pages]
        chosen = set(self._selected_ids)
        if removed not in order:
            return self._selected_ids[0] if self._selected_ids else None
        start = order.index(removed)
        for page_id in order[start + 1 :] + list(reversed(order[:start])):
            if page_id in chosen:
                return page_id
        return None

    def _prepare_page_drag(self, page_id: str):
        if self._canvas.split_active or self._tray.get(page_id) is None:
            return None
        if page_id not in self._selected_ids:
            self._select(page_id)
        self._drag_ids = [
            page.id for page in self._tray.pages if page.id in set(self._selected_ids)
        ]
        return Gdk.ContentProvider.new_for_value(
            GObject.Value(GObject.TYPE_STRING, page_id)
        )

    def _on_drag_begin(self) -> None:
        self._drag_active = True

    def _on_drag_end(self) -> None:
        self._clear_drop_indicator()
        GLib.idle_add(self._clear_drag_active)

    def _on_drag_cancel(self) -> bool:
        self._on_drag_end()
        return False

    def _clear_drag_active(self) -> bool:
        self._drag_active = False
        self._drag_ids = []
        return False

    def _drop_after_cell(
        self, page_id: str, x: float, y: float
    ) -> bool:
        """True when the pointer is past the cell midpoint in reading order."""
        row = self._thumb_buttons.get(page_id)
        if row is None:
            return False
        w = float(row.get_width() or 0)
        h = float(row.get_height() or 0)
        if w <= 0 or h <= 0:
            return False
        # Prefer horizontal half (LTR); fall back to vertical for tall cells.
        return (x / w) + (y / h) >= 1.0

    def _on_thumb_drag_motion(
        self, page_id: str, x: float, y: float
    ) -> Gdk.DragAction:
        self._show_drop_indicator(
            page_id, after=self._drop_after_cell(page_id, x, y)
        )
        return Gdk.DragAction.MOVE

    def _on_thumb_drop(self, page_id: str, x: float, y: float) -> bool:
        return self._accept_thumb_drop(
            page_id, after=self._drop_after_cell(page_id, x, y)
        )

    def _accept_thumb_drop(self, target_id: str, *, after: bool) -> bool:
        if not self._drag_ids:
            return False
        order = [page.id for page in self._tray.pages]
        index = block_insert_index(
            order,
            self._drag_ids,
            target_id,
            after=after,
        )
        self._flush_canvas_to_page()
        self._clear_drop_indicator()
        self._tray.place_block(list(self._drag_ids), index)
        if [page.id for page in self._tray.pages] != order:
            keep = self._selected_id
            if self._drag_active:
                GLib.idle_add(self._refresh_after_drag, keep)
            else:
                self.refresh(select=keep)
        return True

    def _refresh_after_drag(self, keep: str | None) -> bool:
        self.refresh(select=keep)
        return False

    def _show_drop_indicator(self, page_id: str, *, after: bool) -> None:
        self._clear_drop_indicator()
        row = self._thumb_buttons.get(page_id)
        if row is None:
            return
        row.add_css_class("stage-drop-after" if after else "stage-drop-before")

    def _clear_drop_indicator(self) -> None:
        for row in self._thumb_buttons.values():
            row.remove_css_class("stage-drop-before")
            row.remove_css_class("stage-drop-after")

    def _set_page_actions(self, enabled: bool) -> None:
        splitting = self._canvas.split_active
        self._deskew_btn.set_sensitive(enabled and not splitting)
        self._delete_btn.set_sensitive(enabled and not splitting)
        self._up_btn.set_sensitive(enabled and not splitting)
        self._down_btn.set_sensitive(enabled and not splitting)
        self._rot_ccw.set_sensitive(enabled and not splitting)
        self._rot_cw.set_sensitive(enabled and not splitting)
        self._split_menu_btn.set_sensitive(enabled and not splitting)
        self._split_confirm_btn.set_visible(splitting)
        self._split_cancel_btn.set_visible(splitting)
        self._split_menu_btn.set_visible(not splitting)

    def _enter_split(self, axis: SplitAxis) -> None:
        if not self._selected_id:
            return
        self._flush_canvas_to_page()
        self._canvas.enter_split_mode(axis)
        self._set_page_actions(True)

    def _exit_split_mode(self) -> None:
        if self._canvas.split_active:
            self._canvas.exit_split_mode()
        self._set_page_actions(bool(self._selected_id))

    def _on_split_cancel(self, *_a) -> None:
        self._exit_split_mode()

    def _on_split_confirm(self, *_a) -> None:
        if not self._selected_id or not self._canvas.split_active:
            return
        page = self._tray.get(self._selected_id)
        if page is None:
            return
        axis = self._canvas.split_axis
        if axis is None:
            return
        positions = self._canvas.split_positions
        if not positions:
            return
        self._flush_canvas_to_page()
        parts = split_crop_along_guides(
            page.crop,
            img_w=page.image_w,
            img_h=page.image_h,
            view_rotate=page.rotate,
            axis=axis,
            positions=positions,
        )
        if parts is None:
            self._toast.add_toast(
                Adw.Toast(
                    title="Every split line must cross the crop box",
                    timeout=3,
                )
            )
            return
        ids = self._tray.replace_with_parts(page.id, parts)
        self._canvas.exit_split_mode()
        self.refresh(select=ids[0])

    def split_shortcut(self) -> bool:
        """S when deskew off: enter split (auto axis) or add another guide."""
        if not self._selected_id:
            return False
        if self._deskew_btn.get_active() or self._canvas.deskew_active:
            return False
        if self._canvas.split_active:
            return self._canvas.add_split_guide_at_largest_gap()
        from yokaze.ui.rotate_geom import display_size

        page = self._tray.get(self._selected_id)
        if page is None:
            return False
        vw, vh = display_size(page.image_w, page.image_h, page.rotate)
        self._enter_split(auto_split_axis(vw, vh))
        return True

    def confirm_split_shortcut(self) -> bool:
        """Enter: confirm split when blue guides are active."""
        if not self._canvas.split_active:
            return False
        self._on_split_confirm()
        return True

    def _flush_canvas_to_page(self) -> None:
        if self._syncing or not self._selected_id:
            return
        if self._deskew_btn.get_active() or self._canvas.deskew_active:
            self._exit_deskew_ui()
        page = self._tray.get(self._selected_id)
        if page is None:
            return
        page.crop = self._canvas.crop
        self._tray.selected_id = self._selected_id
        if self._tray.durable:
            self._tray.persist()

    def _nudge_rotate(self, delta: int) -> None:
        if not self._selected_id or self._canvas.split_active:
            return
        page = self._tray.get(self._selected_id)
        if page is None:
            return
        self._flush_canvas_to_page()
        page.rotate = step_rotate(page.rotate, delta=delta, base=0)
        self._canvas.set_view_rotate(page.rotate)
        self._rotate_label.set_text(f"{page.rotate}°")
        if self._tray.durable:
            self._tray.persist()
        self.refresh(select=page.id)

    def _on_full_page(self, *_a) -> None:
        if not self._selected_id or self._canvas.split_active:
            return
        page = self._tray.get(self._selected_id)
        if page is None:
            return
        page.crop = CropRect(0, 0, page.image_w, page.image_h)
        self._canvas.set_crop(page.crop)
        if self._tray.durable:
            self._tray.persist()

    def _move(self, delta: int) -> None:
        ids = self._selected_page_ids()
        if not ids:
            return
        self._flush_canvas_to_page()
        self._tray.nudge_block(ids, delta)
        self.refresh(select=self._selected_id)

    def fit_shortcut(self) -> bool:
        self._canvas.zoom_fit()
        return True

    def undo_crop_shortcut(self) -> bool:
        if self._canvas.split_active:
            return False
        return self._canvas.undo_crop_edit()

    def undo_shortcut(self) -> bool:
        """Ctrl+Z: undo the last crop edit, or restore the last deleted page."""
        if self.undo_crop_shortcut():
            return True
        return self.undo_delete()

    def focus_crop_corner_shortcut(self, keyval: int) -> bool:
        which = crop_corner_for_key(keyval)
        if which is None:
            return False
        self._canvas.focus_crop_corner(which)
        return True

    def toggle_deskew_shortcut(self) -> bool:
        if not self._selected_id or self._canvas.split_active:
            return False
        self._deskew_btn.set_active(not self._deskew_btn.get_active())
        return True

    def toggle_deskew_grid_shortcut(self) -> bool:
        if not self._deskew_btn.get_active() and not self._canvas.deskew_active:
            return False
        self._canvas.toggle_deskew_grid()
        return True

    def cycle_solo_preview_shortcut(self) -> bool:
        """D when deskew off: toggle solo crop preview (white)."""
        if self._deskew_btn.get_active() or self._canvas.deskew_active:
            return False
        if self._canvas.split_active:
            return False
        return self._canvas.cycle_solo_preview()

    def flip_solo_background_shortcut(self) -> bool:
        """Space while solo: white ↔ black."""
        return self._canvas.flip_solo_background()

    def cycle_deskew_grid_color_shortcut(self) -> bool:
        if not self._deskew_btn.get_active() and not self._canvas.deskew_active:
            return False
        self._canvas.cycle_deskew_grid_color()
        return True

    def toggle_deskew_tip_shortcut(self) -> bool:
        """S: detected angle (straight page) ↔ the live deskew adjustment."""
        if not self._deskew_btn.get_active() and not self._canvas.deskew_active:
            return False
        tip = 0.0
        self._deskew_suppress_edit_track = True
        try:
            if self._deskew_showing_tip:
                modified = (
                    tip
                    if self._deskew_modified_deg is None
                    else float(self._deskew_modified_deg)
                )
                self._canvas.set_deskew_session(modified)
                self._deskew_showing_tip = False
            else:
                self._deskew_modified_deg = float(self._canvas.deskew_angle_deg)
                self._canvas.set_deskew_session(tip)
                self._deskew_showing_tip = True
        finally:
            self._deskew_suppress_edit_track = False
        self._refresh_deskew_label()
        return True

    def _on_deskew_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            key = self._selected_id
            session = self._temp_deskew.get(key) if key is not None else None
            self._crop_before_deskew = self._canvas.crop
            self._canvas.enter_deskew_mode(0.0, session_deg=session)
            self._deskew_modified_deg = float(self._canvas.deskew_angle_deg)
            self._deskew_showing_tip = False
            self._refresh_deskew_label()
            return
        self._stash_temp_deskew()
        saved = self._crop_before_deskew
        self._crop_before_deskew = None
        self._deskew_showing_tip = False
        if self._canvas.deskew_active:
            self._canvas.exit_deskew_mode()
        self._canvas.clear_deskew_preview()
        if saved is not None:
            self._canvas.set_crop(saved)
        self._refresh_deskew_label()

    def _stash_temp_deskew(self) -> None:
        key = self._selected_id
        if key is None or not self._canvas.deskew_active:
            return
        if self._deskew_showing_tip and self._deskew_modified_deg is not None:
            self._temp_deskew[key] = float(self._deskew_modified_deg)
            return
        self._temp_deskew[key] = float(self._canvas.deskew_angle_deg)

    def _exit_deskew_ui(self) -> None:
        if not self._deskew_btn.get_active() and not self._canvas.deskew_active:
            return
        if self._deskew_btn.get_active():
            self._deskew_btn.set_active(False)
            return
        self._stash_temp_deskew()
        saved = self._crop_before_deskew
        self._crop_before_deskew = None
        self._deskew_showing_tip = False
        self._canvas.exit_deskew_mode()
        self._canvas.clear_deskew_preview()
        if saved is not None:
            self._canvas.set_crop(saved)
        self._refresh_deskew_label()

    def _on_deskew_canvas_changed(self) -> None:
        if not self._deskew_suppress_edit_track:
            self._deskew_showing_tip = abs(
                self._canvas.deskew_angle_deg - self._canvas.deskew_tip_deg
            ) < 0.025
            self._deskew_modified_deg = float(self._canvas.deskew_angle_deg)
            key = self._selected_id
            if key is not None and not self._deskew_showing_tip:
                self._temp_deskew[key] = float(self._canvas.deskew_angle_deg)
        self._refresh_deskew_label()

    def _refresh_deskew_label(self) -> None:
        if not self._deskew_btn.get_active() and not self._canvas.deskew_active:
            self._deskew_label.set_text("")
            self._deskew_label.set_visible(False)
            return
        angle = float(self._canvas.deskew_angle_deg)
        detected = abs(angle - float(self._canvas.deskew_tip_deg)) < 0.025
        tag = "detected" if detected else "live"
        self._deskew_label.set_text(f"{angle:.2f}° ({tag})")
        self._deskew_label.set_visible(True)

    def undo_delete(self) -> bool:
        """Restore the last deleted Stage page."""
        if self._canvas.split_active:
            return True
        self._flush_canvas_to_page()
        page_id = self._tray.undo_delete()
        if page_id is None:
            return False
        self.refresh(select=page_id)
        return True

    def _selected_page_ids(self) -> list[str]:
        chosen = set(self._selected_ids)
        ids = [page.id for page in self._tray.pages if page.id in chosen]
        if ids:
            return ids
        if self._selected_id and self._tray.get(self._selected_id) is not None:
            return [self._selected_id]
        return []

    def _on_delete(self, *_a) -> None:
        if self._canvas.split_active:
            return
        victims = self._selected_page_ids()
        if not victims:
            return
        ids = [page.id for page in self._tray.pages]
        first = min(ids.index(page_id) for page_id in victims)
        self._flush_canvas_to_page()
        self._selected_id = None
        self._selected_ids = []
        for page_id in reversed(victims):
            self._tray.remove(page_id)
        remaining = [page.id for page in self._tray.pages]
        keep = remaining[min(first, len(remaining) - 1)] if remaining else None
        self.refresh(select=keep)

    def _on_clear(self, *_a) -> None:
        self._selected_id = None
        self._selected_ids = []
        self._tray.clear()
        self.refresh()
        self._toast.add_toast(Adw.Toast(title="Stage cleared", timeout=2))

    def _send_targets(self) -> list:
        page_ids = [page.id for page in self._tray.pages]
        wanted = set(send_page_ids(page_ids, self._selected_ids))
        return [page for page in self._tray.pages if page.id in wanted]

    def _on_send(self, *_a) -> None:
        self._flush_canvas_to_page()
        if not self._tray.pages:
            return
        targets = self._send_targets()
        sent_ids = [page.id for page in targets]
        whole_tray = len(sent_ids) == len(self._tray.pages)
        self._send_btn.set_sensitive(False)
        self._clear_btn.set_sensitive(False)
        self._begin_send(sent_ids, whole_tray, skip_missing_replace=False)

    def _begin_send(
        self,
        sent_ids: list[str],
        whole_tray: bool,
        *,
        skip_missing_replace: bool,
    ) -> None:
        targets = [
            page
            for page in self._tray.pages
            if page.id in set(sent_ids)
        ]

        def work() -> None:
            try:
                msg = send_staging_tray(
                    self._tray,
                    pages=None if whole_tray else targets,
                    skip_missing_replace=skip_missing_replace,
                )
                GLib.idle_add(self._on_send_done, True, msg, sent_ids, whole_tray)
            except ReplaceDocumentGone as exc:
                GLib.idle_add(self._on_replace_gone, str(exc), sent_ids, whole_tray)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_send_done, False, str(exc), sent_ids, whole_tray)

        threading.Thread(target=work, daemon=True).start()

    def _on_replace_gone(
        self, detail: str, sent_ids: list[str], whole_tray: bool
    ) -> bool:
        self._send_btn.set_sensitive(True)
        self._clear_btn.set_sensitive(True)
        confirm_action(
            self,
            "Paperless document is gone",
            f"{detail}\n\nSend this Stage tray as a new document anyway?",
            on_yes=lambda: self._retry_send_skipping_gone(sent_ids, whole_tray),
        )
        return False

    def _retry_send_skipping_gone(
        self, sent_ids: list[str], whole_tray: bool
    ) -> None:
        self._send_btn.set_sensitive(False)
        self._clear_btn.set_sensitive(False)
        self._begin_send(sent_ids, whole_tray, skip_missing_replace=True)

    def _on_send_done(
        self,
        ok: bool,
        message: str,
        sent_ids: list[str] | None = None,
        whole_tray: bool = True,
    ) -> None:
        self._send_btn.set_sensitive(True)
        self._clear_btn.set_sensitive(True)
        if ok:
            if whole_tray:
                self._selected_id = None
                self._selected_ids = []
                self._tray.clear()
                self.refresh()
            else:
                self._drop_sent_pages(sent_ids or [])
            self._toast.add_toast(Adw.Toast(title=message, timeout=4))
        else:
            from .error_ui import present_error

            root = self.get_root()
            parent = root if isinstance(root, Gtk.Window) else None
            present_error(parent, "Send failed", message)

    def _drop_sent_pages(self, sent_ids: list[str]) -> None:
        """Take the uploaded pages off the tray and keep the rest."""
        order = [page.id for page in self._tray.pages]
        present = [page_id for page_id in sent_ids if page_id in set(order)]
        if not present:
            self.refresh(select=self._selected_id)
            return
        first = min(order.index(page_id) for page_id in present)
        self._tray.discard_pages(present)
        remaining = [page.id for page in self._tray.pages]
        keep = remaining[min(first, len(remaining) - 1)] if remaining else None
        self._selected_id = None
        self._selected_ids = []
        self.refresh(select=keep)

    def handle_nav_key(self, keyval: int) -> bool:
        if not self._tray.pages:
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace):
            if self._canvas.split_active:
                return True
            self._on_delete()
            return True
        ids = [p.id for p in self._tray.pages]
        if self._selected_id not in ids:
            self._select(ids[0])
            return True
        idx = ids.index(self._selected_id)
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Right):
            self._select(ids[(idx + 1) % len(ids)])
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Left):
            self._select(ids[(idx - 1) % len(ids)])
            return True
        return False


def _event_has_control(gesture: Gtk.Gesture) -> bool:
    event = gesture.get_current_event()
    if event is None:
        return False
    return bool(event.get_modifier_state() & Gdk.ModifierType.CONTROL_MASK)


def _stage_thumb_pixbuf(page) -> GdkPixbuf.Pixbuf | None:
    """Sidebar thumb at full overlay resolution; GTK shrinks for display."""
    try:
        import cv2

        from yokaze.ui.archive_thumbs import (
            apply_overlay_bgr,
            bgr_to_rgb_bytes,
            thumb_bgr,
        )

        bgr = None
        path = getattr(page, "path", None)
        if path is not None and path.is_file():
            loaded = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if loaded is not None:
                bgr = apply_overlay_bgr(loaded, page.crop, page.rotate)
        if bgr is None:
            if not page.archive or int(page.page) < 1:
                return None
            # No max_edge — keep original painted pixels; only the Picture is small.
            bgr = thumb_bgr(
                page.archive,
                int(page.page),
                page.crop,
                page.rotate,
                max_edge=None,
            )
        rgb, width, height = bgr_to_rgb_bytes(bgr)
        return GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgb),
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            width,
            height,
            width * 3,
        )
    except Exception:  # noqa: BLE001 — missing history / raw on old archives
        return None
