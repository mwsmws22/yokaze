"""Stage tab tray — durable under XDG stage-tray/ (quit/F5/reopen safe)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2

from yokaze.lib.util.io import image_size
from yokaze.lib.util.types import CropRect as LibCropRect
from yokaze.lib.util.types import PageWork
from yokaze.pipeline.wiring import default_pipeline
from yokaze.ui.deliveries import Delivery, DeliveryPage, append_delivery
from yokaze.ui.models import CropRect

_PAGE_STEM_RE = re.compile(r"page-(\d+)", re.I)
_MANIFEST = "manifest.json"


class ReplaceDocumentGone(RuntimeError):
    """Paperless has no document for a Restage UUID; Stage may ask to send anyway."""


def block_insert_index(
    order: list[str], moving: list[str], target: str, *, after: bool
) -> int:
    """Where a dragged block should land, counting only pages that stay put."""
    moving_ids = set(moving)
    if target not in order:
        return sum(1 for page_id in order if page_id not in moving_ids)
    insert_at = order.index(target) + (1 if after else 0)
    return sum(
        1
        for index, page_id in enumerate(order)
        if index < insert_at and page_id not in moving_ids
    )


def _default_stage_dir() -> Path:
    from yokaze.lib import paths as app_paths

    return app_paths.resolve_stage_tray_dir()


@dataclass
class StagedPage:
    """One tray page: brod TIFF + delivery overlays (crop/rotate)."""

    id: str
    path: Path
    crop: CropRect
    rotate: int = 0
    color_mode: str = "color"
    image_w: int = 0
    image_h: int = 0
    label: str = ""
    archive: str = ""
    page: int = 0
    finished: bool = False


@dataclass
class StagingTray:
    """Stage page list. Main UI tray is durable; helpers may use ephemeral temps."""

    pages: list[StagedPage] = field(default_factory=list)
    replace_pending: list[str] = field(default_factory=list)
    selected_id: str | None = None
    durable: bool = False
    _root: Path | None = None
    _seq: int = 0
    _undo_delete: list[tuple[int, StagedPage]] = field(default_factory=list)

    def _ensure_root(self) -> Path:
        if self._root is None:
            if self.durable:
                self._root = _default_stage_dir()
                self._root.mkdir(parents=True, exist_ok=True)
            else:
                self._root = Path(tempfile.mkdtemp(prefix="yokaze-tray-"))
        return self._root

    def __len__(self) -> int:
        return len(self.pages)

    def _note_changed(self) -> None:
        if self.durable:
            self.persist()

    def clear(self) -> None:
        self.pages.clear()
        self.replace_pending.clear()
        self.selected_id = None
        self._seq = 0
        self._undo_delete.clear()
        if self._root is not None and self._root.is_dir():
            for child in self._root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        if self.durable:
            self._ensure_root()
            self.persist()
        else:
            if self._root is not None and self._root.is_dir():
                shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def persist(self) -> None:
        """Write manifest (+ keep TIFFs) so quit/F5/reopen restores the tray."""
        if not self.durable:
            return
        root = self._ensure_root()
        pages_out: list[dict[str, Any]] = []
        for p in self.pages:
            try:
                rel = str(p.path.resolve().relative_to(root.resolve()))
            except ValueError as exc:
                raise RuntimeError(
                    f"staged page {p.id} path {p.path} is outside tray root {root}"
                ) from exc
            pages_out.append(
                {
                    "id": p.id,
                    "file": rel,
                    "crop": p.crop.as_dict(),
                    "rotate": int(p.rotate) % 360,
                    "color_mode": p.color_mode,
                    "image_w": int(p.image_w),
                    "image_h": int(p.image_h),
                    "label": p.label,
                    "archive": p.archive,
                    "page": int(p.page),
                    "finished": bool(p.finished),
                }
            )
        payload = {
            "seq": int(self._seq),
            "replace_pending": list(self.replace_pending),
            "selected_id": self.selected_id,
            "pages": pages_out,
        }
        (root / _MANIFEST).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def load_persisted(cls) -> StagingTray:
        """Load durable Stage tray from XDG ``stage-tray/``, or empty durable tray."""
        root = _default_stage_dir()
        tray = cls(durable=True, _root=root)
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / _MANIFEST
        if not manifest.is_file():
            return tray
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return tray
        pages_data = data.get("pages") or []
        if not isinstance(pages_data, list):
            return tray
        tray._seq = int(data.get("seq") or 0)
        tray.replace_pending = [
            str(u) for u in (data.get("replace_pending") or []) if u
        ]
        for entry in pages_data:
            if not isinstance(entry, dict):
                continue
            rel = str(entry.get("file") or "")
            path = root / rel
            if not path.is_file():
                continue
            crop = CropRect.from_dict(entry["crop"]) if entry.get("crop") else None
            w = int(entry.get("image_w") or 0)
            h = int(entry.get("image_h") or 0)
            if w < 1 or h < 1:
                w, h = image_size(path)
            if crop is None:
                crop = CropRect(0, 0, w, h)
            tray.pages.append(
                StagedPage(
                    id=str(entry.get("id") or uuid.uuid4()),
                    path=path,
                    crop=crop.clamp(w, h),
                    rotate=int(entry.get("rotate") or 0) % 360,
                    color_mode=str(entry.get("color_mode") or "color"),
                    image_w=w,
                    image_h=h,
                    label=str(entry.get("label") or ""),
                    archive=str(entry.get("archive") or ""),
                    page=int(entry.get("page") or 0),
                    finished=bool(entry.get("finished")),
                )
            )
        select = data.get("selected_id")
        if select and tray.get(str(select)) is not None:
            tray.selected_id = str(select)
        elif tray.pages:
            tray.selected_id = tray.pages[0].id
        return tray

    def append_workdir(
        self,
        workdir: Path,
        *,
        color_mode: str,
        scan_name: str,
        page_crops: dict[Path, LibCropRect | None] | None = None,
        archive_pages: dict[Path, int] | None = None,
        page_rotates: dict[Path, int] | None = None,
        finished: bool = False,
    ) -> int:
        """Copy enhanced workdir pages onto the end of the tray. Returns count added.

        ``archive_pages`` maps workdir path → archive ``raw/page-NNNN`` index.
        Prefer that over parsing ``batch-NN-page-MMMM`` stems (MMMM is batch-local).
        ``page_rotates`` maps workdir path → Stage overlay rotate (default 0).
        """
        crops = page_crops or {}
        archived = archive_pages or {}
        rotates = page_rotates or {}
        paths = sorted(
            {
                p.resolve(): p
                for p in (*workdir.glob("*.tiff"), *workdir.glob("*.tif"))
            }.values(),
            key=lambda p: p.name,
        )
        root = self._ensure_root()
        added = 0
        for src in paths:
            if src.suffix.lower() not in {".tiff", ".tif"}:
                continue
            page_num = archived.get(src.resolve()) or archived.get(src)
            if page_num is None:
                match = _PAGE_STEM_RE.search(src.stem)
                # Only trust stem page nums for flat page-NNNN names, not batch-*.
                if match and not src.stem.lower().startswith("batch-"):
                    page_num = int(match.group(1))
                else:
                    page_num = added + 1
            self._seq += 1
            dest = root / f"page-{self._seq:04d}{src.suffix.lower()}"
            shutil.copy2(src, dest)
            w, h = image_size(dest)
            lib_crop = (
                crops.get(src.resolve())
                or crops.get(src)
                or crops.get(src.name)  # type: ignore[arg-type]
                or crops.get(src.stem)  # type: ignore[arg-type]
            )
            if lib_crop is not None:
                crop = CropRect(lib_crop.x, lib_crop.y, lib_crop.w, lib_crop.h).clamp(
                    w, h
                )
            else:
                crop = CropRect(0, 0, w, h)
            rotate = int(
                rotates.get(src.resolve(), rotates.get(src, 0))  # type: ignore[arg-type]
            ) % 360
            self.pages.append(
                StagedPage(
                    id=str(uuid.uuid4()),
                    path=dest,
                    crop=crop,
                    rotate=rotate,
                    color_mode=color_mode,
                    image_w=w,
                    image_h=h,
                    label=f"{scan_name} · {src.stem}",
                    archive=scan_name,
                    page=int(page_num),
                    finished=finished,
                )
            )
            added += 1
        if added:
            self._note_changed()
        return added

    def append_staged_page(self, page: StagedPage) -> None:
        self.pages.append(page)
        self._note_changed()

    def add_replace_pending(self, task_uuid: str) -> None:
        if task_uuid and task_uuid not in self.replace_pending:
            self.replace_pending.append(task_uuid)
            self._note_changed()

    def discard_pages(self, page_ids: list[str]) -> None:
        """Drop sent pages. Their files go with them, and they cannot be undone."""
        drop = set(page_ids)
        if not drop:
            return
        gone = [page for page in self.pages if page.id in drop]
        self.pages = [page for page in self.pages if page.id not in drop]
        self._undo_delete.clear()
        if self.selected_id in drop:
            self.selected_id = self.pages[0].id if self.pages else None
        root = self._root.resolve() if self._root is not None else None
        for page in gone:
            self._unlink_owned_tiff(page.path, root)
        self._note_changed()

    def _unlink_owned_tiff(self, path: Path, root: Path | None) -> None:
        if root is None or not path.is_file():
            return
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return
        path.unlink()

    def remove(self, page_id: str) -> StagedPage | None:
        """Remove a page; keep its TIFF so Ctrl+Z can restore it."""
        idx = next((i for i, p in enumerate(self.pages) if p.id == page_id), None)
        if idx is None:
            return None
        page = self.pages.pop(idx)
        self._undo_delete.append((idx, page))
        if self.selected_id == page_id:
            self.selected_id = self.pages[0].id if self.pages else None
        self._note_changed()
        return page

    def undo_delete(self) -> str | None:
        """Restore the most recently deleted page. Returns its id, or None."""
        if not self._undo_delete:
            return None
        idx, page = self._undo_delete.pop()
        if not page.path.is_file():
            return None
        insert_at = max(0, min(idx, len(self.pages)))
        self.pages.insert(insert_at, page)
        self.selected_id = page.id
        self._note_changed()
        return page.id

    def move(self, page_id: str, *, delta: int) -> None:
        self.nudge_block([page_id], delta)

    def nudge_block(self, page_ids: list[str], delta: int) -> None:
        """Slide these pages one slot up or down, keeping gaps between them."""
        if delta == 0:
            return
        selected = set(page_ids)
        if not any(page.id in selected for page in self.pages):
            return
        before = [page.id for page in self.pages]
        if delta > 0:
            self._nudge_block_down(selected)
        else:
            self._nudge_block_up(selected)
        if [page.id for page in self.pages] == before:
            return
        self._note_changed()

    def _nudge_block_down(self, selected: set[str]) -> None:
        index = len(self.pages) - 1
        while index >= 0:
            nxt = index + 1
            if (
                self.pages[index].id in selected
                and nxt < len(self.pages)
                and self.pages[nxt].id not in selected
            ):
                self.pages[index], self.pages[nxt] = self.pages[nxt], self.pages[index]
            index -= 1

    def _nudge_block_up(self, selected: set[str]) -> None:
        index = 0
        while index < len(self.pages):
            prev = index - 1
            if (
                self.pages[index].id in selected
                and prev >= 0
                and self.pages[prev].id not in selected
            ):
                self.pages[index], self.pages[prev] = self.pages[prev], self.pages[index]
            index += 1

    def place_block(self, page_ids: list[str], index: int) -> None:
        """Move these pages, in their current order, so the block starts at index.

        ``index`` counts only pages that are not part of the block. 0 puts the
        block first; ``len(rest)`` appends it.
        """
        wanted = set(page_ids)
        moving = [page for page in self.pages if page.id in wanted]
        if not moving:
            return
        rest = [page for page in self.pages if page.id not in wanted]
        slot = max(0, min(int(index), len(rest)))
        updated = rest[:slot] + moving + rest[slot:]
        if [page.id for page in updated] == [page.id for page in self.pages]:
            return
        self.pages[:] = updated
        self._note_changed()

    def get(self, page_id: str) -> StagedPage | None:
        return next((p for p in self.pages if p.id == page_id), None)

    def replace_with_halves(
        self,
        page_id: str,
        crop_a: CropRect,
        crop_b: CropRect,
    ) -> tuple[str, str]:
        """Replace one page with two halves. See ``replace_with_parts``."""
        ids = self.replace_with_parts(page_id, [crop_a, crop_b])
        return ids[0], ids[1]

    def replace_with_parts(
        self,
        page_id: str,
        crops: list[CropRect],
    ) -> list[str]:
        """Replace one page with N parts sharing the same brod path/overlays base.

        Order is top→bottom or left→right. Returns new ids in that order.
        """
        if len(crops) < 2:
            raise ValueError("replace_with_parts needs at least two crops")
        idx = next((i for i, p in enumerate(self.pages) if p.id == page_id), None)
        if idx is None:
            raise KeyError(f"staged page not found: {page_id}")
        src = self.pages[idx]

        def _part(crop: CropRect) -> StagedPage:
            return StagedPage(
                id=str(uuid.uuid4()),
                path=src.path,
                crop=crop.clamp(src.image_w, src.image_h),
                rotate=src.rotate,
                color_mode=src.color_mode,
                image_w=src.image_w,
                image_h=src.image_h,
                label=src.label,
                archive=src.archive,
                page=src.page,
                finished=src.finished,
            )

        parts = [_part(c) for c in crops]
        self.pages[idx : idx + 1] = parts
        self._note_changed()
        return [p.id for p in parts]

    def delivery_pages(self) -> list[DeliveryPage]:
        return delivery_pages_for(self.pages)


def _rotate_bgr(bgr, rotate: int):
    rotate = int(rotate) % 360
    if rotate == 0:
        return bgr
    if rotate == 90:
        return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    if rotate == 180:
        return cv2.rotate(bgr, cv2.ROTATE_180)
    if rotate == 270:
        return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return bgr


def render_staged_page_bgr(page: StagedPage):
    """Apply crop + rotate overlays to a brod TIFF (no tone)."""
    img = cv2.imread(str(page.path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"cannot read staged page {page.path}")
    h, w = img.shape[:2]
    crop = page.crop.clamp(w, h)
    sliced = img[crop.y : crop.y + crop.h, crop.x : crop.x + crop.w].copy()
    return _rotate_bgr(sliced, page.rotate)


def _current_brod_pin() -> str:
    from yokaze.lib.pins.registry import load_stage_pins, stage_pins_repo_root

    return load_stage_pins(stage_pins_repo_root())["brod"]


def require_paperless_send_gates(*, action: str = "scan") -> None:
    """Fail fast: Paperless config, clean tree, and matching stage pins.

    Call before any rebord / tray work so History (and Stage) can warn without
    burning preprocess time first.
    """
    from yokaze.lib.config.scanner import apply_config, load_config
    from yokaze.lib.pins.policy import require_clean_for, require_pins_match
    from yokaze.lib.pins.registry import stage_pins_repo_root

    cfg = load_config()
    apply_config(cfg)
    if not cfg.is_configured:
        raise RuntimeError(
            "Paperless not configured — set PAPERLESS_URL and PAPERLESS_TOKEN"
        )
    repo = stage_pins_repo_root()
    require_clean_for(action, repo=repo)
    require_pins_match(("brod", "crop", "tone"), repo=repo)


def delivery_pages_for(pages: list[StagedPage]) -> list[DeliveryPage]:
    out: list[DeliveryPage] = []
    for page in pages:
        if not page.archive or page.page < 1:
            raise RuntimeError(
                f"staged page {page.label!r} has no archive ref — cannot record delivery"
            )
        out.append(
            DeliveryPage(
                archive=page.archive,
                page=page.page,
                crop=page.crop,
                rotate=page.rotate,
            )
        )
    return out


def _pages_for_send(
    staged: list[StagedPage], work: Path
) -> tuple[list[PageWork], list[PageWork]]:
    """Crop and rotate each page. The second list still needs tone and sharpen."""
    rendered: list[PageWork] = []
    untuned: list[PageWork] = []
    for i, page in enumerate(staged, start=1):
        bgr = render_staged_page_bgr(page)
        out = work / f"page-{i:04d}.tiff"
        if not cv2.imwrite(str(out), bgr):
            raise RuntimeError(f"failed to write {out}")
        item = PageWork(path=out, page_num=i)
        rendered.append(item)
        if not page.finished:
            untuned.append(item)
    return rendered, untuned


def send_staging_tray(
    tray: StagingTray,
    *,
    name: str | None = None,
    pages: list[StagedPage] | None = None,
    skip_missing_replace: bool = False,
    target: str = "paperless",
) -> str:
    """PDF + deliver for the tray, or for ``pages`` when that is a subset.

    Tone and sharpen run only on pages that are not already finished. A corpus
    page staged from Evaluate is finished; a History page is not.

    Paperless whole-tray sends delete replace-pendings. A subset leaves them, and
    the pages that were not sent. Does not clear the tray. Missing Paperless docs
    abort unless ``skip_missing_replace`` is true (Stage confirmed in a popup).
    """
    from yokaze.lib.deliver import paperless as pl

    if target not in ("paperless", "naps2", "folder"):
        raise ValueError(f"invalid stage deliver target {target!r}")

    staged = list(tray.pages if pages is None else pages)
    if not staged:
        raise RuntimeError("Stage is empty")
    whole_tray = pages is None or {page.id for page in staged} == {
        page.id for page in tray.pages
    }

    if target == "paperless":
        require_paperless_send_gates(action="scan")
        if whole_tray:
            found, missing = pl.resolve_replace_document_ids(tray.replace_pending)
            if missing and not skip_missing_replace:
                listing = "\n".join(f"• {u}" for u in missing)
                raise ReplaceDocumentGone(
                    "Paperless no longer has the document this Stage send would replace:\n"
                    f"{listing}"
                )
            pl.delete_replace_documents(found)
    elif target == "folder":
        folder = os.environ.get("FOLDER_DIR", "").strip()
        if not folder:
            raise RuntimeError("Folder destination is not configured in Settings")

    pipeline = default_pipeline()
    work = Path(tempfile.mkdtemp(prefix="yokaze-stage-send-"))
    try:
        rendered, untuned = _pages_for_send(staged, work)

        color_mode = staged[-1].color_mode
        previous = os.environ.get("COLOR_MODE")
        os.environ["COLOR_MODE"] = color_mode
        try:
            doc_name = name or f"stage-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"
            pdf_out = work / f"{doc_name}.pdf"
            if untuned:
                pipeline.finish.apply_tone(untuned)
            pdf = pipeline.finish.write_pdf(rendered, pdf_out)
            task_uuid = pipeline.deliver.deliver(
                pdf, target=target, name=doc_name  # type: ignore[arg-type]
            )
        finally:
            if previous is None:
                os.environ.pop("COLOR_MODE", None)
            else:
                os.environ["COLOR_MODE"] = previous

        if target == "paperless":
            if not task_uuid:
                raise RuntimeError("Paperless upload returned no task UUID")
            append_delivery(
                Delivery(
                    task_uuid=task_uuid,
                    brod_pin=_current_brod_pin(),
                    pages=delivery_pages_for(staged),
                    name=doc_name,
                )
            )
            return f"Done: {doc_name}.pdf uploaded to Paperless"
        if target == "naps2":
            return f"Done: {doc_name}.pdf sent to NAPS2"
        return f"Done: {doc_name}.pdf saved to folder"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restage_delivery(delivery: Delivery, tray: StagingTray) -> int:
    """Replay archive brod history, apply delivery overlays, append + replace-pending.

    Same tip replay as Paperless thumbs — no live re-brod and no brod-pin gate.
    """
    from yokaze.ui.archive_thumbs import replay_archive_page

    if not delivery.pages:
        return 0
    root = tray._ensure_root()
    added = 0
    for entry in delivery.pages:
        picture = replay_archive_page(entry.archive, entry.page)
        tray._seq += 1
        dest = root / f"page-{tray._seq:04d}.tiff"
        if not cv2.imwrite(str(dest), picture.bgr):
            raise RuntimeError(f"failed to write restaged page {dest}")
        w, h = image_size(dest)
        crop = entry.crop.clamp(w, h)
        tray.pages.append(
            StagedPage(
                id=str(uuid.uuid4()),
                path=dest,
                crop=crop,
                rotate=entry.rotate,
                color_mode="color",
                image_w=w,
                image_h=h,
                label=f"{entry.archive} · page-{entry.page:04d}",
                archive=entry.archive,
                page=entry.page,
                finished=picture.finished,
            )
        )
        added += 1
    if added:
        tray.add_replace_pending(delivery.task_uuid)
        tray._note_changed()
    return added
