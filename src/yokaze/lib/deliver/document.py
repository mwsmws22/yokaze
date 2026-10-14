"""Deliver an already-built PDF to Paperless, a folder, or NAPS2."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from yokaze.lib.deliver.paperless import post_document
from yokaze.lib.util.errors import DeliverFailedError
from yokaze.lib.util.types import DeliverTarget


class Deliver:
    """Native DeliverPort — no shell adapter."""

    def deliver(
        self,
        pdf: Path,
        *,
        target: DeliverTarget,
        name: str | None = None,
        case: str | None = None,
    ) -> str | None:
        """Deliver PDF. Paperless returns consume task UUID; others return None."""
        del case  # save-case is not a PDF delivery path
        if not pdf.is_file():
            raise DeliverFailedError(f"PDF not found: {pdf}")
        out_name = name or f"scan-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"

        if target == "paperless":
            return post_document(pdf, name=out_name)
        if target == "folder":
            _deliver_folder(pdf, out_name)
            return None
        if target == "naps2":
            _deliver_naps2(pdf, out_name)
            return None
        if target == "save-case":
            raise DeliverFailedError(
                "save-case delivers raw TIFFs, not a PDF (use corpus archive path)"
            )
        raise DeliverFailedError(
            f"Unknown deliver target: {target!r} (expected paperless|naps2|folder)"
        )


def _deliver_folder(pdf: Path, name: str) -> None:
    folder = os.environ.get("FOLDER_DIR", "")
    if not folder:
        raise DeliverFailedError("Error: FOLDER_DIR required for --target folder")
    out_dir = Path(folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{name}.pdf"
    shutil.copy2(pdf, dest)
    print(f"Done: {dest} ({_human_size(dest)})")


def _deliver_naps2(pdf: Path, name: str) -> None:
    out_dir = Path.home() / "Documents" / "scanner-inbox"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{name}.pdf"
    shutil.copy2(pdf, dest)
    print(f"Opening in NAPS2: {dest} ({_human_size(dest)})")
    try:
        subprocess.Popen(
            ["naps2", str(dest)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise DeliverFailedError("naps2 not found on PATH") from exc


def _human_size(path: Path) -> str:
    n = float(path.stat().st_size)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024.0 or unit == "T":
            if unit == "B":
                return f"{int(n)}B"
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"
