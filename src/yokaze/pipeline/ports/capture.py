from __future__ import annotations
from pathlib import Path
from typing import Protocol
from yokaze.lib.util.types import PageWork

class CapturePort(Protocol):
    def capture_batch(self, workdir: Path, *, color_mode: str, raw: bool = False) -> list[PageWork]:
        ...
