"""Replay historical brod from raw + stored history figures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yokaze.lib.brod.apply import crop_bgr, deskew_bgr, rotate_bgr
from yokaze.lib.util.io import read_bgr


def replay_brod_bgr(
    raw_path: Path,
    *,
    rough: dict[str, int],
    rotate: int = 0,
    deskew: float = 0.0,
) -> Any:
    """Build brod-space BGR from raw in memory (one read, no temp TIFF)."""
    img = read_bgr(raw_path)
    img = crop_bgr(img, rough["w"], rough["h"], rough["x"], rough["y"])
    if rotate in (90, 180, 270):
        img = rotate_bgr(img, rotate)
    if abs(float(deskew)) >= 0.05:
        img = deskew_bgr(img, float(deskew))
    return img
