"""Apply finish (tone + sharpen) knobs to an in-memory BGR crop."""

from __future__ import annotations

import numpy as np

from yokaze.lib.tone.color import tone_bgr
from yokaze.lib.tone.sharpen import sharpen_bgr


def apply_finish_knobs_bgr(bgr: np.ndarray, knobs: dict[str, float]) -> np.ndarray:
    """Tone + sharpen ``bgr`` with an explicit knob dict (no env read).

    Shared by Tune Live paint and static profile panes so both paths use the
    same finishing math. Sharpen is skipped when Amount is 0.
    """
    out = tone_bgr(
        bgr,
        black_pct=knobs["TONE_BLACK_PCT"],
        white_pct=knobs["TONE_WHITE_PCT"],
        gamma=knobs["TONE_GAMMA"],
        contrast=knobs.get("TONE_CONTRAST", 1.0),
        saturation=knobs["TONE_SATURATION"],
        brightness=knobs["TONE_BRIGHTNESS"],
        balance_r=knobs["TONE_BALANCE_R"],
        balance_g=knobs["TONE_BALANCE_G"],
        balance_b=knobs["TONE_BALANCE_B"],
    )
    amount = knobs.get("SHARPEN_AMOUNT", 0.0)
    if amount > 0.0:
        out = sharpen_bgr(
            out,
            amount=amount,
            radius=knobs.get("SHARPEN_RADIUS", 1.0),
            threshold=knobs.get("SHARPEN_THRESHOLD", 0.0),
        )
    return out


def apply_current_finish_bgr(bgr: np.ndarray) -> np.ndarray:
    """Tone + sharpen from the package finish default."""
    from yokaze.lib.tone.knobs import load_finish_knobs

    return apply_finish_knobs_bgr(bgr, load_finish_knobs())
