"""Orchestrate capture → preprocess → crop → finish → deliver for one document."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import NamedTuple

from yokaze.lib.util.errors import CropFailedError, YokazeError
from yokaze.lib.util.types import PageWork
from yokaze.lib.pins import require_clean_for, require_pins_match, stage_pins_repo_root
from yokaze.pipeline.ports.capture import CapturePort
from yokaze.pipeline.ports.crop import CropPort
from yokaze.pipeline.ports.deliver import DeliverPort, DeliverTarget
from yokaze.pipeline.ports.tone import FinishPort
from yokaze.pipeline.ports.preprocess import PreprocessPort
from yokaze.lib.capture import drop_blank_pages

_PAGE_NUM_RE = re.compile(r"page-(\d{4})\.tiff$", re.I)


class CapturedBatch(NamedTuple):
    """Raw pages from one `capture_only()` call, pending `enhance_batch()`."""

    new_pages: list[PageWork]


def _workdir_tiff_paths(workdir: Path) -> list[Path]:
    paths = sorted(workdir.glob("batch-*-page-*.tiff"))
    if not paths:
        paths = sorted(workdir.glob("page-*.tiff"))
    return paths


def _list_workdir_pages(workdir: Path) -> list[PageWork]:
    """Refresh PageWork after filesystem mutations (e.g. blank-page deletes)."""
    return [
        PageWork(path=p, page_num=i)
        for i, p in enumerate(_workdir_tiff_paths(workdir), start=1)
    ]


def archive_raw_pages(dest: Path, pages: list[Path]) -> dict[Path, int]:
    """Copy true-raw scanner TIFFs into dest/raw/, continuing page-NNNN numbering.

    Returns a map of ``src.resolve() → archive page number`` so callers can
    keep ``PageWork.page_num`` aligned with ``raw/page-NNNN`` even when workdir
    files are still named ``batch-NN-page-MMMM``.
    """
    mapping: dict[Path, int] = {}
    if not pages:
        return mapping
    raw_dir = dest / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    next_index = 1
    existing = sorted(raw_dir.glob("page-*.tiff"))
    if existing:
        match = _PAGE_NUM_RE.search(existing[-1].name)
        if match:
            next_index = int(match.group(1)) + 1
    for offset, src in enumerate(pages):
        page_num = next_index + offset
        dest_path = raw_dir / f"page-{page_num:04d}.tiff"
        shutil.copy2(src, dest_path)
        mapping[src.resolve()] = page_num
    source = os.environ.get("SCAN_LAST_SOURCE") or os.environ.get("SCAN_SOURCE")
    if source:
        from yokaze.lib.metadata import set_scan_source

        set_scan_source(dest, source.strip())
    return mapping


def drop_blank_pages_after_capture(pages: list[Path]) -> list[Path]:
    """Delete nearly blank duplex backs among newly captured paths."""
    return drop_blank_pages(pages)


def write_archive_brod_history(archive_dir: Path, pages: list[PageWork]) -> None:
    """Persist preprocess + detect crop onto archive metadata (attempt tip 0).

    Uses ``PageWork.page_num`` (archive ``raw/page-NNNN`` index). Do not parse
    ``batch-NN-page-MMMM`` stems — MMMM is batch-local and collides across batches.
    Call after crop detect / burn-in so ``PageWork.crop`` is available.
    """
    from yokaze.lib.brod.ops import PreprocessMeta
    from yokaze.lib.metadata import record_history

    for page in pages:
        meta = page.brod_meta
        if not isinstance(meta, PreprocessMeta):
            continue
        page_num = int(page.page_num)
        rough = dict(meta.rough) if isinstance(meta.rough, dict) else None
        crop = None
        if page.crop is not None:
            crop = {
                "x": int(page.crop.x),
                "y": int(page.crop.y),
                "w": int(page.crop.w),
                "h": int(page.crop.h),
            }
        record_history(
            archive_dir,
            page_num,
            attempt=0,
            deskew_deg=float(meta.deskew_angle_deg or 0.0),
            rotate=int(meta.rotate) if meta.rotate in (0, 90, 180, 270) else 0,
            rough=rough,
            crop=crop,
        )


class DocumentPipeline:
    def __init__(
        self,
        capture: CapturePort,
        preprocess: PreprocessPort,
        crop: CropPort,
        finish: FinishPort,
        deliver: DeliverPort,
    ):
        self.capture = capture
        self.preprocess = preprocess
        self.crop = crop
        self.finish = finish
        self.deliver = deliver

    def _enhance_pages(
        self,
        pages: list[PageWork],
        *,
        case_slug: str,
        soft_crop: bool = False,
        apply_crop: bool = True,
        archive_dir: Path | None = None,
    ) -> list[PageWork]:
        """Preprocess → crop. Optionally record brod history on the scan archive.

        Corpus brod/ TIFF artifacts stay pin-gated corpus tools only. When
        ``archive_dir`` is set, write archive ``metadata.json`` history tips so
        Stage/Paperless can replay thumbs.

        When ``apply_crop`` is False (Stage tray path), detect the crop rect and
        leave it on ``PageWork.crop`` without rewriting the TIFF — overlays stay
        editable until Send.
        """
        if not pages:
            return pages
        from yokaze.lib.brod.rotation_plan import (
            resolve_rotation_plan,
            resolve_scan_source,
        )

        rotate_plan = resolve_rotation_plan(
            [p.path for p in pages],
            scan_source=resolve_scan_source(),
        )
        pages = self.preprocess.preprocess_pages(
            pages,
            artifact_dir=None,
            case_slug=case_slug,
            rotate_plan=rotate_plan,
        )
        if not apply_crop:
            pages = self._detect_crops(pages, soft_crop=soft_crop)
        else:
            cropped: list[PageWork] = []
            for page in pages:
                try:
                    cropped.append(self.crop.crop_page(page))
                except CropFailedError:
                    if not soft_crop:
                        raise
                    cropped.append(page)
            pages = cropped
        if archive_dir is not None:
            write_archive_brod_history(archive_dir, pages)
        return pages

    def _detect_crops(
        self, pages: list[PageWork], *, soft_crop: bool
    ) -> list[PageWork]:
        """Attach detect_crop rects without mutating page files."""
        out: list[PageWork] = []
        for page in pages:
            try:
                rect, w, h = self.crop.detect_crop(page.path)
                page.preprocess_w = w
                page.preprocess_h = h
                page.crop = rect
            except CropFailedError:
                if not soft_crop:
                    raise
            out.append(page)
        return out

    def capture_only(
        self,
        workdir: Path,
        *,
        color_mode: str,
        save_raw_dir: Path | None = None,
    ) -> CapturedBatch:
        """Capture one batch and archive/blank-drop it. No enhancement yet.

        Returns just the newly captured pages (continuing the page numbering
        already present in ``workdir``) so a caller can start the *next*
        physical scan immediately and hand these off to `enhance_batch()`
        — on a background thread, if desired — without blocking on it.

        Archives are raw-only: this writes ``raw/`` + ``metadata.json``
        (``scan_source``) and nothing else. Preprocess for a scan only gets
        built later, on demand, when it's added to the corpus.
        """
        repo = stage_pins_repo_root()
        require_clean_for("scan", repo=repo)
        require_pins_match(("brod", "crop", "tone"), repo=repo)

        before = {p.resolve() for p in _workdir_tiff_paths(workdir)}
        self.capture.capture_batch(workdir, color_mode=color_mode)

        new_raw = [
            p for p in _workdir_tiff_paths(workdir) if p.resolve() not in before
        ]
        archive_pages: dict[Path, int] = {}
        if save_raw_dir is not None:
            archive_pages = archive_raw_pages(save_raw_dir, new_raw)

        drop_blank_pages_after_capture(new_raw)
        surviving = _list_workdir_pages(workdir)
        new_pages: list[PageWork] = []
        for page in surviving:
            if page.path.resolve() not in before:
                archived = archive_pages.get(page.path.resolve())
                if archived is not None:
                    page.page_num = archived
                new_pages.append(page)
        return CapturedBatch(new_pages=new_pages)

    def enhance_batch(
        self,
        batch: CapturedBatch,
        *,
        case_slug: str,
        apply_crop: bool = True,
        archive_dir: Path | None = None,
    ) -> None:
        """Preprocess/crop a batch from `capture_only()`, in place.

        A page whose crop can't be detected (``soft_crop``) is delivered
        as-is (uncropped) rather than failing the whole scan — a crop miss on
        one page shouldn't block the document. Add it to the corpus later to
        fix the crop; don't let it interrupt delivery now.

        ``apply_crop=False`` keeps brod pixels and stores the detect rect on
        each ``PageWork`` for the Stage tray.
        """
        self._enhance_pages(
            batch.new_pages,
            case_slug=case_slug,
            soft_crop=True,
            apply_crop=apply_crop,
            archive_dir=archive_dir,
        )

    def run_capture_enhance(
        self,
        workdir: Path,
        *,
        color_mode: str,
        case_slug: str,
        save_raw_dir: Path | None = None,
    ) -> list[PageWork]:
        """Capture one batch, enhance only the new pages, leave prior pages alone.

        Synchronous convenience wrapper around `capture_only()` +
        `enhance_batch()` for callers (e.g. the headless scan CLI) that don't
        need to overlap capture and enhancement across batches.
        """
        batch = self.capture_only(
            workdir, color_mode=color_mode, save_raw_dir=save_raw_dir
        )
        self.enhance_batch(
            batch, case_slug=case_slug, archive_dir=save_raw_dir
        )
        return _list_workdir_pages(workdir)

    def finalize_workdir(
        self,
        workdir: Path,
        pdf_out: Path,
        *,
        color_mode: str,
        target: DeliverTarget,
        name: str | None = None,
        case: str | None = None,
    ) -> tuple[Path, str | None]:
        """Tone + PDF + deliver for all pages already enhanced in ``workdir``.

        Returns ``(pdf_path, paperless_task_uuid_or_none)``.
        """
        pages = _list_workdir_pages(workdir)
        if not pages:
            raise YokazeError("Nothing to finalize (no pages captured)")
        previous = os.environ.get("COLOR_MODE")
        os.environ["COLOR_MODE"] = color_mode
        try:
            pdf = self.finish.tone_and_pdf(pages, pdf_out)
        finally:
            if previous is None:
                os.environ.pop("COLOR_MODE", None)
            else:
                os.environ["COLOR_MODE"] = previous
        task_uuid = self.deliver.deliver(pdf, target=target, name=name, case=case)
        return pdf, task_uuid

    def _stage_raw_pages(
        self,
        raw_dir: Path,
        workdir: Path,
        *,
        case_slug: str,
        finish: bool,
        color_mode: str,
    ) -> list[PageWork]:
        """Copy raw → blank-drop → preprocess/crop → optional tone."""
        sources = sorted(
            {p.resolve(): p for p in (*raw_dir.glob("*.tiff"), *raw_dir.glob("*.tif"))}.values(),
            key=lambda p: p.name,
        )
        pages_paths: list[Path] = []
        for i, src in enumerate(sources, start=1):
            dest = workdir / f"page-{i:04d}.tiff"
            shutil.copy2(src, dest)
            pages_paths.append(dest)
        if not pages_paths:
            raise YokazeError(f"no raw TIFFs in {raw_dir}")
        drop_blank_pages_after_capture(pages_paths)
        pages = _list_workdir_pages(workdir)
        self._enhance_pages(
            pages,
            case_slug=case_slug,
            soft_crop=True,
        )
        pages = _list_workdir_pages(workdir)
        if finish:
            previous = os.environ.get("COLOR_MODE")
            os.environ["COLOR_MODE"] = color_mode
            try:
                self.finish.apply_tone(pages)
            finally:
                if previous is None:
                    os.environ.pop("COLOR_MODE", None)
                else:
                    os.environ["COLOR_MODE"] = previous
            pages = _list_workdir_pages(workdir)
        return pages

    def replay_raw_to_page_jpegs(
        self,
        raw_dir: Path,
        out_dir: Path,
        *,
        case_slug: str = "archive",
        color_mode: str = "auto",
        finish: bool = True,
        jpeg_quality: int = 85,
        workdir: Path | None = None,
    ) -> list[Path]:
        """Replay pipeline on raw TIFFs; write page-NNNN.jpg under out_dir.

        ``finish=False``: preprocess + crop only (no color / tone).
        ``finish=True``: also color mode + tone, then JPEG (no PDF).
        """
        import tempfile

        from PIL import Image

        own_workdir = workdir is None
        if workdir is None:
            workdir = Path(tempfile.mkdtemp(prefix="yokaze-preview-"))
        try:
            pages = self._stage_raw_pages(
                raw_dir,
                workdir,
                case_slug=case_slug,
                finish=finish,
                color_mode=color_mode,
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            written: list[Path] = []
            for page in pages:
                jpg = out_dir / f"page-{page.page_num:04d}.jpg"
                with Image.open(page.path) as img:
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    elif img.mode == "L":
                        img = img.convert("RGB")
                    img.save(jpg, format="JPEG", quality=jpeg_quality, optimize=True)
                written.append(jpg)
            return written
        finally:
            if own_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def replay_raw_to_pdf(
        self,
        raw_dir: Path,
        pdf_out: Path,
        *,
        color_mode: str = "auto",
        case_slug: str = "archive",
        workdir: Path | None = None,
    ) -> Path:
        """Replay enhance+finish on corpus raw TIFFs (no capture). Legacy preview PDF."""
        import tempfile

        own_workdir = workdir is None
        if workdir is None:
            workdir = Path(tempfile.mkdtemp(prefix="yokaze-preview-"))
        try:
            pages = self._stage_raw_pages(
                raw_dir,
                workdir,
                case_slug=case_slug,
                finish=False,
                color_mode=color_mode,
            )
            previous = os.environ.get("COLOR_MODE")
            os.environ["COLOR_MODE"] = color_mode
            try:
                return self.finish.tone_and_pdf(pages, pdf_out)
            finally:
                if previous is None:
                    os.environ.pop("COLOR_MODE", None)
                else:
                    os.environ["COLOR_MODE"] = previous
        finally:
            if own_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def run_to_pdf(
        self,
        workdir: Path,
        pdf_out: Path,
        *,
        color_mode: str,
        case_slug: str = "archive",
        save_raw_dir: Path | None = None,
    ) -> Path:
        self.run_capture_enhance(
            workdir,
            color_mode=color_mode,
            case_slug=case_slug,
            save_raw_dir=save_raw_dir,
        )
        previous = os.environ.get("COLOR_MODE")
        os.environ["COLOR_MODE"] = color_mode
        try:
            return self.finish.tone_and_pdf(_list_workdir_pages(workdir), pdf_out)
        finally:
            if previous is None:
                os.environ.pop("COLOR_MODE", None)
            else:
                os.environ["COLOR_MODE"] = previous

    def run_and_deliver(
        self,
        workdir: Path,
        pdf_out: Path,
        *,
        color_mode: str,
        target: DeliverTarget,
        name: str | None = None,
        case: str | None = None,
        case_slug: str = "archive",
        save_raw_dir: Path | None = None,
    ) -> tuple[Path, str | None]:
        self.run_capture_enhance(
            workdir,
            color_mode=color_mode,
            case_slug=case_slug,
            save_raw_dir=save_raw_dir,
        )
        return self.finalize_workdir(
            workdir,
            pdf_out,
            color_mode=color_mode,
            target=target,
            name=name,
            case=case,
        )
