"""Scan archive listing and paths — user-mode History/Send (no lab imports)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable

import yokaze.ui as ui

_PAGE_RE = re.compile(r"(?:^|-)(\d{4})\.tiff$", re.I)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TIFF_GLOB = ("*.tiff", "*.tif", "*.TIFF", "*.TIF")


def count_tiffs(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    names: set[str] = set()
    for pattern in _TIFF_GLOB:
        for p in directory.glob(pattern):
            if p.is_file() and not p.name.endswith("-example.tiff"):
                names.add(p.name)
    return len(names)


def list_archives() -> list[str]:
    """Archive directory names under ARCHIVES, newest first by mtime."""
    root = ui.ARCHIVES
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in dirs]


def archive_info(name: str) -> dict[str, Any]:
    path = ui.ARCHIVES / name
    if not path.is_dir():
        raise FileNotFoundError(f"archive not found: {name}")
    return {
        "name": name,
        "raw_count": count_tiffs(path / "raw"),
        "path": path,
    }


def page_numbers_from_tiffs(paths: Iterable[Path]) -> list[int]:
    pages: set[int] = set()
    for path in paths:
        if path.name.endswith("-example.tiff"):
            continue
        m = _PAGE_RE.search(path.name)
        if m:
            pages.add(int(m.group(1)))
    return sorted(pages)


def iter_dir_tiffs(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for pattern in _TIFF_GLOB:
        for p in sorted(directory.glob(pattern)):
            if not p.is_file() or p.name.endswith("-example.tiff"):
                continue
            if p.name in seen:
                continue
            seen.add(p.name)
            found.append(p)
    return found


def find_page_tiff(directory: Path, page: int) -> Path | None:
    preferred = directory / f"page-{page:04d}.tiff"
    if preferred.is_file():
        return preferred
    for path in iter_dir_tiffs(directory):
        m = _PAGE_RE.search(path.name)
        if m and int(m.group(1)) == page:
            return path
    return None


def list_archive_pages(name: str) -> list[int]:
    """Sorted page numbers for an archive's ``raw/`` TIFFs."""
    path = ui.ARCHIVES / name
    if not path.is_dir():
        raise FileNotFoundError(f"archive not found: {name}")
    return page_numbers_from_tiffs(iter_dir_tiffs(path / "raw"))


def archive_raw_path(name: str, page: int) -> Path | None:
    path = ui.ARCHIVES / name
    if not path.is_dir():
        return None
    return find_page_tiff(path / "raw", page)


def delete_archive(name: str) -> None:
    root = ui.ARCHIVES
    path = root / name
    if not path.is_dir():
        raise FileNotFoundError(f"archive not found: {name}")
    if path.resolve().parent != root.resolve():
        raise ValueError(f"refusing to delete outside archives: {name}")
    shutil.rmtree(path)


def validate_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def duplex_sheet_pages(page: int, available: list[int] | None = None) -> list[int]:
    """Front+back page numbers for the sheet containing ``page``.

    Odd = front, even = back. When ``available`` is set, only pages present in
    that list are returned (e.g. blank back missing from archive listing).
    """
    if page < 1:
        raise ValueError(f"invalid page: {page}")
    front = page if page % 2 == 1 else page - 1
    back = front + 1
    pair = [front, back]
    if available is None:
        return pair
    avail = set(available)
    found = [p for p in pair if p in avail]
    return found if found else ([page] if page in avail else [])
