"""Native capture stage: SANE batch into workdir TIFFs."""

from __future__ import annotations

from pathlib import Path

from yokaze.lib.util.types import PageWork
from yokaze.lib.capture.device import capture_pages


class Capture:
    """Native CapturePort — scanimage subprocess + fcntl lock, no bash."""

    def capture_batch(
        self,
        workdir: Path,
        *,
        color_mode: str,
        raw: bool = False,
    ) -> list[PageWork]:
        # Color treatment is applied later in finish; capture is always Color.
        _ = color_mode, raw
        pages, _source = capture_pages(workdir)
        return [PageWork(path=p, page_num=i) for i, p in enumerate(pages, start=1)]
