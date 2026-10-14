"""Solve White % + Brightness so a locked patch reads white with min adjust."""

from __future__ import annotations

import math

import numpy as np

from yokaze.lib.tone.color import tone_bgr
from yokaze.lib.tone.sharpen import sharpen_bgr

# Match Tune White % / Brightness slider ranges.
_WHITE_LO = 40.0
_WHITE_HI = 100.0
_BRIGHT_LO = 0.0
_BRIGHT_HI = 2.0
_WHITE_NEUTRAL = 100.0
_BRIGHT_NEUTRAL = 1.0
# Default lock floor: every pixel channel in the rect must be strictly above this.
_DEFAULT_MIN_WHITE = 250.0


def _clip_rect(
    rect: tuple[int, int, int, int], iw: int, ih: int
) -> tuple[int, int, int, int] | None:
    x, y, w, h = rect
    if w < 1 or h < 1:
        return None
    x0 = max(0, min(x, iw - 1))
    y0 = max(0, min(y, ih - 1))
    x1 = max(x0 + 1, min(x + w, iw))
    y1 = max(y0 + 1, min(y + h, ih))
    return x0, y0, x1 - x0, y1 - y0


def _patch_min(
    bgr: np.ndarray, rect: tuple[int, int, int, int]
) -> float | None:
    """Darkest channel value anywhere in the lock rect (not the mean)."""
    clipped = _clip_rect(rect, bgr.shape[1], bgr.shape[0])
    if clipped is None:
        return None
    x, y, w, h = clipped
    patch = bgr[y : y + h, x : x + w]
    if patch.size == 0:
        return None
    return float(np.min(patch))


def _is_white(min_val: float, floor: float) -> bool:
    return min_val > floor


def _white_deficit(min_val: float, floor: float) -> float:
    """How far the darkest pixel is from clearing the white floor (0 = ok)."""
    # Need min > floor → treat floor+1 as the first acceptable uint8 level.
    return max(0.0, (floor + 1.0) - min_val)


def _adjust_cost(white_pct: float, brightness: float) -> float:
    """Squared distance from neutral knobs — prefer the smallest tweak."""
    dw = (_WHITE_NEUTRAL - white_pct) / (_WHITE_HI - _WHITE_LO)
    db = (brightness - _BRIGHT_NEUTRAL) / (_BRIGHT_HI - _BRIGHT_LO)
    return dw * dw + db * db


def _extract_solve_roi(
    bgr: np.ndarray,
    rect: tuple[int, int, int, int],
    *,
    sharpen_radius: float,
) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    """Crop a padded ROI so tone+sharpen only run on the lock neighbourhood."""
    clipped = _clip_rect(rect, bgr.shape[1], bgr.shape[0])
    if clipped is None:
        return None
    x, y, w, h = clipped
    pad = max(2, int(math.ceil(3.0 * float(sharpen_radius))))
    ih, iw = bgr.shape[:2]
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(iw, x + w + pad)
    y1 = min(ih, y + h + pad)
    roi = np.ascontiguousarray(bgr[y0:y1, x0:x1])
    local = (x - x0, y - y0, w, h)
    return roi, local


def _patch_min_after_knobs(
    roi: np.ndarray,
    local_rect: tuple[int, int, int, int],
    *,
    black_pct: float,
    white_pct: float,
    gamma: float,
    saturation: float,
    brightness: float,
    balance_r: float,
    balance_g: float,
    balance_b: float,
    sharpen_amount: float,
    sharpen_radius: float,
    sharpen_threshold: float,
) -> float | None:
    out = tone_bgr(
        roi,
        black_pct=black_pct,
        white_pct=white_pct,
        gamma=gamma,
        saturation=saturation,
        brightness=brightness,
        balance_r=balance_r,
        balance_g=balance_g,
        balance_b=balance_b,
    )
    out = sharpen_bgr(
        out,
        amount=sharpen_amount,
        radius=sharpen_radius,
        threshold=sharpen_threshold,
    )
    return _patch_min(out, local_rect)


def solve_white_lock_knobs(
    bgr: np.ndarray,
    rect: tuple[int, int, int, int],
    *,
    black_pct: float,
    gamma: float,
    saturation: float,
    sharpen_amount: float,
    sharpen_radius: float,
    sharpen_threshold: float,
    min_white: float = _DEFAULT_MIN_WHITE,
    balance_r: float = 1.0,
    balance_g: float = 1.0,
    balance_b: float = 1.0,
) -> tuple[float, float] | None:
    """Minimal White % / Brightness tweak so every lock-rect pixel is > min_white.

    Other tone/sharpen knobs stay fixed. Among candidates where the darkest
    channel in the rect clears ``min_white``, pick the closest to neutral
    (100%, 1.0). If nothing reaches white, fall back to the least deficit,
    then least adjust.
    """
    floor = float(min_white)
    extracted = _extract_solve_roi(bgr, rect, sharpen_radius=sharpen_radius)
    if extracted is None:
        return None
    roi, local_rect = extracted

    # (deficit, cost, white, bright) — lexicographic best.
    best: tuple[float, float, float, float] | None = None

    for white_pct in np.linspace(_WHITE_HI, _WHITE_LO, 25):
        for brightness in np.linspace(_BRIGHT_LO, _BRIGHT_HI, 21):
            min_val = _patch_min_after_knobs(
                roi,
                local_rect,
                black_pct=black_pct,
                white_pct=float(white_pct),
                gamma=gamma,
                saturation=saturation,
                brightness=float(brightness),
                balance_r=balance_r,
                balance_g=balance_g,
                balance_b=balance_b,
                sharpen_amount=sharpen_amount,
                sharpen_radius=sharpen_radius,
                sharpen_threshold=sharpen_threshold,
            )
            if min_val is None:
                continue
            deficit = (
                0.0 if _is_white(min_val, floor) else _white_deficit(min_val, floor)
            )
            cost = _adjust_cost(float(white_pct), float(brightness))
            cand = (deficit, cost, float(white_pct), float(brightness))
            if best is None or cand < best:
                best = cand

    if best is None:
        return None

    _, _, w0, b0 = best

    for white_pct in np.linspace(
        max(_WHITE_LO, w0 - 3), min(_WHITE_HI, w0 + 3), 13
    ):
        for brightness in np.linspace(
            max(_BRIGHT_LO, b0 - 0.12), min(_BRIGHT_HI, b0 + 0.12), 9
        ):
            min_val = _patch_min_after_knobs(
                roi,
                local_rect,
                black_pct=black_pct,
                white_pct=float(white_pct),
                gamma=gamma,
                saturation=saturation,
                brightness=float(brightness),
                balance_r=balance_r,
                balance_g=balance_g,
                balance_b=balance_b,
                sharpen_amount=sharpen_amount,
                sharpen_radius=sharpen_radius,
                sharpen_threshold=sharpen_threshold,
            )
            if min_val is None:
                continue
            deficit = (
                0.0 if _is_white(min_val, floor) else _white_deficit(min_val, floor)
            )
            cost = _adjust_cost(float(white_pct), float(brightness))
            cand = (deficit, cost, float(white_pct), float(brightness))
            if cand < best:
                best = cand

    assert best is not None
    # Brightness needs ≥2 decimals: rounding 1.04→1.0 can undo a white-enough solve.
    return round(best[2], 1), round(best[3], 2)
