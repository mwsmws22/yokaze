"""Bisect / multi-cut a Stage crop along view-relative split guides."""

from __future__ import annotations

from typing import Literal

from yokaze.ui.models import CropRect
from yokaze.ui.rotate_geom import display_size, file_crop_to_view, view_crop_to_file

SplitAxis = Literal["horizontal", "vertical"]


def auto_split_axis(view_w: int, view_h: int) -> SplitAxis:
    """Wide or square → vertical guides; tall → horizontal."""
    if int(view_w) >= int(view_h):
        return "vertical"
    return "horizontal"


def largest_gap_midpoint(positions: list[float], *, span: float) -> float:
    """Midpoint of the largest gap between 0, sorted guides, and ``span``."""
    span = float(span)
    if span <= 0:
        return 0.0
    bounds = [0.0, *sorted(float(p) for p in positions), span]
    best_lo, best_hi = 0.0, span
    best = -1.0
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        gap = hi - lo
        if gap > best:
            best = gap
            best_lo, best_hi = lo, hi
    return (best_lo + best_hi) / 2.0


def bisect_crop_for_split(
    crop: CropRect,
    *,
    img_w: int,
    img_h: int,
    view_rotate: int = 0,
    axis: SplitAxis,
    position: float,
) -> tuple[CropRect, CropRect] | None:
    """Split ``crop`` along one guide. See ``split_crop_along_guides``."""
    parts = split_crop_along_guides(
        crop,
        img_w=img_w,
        img_h=img_h,
        view_rotate=view_rotate,
        axis=axis,
        positions=[position],
    )
    if parts is None or len(parts) != 2:
        return None
    return parts[0], parts[1]


def split_crop_along_guides(
    crop: CropRect,
    *,
    img_w: int,
    img_h: int,
    view_rotate: int = 0,
    axis: SplitAxis,
    positions: list[float],
) -> list[CropRect] | None:
    """Split ``crop`` (file space) along full-page guides in view space.

    ``axis`` is view-relative. ``positions`` are guide coordinates in view
    pixels (unsorted OK). Returns N+1 file-space crops (top→bottom or
    left→right), or ``None`` if any cut misses the crop or a part is empty.
    """
    if axis not in ("horizontal", "vertical"):
        raise ValueError(f"invalid split axis {axis!r}")
    if not positions:
        return None
    view_crop = file_crop_to_view(crop, img_w, img_h, view_rotate)
    view_w, view_h = display_size(img_w, img_h, view_rotate)
    cuts = sorted({int(round(p)) for p in positions})
    if axis == "horizontal":
        cuts = [max(0, min(t, view_h)) for t in cuts]
        view_parts = _partition_horizontal(view_crop, cuts)
    else:
        cuts = [max(0, min(t, view_w)) for t in cuts]
        view_parts = _partition_vertical(view_crop, cuts)
    if view_parts is None:
        return None
    out: list[CropRect] = []
    for part in view_parts:
        file_part = view_crop_to_file(part, img_w, img_h, view_rotate).clamp(
            img_w, img_h
        )
        if file_part.w < 1 or file_part.h < 1:
            return None
        out.append(file_part)
    return out


def _partition_horizontal(
    crop: CropRect, cuts: list[int]
) -> list[CropRect] | None:
    y0, y1 = crop.y, crop.y + crop.h
    for t in cuts:
        if t <= y0 or t >= y1:
            return None
    bounds = [y0, *cuts, y1]
    parts: list[CropRect] = []
    for i in range(len(bounds) - 1):
        top, bot = bounds[i], bounds[i + 1]
        if bot - top < 1:
            return None
        parts.append(CropRect(crop.x, top, crop.w, bot - top))
    return parts


def _partition_vertical(
    crop: CropRect, cuts: list[int]
) -> list[CropRect] | None:
    x0, x1 = crop.x, crop.x + crop.w
    for t in cuts:
        if t <= x0 or t >= x1:
            return None
    bounds = [x0, *cuts, x1]
    parts: list[CropRect] = []
    for i in range(len(bounds) - 1):
        left, right = bounds[i], bounds[i + 1]
        if right - left < 1:
            return None
        parts.append(CropRect(left, crop.y, right - left, crop.h))
    return parts
