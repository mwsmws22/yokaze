"""Zoomable page canvas with a resizable crop rectangle (edges/corners only)."""

from __future__ import annotations

from collections.abc import Callable
import math
from typing import Literal

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gtk, Pango, PangoCairo  # noqa: E402

from yokaze.lib.geometry import DESKEW_STEP_DEG, project_crop, round_deskew

from .models import CropRect
from .page_split import SplitAxis
from .rotate_geom import (
    file_crop_to_view,
    norm_rotate,
    view_crop_to_file,
)

_MIN_SIZE = 16
_ZOOM_STEP = 1.32
_SCROLL_ZOOM_BASE = 1.28
_EDGE_HIT = 12
_CORNER_HIT = 12
# Absolute scale = screen pixels per image pixel. Was 8 (~800%); Gwenview
# routinely goes to 1600%+ for pixel inspection.
_MAX_SCALE = 32.0
# Gap between a focused crop corner and the window edge.
_CORNER_VIEW_PAD = 96.0
# Single-pane contain-fit inset (History / Annotate). Multi-pane compare
# stages use fit_scale=1.0 and put the gutter on the stage instead, so the
# center gap is not double the outer margin.
FIT_SCALE = 0.96


def rotated_view_corners(
    ox: float, oy: float, dw: float, dh: float, deg: float
) -> tuple[tuple[float, float], ...]:
    """Image rectangle after the same center spin as the deskew preview."""
    corners = (
        (ox, oy),
        (ox + dw, oy),
        (ox + dw, oy + dh),
        (ox, oy + dh),
    )
    if abs(deg) <= 1e-9:
        return corners
    cx = ox + dw / 2.0
    cy = oy + dh / 2.0
    rad = math.radians(deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    spun: list[tuple[float, float]] = []
    for x, y in corners:
        dx = x - cx
        dy = y - cy
        spun.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
    return tuple(spun)


def view_corners_bounds(
    corners: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, float]:
    """Axis-aligned box ``(left, top, width, height)`` that covers ``corners``."""
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    left, top = min(xs), min(ys)
    return left, top, max(xs) - left, max(ys) - top


_SEL_MIN = 8
_DESKEW_SCROLL_STEP = DESKEW_STEP_DEG
_DESKEW_GRID_PX = 40.0
_DESKEW_GRID_SPACING_MIN = 8.0
_DESKEW_GRID_SPACING_MAX = 400.0
_DESKEW_LOCK_CLICK_SLOP_PX = 5.0
_DESKEW_CLAMP = 45.0
# (r, g, b, a) — W cycles red → blue → green → red
_DESKEW_GRID_COLORS: tuple[tuple[float, float, float, float], ...] = (
    (0.9, 0.15, 0.1, 0.55),
    (0.2, 0.45, 0.95, 0.55),
    (0.15, 0.75, 0.25, 0.55),
)


def crop_corner_view(
    crop: CropRect,
    which: int,
    view_w: int,
    view_h: int,
    img_w: int,
    img_h: int,
) -> tuple[float, float, float] | None:
    """Scale and pan that place crop corner ``which`` on the matching view corner.

    1 top-left, 2 top-right, 3 bottom-right, 4 bottom-left. The view shows
    about an eighth of the crop, with that corner inset from the window edge.
    """
    if which not in (1, 2, 3, 4) or crop.w < 1 or crop.h < 1 or img_w < 1 or img_h < 1:
        return None
    corners = (
        (crop.x, crop.y),
        (crop.x + crop.w, crop.y),
        (crop.x + crop.w, crop.y + crop.h),
        (crop.x, crop.y + crop.h),
    )
    ix, iy = corners[which - 1]
    aw = max(1, int(view_w))
    ah = max(1, int(view_h))
    scale = min(aw / (crop.w * 0.125), ah / (crop.h * 0.125))
    scale = max(0.05, min(_MAX_SCALE, scale))
    pad = min(_CORNER_VIEW_PAD, aw * 0.2, ah * 0.2)
    targets = (
        (pad, pad),
        (float(aw) - pad, pad),
        (float(aw) - pad, float(ah) - pad),
        (pad, float(ah) - pad),
    )
    vx, vy = targets[which - 1]
    pan_x = vx - (aw - img_w * scale) / 2.0 - ix * scale
    pan_y = vy - (ah - img_h * scale) / 2.0 - iy * scale
    return scale, pan_x, pan_y


def _clamp_grid_spacing(value: float) -> float:
    return max(
        _DESKEW_GRID_SPACING_MIN, min(_DESKEW_GRID_SPACING_MAX, float(value))
    )


def _fitted_grid_spacing(span: float) -> float:
    """Cell size near the default grid, with the far edge landing on a line."""
    cells = max(1, round(float(span) / _DESKEW_GRID_PX))
    return _clamp_grid_spacing(float(span) / cells)


def _draw_centered_label(
    widget: Gtk.Widget, cr: cairo.Context, width: int, height: int, text: str
) -> None:
    """Dim centered empty-state, shared by FitImageCanvas and CropCanvas."""
    layout = widget.create_pango_layout(text)
    tw, th = layout.get_pixel_size()
    cr.set_source_rgb(0.6, 0.6, 0.6)
    cr.move_to((width - tw) / 2, (height - th) / 2)
    PangoCairo.show_layout(cr, layout)


def _paint_scaled_pixbuf(cr: cairo.Context, pixbuf: GdkPixbuf.Pixbuf, scale: float) -> None:
    """Draw ``pixbuf`` under the current cairo transform.

    At scale ≥ 1 (magnified), use nearest-neighbour so image pixels stay
    crisp like Gwenview. Below 1 (fit/overview), bilinear keeps downscales
    from looking crunchy.
    """
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    pattern = cr.get_source()
    pattern.set_filter(
        cairo.FILTER_NEAREST if scale >= 1.0 else cairo.FILTER_BILINEAR
    )
    cr.paint()


class FitImageCanvas(Gtk.DrawingArea):
    """Zoomable page canvas (Preview): fit + scroll zoom, same speed as Annotate."""

    def __init__(self, *, fit_scale: float = FIT_SCALE) -> None:
        super().__init__()
        self.add_css_class("lab-canvas")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._on_draw)
        self._fit_factor = float(fit_scale)
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        self._img_w = 0
        self._img_h = 0
        self._scale = 1.0
        self._fit_scale = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._pointer_x = 0.0
        self._pointer_y = 0.0
        self._bg_light = False
        self._panning = False
        self._pan_start = (0.0, 0.0)
        # Optional drag-to-select rectangle (Tune white lock). Off by default.
        self._rect_select = False
        self._sel_rect: tuple[int, int, int, int] | None = None
        self._sel_drag_view0: tuple[float, float] | None = None
        self._on_selection_changed: Callable[[tuple[int, int, int, int] | None], None] | None = (
            None
        )
        self._placeholder: str | None = None

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("enter", self._on_motion)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    def set_rect_select_mode(self, enabled: bool) -> None:
        """When on, primary drag draws a selection rect instead of panning."""
        self._rect_select = bool(enabled)
        if not self._rect_select:
            self._sel_drag_view0 = None

    def set_selection_rect(self, rect: tuple[int, int, int, int] | None) -> None:
        """Overlay rect in image coords ``(x, y, w, h)``, or clear."""
        self._sel_rect = rect
        self.queue_draw()

    def set_on_selection_changed(
        self,
        callback: Callable[[tuple[int, int, int, int] | None], None] | None,
    ) -> None:
        self._on_selection_changed = callback

    def set_background_light(self, light: bool) -> None:
        self._bg_light = bool(light)
        if light:
            self.add_css_class("lab-canvas-light")
        else:
            self.remove_css_class("lab-canvas-light")
        self.queue_draw()

    def _recompute_fit(self) -> None:
        alloc = self.get_allocation()
        aw, ah = max(1, alloc.width), max(1, alloc.height)
        if not self._img_w:
            self._fit_scale = 1.0
            self._scale = 1.0
            return
        self._fit_scale = min(aw / self._img_w, ah / self._img_h) * self._fit_factor

    def _reset_to_fit(self) -> None:
        self._recompute_fit()
        self._scale = self._fit_scale
        self._pan_x = self._pan_y = 0.0

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.DrawingArea.do_size_allocate(self, width, height, baseline)
        if not self._pixbuf:
            return
        old_fit = self._fit_scale
        at_fit = abs(self._scale - old_fit) < 1e-3
        self._recompute_fit()
        if at_fit:
            self._scale = self._fit_scale
            self._pan_x = self._pan_y = 0.0

    def zoom_fit(self) -> None:
        self._reset_to_fit()
        self.queue_draw()

    def zoom_by(
        self,
        factor: float,
        *,
        anchor: tuple[float, float] | None = None,
    ) -> None:
        if anchor is None:
            alloc = self.get_allocation()
            anchor = (alloc.width / 2, alloc.height / 2)
        self._zoom_at(anchor[0], anchor[1], factor)

    @property
    def placeholder(self) -> str | None:
        return self._placeholder

    def set_image_file(self, path: str | None, *, keep_view: bool = False) -> None:
        if not path:
            self.clear_image()
            return
        self.set_image_pixbuf(GdkPixbuf.Pixbuf.new_from_file(path), keep_view=keep_view)

    def set_image_pixbuf(
        self, pixbuf: GdkPixbuf.Pixbuf, *, keep_view: bool = False
    ) -> None:
        """Paint from an in-memory ``GdkPixbuf`` — no disk I/O.

        Lets a live-tuning caller build a pixbuf straight from a numpy array
        (e.g. via ``GdkPixbuf.Pixbuf.new_from_bytes``) and repaint on every
        slider tick without a JPEG encode/decode round-trip.
        """
        rel = 1.0
        pan = (0.0, 0.0)
        if keep_view and self._fit_scale > 1e-9:
            rel = self._scale / self._fit_scale
            pan = (self._pan_x, self._pan_y)
        self._placeholder = None
        self._pixbuf = pixbuf
        self._img_w = self._pixbuf.get_width()
        self._img_h = self._pixbuf.get_height()
        if keep_view:
            self._recompute_fit()
            self._scale = max(0.05, min(_MAX_SCALE, self._fit_scale * rel))
            self._pan_x, self._pan_y = pan
        else:
            self._reset_to_fit()
        self.queue_draw()

    def clear_image(self, placeholder: str | None = None) -> None:
        self._pixbuf = None
        self._img_w = self._img_h = 0
        self._scale = self._fit_scale = 1.0
        self._pan_x = self._pan_y = 0.0
        self._placeholder = placeholder
        self.queue_draw()

    def _zoom_at(self, vx: float, vy: float, factor: float) -> None:
        if not self._img_w:
            return
        ix, iy = self._view_to_img(vx, vy)
        old_scale = self._scale
        new_scale = max(0.05, min(_MAX_SCALE, old_scale * factor))
        if abs(new_scale - old_scale) < 1e-9:
            return
        alloc = self.get_allocation()
        aw, ah = max(1, alloc.width), max(1, alloc.height)
        ox_new = vx - ix * new_scale
        oy_new = vy - iy * new_scale
        self._scale = new_scale
        self._pan_x = ox_new - (aw - self._img_w * new_scale) / 2
        self._pan_y = oy_new - (ah - self._img_h * new_scale) / 2
        self.queue_draw()

    def _origin(self) -> tuple[float, float]:
        alloc = self.get_allocation()
        dw = self._img_w * self._scale
        dh = self._img_h * self._scale
        return (
            (alloc.width - dw) / 2 + self._pan_x,
            (alloc.height - dh) / 2 + self._pan_y,
        )

    def _view_to_img(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self._origin()
        return (x - ox) / self._scale, (y - oy) / self._scale

    def _on_motion(self, _c, x: float, y: float) -> None:
        self._pointer_x = x
        self._pointer_y = y

    def _on_scroll(self, _c, _dx, dy) -> bool:
        if dy == 0:
            return True
        factor = _SCROLL_ZOOM_BASE ** (-dy)
        self.zoom_by(factor, anchor=(self._pointer_x, self._pointer_y))
        return True

    def _on_drag_begin(self, _g, x: float, y: float) -> None:
        if self._rect_select and self._img_w:
            self._sel_drag_view0 = (x, y)
            self._panning = False
            return
        self._panning = True
        self._pan_start = (self._pan_x, self._pan_y)

    def _on_drag_update(self, _g, offset_x, offset_y) -> None:
        if self._rect_select and self._sel_drag_view0 is not None and self._img_w:
            vx0, vy0 = self._sel_drag_view0
            self._sel_rect = self._norm_sel_rect(vx0, vy0, vx0 + offset_x, vy0 + offset_y)
            self.queue_draw()
            return
        if not self._panning:
            return
        self._pan_x = self._pan_start[0] + offset_x
        self._pan_y = self._pan_start[1] + offset_y
        self.queue_draw()

    def _on_drag_end(self, *_a) -> None:
        if self._rect_select and self._sel_drag_view0 is not None:
            self._sel_drag_view0 = None
            if self._sel_rect is not None and (
                self._sel_rect[2] < _SEL_MIN or self._sel_rect[3] < _SEL_MIN
            ):
                self._sel_rect = None
            if self._on_selection_changed is not None:
                self._on_selection_changed(self._sel_rect)
            self.queue_draw()
            return
        self._panning = False

    def _norm_sel_rect(
        self, vx0: float, vy0: float, vx1: float, vy1: float
    ) -> tuple[int, int, int, int] | None:
        ix0, iy0 = self._view_to_img(vx0, vy0)
        ix1, iy1 = self._view_to_img(vx1, vy1)
        x0 = int(round(min(ix0, ix1)))
        y0 = int(round(min(iy0, iy1)))
        x1 = int(round(max(ix0, ix1)))
        y1 = int(round(max(iy0, iy1)))
        x0 = max(0, min(x0, self._img_w - 1))
        y0 = max(0, min(y0, self._img_h - 1))
        x1 = max(x0 + 1, min(x1, self._img_w))
        y1 = max(y0 + 1, min(y1, self._img_h))
        return (x0, y0, x1 - x0, y1 - y0)

    def _on_draw(self, _area, cr, width: int, height: int) -> None:
        if self._bg_light:
            cr.set_source_rgb(1.0, 1.0, 1.0)
        else:
            cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        if not self._pixbuf:
            if self._placeholder:
                _draw_centered_label(self, cr, width, height, self._placeholder)
            return
        ox, oy = self._origin()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(self._scale, self._scale)
        _paint_scaled_pixbuf(cr, self._pixbuf, self._scale)
        cr.restore()
        if self._sel_rect is not None:
            sx, sy, sw, sh = self._sel_rect
            cr.set_source_rgba(0.2, 0.85, 1.0, 0.95)
            cr.set_line_width(2.0)
            cr.rectangle(
                ox + sx * self._scale,
                oy + sy * self._scale,
                sw * self._scale,
                sh * self._scale,
            )
            cr.stroke()


class CropCanvas(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("lab-canvas")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._on_draw)

        self._placeholder = "Select a page to annotate"
        self._file_pixbuf: GdkPixbuf.Pixbuf | None = None
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        self._file_w = 0
        self._file_h = 0
        self._img_w = 0
        self._img_h = 0
        self._view_rotate = 0
        self._scale = 1.0
        self._fit_scale = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._pointer_x = 0.0
        self._pointer_y = 0.0
        self._crop = CropRect(0, 0, 1, 1)  # display / view space
        self._drag_mode: str | None = None
        self._drag_start = (0.0, 0.0)
        self._crop_start = CropRect(0, 0, 1, 1)
        self._split_axis: SplitAxis | None = None
        self._split_positions: list[float] = []
        self._split_drag_index: int | None = None
        self._split_drag: bool = False
        self._solo_phase: Literal["off", "black", "white"] = "off"
        self._solo_saved_view: tuple[float, float, float] | None = None
        # Sticky across solo off/on and page switches (one canvas per Stage/Annotate).
        self._solo_prefer_light: bool = True
        self._bg_light: bool = False
        self._deskew_active: bool = False
        self._deskew_base_deg: float = 0.0
        self._deskew_session_deg: float = 0.0
        # Angle the current crop is axis-aligned in. None = tip/file (base).
        self._crop_frame_deg: float | None = None
        # Box the user edited, at ``_crop_basis_deg``. Display is that box
        # projected by the full session delta, so repeated nudges do not grow it.
        self._crop_basis: CropRect | None = None
        self._crop_basis_deg: float = 0.0
        self._crop_undo: list[tuple] = []
        self._crop_edit_before: tuple | None = None
        self._deskew_grid_ox: float = 0.0
        self._deskew_grid_oy: float = 0.0
        self._deskew_grid_sx: float = _DESKEW_GRID_PX
        self._deskew_grid_sy: float = _DESKEW_GRID_PX
        self._deskew_grid_drag: bool = False
        self._deskew_grid_drag_start: tuple[float, float] = (0.0, 0.0)
        self._deskew_grid_visible: bool = True
        self._deskew_grid_color_i: int = 0
        # Locked grid lines (image space); at most one V and one H.
        self._deskew_lock_vx: float | None = None
        self._deskew_lock_vk: int | None = None
        self._deskew_lock_hy: float | None = None
        self._deskew_lock_hk: int | None = None
        self._deskew_hover: tuple[str, int, float] | None = None  # axis, k, pos
        self._deskew_line_drag: bool = False
        self._deskew_line_drag_axis: str | None = None
        self._deskew_line_drag_start_pos: float = 0.0
        self._deskew_line_drag_cells: int = 0
        self._deskew_click_line: tuple[str, int, float] | None = None
        self._deskew_press_xy: tuple[float, float] | None = None
        self._deskew_suppress_lock_click: bool = False
        self._deskew_changed: Callable[[], None] | None = None

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("enter", self._on_motion)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

    @property
    def placeholder(self) -> str:
        return self._placeholder

    @property
    def crop(self) -> CropRect:
        """Crop in preprocess / file coordinates."""
        if not self._file_w:
            return self._crop
        return view_crop_to_file(
            self._crop, self._file_w, self._file_h, self._view_rotate
        )

    def set_crop(self, crop: CropRect) -> None:
        """Accept crop in preprocess / file coordinates."""
        if self._solo_phase != "off":
            self._exit_solo_preview(restore_view=False)
        if self._file_w and self._file_h:
            crop = crop.clamp(self._file_w, self._file_h)
            self._crop = file_crop_to_view(
                crop, self._file_w, self._file_h, self._view_rotate
            )
            if self._img_w and self._img_h:
                self._crop = self._crop.clamp(self._img_w, self._img_h)
        else:
            self._crop = crop
        self._adopt_crop_basis(self._crop, self._deskew_base_deg)
        self._show_crop_at_session()
        self.queue_draw()

    def set_crop_from_user(self, crop: CropRect) -> None:
        """Replace the box from a button. Ctrl+Z restores the previous box."""
        self._begin_crop_edit()
        self.set_crop(crop)
        self._end_crop_edit()

    def undo_crop_edit(self) -> bool:
        """Restore the previous user crop edit. False when there is nothing to undo."""
        if not self._crop_undo:
            return False
        self._restore_crop_snap(self._crop_undo.pop())
        return True

    def _crop_snap(self) -> tuple:
        basis = self._crop_basis
        return (
            CropRect(self._crop.x, self._crop.y, self._crop.w, self._crop.h),
            None if basis is None else CropRect(basis.x, basis.y, basis.w, basis.h),
            float(self._crop_basis_deg),
            self._crop_frame_deg,
        )

    def _restore_crop_snap(self, snap: tuple) -> None:
        view, basis, basis_deg, frame_deg = snap
        self._crop = CropRect(view.x, view.y, view.w, view.h)
        self._crop_basis = (
            None if basis is None else CropRect(basis.x, basis.y, basis.w, basis.h)
        )
        self._crop_basis_deg = float(basis_deg)
        self._crop_frame_deg = frame_deg
        self.queue_draw()

    def _begin_crop_edit(self) -> None:
        self._crop_edit_before = self._crop_snap()

    def _end_crop_edit(self) -> None:
        before = self._crop_edit_before
        self._crop_edit_before = None
        if before is None or before[0] == self._crop:
            return
        self._crop_undo.append(before)
        if len(self._crop_undo) > 30:
            del self._crop_undo[0]

    def set_view_rotate(self, deg: int) -> None:
        """Extra CW spin for Annotate preview; crop stays correct in file space."""
        file_crop = self.crop if self._file_w else self._crop
        self._view_rotate = norm_rotate(deg)
        self._rebuild_display()
        if self._file_w:
            self.set_crop(file_crop)
        else:
            self.queue_draw()
        if self._split_axis is not None:
            self._clamp_split_positions()
            self.queue_draw()

    def enter_split_mode(self, axis: SplitAxis) -> None:
        """Show a full-page blue guide; crop handles disabled until exit."""
        if axis not in ("horizontal", "vertical"):
            raise ValueError(f"invalid split axis {axis!r}")
        if self._solo_phase != "off":
            self._exit_solo_preview(restore_view=True)
        if self._deskew_active:
            self.exit_deskew_mode()
        self._split_axis = axis
        mid = self._split_span() / 2.0
        self._split_positions = [mid]
        self._clamp_split_positions()
        self._drag_mode = None
        self._split_drag = False
        self._split_drag_index = None
        self.queue_draw()

    def add_split_guide_at_largest_gap(self) -> bool:
        """Add another guide at the midpoint of the largest gap. Requires split mode."""
        if self._split_axis is None:
            return False
        span = self._split_span()
        if span <= 0:
            return False
        from yokaze.ui.page_split import largest_gap_midpoint

        pos = largest_gap_midpoint(self._split_positions, span=span)
        self._split_positions.append(pos)
        self._clamp_split_positions()
        self.queue_draw()
        return True

    def exit_split_mode(self) -> None:
        self._split_axis = None
        self._split_positions = []
        self._split_drag = False
        self._split_drag_index = None
        self._drag_mode = None
        self.queue_draw()

    def enter_deskew_mode(
        self,
        tip_deskew_deg: float = 0.0,
        *,
        session_deg: float | None = None,
    ) -> None:
        """Hide crop chrome; show grid.

        ``tip_deskew_deg`` is the brod-baked baseline (visual delta 0 at tip).
        ``session_deg`` restores a prior temp angle for this page; default tip.
        """
        if self._solo_phase != "off":
            self._exit_solo_preview(restore_view=True)
        if self._split_axis is not None:
            self.exit_split_mode()
        tip = float(tip_deskew_deg)
        tip = max(-_DESKEW_CLAMP, min(_DESKEW_CLAMP, tip))
        session = tip if session_deg is None else float(session_deg)
        session = max(-_DESKEW_CLAMP, min(_DESKEW_CLAMP, session))
        self._deskew_active = True
        self._deskew_base_deg = tip
        self._deskew_session_deg = session
        self._deskew_grid_drag = False
        self._deskew_line_drag = False
        self._snap_deskew_grid_to_crop()
        self._deskew_grid_visible = True
        self._clear_deskew_locks()
        self._deskew_hover = None
        self._deskew_click_line = None
        self._deskew_press_xy = None
        self._deskew_suppress_lock_click = False
        self._drag_mode = None
        self.queue_draw()
        if self._deskew_changed is not None:
            self._deskew_changed()

    def exit_deskew_mode(self) -> None:
        """Leave deskew edit chrome; keep session angle for crop-mode preview."""
        was = self._deskew_active
        if was:
            self._realign_crop()
        self._deskew_active = False
        self._deskew_grid_drag = False
        self._deskew_line_drag = False
        self._deskew_grid_ox = 0.0
        self._deskew_grid_oy = 0.0
        self._deskew_grid_visible = True
        self._clear_deskew_locks()
        self._deskew_hover = None
        self._deskew_click_line = None
        self._deskew_press_xy = None
        self._deskew_suppress_lock_click = False
        self._drag_mode = None
        self.queue_draw()
        if was and self._deskew_changed is not None:
            self._deskew_changed()

    def _clear_deskew_locks(self) -> None:
        self._deskew_lock_vx = None
        self._deskew_lock_vk = None
        self._deskew_lock_hy = None
        self._deskew_lock_hk = None

    def _snap_deskew_grid_to_crop(self) -> None:
        """Start the grid on the current crop so page edges sit on the lines."""
        crop = self._crop
        if crop.w < _DESKEW_GRID_SPACING_MIN or crop.h < _DESKEW_GRID_SPACING_MIN:
            self._deskew_grid_ox = 0.0
            self._deskew_grid_oy = 0.0
            return
        self._deskew_grid_sx = _fitted_grid_spacing(crop.w)
        self._deskew_grid_sy = _fitted_grid_spacing(crop.h)
        self._deskew_grid_ox = float(crop.x)
        self._deskew_grid_oy = float(crop.y)

    def _adopt_crop_basis(self, crop: CropRect, at_deg: float) -> None:
        """Remember the box the user edited, axis-aligned at ``at_deg``."""
        self._crop_basis = CropRect(int(crop.x), int(crop.y), int(crop.w), int(crop.h))
        self._crop_basis_deg = float(at_deg)

    def _show_crop_at_session(self) -> None:
        """Draw the basis box projected by the whole session delta, not each step."""
        if self._crop_basis is None:
            at = (
                self._deskew_base_deg
                if self._crop_frame_deg is None
                else self._crop_frame_deg
            )
            self._adopt_crop_basis(self._crop, at)
        delta = self._deskew_session_deg - self._crop_basis_deg
        basis = self._crop_basis
        if basis is None:
            return
        if abs(delta) >= 1e-4 and self._img_w > 0 and self._img_h > 0:
            self._crop = project_crop(basis, delta, self._img_w, self._img_h)
        else:
            self._crop = CropRect(basis.x, basis.y, basis.w, basis.h)
        self._crop_frame_deg = float(self._deskew_session_deg)

    def _realign_crop(self) -> None:
        """Project the box so it stays straight on the page at the session angle."""
        self._show_crop_at_session()

    def apply_deskew_preview(
        self,
        tip_deskew_deg: float = 0.0,
        session_deg: float | None = None,
    ) -> None:
        """Keep tip/session angles for live preview without entering deskew mode."""
        tip = float(tip_deskew_deg)
        tip = max(-_DESKEW_CLAMP, min(_DESKEW_CLAMP, tip))
        session = tip if session_deg is None else float(session_deg)
        session = max(-_DESKEW_CLAMP, min(_DESKEW_CLAMP, session))
        self._deskew_base_deg = tip
        self._deskew_session_deg = session
        self._realign_crop()
        self.queue_draw()

    def clear_deskew_preview(self) -> None:
        self._deskew_base_deg = 0.0
        self._deskew_session_deg = 0.0
        self._crop_frame_deg = None
        self.queue_draw()

    @property
    def deskew_active(self) -> bool:
        return self._deskew_active

    @property
    def deskew_angle_deg(self) -> float:
        """Absolute session deskew (tip ± nudge)."""
        return float(self._deskew_session_deg)

    @property
    def deskew_tip_deg(self) -> float:
        """Brod-baked tip baseline for this deskew session."""
        return float(self._deskew_base_deg)

    @property
    def deskew_grid_visible(self) -> bool:
        return self._deskew_grid_visible

    def set_deskew_grid_visible(self, visible: bool) -> None:
        if not self._deskew_active:
            return
        self._deskew_grid_visible = bool(visible)
        self.queue_draw()

    def toggle_deskew_grid(self) -> bool:
        """Toggle grid overlay; returns new visibility (False if not in deskew)."""
        if not self._deskew_active:
            return False
        self._deskew_grid_visible = not self._deskew_grid_visible
        self.queue_draw()
        return self._deskew_grid_visible

    def cycle_deskew_grid_color(self) -> int:
        """Cycle grid color red→blue→green; returns new index (or -1 if inactive)."""
        if not self._deskew_active:
            return -1
        self._deskew_grid_color_i = (self._deskew_grid_color_i + 1) % len(
            _DESKEW_GRID_COLORS
        )
        self.queue_draw()
        return self._deskew_grid_color_i

    @property
    def visual_deskew_deg(self) -> float:
        """Cairo rotate amount; brod is already tip-deskewed."""
        return float(self._deskew_session_deg - self._deskew_base_deg)

    def set_deskew_session(self, deg: float) -> None:
        """Set absolute session deskew (deskew mode or crop preview)."""
        self._set_deskew_session(deg)

    def _set_deskew_session(self, deg: float) -> None:
        self._deskew_session_deg = max(
            -_DESKEW_CLAMP, min(_DESKEW_CLAMP, float(deg))
        )
        self.queue_draw()
        if self._deskew_changed is not None:
            self._deskew_changed()

    def nudge_deskew(self, delta_deg: float) -> None:
        """Step the session angle. The crop box stays as edited; the image rotates."""
        if not self._img_w and not self._deskew_active:
            return
        self._set_deskew_session(
            round_deskew(self._deskew_session_deg + float(delta_deg))
        )

    @property
    def split_active(self) -> bool:
        return self._split_axis is not None

    @property
    def split_axis(self) -> SplitAxis | None:
        return self._split_axis

    @property
    def split_position(self) -> float:
        """First guide coordinate (compat); prefer ``split_positions``."""
        if not self._split_positions:
            return 0.0
        return float(self._split_positions[0])

    @property
    def split_positions(self) -> list[float]:
        """All guide coordinates in view-image pixels."""
        return list(self._split_positions)

    def _split_span(self) -> float:
        if self._split_axis == "horizontal":
            return float(max(0, self._img_h))
        if self._split_axis == "vertical":
            return float(max(0, self._img_w))
        return 0.0

    def _clamp_split_positions(self) -> None:
        span = self._split_span()
        self._split_positions = [
            max(0.0, min(float(p), span)) for p in self._split_positions
        ]

    def _clamp_split_pos(self) -> None:
        """Clamp the actively dragged guide (or all if none active)."""
        span = self._split_span()
        if self._split_drag_index is not None and 0 <= self._split_drag_index < len(
            self._split_positions
        ):
            i = self._split_drag_index
            self._split_positions[i] = max(
                0.0, min(float(self._split_positions[i]), span)
            )
        else:
            self._clamp_split_positions()

    def set_image_file(self, path: str) -> None:
        self._file_pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        self._file_w = self._file_pixbuf.get_width()
        self._file_h = self._file_pixbuf.get_height()
        self._view_rotate = 0
        self._split_axis = None
        self._split_positions = []
        self._split_drag = False
        self._split_drag_index = None
        if self._solo_phase != "off":
            self._exit_solo_preview(restore_view=False)
        self._deskew_active = False
        self._deskew_base_deg = 0.0
        self._deskew_session_deg = 0.0
        self._crop_frame_deg = None
        self._deskew_grid_drag = False
        self._deskew_line_drag = False
        self._deskew_grid_ox = 0.0
        self._deskew_grid_oy = 0.0
        self._deskew_grid_sx = _DESKEW_GRID_PX
        self._deskew_grid_sy = _DESKEW_GRID_PX
        self._clear_deskew_locks()
        self._deskew_hover = None
        self._rebuild_display()
        self._crop = CropRect(0, 0, self._img_w, self._img_h)
        self._adopt_crop_basis(self._crop, 0.0)
        self._crop_undo.clear()
        self._crop_edit_before = None
        self._pan_x = self._pan_y = 0.0
        self._recompute_fit()
        self.queue_draw()

    def clear_image(self) -> None:
        self._file_pixbuf = None
        self._pixbuf = None
        self._file_w = self._file_h = 0
        self._img_w = self._img_h = 0
        self._view_rotate = 0
        self._pan_x = self._pan_y = 0.0
        self._split_axis = None
        self._split_positions = []
        self._split_drag = False
        self._split_drag_index = None
        if self._solo_phase != "off":
            self._exit_solo_preview(restore_view=False)
        self._deskew_active = False
        self._deskew_base_deg = 0.0
        self._deskew_session_deg = 0.0
        self._crop_frame_deg = None
        self._crop_basis = None
        self._crop_basis_deg = 0.0
        self._crop_undo.clear()
        self._crop_edit_before = None
        self._deskew_grid_drag = False
        self._deskew_line_drag = False
        self._deskew_grid_ox = 0.0
        self._deskew_grid_oy = 0.0
        self._deskew_grid_sx = _DESKEW_GRID_PX
        self._deskew_grid_sy = _DESKEW_GRID_PX
        self._clear_deskew_locks()
        self._deskew_hover = None
        self.queue_draw()

    def _rebuild_display(self) -> None:
        if self._file_pixbuf is None:
            self._pixbuf = None
            self._img_w = self._img_h = 0
            return
        pix = self._file_pixbuf
        r = self._view_rotate
        if r == 90:
            pix = pix.rotate_simple(GdkPixbuf.PixbufRotation.CLOCKWISE)
        elif r == 180:
            pix = pix.rotate_simple(GdkPixbuf.PixbufRotation.UPSIDEDOWN)
        elif r == 270:
            pix = pix.rotate_simple(GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE)
        self._pixbuf = pix
        self._img_w = pix.get_width()
        self._img_h = pix.get_height()
        self._recompute_fit()

    def zoom_by(
        self,
        factor: float,
        *,
        anchor: tuple[float, float] | None = None,
    ) -> None:
        if anchor is None:
            alloc = self.get_allocation()
            anchor = (alloc.width / 2, alloc.height / 2)
        self._zoom_at(anchor[0], anchor[1], factor)

    def zoom_fit(self) -> None:
        self._pan_x = self._pan_y = 0.0
        self._recompute_fit()
        self._scale = self._fit_scale
        self.queue_draw()

    def zoom_in(self) -> None:
        self.zoom_by(_ZOOM_STEP)

    def focus_crop_corner(self, which: int) -> bool:
        """Zoom to crop corner 1–4 (Tab, Q, W, E). Works in crop and deskew mode."""
        if not self._img_w or not self._img_h:
            return False
        if self._solo_phase != "off":
            return False
        alloc = self.get_allocation()
        view = crop_corner_view(
            self._crop, which, alloc.width, alloc.height, self._img_w, self._img_h
        )
        if view is None:
            return False
        self._scale, self._pan_x, self._pan_y = view
        self.queue_draw()
        return True

    def zoom_out(self) -> None:
        self.zoom_by(1 / _ZOOM_STEP)

    @property
    def solo_phase(self) -> Literal["off", "black", "white"]:
        return self._solo_phase

    @property
    def background_light(self) -> bool:
        return self._bg_light

    def set_background_light(self, light: bool) -> None:
        self._bg_light = bool(light)
        if light:
            self.add_css_class("lab-canvas-light")
        else:
            self.remove_css_class("lab-canvas-light")
        self.queue_draw()

    def cycle_solo_preview(self) -> bool:
        """Toggle solo crop preview. Re-enter uses last black/white choice."""
        if self._deskew_active or self._split_axis is not None:
            return False
        if self._solo_phase != "off":
            self._solo_prefer_light = self._solo_phase == "white"
            self._exit_solo_preview(restore_view=True)
            return True
        if not self._pixbuf or self._crop.w < 1 or self._crop.h < 1:
            return False
        self._solo_saved_view = (self._pan_x, self._pan_y, self._scale)
        if self._solo_prefer_light:
            self._solo_phase = "white"
            self.set_background_light(True)
        else:
            self._solo_phase = "black"
            self.set_background_light(False)
        self.zoom_fit()
        return True

    def flip_solo_background(self) -> bool:
        """Space while solo: white ↔ black. False when not in solo."""
        if self._solo_phase == "off":
            return False
        if self._solo_phase == "white":
            self._solo_phase = "black"
            self.set_background_light(False)
        else:
            self._solo_phase = "white"
            self.set_background_light(True)
        self._solo_prefer_light = self._solo_phase == "white"
        return True

    def _exit_solo_preview(self, *, restore_view: bool) -> None:
        if self._solo_phase != "off":
            self._solo_prefer_light = self._solo_phase == "white"
        self._solo_phase = "off"
        self.set_background_light(False)
        if restore_view and self._solo_saved_view is not None:
            self._pan_x, self._pan_y, self._scale = self._solo_saved_view
        self._solo_saved_view = None
        self.queue_draw()

    def _content_size(self) -> tuple[int, int]:
        if self._solo_phase != "off":
            c = self._crop.clamp(self._img_w, self._img_h)
            return max(1, c.w), max(1, c.h)
        return self._img_w, self._img_h

    def _recompute_fit(self) -> None:
        alloc = self.get_allocation()
        aw, ah = max(1, alloc.width), max(1, alloc.height)
        iw, ih = self._content_size()
        if not iw or not ih:
            self._fit_scale = 1.0
            return
        self._fit_scale = min(aw / iw, ah / ih) * FIT_SCALE
        self._scale = self._fit_scale

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.DrawingArea.do_size_allocate(self, width, height, baseline)
        if self._pixbuf:
            old = self._fit_scale
            self._recompute_fit()
            if abs(self._scale - old) < 1e-3:
                self._scale = self._fit_scale
                self._pan_x = self._pan_y = 0.0

    def _zoom_at(self, vx: float, vy: float, factor: float) -> None:
        if not self._img_w:
            return
        ix, iy = self._view_to_img(vx, vy)
        old_scale = self._scale
        new_scale = max(0.05, min(_MAX_SCALE, old_scale * factor))
        if abs(new_scale - old_scale) < 1e-9:
            return

        alloc = self.get_allocation()
        aw, ah = max(1, alloc.width), max(1, alloc.height)
        cw, ch = self._content_size()
        ox_new = vx - ix * new_scale
        oy_new = vy - iy * new_scale
        self._scale = new_scale
        self._pan_x = ox_new - (aw - cw * new_scale) / 2
        self._pan_y = oy_new - (ah - ch * new_scale) / 2
        self.queue_draw()

    def _origin(self) -> tuple[float, float]:
        alloc = self.get_allocation()
        cw, ch = self._content_size()
        dw = cw * self._scale
        dh = ch * self._scale
        return (
            (alloc.width - dw) / 2 + self._pan_x,
            (alloc.height - dh) / 2 + self._pan_y,
        )

    def _img_to_view(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self._origin()
        return ox + x * self._scale, oy + y * self._scale

    def _view_to_img(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self._origin()
        return (x - ox) / self._scale, (y - oy) / self._scale

    def _hit(self, vx: float, vy: float) -> str | None:
        if self._deskew_active or not self._img_w:
            return None

        cx, cy = self._img_to_view(self._crop.x, self._crop.y)
        cw = self._crop.w * self._scale
        ch = self._crop.h * self._scale

        corners = (
            ("nw", cx, cy),
            ("ne", cx + cw, cy),
            ("sw", cx, cy + ch),
            ("se", cx + cw, cy + ch),
        )
        for name, hx, hy in corners:
            if abs(vx - hx) <= _CORNER_HIT and abs(vy - hy) <= _CORNER_HIT:
                return name

        x_lo, x_hi = self._edge_span(cx, cw, _CORNER_HIT)
        y_lo, y_hi = self._edge_span(cy, ch, _CORNER_HIT)

        if abs(vy - cy) <= _EDGE_HIT and x_lo <= vx <= x_hi:
            return "n"
        if abs(vy - (cy + ch)) <= _EDGE_HIT and x_lo <= vx <= x_hi:
            return "s"
        if abs(vx - cx) <= _EDGE_HIT and y_lo <= vy <= y_hi:
            return "w"
        if abs(vx - (cx + cw)) <= _EDGE_HIT and y_lo <= vy <= y_hi:
            return "e"

        return None

    @staticmethod
    def _edge_span(origin: float, length: float, margin: float) -> tuple[float, float]:
        if length <= 2 * margin:
            return origin, origin + length
        return origin + margin, origin + length - margin

    def _on_pressed(self, gesture, _n, x, y) -> None:
        if self._solo_phase != "off":
            self._drag_mode = None
            return
        if self._deskew_active:
            self._drag_mode = None
            self._deskew_suppress_lock_click = False
            self._deskew_press_xy = (float(x), float(y))
            state = gesture.get_current_event_state()
            if state & Gdk.ModifierType.CONTROL_MASK and self._deskew_grid_visible:
                self._deskew_click_line = self._nearest_deskew_line(x, y)
            else:
                self._deskew_click_line = None
            return
        if self._split_axis is not None:
            hit = self._hit_split_index(x, y)
            self._split_drag_index = hit
            self._split_drag = hit is not None
            self._drag_mode = None
            return
        self._drag_mode = self._hit(x, y)
        self._drag_start = self._view_to_img(x, y)
        self._crop_start = CropRect(
            self._crop.x, self._crop.y, self._crop.w, self._crop.h
        )
        if self._drag_mode:
            self._begin_crop_edit()

    def _on_released(self, gesture, _n, x, y) -> None:
        # Only Ctrl+click locks. Any drag (or click-cancel from drag) moves
        # enough — or sets suppress — so release must not lock.
        if (
            self._deskew_active
            and self._deskew_click_line is not None
            and not self._deskew_suppress_lock_click
            and self._deskew_press_xy is not None
        ):
            dx = float(x) - self._deskew_press_xy[0]
            dy = float(y) - self._deskew_press_xy[1]
            if (dx * dx + dy * dy) <= (
                _DESKEW_LOCK_CLICK_SLOP_PX * _DESKEW_LOCK_CLICK_SLOP_PX
            ):
                state = gesture.get_current_event_state()
                if state & Gdk.ModifierType.CONTROL_MASK:
                    self._toggle_deskew_lock(self._deskew_click_line)
        self._deskew_click_line = None
        self._deskew_press_xy = None
        self._deskew_suppress_lock_click = False
        self._drag_mode = None
        self._split_drag = False
        self._split_drag_index = None
        self._deskew_grid_drag = False
        self._deskew_line_drag = False
        self._deskew_line_drag_axis = None

    def _on_drag_begin(self, gesture, x: float, y: float) -> None:
        if self._deskew_active:
            # Cancel pending lock before click gesture can treat this as a click.
            self._deskew_suppress_lock_click = True
            self._deskew_click_line = None
            state = gesture.get_current_event_state()
            if state & Gdk.ModifierType.CONTROL_MASK and self._deskew_grid_visible:
                hit = self._nearest_deskew_line(x, y)
                if hit is not None and self._begin_deskew_line_drag(hit):
                    self._deskew_grid_drag = False
                    return
                self._deskew_line_drag = False
                self._deskew_grid_drag = False
                return
            self._deskew_line_drag = False
            self._deskew_grid_drag = True
            self._deskew_grid_drag_start = (
                self._deskew_grid_ox,
                self._deskew_grid_oy,
            )
            return
        if self._split_axis is None:
            return
        ix, iy = self._view_to_img(x, y)
        idx = self._split_drag_index
        if idx is None:
            idx = self._hit_split_index(x, y)
        if idx is None and self._split_positions:
            idx = self._nearest_split_index(ix, iy)
        if idx is None:
            return
        self._split_drag_index = idx
        self._split_drag = True
        self._drag_start = (ix, iy)
        if self._split_axis == "horizontal":
            self._split_positions[idx] = iy
        else:
            self._split_positions[idx] = ix
        self._clamp_split_pos()
        self.queue_draw()

    def _on_drag_update(self, _g, offset_x, offset_y) -> None:
        if self._deskew_active:
            # Belt-and-suspenders: any drag motion cancels pending lock click.
            self._deskew_suppress_lock_click = True
            self._deskew_click_line = None
            if self._deskew_line_drag:
                self._update_deskew_line_drag(offset_x, offset_y)
            elif self._deskew_grid_drag:
                ox0, oy0 = self._deskew_grid_drag_start
                scale = self._scale if self._scale else 1.0
                self._deskew_grid_ox = ox0 + float(offset_x) / scale
                self._deskew_grid_oy = oy0 + float(offset_y) / scale
                self._snap_deskew_locks()
                self.queue_draw()
            return
        if self._split_axis is not None:
            if (
                not self._split_drag
                or self._split_drag_index is None
                or not self._img_w
            ):
                return
            ix0, iy0 = self._drag_start
            i = self._split_drag_index
            if self._split_axis == "horizontal":
                self._split_positions[i] = iy0 + offset_y / self._scale
            else:
                self._split_positions[i] = ix0 + offset_x / self._scale
            self._clamp_split_pos()
            self.queue_draw()
            return
        if not self._drag_mode or not self._img_w:
            return
        dix = offset_x / self._scale
        diy = offset_y / self._scale
        ix0, iy0 = self._drag_start
        ix, iy = ix0 + dix, iy0 + diy
        c0 = self._crop_start
        mode = self._drag_mode
        x, y, w, h = c0.x, c0.y, c0.w, c0.h
        if "n" in mode:
            new_y = int(round(iy))
            h = c0.y + c0.h - new_y
            y = new_y
        if "s" in mode:
            h = int(round(iy - c0.y))
        if "w" in mode:
            new_x = int(round(ix))
            w = c0.x + c0.w - new_x
            x = new_x
        if "e" in mode:
            w = int(round(ix - c0.x))
        if w < _MIN_SIZE:
            if "w" in mode:
                x = x + w - _MIN_SIZE
            w = _MIN_SIZE
        if h < _MIN_SIZE:
            if "n" in mode:
                y = y + h - _MIN_SIZE
            h = _MIN_SIZE
        view = CropRect(x, y, w, h)
        if self._img_w and self._img_h:
            view = view.clamp(self._img_w, self._img_h)
        self._adopt_crop_basis(view, self._deskew_session_deg)
        self._crop = CropRect(view.x, view.y, view.w, view.h)
        self._crop_frame_deg = float(self._deskew_session_deg)
        self.queue_draw()

    def _on_drag_end(self, *_a) -> None:
        self._end_crop_edit()
        self._drag_mode = None
        self._split_drag = False
        self._deskew_grid_drag = False
        self._deskew_line_drag = False
        self._deskew_line_drag_axis = None

    def _snap_deskew_locks(self) -> None:
        """Keep locked lines fixed in image space after phase/spacing changes."""
        if self._deskew_lock_vx is not None and self._deskew_lock_vk is not None:
            self._deskew_grid_ox = (
                self._deskew_lock_vx - self._deskew_lock_vk * self._deskew_grid_sx
            )
        if self._deskew_lock_hy is not None and self._deskew_lock_hk is not None:
            self._deskew_grid_oy = (
                self._deskew_lock_hy - self._deskew_lock_hk * self._deskew_grid_sy
            )

    def _nearest_deskew_line(
        self, vx: float, vy: float
    ) -> tuple[str, int, float] | None:
        """Closest grid line to the pointer: (axis, index, image-space position)."""
        if not self._img_w or not self._deskew_grid_visible:
            return None
        sx = self._deskew_grid_sx
        sy = self._deskew_grid_sy
        if sx < 1e-6 or sy < 1e-6:
            return None
        ix, iy = self._view_to_img(vx, vy)
        kv = int(round((ix - self._deskew_grid_ox) / sx))
        kh = int(round((iy - self._deskew_grid_oy) / sy))
        pos_v = self._deskew_grid_ox + kv * sx
        pos_h = self._deskew_grid_oy + kh * sy
        dist_v = abs(ix - pos_v)
        dist_h = abs(iy - pos_h)
        if dist_v <= dist_h:
            return ("v", kv, pos_v)
        return ("h", kh, pos_h)

    def _toggle_deskew_lock(self, line: tuple[str, int, float]) -> None:
        axis, k, pos = line
        if axis == "v":
            if self._deskew_lock_vx is not None and self._deskew_lock_vk == k:
                self._deskew_lock_vx = None
                self._deskew_lock_vk = None
            else:
                self._deskew_lock_vx = float(pos)
                self._deskew_lock_vk = int(k)
                self._snap_deskew_locks()
        else:
            if self._deskew_lock_hy is not None and self._deskew_lock_hk == k:
                self._deskew_lock_hy = None
                self._deskew_lock_hk = None
            else:
                self._deskew_lock_hy = float(pos)
                self._deskew_lock_hk = int(k)
                self._snap_deskew_locks()
        self.queue_draw()

    def _begin_deskew_line_drag(self, hit: tuple[str, int, float]) -> bool:
        axis, k, pos = hit
        if axis == "v":
            if self._deskew_lock_vx is None or self._deskew_lock_vk is None:
                return False
            if k == self._deskew_lock_vk:
                return False
            cells = k - self._deskew_lock_vk
            if cells == 0:
                return False
            self._deskew_line_drag = True
            self._deskew_line_drag_axis = "v"
            self._deskew_line_drag_start_pos = float(pos)
            self._deskew_line_drag_cells = int(cells)
            return True
        if self._deskew_lock_hy is None or self._deskew_lock_hk is None:
            return False
        if k == self._deskew_lock_hk:
            return False
        cells = k - self._deskew_lock_hk
        if cells == 0:
            return False
        self._deskew_line_drag = True
        self._deskew_line_drag_axis = "h"
        self._deskew_line_drag_start_pos = float(pos)
        self._deskew_line_drag_cells = int(cells)
        return True

    def _update_deskew_line_drag(self, offset_x: float, offset_y: float) -> None:
        scale = self._scale if self._scale else 1.0
        n = self._deskew_line_drag_cells
        if n == 0:
            return
        if self._deskew_line_drag_axis == "v":
            if self._deskew_lock_vx is None or self._deskew_lock_vk is None:
                return
            d1 = self._deskew_line_drag_start_pos + float(offset_x) / scale
            sx = (d1 - self._deskew_lock_vx) / float(n)
            if sx <= 0:
                return
            self._deskew_grid_sx = _clamp_grid_spacing(sx)
            self._snap_deskew_locks()
            self.queue_draw()
            return
        if self._deskew_lock_hy is None or self._deskew_lock_hk is None:
            return
        d1 = self._deskew_line_drag_start_pos + float(offset_y) / scale
        sy = (d1 - self._deskew_lock_hy) / float(n)
        if sy <= 0:
            return
        self._deskew_grid_sy = _clamp_grid_spacing(sy)
        self._snap_deskew_locks()
        self.queue_draw()

    def _hit_split(self, vx: float, vy: float) -> bool:
        return self._hit_split_index(vx, vy) is not None

    def _hit_split_index(self, vx: float, vy: float) -> int | None:
        if not self._img_w or self._split_axis is None:
            return None
        best_i: int | None = None
        best_d = _EDGE_HIT * 1.5
        for i, pos in enumerate(self._split_positions):
            if self._split_axis == "horizontal":
                _, ly = self._img_to_view(0, pos)
                d = abs(vy - ly)
            else:
                lx, _ = self._img_to_view(pos, 0)
                d = abs(vx - lx)
            if d <= best_d:
                best_d = d
                best_i = i
        return best_i

    def _nearest_split_index(self, ix: float, iy: float) -> int | None:
        if not self._split_positions:
            return None
        if self._split_axis == "horizontal":
            return min(
                range(len(self._split_positions)),
                key=lambda i: abs(self._split_positions[i] - iy),
            )
        return min(
            range(len(self._split_positions)),
            key=lambda i: abs(self._split_positions[i] - ix),
        )

    def _on_motion(self, controller, x: float, y: float) -> None:
        self._pointer_x = x
        self._pointer_y = y
        if not self._deskew_active or not self._deskew_grid_visible:
            if self._deskew_hover is not None:
                self._deskew_hover = None
                self.queue_draw()
            return
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            hover = self._nearest_deskew_line(x, y)
        else:
            hover = None
        if hover != self._deskew_hover:
            self._deskew_hover = hover
            self.queue_draw()

    def _on_scroll(self, controller, _dx, dy) -> bool:
        if dy == 0:
            return True
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK and self._img_w:
            # Match zoom: negative dy → increase (natural wheel).
            self.nudge_deskew(_DESKEW_SCROLL_STEP * (-dy))
            return True
        factor = _SCROLL_ZOOM_BASE ** (-dy)
        self.zoom_by(factor, anchor=(self._pointer_x, self._pointer_y))
        return True

    def _fill_crop_mat(
        self,
        cr: cairo.Context,
        ox: float,
        oy: float,
        dw: float,
        dh: float,
        cx: float,
        cy: float,
        cw: float,
        ch: float,
    ) -> None:
        """Dim the page outside the crop. The dimmed page spins with the image."""
        cr.new_path()
        corners = rotated_view_corners(ox, oy, dw, dh, self.visual_deskew_deg)
        cr.move_to(*corners[0])
        for x, y in corners[1:]:
            cr.line_to(x, y)
        cr.close_path()
        cr.rectangle(cx, cy, cw, ch)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
        cr.fill()

    def _on_draw(self, _area, cr, width, height) -> None:
        if self._bg_light:
            cr.set_source_rgb(1.0, 1.0, 1.0)
        else:
            cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        if not self._pixbuf:
            _draw_centered_label(self, cr, width, height, self._placeholder)
            return

        if self._solo_phase != "off":
            self._draw_solo_crop(cr)
            return

        ox, oy = self._origin()
        dw = self._img_w * self._scale
        dh = self._img_h * self._scale
        cr.save()
        if abs(self.visual_deskew_deg) > 1e-9:
            cr.translate(ox + dw / 2.0, oy + dh / 2.0)
            cr.rotate(math.radians(self.visual_deskew_deg))
            cr.translate(-dw / 2.0, -dh / 2.0)
        else:
            cr.translate(ox, oy)
        cr.scale(self._scale, self._scale)
        _paint_scaled_pixbuf(cr, self._pixbuf, self._scale)
        cr.restore()

        if self._deskew_active and self._deskew_grid_visible:
            self._draw_deskew_grid(cr, ox, oy, dw, dh)
            return
        if self._deskew_active:
            return

        cx, cy = self._img_to_view(self._crop.x, self._crop.y)
        cw = self._crop.w * self._scale
        ch = self._crop.h * self._scale
        self._fill_crop_mat(cr, ox, oy, dw, dh, cx, cy, cw, ch)

        cr.set_source_rgb(0.9, 0.15, 0.1)
        cr.set_line_width(2)
        cr.rectangle(cx, cy, cw, ch)
        cr.stroke()
        if self._split_axis is None:
            for hx, hy in (
                (cx, cy),
                (cx + cw, cy),
                (cx, cy + ch),
                (cx + cw, cy + ch),
            ):
                cr.rectangle(hx - 4, hy - 4, 8, 8)
                cr.fill()
        else:
            self._draw_split_guide(cr, ox, oy, dw, dh)

    def _draw_solo_crop(self, cr: cairo.Context) -> None:
        """Paint only the crop region, fit-centered (Evaluate-style)."""
        assert self._pixbuf is not None
        c = self._crop.clamp(self._img_w, self._img_h)
        if c.w < 1 or c.h < 1:
            return
        sub = self._pixbuf.new_subpixbuf(int(c.x), int(c.y), int(c.w), int(c.h))
        ox, oy = self._origin()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(self._scale, self._scale)
        _paint_scaled_pixbuf(cr, sub, self._scale)
        cr.restore()

    def _draw_deskew_grid(
        self, cr: cairo.Context, ox: float, oy: float, dw: float, dh: float
    ) -> None:
        """Axis-aligned grid clipped to the spun page so rotated corners stay covered."""
        scale = self._scale if self._scale else 1.0
        step_x = self._deskew_grid_sx * scale
        step_y = self._deskew_grid_sy * scale
        if step_x < 1e-6 or step_y < 1e-6:
            return
        corners = rotated_view_corners(ox, oy, dw, dh, self.visual_deskew_deg)
        left, top, width, height = view_corners_bounds(corners)
        cr.save()
        cr.move_to(*corners[0])
        for x, y in corners[1:]:
            cr.line_to(x, y)
        cr.close_path()
        cr.clip()
        rgba = _DESKEW_GRID_COLORS[
            self._deskew_grid_color_i % len(_DESKEW_GRID_COLORS)
        ]
        cr.set_source_rgba(*rgba)
        cr.set_line_width(2)
        phase_x = ox + self._deskew_grid_ox * scale
        phase_y = oy + self._deskew_grid_oy * scale
        x = phase_x % step_x
        if x > 0:
            x -= step_x
        while x < left:
            x += step_x
        while x <= left + width + 0.5:
            cr.move_to(x, top)
            cr.line_to(x, top + height)
            cr.stroke()
            x += step_x
        y = phase_y % step_y
        if y > 0:
            y -= step_y
        while y < top:
            y += step_y
        while y <= top + height + 0.5:
            cr.move_to(left, y)
            cr.line_to(left + width, y)
            cr.stroke()
            y += step_y

        # Hover highlight (Ctrl).
        if self._deskew_hover is not None:
            axis, _k, pos = self._deskew_hover
            cr.set_source_rgba(1.0, 1.0, 0.2, 0.9)
            cr.set_line_width(3)
            if axis == "v":
                lx, _ = self._img_to_view(pos, 0)
                cr.move_to(lx, top)
                cr.line_to(lx, top + height)
            else:
                _, ly = self._img_to_view(0, pos)
                cr.move_to(left, ly)
                cr.line_to(left + width, ly)
            cr.stroke()

        # Locked lines (thicker + dash).
        cr.set_dash([8, 4])
        cr.set_line_width(4)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        if self._deskew_lock_vx is not None:
            lx, _ = self._img_to_view(self._deskew_lock_vx, 0)
            cr.move_to(lx, top)
            cr.line_to(lx, top + height)
            cr.stroke()
        if self._deskew_lock_hy is not None:
            _, ly = self._img_to_view(0, self._deskew_lock_hy)
            cr.move_to(left, ly)
            cr.line_to(left + width, ly)
            cr.stroke()
        cr.set_dash([])
        cr.restore()

    def _draw_split_guide(
        self, cr: cairo.Context, ox: float, oy: float, dw: float, dh: float
    ) -> None:
        cr.set_source_rgb(0.2, 0.45, 0.95)
        cr.set_line_width(2)
        for pos in self._split_positions:
            if self._split_axis == "horizontal":
                _, ly = self._img_to_view(0, pos)
                cr.move_to(ox, ly)
                cr.line_to(ox + dw, ly)
            else:
                lx, _ = self._img_to_view(pos, 0)
                cr.move_to(lx, oy)
                cr.line_to(lx, oy + dh)
            cr.stroke()
