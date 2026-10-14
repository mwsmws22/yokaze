"""Axis-aligned crop rectangle shared by pipeline and UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
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

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CropRect:
        return cls(int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))
