"""DocumentPipeline.capture_only(): enforce dirty/pin scan gates once."""

from __future__ import annotations

from pathlib import Path

import pytest
import yokaze.pipeline.document as document
from yokaze.pipeline.document import DocumentPipeline


class _CaptureStub:
    def __init__(self) -> None:
        self.calls = 0

    def capture_batch(self, workdir: Path, *, color_mode: str) -> None:
        self.calls += 1
        page = workdir / f"batch-{self.calls:04d}-page-0001.tiff"
        page.write_bytes(b"fake-tiff")


def _pipeline(capture: _CaptureStub) -> DocumentPipeline:
    return DocumentPipeline(
        capture=capture,
        preprocess=None,
        crop=None,
        finish=None,
        deliver=None,
    )


def test_capture_only_refuses_scan_when_tree_dirty(tmp_path, monkeypatch):
    capture = _CaptureStub()
    pipeline = _pipeline(capture)
    monkeypatch.setattr(document, "drop_blank_pages_after_capture", lambda pages: pages)
    monkeypatch.setattr(
        document,
        "require_clean_for",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dirty tree")),
        raising=False,
    )
    monkeypatch.setattr(document, "require_pins_match", lambda *_a, **_k: None, raising=False)

    with pytest.raises(RuntimeError, match="dirty tree"):
        pipeline.capture_only(tmp_path, color_mode="auto")

    assert capture.calls == 0


def test_capture_only_refuses_scan_when_stage_pins_mismatch(tmp_path, monkeypatch):
    capture = _CaptureStub()
    pipeline = _pipeline(capture)
    monkeypatch.setattr(document, "drop_blank_pages_after_capture", lambda pages: pages)
    monkeypatch.setattr(document, "require_clean_for", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(
        document,
        "require_pins_match",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("pins mismatch")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="pins mismatch"):
        pipeline.capture_only(tmp_path, color_mode="auto")

    assert capture.calls == 0


def test_capture_only_proceeds_when_tree_clean_and_pins_match(tmp_path, monkeypatch):
    capture = _CaptureStub()
    pipeline = _pipeline(capture)
    monkeypatch.setattr(document, "drop_blank_pages_after_capture", lambda pages: pages)
    monkeypatch.setattr(document, "require_clean_for", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(document, "require_pins_match", lambda *_a, **_k: None, raising=False)

    batch = pipeline.capture_only(tmp_path, color_mode="auto")

    assert capture.calls == 1
    assert [page.path.name for page in batch.new_pages] == ["batch-0001-page-0001.tiff"]


def test_capture_only_assigns_sequential_archive_page_nums(tmp_path, monkeypatch):
    """Second batch's batch-*-page-0001 maps to archive page 2, not 1."""
    capture = _CaptureStub()
    pipeline = _pipeline(capture)
    monkeypatch.setattr(document, "drop_blank_pages_after_capture", lambda pages: pages)
    monkeypatch.setattr(document, "require_clean_for", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(document, "require_pins_match", lambda *_a, **_k: None, raising=False)

    archive = tmp_path / "archive"
    work = tmp_path / "work"
    work.mkdir()

    batch1 = pipeline.capture_only(work, color_mode="auto", save_raw_dir=archive)
    batch2 = pipeline.capture_only(work, color_mode="auto", save_raw_dir=archive)

    assert [p.page_num for p in batch1.new_pages] == [1]
    assert [p.page_num for p in batch2.new_pages] == [2]
    assert (archive / "raw" / "page-0001.tiff").is_file()
    assert (archive / "raw" / "page-0002.tiff").is_file()


class _DuplexCaptureStub:
    """Each call writes front+back; backs are blank-dropped after archive."""

    def __init__(self) -> None:
        self.calls = 0

    def capture_batch(self, workdir: Path, *, color_mode: str) -> None:
        self.calls += 1
        for side in (1, 2):
            page = workdir / f"batch-{self.calls:02d}-page-{side:04d}.tiff"
            page.write_bytes(b"fake-tiff")


def test_capture_only_keeps_archive_page_when_blank_back_dropped(tmp_path, monkeypatch):
    """Archived blanks shift raw indices; survivors must keep archive page nums."""
    capture = _DuplexCaptureStub()
    pipeline = _pipeline(capture)

    def drop_backs(pages: list[Path]) -> list[Path]:
        kept: list[Path] = []
        for path in pages:
            if path.name.endswith("page-0002.tiff"):
                path.unlink(missing_ok=True)
            else:
                kept.append(path)
        return kept

    monkeypatch.setattr(document, "drop_blank_pages_after_capture", drop_backs)
    monkeypatch.setattr(document, "require_clean_for", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(document, "require_pins_match", lambda *_a, **_k: None, raising=False)

    archive = tmp_path / "archive"
    work = tmp_path / "work"
    work.mkdir()

    batch1 = pipeline.capture_only(work, color_mode="auto", save_raw_dir=archive)
    batch2 = pipeline.capture_only(work, color_mode="auto", save_raw_dir=archive)

    assert [p.page_num for p in batch1.new_pages] == [1]
    assert [p.page_num for p in batch2.new_pages] == [3]
    assert sorted(p.name for p in (archive / "raw").iterdir()) == [
        "page-0001.tiff",
        "page-0002.tiff",
        "page-0003.tiff",
        "page-0004.tiff",
    ]
