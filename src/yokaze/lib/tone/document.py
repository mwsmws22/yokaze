"""Native finish stage: color mode, tone, PDF build."""

from __future__ import annotations

from pathlib import Path

from yokaze.lib.deliver.pdf import build_pdf
from yokaze.lib.tone.color import apply_color_mode, tone_pages
from yokaze.lib.tone.sharpen import sharpen_pages
from yokaze.lib.util.errors import YokazeError
from yokaze.lib.util.types import PageWork


class Finish:
    """Native FinishPort — in-process imaging (OpenCV / Pillow)."""

    def apply_tone(self, pages: list[PageWork]) -> list[PageWork]:
        """Color mode + tone curves in place (no PDF)."""
        if not pages:
            return pages
        paths = [p.path for p in pages]
        apply_color_mode(paths)
        tone_pages(paths)
        sharpen_pages(paths)
        return pages

    def write_pdf(self, pages: list[PageWork], pdf_out: Path) -> Path:
        """Build a PDF from pages that are already tone and sharpen."""
        if not pages:
            raise YokazeError("write_pdf: no pages")
        stage_pins = None
        try:
            from yokaze.lib.pins.registry import load_stage_pins, stage_pins_repo_root

            stage_pins = load_stage_pins(stage_pins_repo_root())
        except RuntimeError:
            pass
        build_pdf(pdf_out, [p.path for p in pages], stage_pins=stage_pins)
        return pdf_out

    def tone_and_pdf(self, pages: list[PageWork], pdf_out: Path) -> Path:
        if not pages:
            raise YokazeError("tone_and_pdf: no pages")
        self.apply_tone(pages)
        return self.write_pdf(pages, pdf_out)
