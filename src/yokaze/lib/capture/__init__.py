"""Native capture stage (SANE scanimage)."""

from yokaze.lib.capture.document import Capture

# Blank helpers live in brod; re-exported for existing capture-time call sites.
from yokaze.lib.brod.blanks import (
    drop_blank_pages,
    drop_blank_pages_in_workdir,
    is_blank_page,
    page_content_stats,
)

__all__ = [
    "Capture",
    "drop_blank_pages",
    "drop_blank_pages_in_workdir",
    "is_blank_page",
    "page_content_stats",
]
