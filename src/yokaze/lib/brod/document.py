"""Native preprocess stage: rough bed + rotate + deskew (before OpenCV crop)."""

from __future__ import annotations

from pathlib import Path

from yokaze.lib.util.types import PageWork
from yokaze.lib.brod.ops import preprocess_page


class Preprocess:
    """Native PreprocessPort — OpenCV imaging + tesseract OSD."""

    def preprocess_pages(
        self,
        pages: list[PageWork],
        *,
        artifact_dir: Path | None,
        case_slug: str,
        rotate_plan: list[int | None] | None = None,
    ) -> list[PageWork]:
        out: list[PageWork] = []
        planned = rotate_plan is not None
        for i, page in enumerate(pages):
            rotate: int | None = None
            if planned and i < len(rotate_plan):
                rotate = rotate_plan[i]
            width, height, meta = preprocess_page(
                page.path,
                rotate=rotate,
                allow_osd=not planned,
                artifact_dir=artifact_dir,
                case_slug=case_slug,
                page_num=page.page_num,
            )
            page.preprocess_w = width
            page.preprocess_h = height
            page.brod_meta = meta
            out.append(page)
        return out
