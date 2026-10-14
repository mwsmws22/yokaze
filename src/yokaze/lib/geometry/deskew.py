"""Axis-aligned crop projected into another small deskew angle."""

from __future__ import annotations

import math

from yokaze.lib.geometry.crop_rect import CropRect

DESKEW_STEP_DEG = 0.05
DESKEW_PASS_DEG = 0.2
DESKEW_WARN_DEG = 0.5


def round_deskew(value: float) -> float:
    """Nearest 0.05°, stored with two decimals."""
    stepped = round(float(value) / DESKEW_STEP_DEG) * DESKEW_STEP_DEG
    return round(stepped, 2)


def deskew_band(abs_delta_deg: float) -> str:
    """``pass`` ≤ 0.2°, ``warn`` ≤ 0.5°, else ``fail``. Warn does not fail a page.

    Round to two decimals before the cut so float noise at 0.20° cannot flip the band.
    """
    delta = round(abs(float(abs_delta_deg)), 2)
    if delta <= DESKEW_PASS_DEG:
        return "pass"
    if delta <= DESKEW_WARN_DEG:
        return "warn"
    return "fail"


def project_crop(
    crop: CropRect, delta_deg: float, image_w: int, image_h: int
) -> CropRect:
    """Rotate ``crop`` by ``delta_deg`` around the image center and take the hull.

    ``delta_deg`` is the destination angle minus the angle the box is already
    aligned to. Zero leaves the box unchanged. The hull is clamped to the image.
    """
    if image_w < 1 or image_h < 1 or abs(float(delta_deg)) < 1e-9:
        return crop.clamp(image_w, image_h) if image_w >= 1 and image_h >= 1 else crop

    cx = image_w / 2.0
    cy = image_h / 2.0
    rad = math.radians(float(delta_deg))
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    corners = (
        (crop.x, crop.y),
        (crop.x + crop.w, crop.y),
        (crop.x + crop.w, crop.y + crop.h),
        (crop.x, crop.y + crop.h),
    )
    xs: list[float] = []
    ys: list[float] = []
    for x, y in corners:
        dx = x - cx
        dy = y - cy
        xs.append(cx + dx * cos_a - dy * sin_a)
        ys.append(cy + dx * sin_a + dy * cos_a)
    x0 = min(xs)
    y0 = min(ys)
    x1 = max(xs)
    y1 = max(ys)
    return CropRect(
        int(math.floor(x0)),
        int(math.floor(y0)),
        max(1, int(math.ceil(x1 - x0))),
        max(1, int(math.ceil(y1 - y0))),
    ).clamp(image_w, image_h)


def shift_crop_with_canvas(
    crop: CropRect,
    old_w: int,
    old_h: int,
    new_w: int,
    new_h: int,
) -> CropRect:
    """Move a box with the picture center. Width and height stay put.

    A republish that changes the baked deskew also changes the canvas. The
    ground-truth angle is unchanged, and the view spin from the baked angle
    to that angle cancels the extra rotation, so the rectangle only follows
    the center. The result is clamped to the new picture.
    """
    dx = int(round((new_w - old_w) / 2))
    dy = int(round((new_h - old_h) / 2))
    return CropRect(crop.x + dx, crop.y + dy, crop.w, crop.h).clamp(new_w, new_h)
