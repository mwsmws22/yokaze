"""Headless one-button scan (yokaze-scan)."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from yokaze.lib.util.errors import YokazeError
from yokaze.pipeline.ports.deliver import DeliverTarget
from yokaze.pipeline.wiring import default_pipeline


def delivery_target_from_env() -> DeliverTarget:
    """Pick paperless vs folder from the same env rules as legacy delivery_mode()."""
    if os.environ.get("PAPERLESS_URL") and os.environ.get("PAPERLESS_TOKEN"):
        return "paperless"
    if os.environ.get("FOLDER_DIR"):
        return "folder"
    raise YokazeError(
        "Error: no delivery configured. Set PAPERLESS_URL+PAPERLESS_TOKEN or FOLDER_DIR."
    )


def run_headless_scan(name: str | None = None) -> str:
    """Capture one batch, enhance, build PDF, deliver. Returns a Done: status line."""
    color_mode = os.environ.get("COLOR_MODE", "color")
    target = delivery_target_from_env()
    filename = name or f"scan-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"

    pipeline = default_pipeline()
    with tempfile.TemporaryDirectory(prefix="yokaze-") as tmp:
        workdir = Path(tmp)
        pdf_out = workdir / f"{filename}.pdf"
        pipeline.run_and_deliver(
            workdir,
            pdf_out,
            color_mode=color_mode,
            target=target,
            name=filename,
        )

    if target == "paperless":
        msg = f"Done: {filename}.pdf uploaded to Paperless"
    else:
        outfile = Path(os.environ["FOLDER_DIR"]) / f"{filename}.pdf"
        msg = f"Done: {outfile}"
    print(msg)
    return msg


def main(argv: list[str] | None = None) -> int:
    from yokaze.lib.config.scanner import apply_config, load_config

    apply_config(load_config())
    args = list(sys.argv[1:] if argv is None else argv)
    name = args[0] if args else None
    try:
        run_headless_scan(name)
        return 0
    except YokazeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
