from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DeliverTarget = Literal["paperless", "naps2", "folder", "save-case"]


@dataclass(frozen=True)
class CropRect:
    x: int
    y: int
    w: int
    h: int

    def clamp(self, image_w: int, image_h: int) -> CropRect:
        x = max(0, min(self.x, image_w - 1))
        y = max(0, min(self.y, image_h - 1))
        w = max(1, min(self.w, image_w - x))
        h = max(1, min(self.h, image_h - y))
        return CropRect(x, y, w, h)

@dataclass
class PageWork:
    """One page mid-pipeline (paths are working copies unless noted)."""
    path: Path
    page_num: int
    preprocess_w: int | None = None
    preprocess_h: int | None = None
    crop: CropRect | None = None
    brod_meta: object | None = None  # PreprocessMeta when set by preprocess
