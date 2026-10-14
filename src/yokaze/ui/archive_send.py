"""Prepare + Stage / Paperless send for History archives."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import cv2

from yokaze.lib.brod.blanks import is_blank_page
from yokaze.lib.crop.opencv_crop import OpenCvCrop
from yokaze.lib.util.errors import CropFailedError
from yokaze.lib.util.types import CropRect as LibCropRect
from yokaze.lib.util.types import PageWork
from yokaze.ui.staging_tray import StagingTray, require_paperless_send_gates, send_staging_tray

_PAGE_STEM_RE = re.compile(r"page-(\d+)", re.I)

SendMode = Literal["from_history", "reprocess"]
SendTarget = Literal["paperless", "stage"]


@dataclass
class PreparedArchive:
    """Temp brod workdir + crop/rotate maps for one archive send."""

    archive: str
    workdir: Path
    crops: dict[Path, LibCropRect | None]
    archive_pages: dict[Path, int]
    rotates: dict[Path, int] = field(default_factory=dict)
    _cleanup_root: Path | None = field(default=None, repr=False)

    def cleanup(self) -> None:
        root = self._cleanup_root
        self._cleanup_root = None
        if root is not None and root.is_dir():
            shutil.rmtree(root, ignore_errors=True)


def rebord_archive_to_workdir(archive: str, workdir: Path) -> list[PageWork]:
    """Copy non-blank raws into ``workdir`` and run preprocess in place.

    Caller creates ``workdir``. Blank raws are omitted via ``is_blank_page``
    only — never ``drop_blank_pages``, which deletes paths in place and would
    destroy archive raws. Returns PageWork with archive page numbers.
    """
    from yokaze.lib.brod.rotation_plan import resolve_rotation_plan, resolve_scan_source
    from yokaze.pipeline.wiring import default_pipeline
    from yokaze import ui as ui_pkg

    raw_dir = ui_pkg.ARCHIVES / archive / "raw"
    if not raw_dir.is_dir():
        raise RuntimeError(f"missing archive raw dir: {raw_dir}")
    raws = sorted(
        {
            p.resolve(): p
            for p in (*raw_dir.glob("page-*.tiff"), *raw_dir.glob("page-*.tif"))
        }.values(),
        key=lambda p: p.name,
    )
    if not raws:
        raise RuntimeError(f"no raw pages in {raw_dir}")

    kept = [p for p in raws if not is_blank_page(p)]
    if not kept:
        raise RuntimeError(f"all pages blank in archive {archive}")

    workdir.mkdir(parents=True, exist_ok=True)
    works: list[PageWork] = []
    for src in kept:
        match = _PAGE_STEM_RE.search(src.stem)
        page_num = int(match.group(1)) if match else len(works) + 1
        dest = workdir / src.name
        shutil.copy2(src, dest)
        works.append(PageWork(path=dest, page_num=page_num))

    plan = resolve_rotation_plan(
        [w.path for w in works],
        scan_source=resolve_scan_source(),
    )
    pipeline = default_pipeline()
    pipeline.preprocess.preprocess_pages(
        works,
        artifact_dir=None,
        case_slug="archive",
        rotate_plan=plan,
    )
    return works


def _detect_crops(workdir: Path) -> dict[Path, LibCropRect | None]:
    cropper = OpenCvCrop()
    out: dict[Path, LibCropRect | None] = {}
    paths = sorted(
        {
            p.resolve(): p
            for p in (*workdir.glob("*.tiff"), *workdir.glob("*.tif"))
        }.values(),
        key=lambda p: p.name,
    )
    for path in paths:
        try:
            rect, _w, _h = cropper.detect_crop(path)
            out[path.resolve()] = rect
        except CropFailedError:
            out[path.resolve()] = None
    return out


def prepare_archive_pages(archive: str) -> PreparedArchive:
    """Fresh re-brod + detect crops into a temp workdir (caller must cleanup)."""
    root = Path(tempfile.mkdtemp(prefix="yokaze-history-send-"))
    workdir = root / "brod"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        works = rebord_archive_to_workdir(archive, workdir)
        archive_pages = {w.path.resolve(): int(w.page_num) for w in works}
        crops = _detect_crops(workdir)
        return PreparedArchive(
            archive=archive,
            workdir=workdir,
            crops=crops,
            archive_pages=archive_pages,
            rotates={},
            _cleanup_root=root,
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _content_archive_pages(archive: str) -> list[int]:
    """Page numbers whose raw is not a blank duplex back.

    Capture archives every raw, then drops blanks before writing history.
    Send must skip those backs instead of requiring a history tip they
    never received.
    """
    from yokaze.ui import archives

    pages = archives.list_archive_pages(archive)
    kept: list[int] = []
    for page in pages:
        raw = archives.archive_raw_path(archive, page)
        if raw is not None and is_blank_page(raw):
            continue
        kept.append(page)
    return kept


def _delivery_overlays_for_archive(
    archive: str,
) -> dict[int, tuple[LibCropRect, int]]:
    """Newest delivery crop+rotate per archive page (first hit wins)."""
    from yokaze.ui.deliveries import load_deliveries

    out: dict[int, tuple[LibCropRect, int]] = {}
    for delivery in load_deliveries():
        for entry in delivery.pages:
            if entry.archive != archive or entry.page in out:
                continue
            out[int(entry.page)] = (
                LibCropRect(
                    entry.crop.x, entry.crop.y, entry.crop.w, entry.crop.h
                ),
                int(entry.rotate) % 360,
            )
    return out


def backfill_archive_tip_crops(archive: str) -> dict[int, dict[str, int]]:
    """Write tip ``crop`` for each archive page that already has brod history.

    Prefer latest delivery overlay (matches From history); else detect on the
    tip-replayed brod. Returns page → crop dict written.
    """
    from yokaze.lib.metadata import set_history_tip_crop
    from yokaze.ui import archives
    from yokaze.ui.archive_thumbs import (
        archive_tree,
        replay_archive_brod_bgr,
        require_brod_history,
    )

    pages = archives.list_archive_pages(archive)
    if not pages:
        raise RuntimeError(f"no raw pages in archive {archive}")

    delivery = {p: crop for p, (crop, _rot) in _delivery_overlays_for_archive(archive).items()}
    tree = archive_tree(archive)
    tipped: list[int] = []
    for page in pages:
        try:
            require_brod_history(tree, page)
        except RuntimeError:
            continue
        tipped.append(page)
    if not tipped:
        raise RuntimeError(f"no tip history in archive {archive}")

    written: dict[int, dict[str, int]] = {}

    need_detect: list[int] = [p for p in tipped if p not in delivery]
    detected: dict[int, LibCropRect | None] = {}
    if need_detect:
        root = Path(tempfile.mkdtemp(prefix="yokaze-tip-crop-backfill-"))
        try:
            workdir = root / "brod"
            workdir.mkdir(parents=True)
            path_by_page: dict[int, Path] = {}
            for page in need_detect:
                brod = replay_archive_brod_bgr(archive, page)
                dest = workdir / f"page-{page:04d}.tiff"
                if not cv2.imwrite(str(dest), brod):
                    raise RuntimeError(f"failed to write tip brod {dest}")
                path_by_page[page] = dest.resolve()
            found = _detect_crops(workdir)
            for page, path in path_by_page.items():
                rect = found.get(path)
                if rect is None:
                    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                    if img is not None:
                        hh, ww = img.shape[:2]
                        rect = LibCropRect(0, 0, ww, hh)
                detected[page] = rect
        finally:
            shutil.rmtree(root, ignore_errors=True)

    for page in tipped:
        rect = delivery.get(page) or detected.get(page)
        if rect is None:
            continue
        crop = {"x": int(rect.x), "y": int(rect.y), "w": int(rect.w), "h": int(rect.h)}
        set_history_tip_crop(tree, page, crop)
        written[page] = crop
    return written


def prepare_archive_from_history(archive: str) -> PreparedArchive:
    """Tip-replay brod + saved overlays into a temp workdir (caller must cleanup).

    Brod comes from tip replay (fails if a content page lacks a rough tip).
    Blank duplex backs archived in ``raw/`` are skipped. Crop/rotate
    prefer the latest Paperless delivery for that archive page (same as Restage);
    else tip ``crop`` if present; else live detect on the tip-replayed brod.
    """
    from yokaze.ui import archives
    from yokaze.ui.archive_thumbs import (
        archive_tree,
        replay_archive_brod_bgr,
        require_brod_history,
    )

    if not archives.list_archive_pages(archive):
        raise RuntimeError(f"no raw pages in archive {archive}")
    pages = _content_archive_pages(archive)
    if not pages:
        raise RuntimeError(f"all pages blank in archive {archive}")

    delivery_overlay = _delivery_overlays_for_archive(archive)

    root = Path(tempfile.mkdtemp(prefix="yokaze-history-tip-"))
    workdir = root / "brod"
    workdir.mkdir(parents=True, exist_ok=True)
    crops: dict[Path, LibCropRect | None] = {}
    rotates: dict[Path, int] = {}
    archive_pages: dict[Path, int] = {}
    try:
        tree = archive_tree(archive)
        for page in pages:
            run = require_brod_history(tree, page)
            brod = replay_archive_brod_bgr(archive, page)
            dest = workdir / f"page-{page:04d}.tiff"
            if not cv2.imwrite(str(dest), brod):
                raise RuntimeError(f"failed to write tip brod {dest}")
            key = dest.resolve()
            archive_pages[key] = int(page)
            h, w = brod.shape[:2]

            if page in delivery_overlay:
                crop, rotate = delivery_overlay[page]
                crops[key] = crop
                rotates[key] = rotate
                continue

            rotates[key] = 0
            tip_crop = run.get("crop")
            if isinstance(tip_crop, dict) and all(
                k in tip_crop for k in ("x", "y", "w", "h")
            ):
                crops[key] = LibCropRect(
                    int(tip_crop["x"]),
                    int(tip_crop["y"]),
                    int(tip_crop["w"]),
                    int(tip_crop["h"]),
                )
            else:
                crops[key] = None  # detect below

        # Detect on tip-replayed brod when neither delivery nor tip crop exists.
        need_detect = [p for p, c in crops.items() if c is None]
        if need_detect:
            detected = _detect_crops(workdir)
            for path in need_detect:
                crops[path] = detected.get(path)
                if crops[path] is None:
                    # Soft miss → full page (same as Stage soft crop)
                    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                    if img is not None:
                        hh, ww = img.shape[:2]
                        crops[path] = LibCropRect(0, 0, ww, hh)

        return PreparedArchive(
            archive=archive,
            workdir=workdir,
            crops=crops,
            archive_pages=archive_pages,
            rotates=rotates,
            _cleanup_root=root,
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def stage_archive(
    archive: str,
    tray: StagingTray,
    *,
    color_mode: str = "color",
    mode: SendMode = "from_history",
) -> int:
    """Append whole archive to the staging tray (From history or Reprocess)."""
    if mode == "from_history":
        prepared = prepare_archive_from_history(archive)
    elif mode == "reprocess":
        prepared = prepare_archive_pages(archive)
    else:
        raise ValueError(f"invalid send mode {mode!r}")
    try:
        added = tray.append_workdir(
            prepared.workdir,
            color_mode=color_mode,
            scan_name=archive,
            page_crops=prepared.crops,
            archive_pages=prepared.archive_pages,
            page_rotates=prepared.rotates or None,
        )
        if added == 0:
            raise RuntimeError("Nothing to stage (no pages)")
        return added
    finally:
        prepared.cleanup()


def send_archive_to_paperless(
    archive: str,
    *,
    color_mode: str = "color",
    mode: SendMode = "from_history",
) -> str:
    """Prepare → ephemeral tray → Paperless (always new document)."""
    require_paperless_send_gates(action="Send to Paperless")
    tray = StagingTray()
    try:
        stage_archive(archive, tray, color_mode=color_mode, mode=mode)
        return send_staging_tray(tray, name=archive)
    finally:
        tray.clear()
