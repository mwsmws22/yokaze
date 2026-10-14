"""Assemble page TIFFs into a JPEG-compressed PDF (Pillow)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

from PIL import Image

from yokaze.lib.util.errors import YokazeError
from yokaze.lib.util.git import git_commit_short


def format_delivery_keywords(
    *,
    commit: str,
    attempt: int | None = None,
    brod: str | None = None,
    crop: str | None = None,
    tone: str | None = None,
) -> str:
    """Compact provenance line for PDF Keywords (Paperless Metadata tab)."""
    parts = ["yokaze"]
    if attempt is not None:
        parts.append(f"attempt={int(attempt)}")
    for key, value in (("brod", brod), ("crop", crop), ("tone", tone)):
        if value:
            parts.append(f"{key}={_short_sha(value)}")
    parts.append(f"commit={_short_sha(commit)}")
    return " ".join(parts)


def _short_sha(value: str) -> str:
    text = (value or "").strip()
    if len(text) > 7 and all(c in "0123456789abcdefABCDEF" for c in text):
        return text[:7]
    return text


def build_pdf(
    pdf_out: Path,
    pages: list[Path],
    *,
    quality: int | None = None,
    density: int | None = None,
    subsample: int | None = None,
    attempt: int | None = None,
    stage_pins: Mapping[str, str] | None = None,
) -> None:
    """Assemble page images into a PDF.

    Delivery scans take their encoding from the environment knobs. Callers with
    their own requirements (calibration charts want maximum fidelity at an exact
    physical size) pass explicit overrides instead of reaching for the env.

    Optional ``attempt`` / ``stage_pins`` are stamped into PDF Keywords so
    Paperless can show them under archived/original document metadata.
    """
    if not pages:
        raise YokazeError("build_pdf: no pages")
    if quality is None:
        quality = int(os.environ.get("JPEG_QUALITY", "75"))
    if density is None:
        density = int(os.environ.get("PDF_DENSITY", "300"))
    if subsample is None:
        # JPEG_SAMPLING is Magick-specific; Pillow uses subsampling 0/1/2.
        sampling = os.environ.get("JPEG_SAMPLING", "4:2:0")
        subsample = 0 if sampling in {"4:4:4", "1x1"} else 2

    pdf_out.parent.mkdir(parents=True, exist_ok=True)
    images: list[Image.Image] = []
    try:
        for path in pages:
            img = Image.open(path)
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            elif img.mode == "L":
                img = img.convert("RGB")
            img.info["dpi"] = (density, density)
            images.append(img)

        commit = git_commit_short()
        pins = stage_pins or {}
        keywords = format_delivery_keywords(
            commit=commit,
            attempt=attempt,
            brod=pins.get("brod"),
            crop=pins.get("crop"),
            tone=pins.get("tone"),
        )
        first, *rest = images
        first.save(
            pdf_out,
            "PDF",
            resolution=float(density),
            quality=quality,
            subsampling=subsample,
            save_all=bool(rest),
            append_images=rest,
            creator="yokaze",
            producer=f"yokaze {commit}",
            keywords=keywords,
        )
    except OSError as exc:
        raise YokazeError(f"failed to write PDF {pdf_out}: {exc}") from exc
    finally:
        for img in images:
            img.close()


def encode_jpeg_bytes(path: Path, *, quality: int, subsample: int) -> bytes:
    """Helper for tests / alternate assemblers."""
    with Image.open(path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, subsampling=subsample)
        return buf.getvalue()
