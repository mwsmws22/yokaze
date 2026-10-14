"""Deskew angle estimation (OpenCV + NAPS2). Apply lives in ``brod.apply``."""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.util.env import env_flag, env_float
from yokaze.lib.util.io import read_bgr, to_gray
from yokaze.lib.brod.apply import apply_deskew_angle
from yokaze.lib.brod.naps2_deskew import naps2_skew_angle


def deskew_engine() -> str:
    """Return ``naps2`` (default) or ``opencv``."""
    eng = os.environ.get("DESKEW_ENGINE", "naps2").strip().lower()
    return eng if eng in {"naps2", "opencv"} else "naps2"


def _estimate_opencv_deskew_angle(
    path: Path, *, threshold_pct: float | None = None
) -> float | None:
    """Legacy minAreaRect estimate (positive = rotate clockwise to straighten)."""
    gray = to_gray(read_bgr(path))
    if threshold_pct is not None:
        thresh_val = int(round(255 * (threshold_pct / 100.0)))
        _, binary = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY)
        work = cv2.bitwise_not(binary)
    else:
        work = cv2.GaussianBlur(gray, (3, 3), 0)
        work = cv2.bitwise_not(
            cv2.adaptiveThreshold(
                work, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
            )
        )

    coords = np.column_stack(np.where(work > 0))
    if coords.shape[0] < 500:
        return None
    pts = coords[:, ::-1].astype(np.float32)
    angle = cv2.minAreaRect(pts)[-1]
    if angle < -45:
        angle = 90.0 + angle
    if abs(angle) < 0.05 or abs(angle) > 40.0:
        return None
    # minAreaRect reports the counter-clockwise correction.
    return -float(angle)


def estimate_deskew_angle(
    path: Path, *, threshold_pct: float | None = None
) -> float | None:
    """Estimate small skew angle in degrees (positive = rotate clockwise to straighten)."""
    if threshold_pct is not None:
        return _estimate_opencv_deskew_angle(path, threshold_pct=threshold_pct)
    if deskew_engine() == "opencv":
        return _estimate_opencv_deskew_angle(path)
    hough = _confident_naps2_angle(path)
    if hough is None:
        return _estimate_opencv_deskew_angle(path)
    if not env_flag("DESKEW_REQUIRE_AGREE", True):
        return hough
    sheet = _estimate_opencv_deskew_angle(path)
    return _sheet_when_hough_disagrees(
        hough,
        sheet,
        agree_deg=env_float("DESKEW_AGREE_DEG", 0.7),
        max_gap_deg=env_float("DESKEW_SHEET_MAX_GAP_DEG", 2.0),
    )


def _confident_naps2_angle(path: Path) -> float | None:
    """Hough angle, or None when the vote is flat or too steep to be skew."""
    gray = to_gray(read_bgr(path))
    # The NAPS2 port keeps NAPS2's counter-clockwise convention.
    angle = -naps2_skew_angle(gray)
    if abs(angle) < 0.05 or abs(angle) > 40.0:
        return None
    return float(angle)


def _sheet_when_hough_disagrees(
    hough: float,
    sheet: float | None,
    *,
    agree_deg: float,
    max_gap_deg: float = 2.0,
) -> float:
    """Keep the Hough angle unless the ink block sits outside the agree window.

    A long paper cut can outvote the text and pull the Hough angle off the
    page. The OpenCV angle follows the ink block. When the two are farther
    apart than ``agree_deg``, or they tilt opposite ways, the block is the
    angle to apply — but only while the gap stays within ``max_gap_deg``. A
    much larger gap means the block found a different structure, so Hough
    stays. Opposite signs disagree even when the numeric gap is still inside
    the agree window.
    """
    if sheet is None:
        return hough
    gap = abs(hough - sheet)
    disagrees = hough * sheet < 0 or gap > agree_deg
    if not disagrees:
        return hough
    if gap > max_gap_deg:
        return hough
    return float(sheet)


def apply_deskew(
    path: Path, *, background: tuple[int, int, int] = (255, 255, 255)
) -> float | None:
    """Estimate then apply deskew; return applied angle or None."""
    angle = estimate_deskew_angle(path)
    if angle is None:
        return None
    apply_deskew_angle(path, angle, background=background)
    return angle


def sheet_deskew_angle(path: Path, threshold: str = "82%") -> float | None:
    """Deskew angle from thresholded sheet silhouette (agree-gate helper)."""
    raw = threshold.strip().rstrip("%")
    try:
        pct = float(raw)
    except ValueError:
        pct = 82.0
    return _estimate_opencv_deskew_angle(path, threshold_pct=pct)
