"""Document-level OSD rotation plan.

After rough bed crop: adaptive OSD, then optional 4-way (-l osd). Unreadable
siblings borrow from the first confident page:

- same duplex side (both odd or both even page numbers) → copy
- opposite side + reference 0° or 180° → copy
- opposite side + reference 90° or 270° → flip (reference + 180°)
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from yokaze.lib.util.env import env_float
from yokaze.lib.brod.ops import PreprocessMeta, rough_bed_crop
from yokaze.lib.brod.osd import page_orientation

# Same pattern as pipeline.document: page-0001.tiff or batch-01-page-0001.tiff
_PAGE_NUM_RE = re.compile(r"page-(\d{4})\.tiff$", re.I)


def resolve_scan_source(artifact_dir: Path | None = None) -> str:
    """``Card Duplex`` or ``ADF Duplex`` from env / metadata / default."""
    env = os.environ.get("SCAN_SOURCE", "").strip()
    if env in {"Card Duplex", "ADF Duplex"}:
        return env
    if artifact_dir is not None:
        from yokaze.lib.metadata import get_scan_source

        return get_scan_source(artifact_dir)
    return "ADF Duplex"


def duplex_page_number(path: Path) -> int | None:
    """1-based scanner page index from ``page-NNNN`` in the filename."""
    match = _PAGE_NUM_RE.search(path.name)
    return int(match.group(1)) if match else None


def same_duplex_side(path: Path, ref: Path) -> bool:
    """True when both paths are the same sheet face (both fronts or both backs)."""
    page = duplex_page_number(path)
    other = duplex_page_number(ref)
    if page is None or other is None:
        return False
    return (page % 2) == (other % 2)


def borrow_rotation(ref_rot: int, *, same_side: bool = False) -> int:
    """Map a confident sibling rotate for an unreadable page.

    Same-side pages copy. Opposite-side: 0→0, 180→180 (copy); 90→270, 270→90
    (flip). Unknown page numbers keep the opposite-side (legacy flip) path.
    """
    if same_side or ref_rot not in (90, 270):
        return ref_rot
    return (ref_rot + 180) % 360


def _page_label(path: Path) -> str:
    return path.stem


def _fmt_angle(rot: int | None) -> str:
    return "as-is" if rot is None else f"{rot}°"


def _fmt_plan(plan: list[int | None]) -> str:
    return ", ".join(_fmt_angle(r) for r in plan)


def _own_rotation_after_rough(
    raw: Path, min_conf: float, *, label: str
) -> int | None:
    """Rough-crop a temp copy, then adaptive (+ optional 4-way) OSD."""
    td = Path(tempfile.mkdtemp(prefix="yokaze-rotplan-"))
    try:
        work = td / "page.tiff"
        shutil.copy2(raw, work)
        rough_bed_crop(work, PreprocessMeta())
        return page_orientation(work, min_conf, label=label)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def resolve_rotation_plan(
    tiffs: list[Path],
    *,
    scan_source: str = "ADF Duplex",  # noqa: ARG001 — kept for call-site compat
) -> list[int | None]:
    """Return one planned rotate per page (``None`` = leave as-is).

    Each page is rough-cropped in a temp, then oriented via adaptive OSD and
    optional 4-way. Unreadable pages borrow from the first confident sibling.
    """
    _ = scan_source  # borrow is angle-based, not source-based
    if not tiffs:
        return []

    labels = [_page_label(path) for path in tiffs]
    min_conf = env_float("ROTATE_MIN_CONFIDENCE", 1.8)
    print("  orienting…", flush=True)
    own: list[int | None] = [
        _own_rotation_after_rough(path, min_conf, label=label)
        for path, label in zip(tiffs, labels, strict=True)
    ]
    ref_idx = next((i for i, r in enumerate(own) if r is not None), None)
    ref_rot = own[ref_idx] if ref_idx is not None else None

    plan: list[int | None] = []
    ref_path = tiffs[ref_idx] if ref_idx is not None else None
    for path, label, rot in zip(tiffs, labels, own, strict=True):
        if rot is not None:
            plan.append(rot)
            continue
        if ref_rot is not None and ref_path is not None:
            same_side = same_duplex_side(path, ref_path)
            borrowed = borrow_rotation(ref_rot, same_side=same_side)
            how = (
                "copy"
                if same_side or ref_rot not in (90, 270)
                else "flip"
            )
            print(
                f"    {label}  → borrow {_fmt_angle(borrowed)} "
                f"from {labels[ref_idx]} ({how})",
                flush=True,
            )
            plan.append(borrowed)
        else:
            print(f"    {label}  → leave as-is (no confident sibling)", flush=True)
            plan.append(None)

    print(f"  plan: {_fmt_plan(plan)}", flush=True)
    return plan
