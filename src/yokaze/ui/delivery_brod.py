"""Pin-gated re-brod of archive raws for Restage (disk-cached).

Thumbs no longer use this module — see ``archive_thumbs`` (history replay).
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from yokaze.ui.deliveries import DeliveryPage

_REBORD_LOCK = threading.Lock()


def current_brod_pin() -> str:
    from yokaze.lib.pins.registry import load_stage_pins, stage_pins_repo_root

    return load_stage_pins(stage_pins_repo_root())["brod"]


def require_matching_brod_pin(delivery_pin: str) -> str:
    current = current_brod_pin()
    if delivery_pin != current:
        raise RuntimeError(
            f"brod pin mismatch for this delivery "
            f"(delivery={delivery_pin[:12]}…, current={current[:12]}…). "
            "Re-brod blocked until pins match."
        )
    return current


def brod_cache_root(brod_pin: str) -> Path:
    from yokaze.lib import paths as app_paths

    return app_paths.resolve_delivery_brod_cache_dir() / brod_pin


def brod_cache_path(brod_pin: str, archive: str, page: int) -> Path:
    return brod_cache_root(brod_pin) / archive / f"page-{page:04d}.tiff"


def cache_tiff_ready(path: Path) -> bool:
    """True only for a complete on-disk TIFF (empty/partial files are misses)."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def ensure_brod_pages(
    pages: list[DeliveryPage],
    *,
    brod_pin: str,
) -> list[tuple[DeliveryPage, Path]]:
    """Return ``(entry, brod_tiff)`` in delivery order; re-brod missing cache entries.

    Used by Restage. ``brod_pin`` is the cache key (usually current after pin gate).
    """
    if not pages:
        return []

    with _REBORD_LOCK:
        by_archive: dict[str, set[int]] = {}
        for entry in pages:
            by_archive.setdefault(entry.archive, set()).add(entry.page)

        for archive, needed in by_archive.items():
            missing = [
                p
                for p in needed
                if not cache_tiff_ready(brod_cache_path(brod_pin, archive, p))
            ]
            if missing:
                _rebord_archive_into_cache(archive, brod_pin)

        out: list[tuple[DeliveryPage, Path]] = []
        for entry in pages:
            path = brod_cache_path(brod_pin, entry.archive, entry.page)
            if not cache_tiff_ready(path):
                raise RuntimeError(
                    f"missing brod cache after rebuild: {entry.archive} page {entry.page}"
                )
            out.append((entry, path))
        return out


def _atomic_copy(src: Path, dest: Path) -> None:
    """Publish ``src`` to ``dest`` without leaving a readable partial TIFF."""
    if not src.is_file() or src.stat().st_size <= 0:
        raise RuntimeError(f"rebord produced empty TIFF: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        shutil.copy2(src, tmp)
        if tmp.stat().st_size <= 0:
            raise RuntimeError(f"rebord copy empty: {tmp}")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _rebord_archive_into_cache(archive: str, brod_pin: str) -> None:
    from yokaze.ui.archive_send import rebord_archive_to_workdir

    cache_dir = brod_cache_root(brod_pin) / archive
    cache_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="yokaze-rebord-") as tmp:
        work = Path(tmp) / "brod"
        works = rebord_archive_to_workdir(archive, work)
        for work_page in works:
            out = cache_dir / f"page-{work_page.page_num:04d}.tiff"
            _atomic_copy(work_page.path, out)
