"""Preprocess ops: rough bed crop, deskew, OSD rotate, artifact write."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yokaze.lib.crop.paper import bed_gray, paper_bbox_trim, sheet_fills_bed
from yokaze.lib.brod.paths import ARTIFACT_DIRNAME
from yokaze.lib.brod.apply import apply_deskew_angle, crop_repage, rotate_repage
from yokaze.lib.brod.geometry import (
    deskew_engine,
    estimate_deskew_angle,
    sheet_deskew_angle,
)
from yokaze.lib.brod.osd import page_orientation
from yokaze.lib.util.env import env_flag, env_float
from yokaze.lib.util.io import image_size


@dataclass
class PreprocessMeta:
    bed_gray: float | None = None
    deskew_angle_deg: float = 0.0
    deskewed: bool = False
    rotate: int = 0  # discrete bake into TIFF: 0|90|180|270
    rough: dict[str, int] | None = field(default=None)


def resolution() -> int:
    raw = os.environ.get("RESOLUTION") or os.environ.get("SCAN_RESOLUTION", "300")
    try:
        return int(raw)
    except ValueError:
        return 300


def estimate_rough_bed(tiff: Path) -> tuple[float, int, int, int, int] | None:
    """Bed sample + padded rough AABB in raw space; does not mutate ``tiff``."""
    bed = bed_gray(tiff)
    width, height = image_size(tiff)
    bbox = paper_bbox_trim(tiff, bed)
    if bbox is None:
        # The sheet already covers the bed. Replay still needs a rough rect,
        # and that rect is the whole raw frame.
        if sheet_fills_bed(tiff, bed):
            return bed, 0, 0, width, height
        return None
    cw, ch, cx, cy = bbox
    pad_mm = env_float("ROUGH_CROP_PAD_MM", 18.0)
    pad = int(resolution() * pad_mm / 25.4 + 0.5)
    cx = max(0, cx - pad)
    cy = max(0, cy - pad)
    x2 = min(width, cx + cw + 2 * pad)
    y2 = min(height, cy + ch + 2 * pad)
    cw = x2 - cx
    ch = y2 - cy
    if cw < 8 or ch < 8:
        return None
    return bed, cx, cy, cw, ch


def rough_bed_crop(tiff: Path, meta: PreprocessMeta) -> None:
    """Generous fuzz-trim against bed grey, then pad so bed fringe remains."""
    est = estimate_rough_bed(tiff)
    if est is None:
        return
    bed, cx, cy, cw, ch = est
    meta.bed_gray = bed
    meta.rough = {"x": cx, "y": cy, "w": cw, "h": ch}
    width, height = image_size(tiff)
    if cx == 0 and cy == 0 and cw == width and ch == height:
        return
    crop_repage(tiff, cw, ch, cx, cy)


def deskew(tiff: Path, meta: PreprocessMeta) -> None:
    """Deskew via DESKEW_ENGINE (default naps2).

    The naps2 engine picks the OpenCV angle when the two estimates disagree.
    That choice lives in ``estimate_deskew_angle``. The opencv engine skips
    the warp instead, when its text and sheet angles disagree.
    """
    meta.deskewed = False
    meta.deskew_angle_deg = 0.0
    if not env_flag("AUTO_DESKEW", True):
        return

    text = estimate_deskew_angle(tiff)
    if text is None:
        return
    if deskew_engine() == "opencv" and env_flag("DESKEW_REQUIRE_AGREE", True):
        threshold = os.environ.get("DESKEW_THRESHOLD", "82%")
        sheet = sheet_deskew_angle(tiff, threshold)
        if sheet is not None:
            agree = env_float("DESKEW_AGREE_DEG", 0.7)
            if abs(text - sheet) > agree:
                return

    apply_deskew_angle(tiff, text)
    meta.deskewed = True
    meta.deskew_angle_deg = text


def gated_page_rotation(tiff: Path, min_confidence: float) -> int | None:
    """Adaptive (+ optional 4-way) OSD. ``tiff`` should already be rough-cropped."""
    return page_orientation(tiff, min_confidence)


def orient_cropped_page(
    tiff: Path,
    rotate: int | None = None,
    *,
    allow_osd: bool = True,
    meta: PreprocessMeta | None = None,
) -> int:
    """Apply planned rotate, or adaptive OSD on the cropped sheet.

    Live enhance always passes a document plan (``allow_osd=False``): ``None``
    means leave as-is — no second OSD and no aspect-ratio guess.

    Returns the discrete rotate baked into the TIFF (0 if as-is).
    """
    applied = 0
    if not env_flag("AUTO_ROTATE", True):
        if meta is not None:
            meta.rotate = 0
        return 0

    rot: int | None
    if rotate is not None:
        rot = rotate
    elif allow_osd:
        min_conf = env_float("ROTATE_MIN_CONFIDENCE_CROP", 1.0)
        rot = gated_page_rotation(tiff, min_conf)
    else:
        if meta is not None:
            meta.rotate = 0
        return 0

    if rot in (90, 180, 270):
        rotate_repage(tiff, rot)
        applied = int(rot)
    if meta is not None:
        meta.rotate = applied
    return applied


def write_preprocess_artifact(
    tiff: Path,
    artifact_dir: Path,
    case_slug: str,
    page_num: int,
    meta: PreprocessMeta,
    *,
    raw_ref: str = "",
) -> tuple[int, int, dict]:
    """Copy working TIFF into artifact_dir/brod/; return size + meta facts.

    Does not write a JSON sidecar — callers persist facts in ``metadata.json``.
    """
    del case_slug  # call-site compatibility
    prep_dir = artifact_dir / ARTIFACT_DIRNAME
    prep_dir.mkdir(parents=True, exist_ok=True)
    page_pad = f"{page_num:04d}"
    out = prep_dir / f"page-{page_pad}.tiff"
    shutil.copy2(tiff, out)
    width, height = image_size(out)
    file_rotate = int(meta.rotate) if meta.rotate in (0, 90, 180, 270) else 0
    file_meta: dict[str, Any] = {
        "image_w": width,
        "image_h": height,
        "bed_gray": meta.bed_gray,
        "deskewed": bool(meta.deskewed),
        "raw": raw_ref,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deskew_angle_deg": float(meta.deskew_angle_deg or 0.0),
        "rotate": file_rotate,
    }
    if isinstance(meta.rough, dict):
        file_meta["rough"] = dict(meta.rough)
    return width, height, file_meta


def preprocess_page(
    tiff: Path,
    *,
    rotate: int | None = None,
    allow_osd: bool = True,
    artifact_dir: Path | None = None,
    case_slug: str = "archive",
    page_num: int = 0,
    raw_ref: str = "",
) -> tuple[int, int, PreprocessMeta]:
    """Rough → orient → deskew. Stops before final OpenCV crop.

    Deskew runs after OSD so NAPS2 letter-bottom Hough sees upright text.
    Always returns ``PreprocessMeta``. With ``artifact_dir``, also writes brod/.
    """
    meta = PreprocessMeta()
    rough_bed_crop(tiff, meta)
    orient_cropped_page(tiff, rotate=rotate, allow_osd=allow_osd, meta=meta)
    deskew(tiff, meta)
    if artifact_dir is not None and page_num > 0:
        write_preprocess_artifact(
            tiff,
            artifact_dir,
            case_slug,
            page_num,
            meta,
            raw_ref=raw_ref,
        )
    width, height = image_size(tiff)
    return width, height, meta
