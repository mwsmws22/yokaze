"""Corpus/archive ``metadata.json`` — GT + file facts + algo history."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yokaze.lib.geometry import CropRect, round_deskew, shift_crop_with_canvas

METADATA_NAME = "metadata.json"
_VALID_ROTATES = frozenset({0, 90, 180, 270})


def metadata_path(tree: Path) -> Path:
    return Path(tree) / METADATA_NAME


def page_key(page: int) -> str:
    return f"page-{int(page):04d}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_tenths(value: float) -> float:
    return round(float(value), 1)


# Paths git may invent mid-action; ignored for dirty gates. Keep pathspecs in sync.
_DATA_ARTIFACT_EXACT = frozenset({"data/attempts.json"})
_DATA_ARTIFACT_PREFIXES = (
    "data/corpus/",
    "data/archives/",
    "data/cache/",
    "data/scratch/",
)
_STATUS_PATHSPECS = (
    ".",
    ":(exclude)data/corpus",
    ":(exclude)data/archives",
    ":(exclude)data/cache",
    ":(exclude)data/scratch",
    ":(exclude)data/attempts.json",
)


def git_is_dirty(*, cwd: Path | None = None) -> bool:
    """True when the worktree has *source* changes that should block corpus writes."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", *_STATUS_PATHSPECS],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return True
    if proc.returncode != 0:
        return True
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        path = _porcelain_path(line)
        if path is None or _is_data_artifact_path(path):
            continue
        return True
    return False


def _porcelain_path(line: str) -> str | None:
    if len(line) < 4:
        return None
    body = line[3:]
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    body = body.strip()
    if body.startswith('"') and body.endswith('"'):
        body = body[1:-1]
    return body or None


def _is_data_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    if normalized in _DATA_ARTIFACT_EXACT:
        return True
    return normalized.startswith(_DATA_ARTIFACT_PREFIXES)


def require_clean_tree(*, cwd: Path | None = None, action: str = "this action") -> None:
    if git_is_dirty(cwd=cwd):
        raise RuntimeError(
            f"refusing {action}: working tree is dirty "
            "(commit or stash first)"
        )


def load_metadata(tree: Path) -> dict[str, Any]:
    path = metadata_path(tree)
    if not path.is_file():
        return {"pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pages": {}}
    if not isinstance(data, dict):
        return {"pages": {}}
    pages = data.get("pages")
    if not isinstance(pages, dict):
        data["pages"] = {}
    return data


def save_metadata(tree: Path, data: dict[str, Any]) -> Path:
    path = metadata_path(tree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _page(data: dict[str, Any], page: int) -> dict[str, Any]:
    pages = data.setdefault("pages", {})
    key = page_key(page)
    entry = pages.get(key)
    if not isinstance(entry, dict):
        entry = {}
        pages[key] = entry
    return entry


def page_gt(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the nested ``gt`` object (empty dict when missing)."""
    gt = entry.get("gt")
    return gt if isinstance(gt, dict) else {}


def get_scan_source(tree: Path, default: str = "ADF Duplex") -> str:
    data = load_metadata(tree)
    src = data.get("scan_source")
    if isinstance(src, str) and src.strip() in {"Card Duplex", "ADF Duplex"}:
        return src.strip()
    return default


def set_scan_source(tree: Path, source: str) -> None:
    data = load_metadata(tree)
    data["scan_source"] = source.strip()
    save_metadata(tree, data)


def set_page_gt(
    tree: Path,
    page: int,
    *,
    blank: bool | None = None,
    rotate: int | None = None,
    crop: dict[str, int] | None = None,
    deskew: float | None = None,
) -> None:
    """Overwrite GT fields (Annotate). Does not touch history or file."""
    data = load_metadata(tree)
    entry = _page(data, page)
    gt = entry.setdefault("gt", {})
    if not isinstance(gt, dict):
        gt = {}
        entry["gt"] = gt
    if blank is not None:
        gt["blank"] = bool(blank)
        if blank:
            gt.pop("rotate", None)
            gt.pop("crop", None)
            gt.pop("deskew", None)
    if rotate is not None:
        if rotate not in _VALID_ROTATES:
            raise ValueError(f"invalid rotate {rotate}")
        prev = gt.get("rotate")
        gt["blank"] = False
        gt["rotate"] = int(rotate)
        if prev != int(rotate):
            gt.pop("crop", None)
    if crop is not None:
        gt["crop"] = {
            "x": int(crop["x"]),
            "y": int(crop["y"]),
            "w": int(crop["w"]),
            "h": int(crop["h"]),
        }
        gt["blank"] = False
    if deskew is not None:
        gt["deskew"] = round_deskew(deskew)
        gt["blank"] = False
    save_metadata(tree, data)


def seed_gt_if_missing(
    tree: Path,
    page: int,
    *,
    blank: bool | None = None,
    rotate: int | None = None,
) -> None:
    data = load_metadata(tree)
    entry = _page(data, page)
    gt = entry.setdefault("gt", {})
    if not isinstance(gt, dict):
        gt = {}
        entry["gt"] = gt
    changed = False
    if blank is not None and "blank" not in gt:
        gt["blank"] = bool(blank)
        changed = True
    if (
        rotate is not None
        and rotate in _VALID_ROTATES
        and "rotate" not in gt
        and not gt.get("blank")
    ):
        gt["blank"] = False
        gt["rotate"] = int(rotate)
        changed = True
    if changed:
        save_metadata(tree, data)


def latest_history_run(entry: dict[str, Any]) -> dict[str, Any] | None:
    hist = entry.get("history")
    if not isinstance(hist, list) or not hist:
        return None
    last = hist[-1]
    return last if isinstance(last, dict) else None


def latest_history_value(entry: dict[str, Any], field: str) -> Any | None:
    """Latest history field (``deskew`` / ``rotate`` / ``crop``)."""
    run = latest_history_run(entry)
    if run is None:
        return None
    if field in run:
        return run.get(field)
    # Legacy parallel arrays
    hist = entry.get("history")
    if isinstance(hist, dict):
        rows = hist.get(field)
        if isinstance(rows, list) and rows:
            last = rows[-1]
            if isinstance(last, dict):
                return last.get("value")
    return None


def _fmt_crop_sig(crop: tuple[int, int, int, int] | None) -> str:
    if crop is None:
        return "none"
    x, y, w, h = crop
    return f"({x},{y},{w}×{h})"


def divergence_lines(
    before: dict[str, tuple[Any, ...]],
    after: dict[str, tuple[Any, ...]],
) -> list[str]:
    """Pages whose stored rotate, deskew, or crop changed."""
    lines: list[str] = []
    for key in sorted(set(before) & set(after)):
        if before[key] == after[key]:
            continue
        brot, bdesk, bcrop = before[key]
        arot, adesk, acrop = after[key]
        bits: list[str] = []
        if brot != arot:
            bits.append(f"rotate {brot} → {arot}")
        if bdesk != adesk:
            bits.append(f"deskew {bdesk} → {adesk}")
        if bcrop != acrop:
            bits.append(f"crop {_fmt_crop_sig(bcrop)} → {_fmt_crop_sig(acrop)}")
        if bits:
            lines.append(f"{key}: " + "; ".join(bits))
    return lines


def history_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    """Comparable rotate / deskew / crop for one history row."""
    crop = row.get("crop")
    crop_sig: tuple[int, int, int, int] | None
    if isinstance(crop, dict):
        try:
            crop_sig = (int(crop["x"]), int(crop["y"]), int(crop["w"]), int(crop["h"]))
        except (KeyError, TypeError, ValueError):
            crop_sig = None
    else:
        crop_sig = None
    rotate = row.get("rotate")
    try:
        deskew = round(float(row["deskew"]), 2) if "deskew" in row else None
    except (TypeError, ValueError):
        deskew = None
    return (rotate, deskew, crop_sig)


def history_row_for_attempt(
    entry: dict[str, Any], attempt_id: int
) -> dict[str, Any] | None:
    """Last history row whose ``attempt`` matches ``attempt_id``."""
    hist = entry.get("history")
    if not isinstance(hist, list):
        return None
    found: dict[str, Any] | None = None
    for row in hist:
        if isinstance(row, dict) and row.get("attempt") == attempt_id:
            found = row
    return found


def page_crop_for_attempt(
    tree: Path, page: int, attempt_id: int
) -> dict[str, int] | None:
    entry = load_metadata(tree).get("pages", {}).get(page_key(page))
    if not isinstance(entry, dict):
        return None
    row = history_row_for_attempt(entry, attempt_id)
    if row is None:
        return None
    crop = row.get("crop")
    if not isinstance(crop, dict):
        return None
    try:
        return {
            "x": int(crop["x"]),
            "y": int(crop["y"]),
            "w": int(crop["w"]),
            "h": int(crop["h"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def page_stems_for_attempt(tree: Path, attempt_id: int) -> list[str]:
    """Non-blank page stems that have a history row for ``attempt_id``."""
    pages = load_metadata(tree).get("pages") or {}
    if not isinstance(pages, dict):
        return []
    stems: list[str] = []
    for stem in sorted(pages):
        entry = pages.get(stem)
        if not isinstance(entry, dict):
            continue
        if page_gt(entry).get("blank"):
            continue
        if history_row_for_attempt(entry, attempt_id) is not None:
            stems.append(stem)
    return stems


def runtime_meta(entry: dict[str, Any]) -> dict[str, Any]:
    """Sidecar-shaped dict for Annotate/backend (file + history tip)."""
    file_meta = entry.get("file") if isinstance(entry.get("file"), dict) else {}
    run = latest_history_run(entry) or {}
    out: dict[str, Any] = {
        "space": "brod",
        "image_w": file_meta.get("image_w"),
        "image_h": file_meta.get("image_h"),
        "bed_gray": file_meta.get("bed_gray"),
        "deskewed": file_meta.get("deskewed"),
        "raw": file_meta.get("raw"),
        "updated": file_meta.get("updated"),
        "deskew_angle_deg": run.get("deskew", 0.0),
    }
    if "rotate" in run:
        out["rotate"] = run["rotate"]
    return out


def set_file_meta(
    tree: Path,
    page: int,
    *,
    image_w: int,
    image_h: int,
    bed_gray: float | None = None,
    deskewed: bool | int = False,
    raw: str = "",
    updated: str | None = None,
) -> None:
    data = load_metadata(tree)
    entry = _page(data, page)
    entry["file"] = {
        "image_w": int(image_w),
        "image_h": int(image_h),
        "bed_gray": float(bed_gray) if bed_gray is not None else None,
        "deskewed": bool(deskewed),
        "raw": raw,
        "updated": updated or utc_now(),
    }
    # Drop null bed_gray for cleaner JSON
    if entry["file"]["bed_gray"] is None:
        entry["file"].pop("bed_gray")
    save_metadata(tree, data)


def set_history_tip_crop(tree: Path, page: int, crop: dict[str, int]) -> None:
    """Overwrite ``crop`` on the latest history tip in place.

    ``record_history`` refuses a second row with the same ``attempt``, so tip
    crop must be patched when enhance already wrote rough/deskew/rotate.
    """
    data = load_metadata(tree)
    entry = data.get("pages", {}).get(page_key(page))
    if not isinstance(entry, dict):
        raise ValueError(f"no metadata page for {page_key(page)}")
    run = latest_history_run(entry)
    if run is None:
        raise ValueError(f"no history tip for {page_key(page)}")
    run["crop"] = {
        "x": int(crop["x"]),
        "y": int(crop["y"]),
        "w": int(crop["w"]),
        "h": int(crop["h"]),
    }
    save_metadata(tree, data)


def record_history(
    tree: Path,
    page: int,
    *,
    attempt: int,
    deskew_deg: float | None = None,
    crop: dict[str, int] | None = None,
    rotate: int | None = None,
    rough: dict[str, int] | None = None,
) -> None:
    """Append one attempt-tagged history run (full snapshot, no length cap)."""
    if deskew_deg is None and crop is None and rotate is None and rough is None:
        return

    data = load_metadata(tree)
    entry = _page(data, page)
    history = entry.get("history")
    if not isinstance(history, list):
        history = []
        entry["history"] = history

    prev = history[-1] if history else {}
    if not isinstance(prev, dict):
        prev = {}
    if prev.get("attempt") == attempt:
        return

    deskew_val = (
        round_tenths(deskew_deg)
        if deskew_deg is not None
        else prev.get("deskew", 0.0)
    )

    if rotate is not None:
        rotate_val = int(rotate) if rotate in _VALID_ROTATES else 0
    else:
        rotate_val = int(prev["rotate"]) if prev.get("rotate") in _VALID_ROTATES else 0

    if crop is not None:
        crop_val = {
            "x": int(crop["x"]),
            "y": int(crop["y"]),
            "w": int(crop["w"]),
            "h": int(crop["h"]),
        }
    else:
        crop_val = prev.get("crop")

    run: dict[str, Any] = {
        "attempt": attempt,
        "at": utc_now(),
        "deskew": deskew_val,
        "rotate": rotate_val,
    }
    if isinstance(crop_val, dict):
        run["crop"] = {
            "x": int(crop_val["x"]),
            "y": int(crop_val["y"]),
            "w": int(crop_val["w"]),
            "h": int(crop_val["h"]),
        }
    if rough is not None:
        rough_val = {
            "x": int(rough["x"]),
            "y": int(rough["y"]),
            "w": int(rough["w"]),
            "h": int(rough["h"]),
        }
    else:
        prev_rough = prev.get("rough")
        rough_val = prev_rough if isinstance(prev_rough, dict) else None
    if isinstance(rough_val, dict):
        run["rough"] = {
            "x": int(rough_val["x"]),
            "y": int(rough_val["y"]),
            "w": int(rough_val["w"]),
            "h": int(rough_val["h"]),
        }
    history.append(run)
    save_metadata(tree, data)


def purge_invalid_attempt_history(tree: Path, valid_ids: set[int]) -> int:
    """Drop history rows with missing or unknown ``attempt``; return count removed."""
    data = load_metadata(tree)
    pages = data.get("pages") or {}
    if not isinstance(pages, dict):
        return 0
    removed = 0
    for entry in pages.values():
        if not isinstance(entry, dict):
            continue
        history = entry.get("history")
        if not isinstance(history, list):
            continue
        kept = [
            row
            for row in history
            if isinstance(row, dict)
            and row.get("attempt") is not None
            and row.get("attempt") in valid_ids
        ]
        removed += len(history) - len(kept)
        if kept:
            entry["history"] = kept
        else:
            entry.pop("history", None)
    if removed:
        save_metadata(tree, data)
    return removed


def purge_attempt_history(tree: Path, attempt_id: int) -> int:
    """Drop every history row tagged with ``attempt_id``; return count removed."""
    data = load_metadata(tree)
    pages = data.get("pages") or {}
    if not isinstance(pages, dict):
        return 0
    removed = 0
    for entry in pages.values():
        if not isinstance(entry, dict):
            continue
        history = entry.get("history")
        if not isinstance(history, list):
            continue
        kept = [
            row
            for row in history
            if not (isinstance(row, dict) and row.get("attempt") == attempt_id)
        ]
        removed += len(history) - len(kept)
        if kept:
            entry["history"] = kept
        else:
            entry.pop("history", None)
    if removed:
        save_metadata(tree, data)
    return removed


def page_gt_core_complete(entry: dict[str, Any]) -> bool:
    gt = page_gt(entry)
    if "blank" not in gt:
        return False
    if gt.get("blank"):
        return True
    return gt.get("rotate") in _VALID_ROTATES


def page_gt_complete(entry: dict[str, Any]) -> bool:
    if not page_gt_core_complete(entry):
        return False
    gt = page_gt(entry)
    if gt.get("blank"):
        return True
    if not isinstance(gt.get("crop"), dict):
        return False
    try:
        float(gt["deskew"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _file_size(entry: dict[str, Any]) -> tuple[int, int] | None:
    file_meta = entry.get("file")
    if not isinstance(file_meta, dict):
        return None
    try:
        width = int(file_meta["image_w"])
        height = int(file_meta["image_h"])
    except (KeyError, TypeError, ValueError):
        return None
    if width < 1 or height < 1:
        return None
    return width, height


def _gt_crop(gt: dict[str, Any]) -> CropRect | None:
    raw = gt.get("crop")
    if not isinstance(raw, dict):
        return None
    try:
        return CropRect.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def gt_crop_for_new_canvas(
    entry: dict[str, Any],
    *,
    blank: bool,
    rotate: int,
    new_w: int,
    new_h: int,
) -> dict[str, int] | None:
    """Shift a ground-truth box onto a republished picture, or leave it.

    Blank pages, a rotate change, a missing box, and a missing old picture
    stay as they are. An unchanged canvas is a no-op.
    """
    if blank or new_w < 1 or new_h < 1:
        return None
    gt = page_gt(entry)
    if gt.get("rotate") != int(rotate):
        return None
    current = _gt_crop(gt)
    old_size = _file_size(entry)
    if current is None or old_size is None:
        return None
    shifted = shift_crop_with_canvas(current, old_size[0], old_size[1], new_w, new_h)
    if shifted == current:
        return None
    return shifted.as_dict()


def _carry_gt_crop(
    tree: Path,
    page: int,
    *,
    blank: bool,
    rotate: int,
    file_meta: dict[str, Any],
) -> None:
    data = load_metadata(tree)
    entry = data.get("pages", {}).get(page_key(page))
    if not isinstance(entry, dict):
        return
    carried = gt_crop_for_new_canvas(
        entry,
        blank=blank,
        rotate=rotate,
        new_w=int(file_meta["image_w"]),
        new_h=int(file_meta["image_h"]),
    )
    if carried is None:
        return
    set_page_gt(tree, page, crop=carried)


def sync_metadata_page(
    tree: Path,
    page: int,
    *,
    attempt: int,
    blank: bool,
    rotate: int | None = None,
    deskew_deg: float | None = None,
    crop: dict[str, int] | None = None,
    rough: dict[str, int] | None = None,
    file_meta: dict[str, Any] | None = None,
) -> None:
    """Seed GT if missing and record one history run; optional file block update."""
    if blank:
        seed_gt_if_missing(tree, page, blank=True)
        plan_rotate = 0
    else:
        seed_gt_if_missing(tree, page, blank=False, rotate=rotate)
        plan_rotate = int(rotate) if rotate in _VALID_ROTATES else 0
    if file_meta is not None:
        _carry_gt_crop(
            tree,
            page,
            blank=blank,
            rotate=plan_rotate,
            file_meta=file_meta,
        )
        set_file_meta(
            tree,
            page,
            image_w=int(file_meta["image_w"]),
            image_h=int(file_meta["image_h"]),
            bed_gray=file_meta.get("bed_gray"),
            deskewed=bool(file_meta.get("deskewed")),
            raw=str(file_meta.get("raw") or ""),
            updated=file_meta.get("updated"),
        )
    record_history(
        tree,
        page,
        attempt=attempt,
        deskew_deg=deskew_deg,
        crop=crop,
        rotate=plan_rotate,
        rough=rough,
    )
