"""Background queue that enhances captured batches while scanning continues.

`DocumentPipeline.capture_only()` returns as soon as the physical SANE scan
finishes, so the scanner is free again immediately. The (much slower)
preprocess/crop enhancement of that batch is handed off here to run on
a background worker pool, so the next batch can be scanned while the previous
one is still being enhanced.

Worker count is controlled by ``SCAN_ENHANCE_WORKERS`` (default 1 =
sequential, matching the legacy one-worker-at-a-time bash queue). Batches are
independent files, so out-of-order completion under N>1 workers is safe.

`enhance_batch()` already treats a failed *crop* as non-fatal (delivers the
page as-is). Anything else raised here is an unexpected bug, not a known
scan-quality issue, so it's both recorded for `wait()` and reported
immediately via ``on_error`` — the caller shouldn't have to wait until
Finish to learn the pipeline is broken.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from yokaze.pipeline.document import CapturedBatch, DocumentPipeline

_ENV_WORKERS = "SCAN_ENHANCE_WORKERS"


def _default_worker_count() -> int:
    try:
        workers = int(os.environ.get(_ENV_WORKERS, "1"))
    except ValueError:
        workers = 1
    return max(1, workers)


class EnhanceQueue:
    """Runs `enhance_batch()` for one document's batches on N worker threads."""

    def __init__(
        self,
        pipeline: DocumentPipeline,
        *,
        case_slug: str,
        max_workers: int | None = None,
        on_error: Callable[[str], None] | None = None,
        apply_crop: bool = True,
        archive_dir: Path | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._case_slug = case_slug
        self._on_error = on_error
        self._apply_crop = apply_crop
        self._archive_dir = archive_dir
        self._max_workers = (
            max_workers if max_workers is not None else _default_worker_count()
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="enhance"
        )
        self._futures: list[Future[None]] = []
        self._batches: list[CapturedBatch] = []
        self._error_lock = threading.Lock()
        self._error: str | None = None
        print(f"[enhance] {self._max_workers} worker(s)", flush=True)

    def set_apply_crop(self, apply_crop: bool) -> None:
        """Toggle burn-in crop for subsequent batches (Stage uses False)."""
        self._apply_crop = apply_crop

    def submit(self, batch: CapturedBatch) -> None:
        """Queue a batch for background enhancement."""
        self._batches.append(batch)
        self._futures.append(self._executor.submit(self._enhance, batch))

    def wait(self) -> str | None:
        """Block until every submitted batch is enhanced; return the first error, if any."""
        for future in self._futures:
            future.result()
        return self._error

    def page_crops(self) -> dict[str, object]:
        """Stem → CropRect recorded on PageWork after enhance (detect or burn-in)."""
        from yokaze.lib.util.types import CropRect as LibCropRect

        out: dict[str, LibCropRect] = {}
        for batch in self._batches:
            for page in batch.new_pages:
                if page.crop is not None:
                    out[page.path.name] = page.crop
                    out[page.path.stem] = page.crop
        return out

    def enhanced_pages(self) -> list[PageWork]:
        """PageWork rows in capture order (archive ``page_num`` + crop)."""
        from yokaze.lib.util.types import PageWork

        pages: list[PageWork] = []
        for batch in self._batches:
            pages.extend(batch.new_pages)
        return pages

    def cancel(self) -> None:
        """Drop any not-yet-started batches (document discarded)."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _enhance(self, batch: CapturedBatch) -> None:
        pages = len(batch.new_pages)
        started = time.monotonic()
        try:
            self._pipeline.enhance_batch(
                batch,
                case_slug=self._case_slug,
                apply_crop=self._apply_crop,
                archive_dir=self._archive_dir,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced via wait(), not raised
            # A failed crop is already handled inside enhance_batch() (soft_crop);
            # anything reaching here is unexpected — log it and flag it now.
            print(f"[enhance] unexpected error: {exc}", flush=True)
            traceback.print_exc()
            with self._error_lock:
                if self._error is None:
                    self._error = str(exc)
            if self._on_error is not None:
                self._on_error(str(exc))
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            print(
                f"[enhance] {pages} page(s) done in {elapsed_ms:.0f} ms "
                f"(workers={self._max_workers})",
                flush=True,
            )
