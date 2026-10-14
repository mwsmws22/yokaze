"""Blank detection and duplex-back drop (BROD · Blank)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.crop.paper import bed_gray, paper_bbox_trim
from yokaze.lib.util.env import env_flag, env_float
from yokaze.lib.util.io import read_bgr, to_gray

# When the paper ROI covers most of the bed, a small printed slip's ink is
# diluted below BLANK_PAGE_MIN_INK. Re-check the densest dark island then.
_BLANK_DILUTE_BBOX_FRAC = 0.35
_INK_ISLAND_BED_DELTA = 12.0
_INK_ISLAND_MIN_AREA = 500


def _stats_on_crop(crop: np.ndarray) -> tuple[float, float, float]:
    std = float(np.std(crop.astype(np.float32)) * 100.0 / 255.0)
    _, th = cv2.threshold(crop, int(0.75 * 255), 255, cv2.THRESH_BINARY)
    # Magick: after 75% threshold, fx:1-mean on binary (ink = dark fraction).
    ink75 = float(1.0 - (np.mean(th) / 255.0))
    # Show-through versos can exceed pale-card ink while still lacking structure;
    # sparse printed labels stay above this edge density (see BLANK_PAGE_MAX_EDGE).
    edges = cv2.Canny(crop, 50, 150)
    edge_frac = float(np.mean(edges > 0))
    return std, ink75, edge_frac


def page_content_stats(tiff: Path) -> tuple[float, float, float] | None:
    """Return (std*100, ink75, edge_frac) on the detected sheet, or None."""
    bed = bed_gray(tiff)
    bbox = paper_bbox_trim(tiff, bed)
    if bbox is None:
        return None
    cw, ch, cx, cy = bbox
    gray = to_gray(read_bgr(tiff))
    height, width = gray.shape[:2]
    x0 = max(0, min(cx, width - 1))
    y0 = max(0, min(cy, height - 1))
    x1 = max(x0 + 1, min(cx + cw, width))
    y1 = max(y0 + 1, min(cy + ch, height))
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    return _stats_on_crop(crop)


def _paper_bbox_frac(tiff: Path) -> float | None:
    bed = bed_gray(tiff)
    bbox = paper_bbox_trim(tiff, bed)
    if bbox is None:
        return None
    gray = to_gray(read_bgr(tiff))
    height, width = gray.shape[:2]
    area = float(height * width)
    if area <= 0:
        return None
    return float(bbox[0] * bbox[1]) / area


def _ink_island_stats(tiff: Path) -> tuple[float, float, float] | None:
    """Stats on the largest dark connected component (ink / slip body)."""
    bed = bed_gray(tiff)
    gray = to_gray(read_bgr(tiff))
    mask = (gray < (bed - _INK_ISLAND_BED_DELTA)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    index = 1 + int(np.argmax(areas))
    x = int(stats[index, cv2.CC_STAT_LEFT])
    y = int(stats[index, cv2.CC_STAT_TOP])
    w = int(stats[index, cv2.CC_STAT_WIDTH])
    h = int(stats[index, cv2.CC_STAT_HEIGHT])
    area = int(stats[index, cv2.CC_STAT_AREA])
    if area < _INK_ISLAND_MIN_AREA or w < 1 or h < 1:
        return None
    crop = gray[y : y + h, x : x + w]
    if crop.size == 0:
        return None
    return _stats_on_crop(crop)


def _looks_blank(stats: tuple[float, float, float]) -> bool:
    max_std = env_float("BLANK_PAGE_MAX_STD", 10.0)
    min_ink = env_float("BLANK_PAGE_MIN_INK", 0.055)
    max_edge = env_float("BLANK_PAGE_MAX_EDGE", 0.012)
    std, ink75, edge_frac = stats
    return std < max_std and ink75 < min_ink and edge_frac < max_edge


def is_blank_page(tiff: Path) -> bool:
    """True when a raw page is a nearly empty duplex back (safe to drop)."""
    stats = page_content_stats(tiff)
    if stats is None:
        return False
    if not _looks_blank(stats):
        return False
    # Huge paper ROI dilutes a small slip (gin CARDNET on an open bed).
    frac = _paper_bbox_frac(tiff)
    dilute = env_float("BLANK_PAGE_DILUTE_BBOX_FRAC", _BLANK_DILUTE_BBOX_FRAC)
    if frac is None or frac < dilute:
        return True
    island = _ink_island_stats(tiff)
    if island is None:
        return True
    return _looks_blank(island)


def partition_blank_pages(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Split ``paths`` into ``(content, blank)`` the way send decides blanks.

    - One page: blank iff ``is_blank_page`` (page-level add to corpus).
    - Several pages (whole doc / sheet): same batch rule as ``drop_blank_pages``
      — if every page looks blank, keep them all as content so a duplex pair
      is not wiped. Mixed batches still mark only the blank-looking sides.
    """
    if not paths:
        return [], []
    if len(paths) == 1:
        path = paths[0]
        if is_blank_page(path):
            return [], [path]
        return [path], []

    content = [path for path in paths if not is_blank_page(path)]
    blank = [path for path in paths if is_blank_page(path)]
    if not blank or not content:
        return list(paths), []
    return content, blank


def drop_blank_pages(paths: list[Path]) -> list[Path]:
    """Delete nearly blank pages; never drop the only kept page(s) in a batch."""
    if not env_flag("BLANK_PAGE_DROP", True):
        return paths
    # Capture never drops a lone page (even if blank); multi-page uses partition.
    if len(paths) <= 1:
        return paths

    content, blank = partition_blank_pages(paths)
    if not blank:
        return paths

    for path in blank:
        path.unlink(missing_ok=True)
    print(f"Dropped {len(blank)} blank page(s)", flush=True)
    return content


def drop_blank_pages_in_workdir(workdir: Path) -> list[Path]:
    """Drop blanks among all batch/page TIFFs currently in workdir."""
    pages = sorted(workdir.glob("batch-*-page-*.tiff"))
    if not pages:
        pages = sorted(workdir.glob("page-*.tiff"))
    if not pages:
        return []
    return drop_blank_pages(pages)
