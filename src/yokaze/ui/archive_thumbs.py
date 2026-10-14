"""Unified Stage/Paperless thumbs: replay archive brod history from raw.

No live OSD and no brod TIFF cache — missing history is an error.

A corpus send records the case slug in the same archive field. That crop is
in the finished picture, so a missing scan raw falls back to that picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.metadata import latest_history_run, load_metadata, page_key
from yokaze.lib.brod.replay import replay_brod_bgr
from yokaze.ui.models import CropRect
from yokaze.ui.staging_tray import _rotate_bgr

# Generate thumbs denser than on-screen size so GTK CONTAIN / HiDPI downscales
# instead of upscaling a tiny bitmap (blurry tall receipts).
THUMB_MAX_EDGE = 1200


def archive_tree(archive: str, *, archive_dir: Path | None = None) -> Path:
    if archive_dir is not None:
        return archive_dir
    from yokaze import ui as ui_pkg

    return ui_pkg.ARCHIVES / archive


def require_brod_history(tree: Path, page: int) -> dict:
    """Latest history tip with rough (required for replay)."""
    data = load_metadata(tree)
    pages = data.get("pages") or {}
    entry = pages.get(page_key(page)) if isinstance(pages, dict) else None
    if not isinstance(entry, dict):
        raise RuntimeError(f"no archive history for page-{page:04d}")
    run = latest_history_run(entry)
    if not isinstance(run, dict):
        raise RuntimeError(f"no archive history for page-{page:04d}")
    rough = run.get("rough")
    if not isinstance(rough, dict):
        raise RuntimeError(
            f"archive history for page-{page:04d} missing rough (re-scan required)"
        )
    return run


def apply_overlay_bgr(brod: np.ndarray, crop: CropRect, rotate: int) -> np.ndarray:
    h, w = brod.shape[:2]
    c = crop.clamp(w, h)
    sliced = brod[c.y : c.y + c.h, c.x : c.x + c.w].copy()
    return _rotate_bgr(sliced, int(rotate) % 360)


def _corpus_finished_bgr(name: str, page: int) -> np.ndarray | None:
    """Finished page when ``name`` is a corpus case rather than a scan archive.

    Returns None when ``name`` is not a corpus directory. A corpus directory
    with no finished page for ``page`` is an error.
    """
    from yokaze import ui as ui_pkg
    from yokaze.ui.backend import (
        _latest_attempt_for_case,
        load_attempt_finished_bgr,
    )

    if not (ui_pkg.CORPUS / name).is_dir():
        return None
    attempt_id = _latest_attempt_for_case(name)
    bgr = load_attempt_finished_bgr(attempt_id, name, page)
    if bgr is None:
        raise RuntimeError(
            f"attempt-{attempt_id:02d} has no finished page for {name}/page-{page:04d}"
        )
    return bgr


@dataclass(frozen=True)
class ReplayedPicture:
    """Picture a delivery crop is measured on, and whether tone is already baked in."""

    bgr: np.ndarray
    finished: bool


def replay_archive_page(
    archive: str,
    page: int,
    *,
    archive_dir: Path | None = None,
) -> ReplayedPicture:
    """Rebuild the picture a delivery crop is measured on.

    Scan archives replay raw through the latest history tip. Corpus cases
    return the latest attempt's finished page, which is the picture Stage
    stored when the case was sent.
    """
    tree = archive_tree(archive, archive_dir=archive_dir)
    raw = tree / "raw" / f"page-{page:04d}.tiff"
    if not raw.is_file():
        alt = tree / "raw" / f"page-{page:04d}.tif"
        if not alt.is_file():
            if archive_dir is None:
                baked = _corpus_finished_bgr(archive, page)
                if baked is not None:
                    return ReplayedPicture(bgr=baked, finished=True)
            raise RuntimeError(f"missing archive raw: {raw}")
        raw = alt
    run = require_brod_history(tree, page)
    rough = {
        "x": int(run["rough"]["x"]),
        "y": int(run["rough"]["y"]),
        "w": int(run["rough"]["w"]),
        "h": int(run["rough"]["h"]),
    }
    brod = replay_brod_bgr(
        raw,
        rough=rough,
        rotate=int(run.get("rotate") or 0) % 360,
        deskew=float(run.get("deskew") or 0.0),
    )
    if brod is None:
        raise RuntimeError(f"replay failed for {archive} page {page}")
    return ReplayedPicture(bgr=brod, finished=False)


def replay_archive_brod_bgr(
    archive: str,
    page: int,
    *,
    archive_dir: Path | None = None,
) -> np.ndarray:
    """Brod or finished picture, without the finished flag."""
    return replay_archive_page(archive, page, archive_dir=archive_dir).bgr


def thumb_bgr(
    archive: str,
    page: int,
    crop: CropRect,
    rotate: int,
    *,
    archive_dir: Path | None = None,
    max_edge: int | None = None,
) -> np.ndarray:
    """Rebuild brod via history replay, apply overlay crop×rotate, optional scale."""
    brod = replay_archive_brod_bgr(archive, page, archive_dir=archive_dir)
    painted = apply_overlay_bgr(brod, crop, rotate)
    if max_edge is not None:
        painted = scale_bgr(painted, max_edge=max_edge)
    return painted


def scale_bgr(bgr: np.ndarray, *, max_edge: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    scale = max_edge / max(w, h)
    if scale >= 1.0:
        return bgr
    return cv2.resize(
        bgr,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def bgr_to_rgb_bytes(bgr: np.ndarray) -> tuple[bytes, int, int]:
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    h, w, _ = rgb.shape
    return rgb.tobytes(), w, h
