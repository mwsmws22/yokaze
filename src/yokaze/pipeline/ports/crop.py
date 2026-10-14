from __future__ import annotations
from pathlib import Path
from typing import Protocol
from yokaze.lib.util.types import CropRect, PageWork

class CropPort(Protocol):
    def crop_page(self, page: PageWork) -> PageWork:
        """Detect paper in preprocess-space TIFF, crop in place, set page.crop."""

    def detect_crop(self, image_path: Path) -> tuple[CropRect, int, int]:
        """Return (rect, image_w, image_h) without writing."""
