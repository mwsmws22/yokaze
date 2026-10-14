from pathlib import Path

from yokaze import ui
from yokaze.ui import archives


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_list_archive_pages_from_raw(tmp_path: Path, monkeypatch):
    root_archives = tmp_path / "archives"
    name = "scan-a"
    root = root_archives / name
    _touch(root / "raw" / "page-0001.tiff")
    _touch(root / "raw" / "page-0002.tiff")
    # Leftover brod/ from older scans must not drive History listing.
    _touch(root / "brod" / "page-0001.tiff")
    monkeypatch.setattr(ui, "ARCHIVES", root_archives)

    assert archives.list_archive_pages(name) == [1, 2]
    assert archives.archive_raw_path(name, 2) == root / "raw" / "page-0002.tiff"


def test_list_archive_pages_ignores_flat_root(tmp_path: Path, monkeypatch):
    """Archives are raw/-only; leftover flat root TIFFs are ignored."""
    root_archives = tmp_path / "archives"
    name = "scan-c"
    root = root_archives / name
    _touch(root / "batch-01-page-0001.tiff")
    _touch(root / "batch-01-page-0002.tiff")
    monkeypatch.setattr(ui, "ARCHIVES", root_archives)

    assert archives.list_archive_pages(name) == []
    assert archives.archive_raw_path(name, 1) is None


def test_missing_page_returns_none(tmp_path: Path, monkeypatch):
    root_archives = tmp_path / "archives"
    name = "scan-d"
    root = root_archives / name
    _touch(root / "raw" / "page-0001.tiff")
    monkeypatch.setattr(ui, "ARCHIVES", root_archives)

    assert archives.archive_raw_path(name, 9) is None


def test_archive_info_raw_only(tmp_path: Path, monkeypatch):
    root_archives = tmp_path / "archives"
    name = "scan-e"
    root = root_archives / name
    _touch(root / "raw" / "page-0001.tiff")
    _touch(root / "brod" / "page-0001.tiff")
    monkeypatch.setattr(ui, "ARCHIVES", root_archives)

    info = archives.archive_info(name)
    assert info["raw_count"] == 1
    assert "preprocess_count" not in info
    assert info["path"] == root
