"""Color mode and tone curves (OpenCV / NumPy)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.util.env import env_flag
from yokaze.lib.util.io import read_bgr, write_image


def apply_color_mode(tiffs: list[Path]) -> None:
    mode = os.environ.get("COLOR_MODE", "color")
    if mode == "color":
        print("Color mode: keeping pages in color.")
        return
    if mode == "gray":
        print("Color mode: converting all pages to grayscale...")
        for tiff in tiffs:
            _to_gray_file(tiff)
        return
    _detect_color_per_page(tiffs)


def _to_gray_file(tiff: Path) -> None:
    bgr = read_bgr(tiff)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    write_image(tiff, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))


def _mean_saturation_pct(bgr: np.ndarray) -> float:
    hsl = cv2.cvtColor(bgr, cv2.COLOR_BGR2HLS)
    # OpenCV HLS: S channel is index 1, range 0–255.
    return float(np.mean(hsl[:, :, 1]) * 100.0 / 255.0)


def _detect_color_per_page(tiffs: list[Path]) -> None:
    print("Detecting color...")
    color_pages = 0
    gray_pages = 0
    for tiff in tiffs:
        sat = _mean_saturation_pct(read_bgr(tiff))
        if sat < 10.0:
            _to_gray_file(tiff)
            gray_pages += 1
        else:
            color_pages += 1

    if gray_pages and color_pages:
        print(f"Mixed: {color_pages} color, {gray_pages} grayscale")
    elif gray_pages:
        print("Converted to grayscale")
    else:
        print("Color detected")


def tone_pages(tiffs: list[Path]) -> None:
    if not env_flag("TONE", True):
        return
    t0 = time.perf_counter()
    for tiff in tiffs:
        _tone_page(tiff)
    ms = int((time.perf_counter() - t0) * 1000)
    print(f"Tone: {len(tiffs)} page(s) in {ms}ms")


def _level_channel(channel: np.ndarray, black_pct: float, white_pct: float) -> np.ndarray:
    """Magick -level black%,white% on a single 8-bit channel."""
    lo = 255.0 * (black_pct / 100.0)
    hi = 255.0 * (white_pct / 100.0)
    if hi <= lo:
        return channel
    out = (channel.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def _apply_gamma(bgr: np.ndarray, gamma: float) -> np.ndarray:
    if abs(gamma - 1.0) < 1e-6:
        return bgr
    inv = 1.0 / gamma
    table = np.array(
        [((i / 255.0) ** inv) * 255.0 for i in range(256)], dtype=np.uint8
    )
    return cv2.LUT(bgr, table)


def _apply_hsv_adjust(
    bgr: np.ndarray, *, saturation: float, brightness: float
) -> np.ndarray:
    """Scale HSV S and V. ``1.0`` on either channel = unchanged."""
    sat_noop = abs(saturation - 1.0) < 1e-6
    bri_noop = abs(brightness - 1.0) < 1e-6
    if sat_noop and bri_noop:
        return bgr
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    if not sat_noop:
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 255)
    if not bri_noop:
        hsv[:, :, 2] = np.clip(hsv[:, :, 2] * brightness, 0, 255)
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _apply_color_balance(
    bgr: np.ndarray, *, balance_b: float, balance_g: float, balance_r: float
) -> np.ndarray:
    """Per-channel RGB gains (OpenCV BGR order). ``1.0`` = unchanged."""
    if (
        abs(balance_b - 1.0) < 1e-6
        and abs(balance_g - 1.0) < 1e-6
        and abs(balance_r - 1.0) < 1e-6
    ):
        return bgr
    out = bgr.astype(np.float32)
    out[:, :, 0] = np.clip(out[:, :, 0] * balance_b, 0, 255)
    out[:, :, 1] = np.clip(out[:, :, 1] * balance_g, 0, 255)
    out[:, :, 2] = np.clip(out[:, :, 2] * balance_r, 0, 255)
    return out.astype(np.uint8)


def _apply_contrast(bgr: np.ndarray, contrast: float) -> np.ndarray:
    """Pivot contrast around mid-gray (128). ``1.0`` = unchanged.

    ``contrast > 1`` expands away from mid-gray; ``< 1`` compresses toward it.
    Pointwise — safe to score on patch medians.
    """
    if abs(contrast - 1.0) < 1e-6:
        return bgr
    out = bgr.astype(np.float32)
    out = (out - 128.0) * contrast + 128.0
    return np.clip(out, 0, 255).astype(np.uint8)


def tone_bgr(
    bgr: np.ndarray,
    *,
    black_pct: float,
    white_pct: float,
    gamma: float,
    saturation: float = 1.0,
    brightness: float = 1.0,
    contrast: float = 1.0,
    balance_r: float = 1.0,
    balance_g: float = 1.0,
    balance_b: float = 1.0,
) -> np.ndarray:
    """Apply the tone curve to an in-memory BGR image; no file I/O.

    RGB per-channel level + gamma + contrast + color balance + optional HSV
    saturation/brightness — same math as the real pipeline (see
    ``_tone_page``), parameterized so callers (e.g. a live-tuning UI) can
    pass explicit values instead of environment knobs.
    """
    channels = cv2.split(bgr)
    leveled = [_level_channel(ch, black_pct, white_pct) for ch in channels]
    toned = _apply_gamma(cv2.merge(leveled), gamma)
    toned = _apply_contrast(toned, contrast)
    toned = _apply_color_balance(
        toned, balance_b=balance_b, balance_g=balance_g, balance_r=balance_r
    )
    return _apply_hsv_adjust(toned, saturation=saturation, brightness=brightness)


def _tone_page(tiff: Path) -> None:
    if not env_flag("TONE", True):
        return
    from yokaze.lib.tone.knobs import load_finish_knobs

    knobs = load_finish_knobs()
    bgr = read_bgr(tiff)
    write_image(
        tiff,
        tone_bgr(
            bgr,
            black_pct=knobs["TONE_BLACK_PCT"],
            white_pct=knobs["TONE_WHITE_PCT"],
            gamma=knobs["TONE_GAMMA"],
            contrast=knobs["TONE_CONTRAST"],
            saturation=knobs["TONE_SATURATION"],
            brightness=knobs["TONE_BRIGHTNESS"],
            balance_r=knobs["TONE_BALANCE_R"],
            balance_g=knobs["TONE_BALANCE_G"],
            balance_b=knobs["TONE_BALANCE_B"],
        ),
    )
