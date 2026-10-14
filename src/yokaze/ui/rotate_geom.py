"""Discrete 0/90/180/270 helpers for Annotate preview vs preprocess coords."""

from __future__ import annotations

from yokaze.ui.models import CropRect

_VALID = frozenset({0, 90, 180, 270})


def norm_rotate(deg: int | None) -> int:
    """Map None/as-is to 0; normalize to {0,90,180,270}."""
    if deg is None:
        return 0
    d = int(deg) % 360
    if d not in _VALID:
        raise ValueError(f"invalid rotate {deg}")
    return d


def preview_spin(gt: int | None, file_rotate: int | None) -> int:
    """Extra CW spin to show on top of the preprocess TIFF."""
    return (norm_rotate(gt) - norm_rotate(file_rotate)) % 360


def step_rotate(current: int | None, *, delta: int, base: int | None = None) -> int:
    """Step GT by ±90. If current unset, start from base (file rotate)."""
    start = norm_rotate(base) if current is None else norm_rotate(current)
    return (start + int(delta)) % 360


def display_size(img_w: int, img_h: int, view_cw: int) -> tuple[int, int]:
    view_cw = norm_rotate(view_cw)
    if view_cw in (90, 270):
        return img_h, img_w
    return img_w, img_h


def file_to_view_xy(
    x: float, y: float, img_w: int, img_h: int, view_cw: int
) -> tuple[float, float]:
    """Map preprocess pixel → display pixel for a CW view spin."""
    view_cw = norm_rotate(view_cw)
    if view_cw == 0:
        return x, y
    if view_cw == 90:
        return img_h - 1 - y, x
    if view_cw == 180:
        return img_w - 1 - x, img_h - 1 - y
    # 270
    return y, img_w - 1 - x


def view_to_file_xy(
    x: float, y: float, img_w: int, img_h: int, view_cw: int
) -> tuple[float, float]:
    """Map display pixel → preprocess pixel (inverse of file_to_view_xy)."""
    view_cw = norm_rotate(view_cw)
    if view_cw == 0:
        return x, y
    if view_cw == 90:
        return y, img_h - 1 - x
    if view_cw == 180:
        return img_w - 1 - x, img_h - 1 - y
    # 270
    return img_w - 1 - y, x


def file_crop_to_view(
    crop: CropRect, img_w: int, img_h: int, view_cw: int
) -> CropRect:
    """Axis-aligned crop in file space → axis-aligned crop in view space."""
    view_cw = norm_rotate(view_cw)
    if view_cw == 0:
        return CropRect(crop.x, crop.y, crop.w, crop.h)
    corners = (
        (crop.x, crop.y),
        (crop.x + crop.w, crop.y),
        (crop.x, crop.y + crop.h),
        (crop.x + crop.w, crop.y + crop.h),
    )
    mapped = [file_to_view_xy(x, y, img_w, img_h, view_cw) for x, y in corners]
    xs = [p[0] for p in mapped]
    ys = [p[1] for p in mapped]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return CropRect(int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0)))


def crop_after_image_cw(
    crop: CropRect, img_w: int, img_h: int, cw: int
) -> CropRect:
    """Map an AABB crop through a CW image rotation; ``img_*`` are pre-rotate dims."""
    cw = norm_rotate(cw)
    if cw == 0:
        return CropRect(crop.x, crop.y, crop.w, crop.h)
    if cw == 90:
        return CropRect(img_h - crop.y - crop.h, crop.x, crop.h, crop.w)
    if cw == 180:
        return CropRect(
            img_w - crop.x - crop.w, img_h - crop.y - crop.h, crop.w, crop.h
        )
    return CropRect(crop.y, img_w - crop.x - crop.w, crop.h, crop.w)


def remap_crop_between_rotates(
    crop: CropRect,
    *,
    cur_w: int,
    cur_h: int,
    from_rotate: int | None,
    to_rotate: int | None,
) -> CropRect:
    """Re-express ``crop`` from ``from_rotate`` frame into ``to_rotate`` (current brod).

    ``cur_w``/``cur_h`` are the *destination* (current preprocess) dimensions.
    """
    spin = (norm_rotate(to_rotate) - norm_rotate(from_rotate)) % 360
    if spin == 0:
        return CropRect(crop.x, crop.y, crop.w, crop.h)
    src_w, src_h = display_size(cur_w, cur_h, spin)
    return crop_after_image_cw(crop, src_w, src_h, spin)


def view_crop_to_file(
    crop: CropRect, img_w: int, img_h: int, view_cw: int
) -> CropRect:
    """Axis-aligned crop in view space → preprocess crop."""
    view_cw = norm_rotate(view_cw)
    if view_cw == 0:
        return CropRect(crop.x, crop.y, crop.w, crop.h)
    corners = (
        (crop.x, crop.y),
        (crop.x + crop.w, crop.y),
        (crop.x, crop.y + crop.h),
        (crop.x + crop.w, crop.y + crop.h),
    )
    mapped = [view_to_file_xy(x, y, img_w, img_h, view_cw) for x, y in corners]
    xs = [p[0] for p in mapped]
    ys = [p[1] for p in mapped]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    out = CropRect(
        int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0))
    )
    return out.clamp(img_w, img_h)
