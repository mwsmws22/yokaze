"""Frozen brod apply helpers: figures → pixels only.

Do not add estimation here. Changes to warp/rotate/crop apply are breaking for
history replay — they are excluded from the brod stage pin allowlist.

Ndarray cores (`*_bgr`) are the source of truth; path helpers read/write around them.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.util.io import read_bgr, write_image


def crop_bgr(img: np.ndarray, cw: int, ch: int, cx: int, cy: int) -> np.ndarray:
    height, width = img.shape[:2]
    x0 = max(0, min(cx, width - 1))
    y0 = max(0, min(cy, height - 1))
    x1 = max(x0 + 1, min(cx + cw, width))
    y1 = max(y0 + 1, min(cy + ch, height))
    return img[y0:y1, x0:x1].copy()


def rotate_bgr(img: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate by 90 / 180 / 270 degrees (clockwise Magick-compatible)."""
    if degrees == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def deskew_bgr(
    img: np.ndarray,
    angle: float,
    *,
    background: tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """Apply a known deskew angle — never estimates."""
    height, width = img.shape[:2]
    center = (width / 2.0, height / 2.0)
    # Stored angles are clockwise-positive. OpenCV rotates counter-clockwise
    # for a positive angle, so negate here and nowhere else.
    matrix = cv2.getRotationMatrix2D(center, -angle, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_w = int(height * sin + width * cos)
    new_h = int(height * cos + width * sin)
    matrix[0, 2] += (new_w / 2) - center[0]
    matrix[1, 2] += (new_h / 2) - center[1]
    return cv2.warpAffine(
        img,
        matrix,
        (new_w, new_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=background,
    )


def crop_repage(path: Path, cw: int, ch: int, cx: int, cy: int) -> None:
    write_image(path, crop_bgr(read_bgr(path), cw, ch, cx, cy))


def rotate_repage(path: Path, degrees: int) -> None:
    """Rotate by 90 / 180 / 270 degrees (clockwise Magick-compatible)."""
    if degrees not in (90, 180, 270):
        return
    write_image(path, rotate_bgr(read_bgr(path), degrees))


def apply_deskew_angle(
    path: Path,
    angle: float,
    *,
    background: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Apply a known deskew angle in place — never estimates."""
    write_image(
        path,
        deskew_bgr(read_bgr(path), angle, background=background),
    )
