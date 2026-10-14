"""A sheet that already covers the bed still gets a rough rect: the whole frame."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.brod.ops import estimate_rough_bed


def _write(path: Path, gray: np.ndarray) -> None:
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    cv2.imwrite(str(path), bgr)


def test_sheet_that_fills_the_bed_uses_the_whole_frame(tmp_path: Path) -> None:
    gray = np.full((300, 400), 190, np.uint8)
    gray[110:190, :] = 40
    gray[:, 160:240] = 40
    path = tmp_path / "full.tiff"
    _write(path, gray)

    est = estimate_rough_bed(path)

    assert est is not None
    _bed, x, y, w, h = est
    assert (x, y, w, h) == (0, 0, 400, 300)


def test_empty_bed_has_no_rough_rect(tmp_path: Path) -> None:
    path = tmp_path / "empty.tiff"
    _write(path, np.full((200, 200), 180, np.uint8))

    assert estimate_rough_bed(path) is None


def test_inset_sheet_is_not_the_whole_frame(tmp_path: Path) -> None:
    gray = np.full((800, 800), 190, np.uint8)
    gray[300:500, 300:500] = 40
    path = tmp_path / "inset.tiff"
    _write(path, gray)

    est = estimate_rough_bed(path)

    assert est is not None
    _bed, x, y, w, h = est
    assert (x, y, w, h) != (0, 0, 800, 800)
    assert w < 800 and h < 800
