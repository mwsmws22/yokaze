"""Unsharp-mask sharpening (OpenCV / NumPy)."""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.util.io import read_bgr, write_image


def sharpen_bgr(
    bgr: np.ndarray,
    *,
    amount: float,
    radius: float,
    threshold: float,
) -> np.ndarray:
    """Classic unsharp mask on an in-memory BGR image; no file I/O.

    ``blurred`` is a Gaussian blur with sigma ``radius``; the sharpened
    output blends the original away from ``blurred`` by ``amount``.
    ``threshold`` suppresses the effect where the local contrast
    (``|bgr - blurred|``) is below it, to avoid amplifying flat-region noise.
    ``amount=0`` is a no-op.
    """
    if amount <= 0:
        return bgr
    blurred = cv2.GaussianBlur(bgr, (0, 0), sigmaX=radius)
    sharpened = cv2.addWeighted(bgr, 1.0 + amount, blurred, -amount, 0)
    if threshold <= 0:
        return sharpened
    low_contrast = (
        cv2.absdiff(bgr, blurred).max(axis=2) < threshold
    )
    out = sharpened.copy()
    out[low_contrast] = bgr[low_contrast]
    return out


def sharpen_pages(tiffs: list[Path]) -> None:
    from yokaze.lib.tone.knobs import load_finish_knobs

    knobs = load_finish_knobs()
    amount = knobs["SHARPEN_AMOUNT"]
    if amount <= 0:
        return
    radius = knobs["SHARPEN_RADIUS"]
    threshold = knobs["SHARPEN_THRESHOLD"]
    t0 = time.perf_counter()
    for tiff in tiffs:
        bgr = read_bgr(tiff)
        out = sharpen_bgr(bgr, amount=amount, radius=radius, threshold=threshold)
        write_image(tiff, out)
    ms = int((time.perf_counter() - t0) * 1000)
    print(f"Sharpen: {len(tiffs)} page(s) in {ms}ms")
