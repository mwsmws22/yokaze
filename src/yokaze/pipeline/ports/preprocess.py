from __future__ import annotations
from pathlib import Path
from typing import Protocol
from yokaze.lib.util.types import PageWork

class PreprocessPort(Protocol):
    def preprocess_pages(
        self,
        pages: list[PageWork],
        *,
        artifact_dir: Path | None,
        case_slug: str,
        rotate_plan: list[int | None] | None = None,
    ) -> list[PageWork]:
        """Rough bed + deskew + rotate. Stops BEFORE final edge crop. May write brod/."""
