from __future__ import annotations
from pathlib import Path
from typing import Protocol
from yokaze.lib.util.types import PageWork

class FinishPort(Protocol):
    def tone_and_pdf(self, pages: list[PageWork], pdf_out: Path) -> Path:
        """Color mode + tone + build PDF."""
