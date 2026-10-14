"""DocumentPipeline.enhance_batch(): a bad crop must not fail the scan."""

from __future__ import annotations

from pathlib import Path

import yokaze.lib.brod.rotation_plan as rotation_plan
from yokaze.lib.util.errors import CropFailedError
from yokaze.lib.util.types import PageWork
from yokaze.pipeline.document import CapturedBatch, DocumentPipeline


class _PassthroughPreprocess:
    def preprocess_pages(self, pages, *, artifact_dir, case_slug, rotate_plan=None):
        return pages


class _FailingCrop:
    """Fails to crop pages whose stem is in `bad_stems`; passes the rest through."""

    def __init__(self, bad_stems: set[str]) -> None:
        self.bad_stems = bad_stems
        self.attempted: list[str] = []

    def crop_page(self, page: PageWork) -> PageWork:
        self.attempted.append(page.path.stem)
        if page.path.stem in self.bad_stems:
            raise CropFailedError(f"no confident crop for {page.path.name}")
        return page


class _PassthroughFinish:
    def tone_and_pdf(self, pages, pdf_out):
        pdf_out.write_bytes(b"%PDF-1.4")
        return pdf_out


def _pipeline(crop) -> DocumentPipeline:
    return DocumentPipeline(
        capture=None,  # not exercised by enhance_batch()
        preprocess=_PassthroughPreprocess(),
        crop=crop,
        finish=_PassthroughFinish(),
        deliver=None,  # not exercised by enhance_batch()
    )


def _page(tmp_path: Path, num: int) -> PageWork:
    path = tmp_path / f"page-{num:04d}.tiff"
    path.write_bytes(b"fake-tiff")
    return PageWork(path=path, page_num=num)


def test_enhance_batch_delivers_uncropped_page_on_crop_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rotation_plan, "resolve_rotation_plan", lambda tiffs, **kw: [None] * len(tiffs)
    )

    pages = [_page(tmp_path, 1), _page(tmp_path, 2)]
    crop = _FailingCrop(bad_stems={"page-0001"})
    pipeline = _pipeline(crop)
    batch = CapturedBatch(new_pages=pages)

    # Must not raise, even though page 1's crop fails.
    pipeline.enhance_batch(batch, case_slug="archive")

    assert crop.attempted == ["page-0001", "page-0002"]


def test_enhance_batch_all_pages_fail_crop_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rotation_plan, "resolve_rotation_plan", lambda tiffs, **kw: [None] * len(tiffs)
    )

    pages = [_page(tmp_path, 1)]
    crop = _FailingCrop(bad_stems={"page-0001"})
    pipeline = _pipeline(crop)
    batch = CapturedBatch(new_pages=pages)

    pipeline.enhance_batch(batch, case_slug="archive")

    assert crop.attempted == ["page-0001"]
