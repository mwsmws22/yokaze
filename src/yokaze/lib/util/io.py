"""Read/write scanned page images (OpenCV)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.util.errors import YokazeError


def read_bgr(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise YokazeError(f"cannot read {path}")
    return img


def write_image(path: Path, bgr: np.ndarray) -> None:
    if not cv2.imwrite(str(path), bgr):
        raise YokazeError(f"cannot write {path}")


def image_size(path: Path) -> tuple[int, int]:
    """Return (width, height) in pixels."""
    img = read_bgr(path)
    height, width = img.shape[:2]
    return width, height


def to_gray(bgr: np.ndarray) -> np.ndarray:
    if bgr.ndim == 2:
        return bgr
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
