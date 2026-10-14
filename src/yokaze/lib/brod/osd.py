"""Tesseract OSD after rough crop: adaptive binarize, optional 4-way (-l osd only).

No color/raw OSD fallback and no language packs (document language unknown).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2

from yokaze.lib.util.env import env_float
from yokaze.lib.util.io import read_bgr

# Covert-proven adaptiveThreshold knobs (Gaussian).
ADAPTIVE_BLOCK_SIZE = 41
ADAPTIVE_C = 15


def osd_script(text: str) -> str:
    """Script name from an OSD block, or empty when the line is missing."""
    match = re.search(r"^Script:\s*(\S+)", text, re.I | re.M)
    return match.group(1) if match else ""


def agreed_cjk_rotation(
    votes: list[tuple[int, float, str]],
    *,
    min_votes: int = 3,
    min_conf: float = 1.0,
) -> tuple[int, float] | None:
    """Angle when every 4-way reading agrees and calls the page Japanese or Han.

    Each vote is ``(absolute rotate, confidence, script)``. Used only after no
    single reading clears the hard gate. Every vote must clear ``min_conf``.
    """
    if len(votes) < min_votes:
        return None
    angles = {total for total, _conf, _script in votes}
    if len(angles) != 1:
        return None
    scripts = {script.casefold() for _total, _conf, script in votes}
    if not scripts or not scripts <= {"japanese", "han"}:
        return None
    if any(conf < min_conf for _total, conf, _script in votes):
        return None
    best = max(conf for _total, conf, _script in votes)
    return next(iter(angles)), best


def parse_osd(text: str) -> tuple[int | None, float | None, str]:
    """Return (rotate, conf, miss_reason). miss_reason empty on success parse."""
    if "Too few characters" in text:
        return None, None, "too_few"
    rot_m = re.search(r"Rotate:\s*(\d+)", text, re.I)
    conf_m = re.search(r"Orientation confidence:\s*([0-9.]+)", text, re.I)
    if not rot_m or not conf_m:
        return None, None, "no_parse"
    try:
        rotate = int(rot_m.group(1))
        conf = float(conf_m.group(1))
    except ValueError:
        return None, None, "no_parse"
    return rotate, conf, ""


def write_adaptive_png(tiff: Path, dest: Path) -> None:
    """Gray + adaptiveThreshold → PNG for Tesseract."""
    bgr = read_bgr(tiff)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        ADAPTIVE_BLOCK_SIZE,
        ADAPTIVE_C,
    )
    if not cv2.imwrite(str(dest), binary):
        raise OSError(f"cannot write adaptive OSD image {dest}")


def run_tesseract_osd(image: Path) -> str:
    """Run ``tesseract --psm 0 -l osd``; return combined stdout+stderr."""
    if shutil.which("tesseract") is None:
        return "Too few characters. Skipping this page\n"
    try:
        proc = subprocess.run(
            [
                "tesseract",
                str(image),
                "stdout",
                "--psm",
                "0",
                "-l",
                "osd",
                "--dpi",
                "300",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "Too few characters. Skipping this page\n"
    return (proc.stdout or "") + (proc.stderr or "")


def _gated_from_text(
    text: str, min_confidence: float
) -> tuple[int | None, float | None, str]:
    rotate, conf, reason = parse_osd(text)
    if reason:
        return None, conf, reason
    assert rotate is not None and conf is not None
    if conf < min_confidence:
        # Keep the angle so callers can soft-accept when 4-way is starved.
        return rotate, conf, "low_conf"
    return rotate, conf, ""


def adaptive_osd(
    tiff: Path, min_confidence: float
) -> tuple[int | None, float | None, str]:
    """OSD on adaptive-binarize of ``tiff``. Returns (rotate, conf, miss_reason)."""
    if shutil.which("tesseract") is None:
        return None, None, "no_tesseract"
    td = Path(tempfile.mkdtemp(prefix="yokaze-osd-adapt-"))
    try:
        png = td / "adaptive.png"
        write_adaptive_png(tiff, png)
        return _gated_from_text(run_tesseract_osd(png), min_confidence)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def four_way_osd(
    tiff: Path, min_confidence: float
) -> tuple[int | None, float | None, str]:
    """Try 0/90/180/270 on grayscale rough crop; best conf ≥ gate → total rotate.

    Uses gray (not adaptive): adaptive primary already failed, and 4-way on
    adaptive can lock onto a wrong high-conf vote (e.g. Colombia).

    If no reading clears the gate, agreeing Japanese or Han readings still
    count. See ``agreed_cjk_rotation``.
    """
    if shutil.which("tesseract") is None:
        return None, None, "no_tesseract"

    td = Path(tempfile.mkdtemp(prefix="yokaze-osd-4way-"))
    try:
        bgr = read_bgr(tiff)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        best_rot: int | None = None
        best_conf = -1.0
        last_reason = "no_parse"
        agreed_votes: list[tuple[int, float, str]] = []
        for pre, flag in (
            (0, None),
            (90, cv2.ROTATE_90_CLOCKWISE),
            (180, cv2.ROTATE_180),
            (270, cv2.ROTATE_90_COUNTERCLOCKWISE),
        ):
            im = gray if flag is None else cv2.rotate(gray, flag)
            trial = td / f"r{pre}.png"
            cv2.imwrite(str(trial), im)
            text = run_tesseract_osd(trial)
            rotate, conf, reason = _gated_from_text(text, min_confidence=0.0)
            if rotate is None or conf is None:
                last_reason = reason or last_reason
                continue
            total = (pre + rotate) % 360
            agreed_votes.append((total, conf, osd_script(text)))
            if conf < min_confidence:
                last_reason = "low_conf"
                if conf > best_conf:
                    best_conf = conf
                continue
            if conf > best_conf:
                best_conf = conf
                best_rot = total

        if best_rot is not None and best_conf >= min_confidence:
            return best_rot, best_conf, ""
        agreed = agreed_cjk_rotation(agreed_votes)
        if agreed is not None:
            return agreed[0], agreed[1], ""
        return None, (best_conf if best_conf >= 0 else None), last_reason
    finally:
        shutil.rmtree(td, ignore_errors=True)


_MISS_REASONS = {
    "too_few": "too few characters",
    "low_conf": "low confidence",
    "no_parse": "could not parse OSD",
    "no_tesseract": "tesseract not found",
}


def _human_miss(reason: str) -> str:
    return _MISS_REASONS.get(reason, reason)


def _osd_prefix(label: str | None) -> str:
    return f"    {label}" if label else "    [osd]"


def page_orientation(
    tiff: Path,
    min_confidence: float,
    *,
    label: str | None = None,
    soft_min_confidence: float | None = None,
) -> int | None:
    """Adaptive OSD, then 4-way if needed. One human log line per call.

    ``tiff`` should already be rough-cropped.

    When both gated passes miss but adaptive had a below-gate vote and 4-way
    failed for ``too_few`` characters, accept that adaptive angle if it clears
    ``soft_min_confidence`` (sparse cards where 4-way never parses).
    """
    prefix = _osd_prefix(label)
    soft_floor = (
        float(soft_min_confidence)
        if soft_min_confidence is not None
        else env_float("ROTATE_SOFT_MIN_CONFIDENCE", 0.5)
    )
    rot, conf, reason = adaptive_osd(tiff, min_confidence)
    if reason == "" and rot is not None and conf is not None:
        print(
            f"{prefix}  adaptive OSD → {rot}° (confidence {conf:.1f})",
            flush=True,
        )
        return rot

    miss = _human_miss(reason or "no_parse")
    rot4, conf4, reason4 = four_way_osd(tiff, min_confidence)
    if reason4 == "" and rot4 is not None and conf4 is not None:
        print(
            f"{prefix}  adaptive OSD missed ({miss}); "
            f"4-way OSD → {rot4}° (confidence {conf4:.1f})",
            flush=True,
        )
        return rot4

    miss4 = _human_miss(reason4 or "no_parse")
    # Soft fallback: character-starved 4-way, but adaptive had a real angle
    # below the hard gate (ozaki-class sparse card @ ~0.6).
    if (
        reason == "low_conf"
        and reason4 == "too_few"
        and rot is not None
        and conf is not None
        and conf >= soft_floor
    ):
        print(
            f"{prefix}  adaptive OSD missed ({miss}); 4-way OSD missed ({miss4}); "
            f"soft adaptive → {rot}° (confidence {conf:.1f})",
            flush=True,
        )
        return rot

    print(
        f"{prefix}  adaptive OSD missed ({miss}); 4-way OSD missed ({miss4})",
        flush=True,
    )
    return None
