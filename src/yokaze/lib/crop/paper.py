"""Paper-on-bed geometry helpers (OpenCV fuzz-style bbox from corner bed grey)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from yokaze.lib.util.io import image_size, read_bgr, to_gray


def _corner_roi(gray: np.ndarray, gravity: str, size: int = 80) -> np.ndarray:
    height, width = gray.shape[:2]
    size = min(size, width, height)
    if gravity == "NorthWest":
        return gray[0:size, 0:size]
    if gravity == "NorthEast":
        return gray[0:size, width - size : width]
    if gravity == "SouthWest":
        return gray[height - size : height, 0:size]
    if gravity == "SouthEast":
        return gray[height - size : height, width - size : width]
    raise ValueError(f"unknown gravity {gravity!r}")


def corner_gray(tiff: Path, gravity: str) -> float:
    gray = to_gray(read_bgr(tiff))
    return float(np.mean(_corner_roi(gray, gravity)))


def bed_gray(tiff: Path) -> float:
    """Lower-median of the four corner greys (bed estimate)."""
    corners = [
        corner_gray(tiff, "NorthWest"),
        corner_gray(tiff, "NorthEast"),
        corner_gray(tiff, "SouthWest"),
        corner_gray(tiff, "SouthEast"),
    ]
    return sorted(corners)[1]


def _content_extent(
    gray: np.ndarray, fuzz: float, bed: float
) -> tuple[int, int, int, int] | None:
    """``cw, ch, cx, cy`` of pixels outside bed fuzz, including a full-frame sheet.

    None when nothing leaves the bed, or the leftover is only a speck.
    """
    height, width = gray.shape[:2]
    # Magick fuzz is % of quantum range; treat as % of 255 for 8-bit gray.
    tol = max(1.0, (fuzz / 100.0) * 255.0)
    foreground = np.abs(gray.astype(np.float32) - float(bed)) > tol
    if not foreground.any():
        return None
    rows = np.any(foreground, axis=1)
    cols = np.any(foreground, axis=0)
    y_idx = np.where(rows)[0]
    x_idx = np.where(cols)[0]
    if y_idx.size == 0 or x_idx.size == 0:
        return None
    y0, y1 = int(y_idx[0]), int(y_idx[-1])
    x0, x1 = int(x_idx[0]), int(x_idx[-1])
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    if cw < width / 40 or ch < height / 40:
        return None
    return cw, ch, x0, y0


def _fills_frame(cw: int, ch: int, width: int, height: int) -> bool:
    return cw >= width - 4 and ch >= height - 4


def foreground_bbox(
    gray: np.ndarray, fuzz: float, bed: float
) -> tuple[int, int, int, int] | None:
    """Return ``cw, ch, cx, cy`` for pixels not within fuzz of ``bed``, or None.

    A sheet that already reaches every edge is None: there is no bed margin
    to trim. ``sheet_fills_bed`` is that case.
    """
    box = _content_extent(gray, fuzz, bed)
    if box is None:
        return None
    cw, ch, _, _ = box
    height, width = gray.shape[:2]
    if _fills_frame(cw, ch, width, height):
        return None
    return box


def sheet_fills_bed(tiff: Path, bed: float, *, fuzz: float | None = None) -> bool:
    """True when content reaches every edge, so the rough rect is the whole scan."""
    if fuzz is None:
        fuzz = float(os.environ.get("PAPER_CROP_FUZZ", "10"))
    gray = to_gray(read_bgr(tiff))
    box = _content_extent(gray, fuzz, bed)
    if box is None:
        return False
    cw, ch, _, _ = box
    height, width = gray.shape[:2]
    return _fills_frame(cw, ch, width, height)


def paper_bbox(
    tiff: Path, fuzz: float, bed: float
) -> tuple[int, int, int, int] | None:
    """Return ``cw, ch, cx, cy`` for content not within fuzz of bed, or None."""
    return foreground_bbox(to_gray(read_bgr(tiff)), fuzz, bed)


def paper_bbox_trim(tiff: Path, bed: float) -> tuple[int, int, int, int] | None:
    fuzz = float(os.environ.get("PAPER_CROP_FUZZ", "10"))
    bbox = paper_bbox(tiff, fuzz, bed)
    if bbox is None:
        return None
    cw, ch, _, _ = bbox
    if min(cw, ch) < 1400:
        fuzz = float(os.environ.get("PAPER_CROP_FUZZ_SMALL", "8"))
        bbox = paper_bbox(tiff, fuzz, bed)
    return bbox
