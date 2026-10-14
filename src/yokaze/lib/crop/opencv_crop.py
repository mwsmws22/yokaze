"""OpenCV paper crop on preprocess frames (bed + fuzz bbox ladder)."""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.util.errors import CropFailedError
from yokaze.lib.util.types import CropRect, PageWork
from yokaze.lib.util.io import image_size, read_bgr, to_gray
from yokaze.lib.crop.paper import bed_gray, foreground_bbox


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _fuzz_ladder() -> list[float]:
    # Start at 12: lower fuzzes often pick near-full frames on pale sheets.
    # Extend past 25 so look-ahead can reach large bed-blob → sheet collapses
    # (e.g. narrow receipt beside speckled ADF bed around fuzz 38).
    raw = (
        os.environ.get("OPENCV_CROP_FUZZES") or "12,14,15,18,20,22,25,30,35,38,40"
    ).strip()
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out or [12.0, 14.0, 15.0, 18.0, 20.0, 22.0, 25.0, 30.0, 35.0, 38.0, 40.0]


def _robust_bed_gray(image_path: Path) -> float:
    """Corner bed grey; if corners are near-white deskew fill, fall back to ring.

    Pure fill corners (lower-median > 250) use ring P25. Mixed fill that lands
    in the mid-240s (ozaki-class) still poisons fuzz — use ring P10 so the
    sample sits on real bed grey rather than the bright fringe above the sheet.
    """
    bed = bed_gray(image_path)
    if bed <= 245.0:
        return bed
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return bed
    ring = np.concatenate(
        [
            img[0:16, :].ravel(),
            img[-16:, :].ravel(),
            img[:, 0:16].ravel(),
            img[:, -16:].ravel(),
        ]
    )
    darkish = ring[ring < 250]
    sample = darkish if darkish.size >= 100 else ring
    # Mixed mid-240s fill needs a lower ring sample; pure 255 corners keep P25.
    pct = 10.0 if bed <= 250.0 else 25.0
    return float(np.percentile(sample, pct))


def _bbox_usable(
    cw: int, ch: int, width: int, height: int, *, max_area_frac: float
) -> bool:
    if cw < 8 or ch < 8:
        return False
    if cw >= width - 8 or ch >= height - 8:
        return False
    if cw * ch > max_area_frac * width * height:
        return False
    return True


def _inward_moves(
    prev: tuple[int, int, int, int], nxt: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """How far each edge of ``nxt`` sits inside ``prev`` (left, top, right, bottom)."""
    pw, ph, px, py = prev
    cw, ch, cx, cy = nxt
    return (
        cx - px,
        cy - py,
        (px + pw) - (cx + cw),
        (py + ph) - (cy + ch),
    )


def _box_touches_bed(
    gray: np.ndarray,
    box: tuple[int, int, int, int],
    bed: float,
    tol: float,
) -> bool:
    """True when an edge line is flat and sits on the corner bed grey."""
    cw, ch, cx, cy = box
    height, width = gray.shape[:2]
    x1 = min(width, cx + cw)
    y1 = min(height, cy + ch)
    if x1 <= cx or y1 <= cy:
        return False
    lines = (
        gray[cy, cx:x1],
        gray[y1 - 1, cx:x1],
        gray[cy:y1, cx],
        gray[cy:y1, x1 - 1],
    )
    for line in lines:
        if line.size == 0:
            continue
        if abs(float(line.mean()) - bed) <= tol and float(line.std()) <= 8.0:
            return True
    return False


def _strip_between(
    gray: np.ndarray,
    prev: tuple[int, int, int, int],
    nxt: tuple[int, int, int, int],
    axis: int,
) -> np.ndarray:
    """Pixels the later box gave up on one edge."""
    pw, ph, px, py = prev
    cw, ch, cx, cy = nxt
    pl, pt, pr, pb = px, py, px + pw, py + ph
    nl, nt, nr, nb = cx, cy, cx + cw, cy + ch
    if axis == 0:
        x0, x1, y0, y1 = pl, nl, max(pt, nt), min(pb, nb)
        return gray[y0:y1, x0:x1]
    if axis == 1:
        y0, y1, x0, x1 = pt, nt, max(pl, nl), min(pr, nr)
        return gray[y0:y1, x0:x1]
    if axis == 2:
        x0, x1, y0, y1 = nr, pr, max(pt, nt), min(pb, nb)
        return gray[y0:y1, x0:x1]
    y0, y1, x0, x1 = nb, pb, max(pl, nl), min(pr, nr)
    return gray[y0:y1, x0:x1]


_BED_FRACTION = 0.90
# Same sheet on both sides of a cut: the dropped band and the box that remains
# sit a similar distance from the bed. A bed blob is much closer than the
# sheet, and a dark fringe around pale paper is much farther.
_SAME_SHEET_LO = 0.75
_SAME_SHEET_HI = 1.25
# A flat band can sit a little off a fill-biased corner estimate and still be
# bed. Past this spread it is a colored sheet. Past this distance it is not
# the bed the estimate was aiming at.
_OFF_ESTIMATE_BED_STD = 8.0
_OFF_ESTIMATE_BED_GREY = 16.0
# A scanner drop-shadow is smooth. A printed border is busy along the line.
_BUSY_EDGE_STD = 25.0
# A pale sheet the corner estimate swallowed is a long run, not a short fringe.
_SWALLOWED_SHEET_MIN_FRAC = 0.006
# Expand may cross a soft near-bed depression (crumpled margin) to paper.
_NEAR_BED_BRIDGE_MAX = 30
_NEAR_BED_BRIDGE_BELOW = 8.0


def _mostly_bed(values: np.ndarray, bed: float, near_tol: float) -> bool:
    """True when the sample sits on corner-bed grey, not on a colored sheet."""
    if values.size == 0:
        return False
    near = np.abs(values.astype(np.float32) - float(bed)) <= near_tol
    return float(near.mean()) >= _BED_FRACTION


def _box_pixels(
    gray: np.ndarray, box: tuple[int, int, int, int]
) -> np.ndarray:
    height, width = gray.shape[:2]
    cw, ch, cx, cy = box
    y0, y1 = max(0, cy), min(height, cy + ch)
    x0, x1 = max(0, cx), min(width, cx + cw)
    if y1 <= y0 or x1 <= x0:
        return gray[0:0, 0:0]
    return gray[y0:y1, x0:x1]


def _shed_pixels(
    gray: np.ndarray,
    prev: tuple[int, int, int, int],
    nxt: tuple[int, int, int, int],
) -> np.ndarray:
    """Pixels inside ``prev`` and outside ``nxt``."""
    height, width = gray.shape[:2]
    pw, ph, px, py = prev
    cw, ch, cx, cy = nxt
    mask = np.zeros((height, width), dtype=bool)
    y0, y1 = max(0, py), min(height, py + ph)
    x0, x1 = max(0, px), min(width, px + pw)
    if y1 <= y0 or x1 <= x0:
        return gray[0:0]
    mask[y0:y1, x0:x1] = True
    ny0, ny1 = max(0, cy), min(height, cy + ch)
    nx0, nx1 = max(0, cx), min(width, cx + cw)
    if ny1 > ny0 and nx1 > nx0:
        mask[ny0:ny1, nx0:nx1] = False
    return gray[mask]


def _shed_is_same_sheet(
    gray: np.ndarray,
    prev: tuple[int, int, int, int],
    nxt: tuple[int, int, int, int],
    bed: float,
) -> bool:
    """True when a smaller fuzz box only cut the same sheet, not scanner bed.

    The dropped band and the box that remains are about the same distance from
    the corner bed. A bed blob is much closer than the sheet it surrounds, and
    a dark fringe around pale paper is much farther, so those collapses stay.
    """
    shed = _shed_pixels(gray, prev, nxt)
    kept = _box_pixels(gray, nxt)
    if shed.size == 0 or kept.size == 0:
        return False
    shed_d = float(np.median(np.abs(shed.astype(np.float32) - float(bed))))
    kept_d = float(np.median(np.abs(kept.astype(np.float32) - float(bed))))
    if kept_d < 1.0:
        return False
    ratio = shed_d / kept_d
    return _SAME_SHEET_LO <= ratio <= _SAME_SHEET_HI


def _one_inward_strip(
    gray: np.ndarray,
    prev: tuple[int, int, int, int],
    nxt: tuple[int, int, int, int],
    soft_px: float,
) -> np.ndarray | None:
    """Pixels of the one edge that moved farther than the snap walk can cross."""
    moves = _inward_moves(prev, nxt)
    beyond = [i for i, move in enumerate(moves) if move > soft_px]
    if len(beyond) != 1:
        return None
    axis = beyond[0]
    if any(move < -12 or move > soft_px for i, move in enumerate(moves) if i != axis):
        return None
    strip = _strip_between(gray, prev, nxt, axis)
    if strip.size == 0:
        return None
    return strip


def _uniform_bed_strip(
    gray: np.ndarray,
    prev: tuple[int, int, int, int],
    nxt: tuple[int, int, int, int],
    bed: float,
    *,
    soft_px: float,
    near_tol: float,
) -> tuple[int, int, int, int] | None:
    """Later box, when it only drops a flat bed band the snap walk cannot cross.

    Area barely changes when one long edge moves, so the collapse test keeps
    the loose box. A brighter sheet in that band is content and stays put.
    """
    strip = _one_inward_strip(gray, prev, nxt, soft_px)
    if strip is None or not _mostly_bed(strip, bed, near_tol):
        return None
    return nxt


def _off_estimate_bed_strip(
    gray: np.ndarray,
    prev: tuple[int, int, int, int],
    nxt: tuple[int, int, int, int],
    bed: float,
    *,
    soft_px: float,
) -> tuple[int, int, int, int] | None:
    """Later box, when the dropped band is flat bed a little off the corner estimate.

    Deskew fill pulls the corner estimate bright, so the real bed can miss the
    near-bed test and the outer line can be a ridge rather than the bed itself.
    The band is still bed when it is flat and much closer to the estimate than
    the sheet that remains. A colored sheet is busier, or about as far from
    the estimate as what it surrounds, and stays.
    """
    strip = _one_inward_strip(gray, prev, nxt, soft_px)
    if strip is None or strip.size < 50:
        return None
    values = strip.astype(np.float32)
    if float(values.std()) > _OFF_ESTIMATE_BED_STD:
        return None
    shed_d = float(np.median(np.abs(values - float(bed))))
    if shed_d > _OFF_ESTIMATE_BED_GREY:
        return None
    kept = _box_pixels(gray, nxt)
    if kept.size == 0:
        return None
    kept_d = float(np.median(np.abs(kept.astype(np.float32) - float(bed))))
    if kept_d < 1.0 or shed_d / kept_d > _SAME_SHEET_LO:
        return None
    return nxt


def _apply_margin(cx: int, cy: int, cw: int, ch: int, margin: int, w: int, h: int) -> CropRect:
    x = cx + margin
    y = cy + margin
    rw = cw - 2 * margin
    rh = ch - 2 * margin
    return CropRect(x, y, max(1, rw), max(1, rh)).clamp(w, h)


class _EdgeRefiner:
    """Grow into pale paper, shave soft bed, snap ramps, peel drop-shadow.

    Expand: recover low-contrast (brighter-than-bed) margins the coarse fuzz
    clipped, without walking into darker scanner-shadow bands. Stop when the
    line mean falls more than ``expand_ramp_drop`` below the brightest paper
    seen on that side (soft pale ramp fading toward bed), or on a big single
    -step jump that does not sustain for a few more lines (bed-side glare
    spike next to the paper edge, not a pale-margin plateau).

    Tighten: pull each edge inward while the edge line still looks like soft
    bed (near bed grey) and is *not* brighter-than-bed paper. Depth is capped
    as a fraction of the longer image side so we cannot eat into content.

    Gradient snap: walk inward for the bed/content boundary. When the profile
    shows a dark trough then a climb (scanner shadow valley), snap on that
    ascent; otherwise use the strongest |Δ| step. Outer bright deskew fill
    that descends toward bed does not abort the walk; soft/bright outer
    margins get a deeper search runway than settled content edges.

    Shadow peel: if the edge is mostly darker-than-bed *and* inward recovery
    reaches bright paper or near-bed grey, shave that drop-shadow band.
    """

    def __init__(self, gray: np.ndarray, bed: float):
        self.gray = gray
        self.bed = bed
        self.bright = gray > bed + _env_float("OPENCV_CROP_BRIGHT_TOL", 8.0)
        self.shadow = gray < bed - _env_float("OPENCV_CROP_SHADOW_TOL", 25.0)
        self.frac = _env_float("OPENCV_CROP_EXPAND_FRAC", 0.55)
        self.max_expand_frac = _env_float("OPENCV_CROP_MAX_EXPAND_FRAC", 0.10)
        # Stop expanding when grey falls this far below the brightest paper
        # seen on that side — blocks soft pale ramps that fade toward bed.
        # 3.5 skips single-line mid-plateau dips (~2.5 grey) without letting
        # chicken-style soft fades toward bed keep growing (those drop farther).
        self.expand_ramp_drop = _env_float("OPENCV_CROP_EXPAND_RAMP_DROP", 3.5)
        # A single-step jump bigger than this only keeps growing into it if the
        # brightness sustains for expand_jump_sustain more lines — otherwise
        # it is treated as a brief bed-side glare/halo spike, not a genuine
        # pale-margin plateau (which rises gradually or holds steady).
        self.expand_max_step = _env_float("OPENCV_CROP_EXPAND_MAX_STEP", 20.0)
        self.expand_jump_sustain = _env_int("OPENCV_CROP_EXPAND_JUMP_SUSTAIN", 8)
        self.tighten = os.environ.get("OPENCV_CROP_TIGHTEN", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        self.soft_tol = _env_float("OPENCV_CROP_TIGHTEN_SOFT_TOL", 30.0)
        self.bright_keep = _env_float("OPENCV_CROP_TIGHTEN_BRIGHT_KEEP", 12.0)
        self.max_tighten_frac = _env_float("OPENCV_CROP_MAX_TIGHTEN_FRAC", 0.002)
        # Extra inward shave after normal tighten when the edge is soft near-bed
        # that climbs into pale paper (long soft fringe the default budget
        # cannot finish). 0 disables. Overshoot lets the walk past bright_keep
        # a few grey levels so the plateau knee is reachable.
        self.soft_climb_tighten_frac = _env_float(
            "OPENCV_CROP_SOFT_CLIMB_TIGHTEN_FRAC", 0.012
        )
        self.soft_climb_overshoot = _env_float(
            "OPENCV_CROP_SOFT_CLIMB_OVERSHOOT", 10.0
        )
        self.snap = os.environ.get("OPENCV_CROP_GRAD_SNAP", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        self.max_snap_frac = _env_float("OPENCV_CROP_MAX_SNAP_FRAC", 0.014)
        # When the edge still looks like outer margin (soft near-bed or bright
        # deskew fill), allow a deeper snap walk so long soft-bed strips and
        # fill→bed→shadow→card profiles can reach the real boundary.
        self.max_soft_snap_frac = _env_float("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", 0.045)
        # Hang-hole / card-shadow voids sit below the soft-near-bed band for a
        # long run before pale paper. Soft cap (~4.5%) is too short for those.
        self.max_void_snap_frac = _env_float("OPENCV_CROP_MAX_VOID_SNAP_FRAC", 0.08)
        self.min_snap_grad = _env_float("OPENCV_CROP_MIN_SNAP_GRAD", 2.0)
        self.shadow_peel = os.environ.get("OPENCV_CROP_SHADOW_PEEL", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        self.max_shadow_peel_frac = _env_float("OPENCV_CROP_MAX_SHADOW_PEEL_FRAC", 0.012)
        self.shadow_peel_frac = _env_float("OPENCV_CROP_SHADOW_PEEL_FRAC", 0.55)

    def _sheet_bright(self, box: CropRect) -> float:
        """Bright reference for the coarse sheet body (not bed-side fill).

        Deskew fill past the page is brighter than the sheet's own bright
        pixels. Comparing an outward step to this, not to bed grey, lets a
        pale sheet still grow while a dark card does not climb into fill.

        A dark card with a bright foil/specular minority makes p90 track that
        minority instead of the page body, so bed-side glare looks like more
        sheet. When the median is deep dark and p90 sits far above it, use the
        median so expand still stops on outward fill.
        """
        x0, y0 = box.x, box.y
        x1, y1 = x0 + box.w, y0 + box.h
        ix = max(2, box.w // 5)
        iy = max(2, box.h // 5)
        patch = self.gray[y0 + iy : y1 - iy, x0 + ix : x1 - ix]
        if patch.size == 0:
            return self.bed
        p50 = float(np.percentile(patch, 50))
        p90 = float(np.percentile(patch, 90))
        # Foil/specular minority sits near bed grey on a deep-dark body. Mid-tone
        # photos with a bright sky also have a p90–p50 gap, so require the body
        # well below the soft-bed band and p90 near the bed (not pale paper).
        if (
            p90 - p50 >= 50.0
            and p50 < self.bed - 2.0 * self.soft_tol
            and abs(p90 - self.bed) <= 2.0 * self.bright_keep
        ):
            return p50
        return p90

    def _leaving_fringe_into_fill(
        self, origin: float, mean: float, sheet_bright: float
    ) -> bool:
        """True when a near-bed edge steps into pixels brighter than the sheet."""
        near_bed = (
            abs(origin - self.bed) <= self.soft_tol
            and origin < self.bed + self.bright_keep
        )
        if not near_bed:
            return False
        return mean > sheet_bright + self.bright_keep

    def _paper_col(self, x: int, y0: int, y1: int) -> bool:
        return self.bright[y0:y1, x].mean() >= self.frac and self.shadow[y0:y1, x].mean() < 0.5

    def _paper_or_near_bed_bridge(
        self,
        idx: int,
        direction: int,
        bound: int,
        get_mean,
        get_paper,
        get_shadow,
    ) -> int | None:
        """Paper at ``idx``, or the first paper across a soft near-bed gap.

        A pale edge can sit a few lines outside the coarse box, with the
        columns in between only a little off the bed. A deep dark gap or a
        run that leaves the bed is not paper.

        An above-bed gap still only opens onto a short lip. A soft dip a few
        grey below the bed is crumpled sheet still on the page, so the paper
        beyond it may be a long margin, not only a thin lip.
        """
        saw_soft_below = False
        for step in range(_NEAR_BED_BRIDGE_MAX):
            cur = idx + direction * step
            if direction > 0 and cur >= bound:
                return None
            if direction < 0 and cur < bound:
                return None
            if get_paper(cur):
                # The next line is already paper: normal expand. An above-bed
                # gap only counts when the paper beyond it dies out again (a
                # short lip), not when it opens into a long bright plateau.
                if step == 0 or self._paper_run_ends(cur, direction, bound, get_paper):
                    return cur
                if saw_soft_below:
                    return cur
                return None
            # Soft near-bed sheet: a little above the bed, or a shallow dip
            # into crumpled paper. Deep dark, and anything past bright_keep,
            # stay stops.
            mean = float(get_mean(cur))
            if mean < self.bed - _NEAR_BED_BRIDGE_BELOW or mean - self.bed > self.bright_keep:
                return None
            if mean < self.bed:
                saw_soft_below = True
            if float(get_shadow(cur)) >= 0.5:
                return None
        return None

    def _paper_run_ends(self, idx: int, direction: int, bound: int, get_paper) -> bool:
        """True when paper at ``idx`` stops within a few lines along ``direction``."""
        for step in range(1, 9):
            cur = idx + direction * step
            if direction > 0 and cur >= bound:
                return False
            if direction < 0 and cur < bound:
                return False
            if not get_paper(cur):
                return True
        return False

    def _paper_row(self, y: int, x0: int, x1: int) -> bool:
        return self.bright[y, x0:x1].mean() >= self.frac and self.shadow[y, x0:x1].mean() < 0.5

    def _bed_fringe_col(
        self, x: int, y0: int, y1: int, inward: int = 0, far: int = 0
    ) -> bool:
        # A short bright lip is the sheet edge, not bed fringe. A long run of
        # dim paper is the pale tail of the sheet and still gets shaved.
        if inward and self._thin_paper_lip(
            x,
            inward,
            far,
            lambda c: self._paper_col(c, y0, y1),
            lambda c: self._line_mean_col(c, y0, y1),
        ):
            return False
        mean = float(self.gray[y0:y1, x].mean())
        if mean >= self.bed + self.bright_keep:
            return False
        return abs(mean - self.bed) <= self.soft_tol

    def _bed_fringe_row(
        self, y: int, x0: int, x1: int, inward: int = 0, far: int = 0
    ) -> bool:
        if inward and self._thin_paper_lip(
            y,
            inward,
            far,
            lambda r: self._paper_row(r, x0, x1),
            lambda r: self._line_mean_row(r, x0, x1),
        ):
            return False
        mean = float(self.gray[y, x0:x1].mean())
        if mean >= self.bed + self.bright_keep:
            return False
        return abs(mean - self.bed) <= self.soft_tol

    def _thin_paper_lip(
        self, idx: int, direction: int, bound: int, get_paper, get_mean
    ) -> bool:
        """A short paper lip whose inside is still pale sheet, not a dark page.

        A bright halo on the outside of a dark sheet also dies out within a
        few lines. That inside line drops below the bed, so tighten still
        shaves it. A pale receipt stays above the bed on the inside of the lip.
        """
        if not get_paper(idx) or not self._paper_run_ends(idx, direction, bound, get_paper):
            return False
        for step in range(1, 9):
            cur = idx + direction * step
            if direction > 0 and cur >= bound:
                return False
            if direction < 0 and cur < bound:
                return False
            if not get_paper(cur):
                return float(get_mean(cur)) > self.bed
        return False

    def _jump_sustains(
        self,
        get_mean,
        get_paper,
        idx: int,
        direction: int,
        limit_bound: int,
        level: float,
    ) -> bool:
        """After a big single-step brightness jump to ``level``, check that the
        next ``expand_jump_sustain`` lines hold near that level rather than
        immediately reverting.

        A genuine pale-margin plateau (paper the coarse fuzz clipped) holds
        steady for a run of lines; a bed-side glare/halo spike right next to
        the paper edge peaks for only a line or two before falling back
        toward bed. Same peak-relative ramp test as the outer expand walk,
        just applied ahead of committing to the jump.
        """
        peak = level
        pos = idx
        for _ in range(self.expand_jump_sustain):
            nxt = pos + direction
            if direction > 0 and nxt >= limit_bound:
                return True
            if direction < 0 and nxt < limit_bound:
                return True
            if not get_paper(nxt):
                return False
            mean = get_mean(nxt)
            peak = max(peak, mean)
            if peak - mean > self.expand_ramp_drop:
                return False
            pos = nxt
        return True

    def expand(self, coarse: CropRect, w: int, h: int) -> CropRect:
        x0, y0 = coarse.x, coarse.y
        x1, y1 = coarse.x + coarse.w, coarse.y + coarse.h
        limit = int(self.max_expand_frac * max(w, h))
        ramp = self.expand_ramp_drop
        step = self.expand_max_step

        sheet_bright = self._sheet_bright(coarse)
        left_limit = max(0, x0 - limit)
        left = x0
        origin_l = self._line_mean_col(x0, y0, y1)
        prev_l = peak_l = origin_l
        while True:
            nxt = self._paper_or_near_bed_bridge(
                left - 1,
                -1,
                left_limit,
                lambda x: self._line_mean_col(x, y0, y1),
                lambda x: self._paper_col(x, y0, y1),
                lambda x: float(self.shadow[y0:y1, x].mean()),
            )
            if nxt is None:
                break
            mean = self._line_mean_col(nxt, y0, y1)
            if self._leaving_fringe_into_fill(origin_l, mean, sheet_bright):
                break
            if abs(mean - prev_l) > step and not self._jump_sustains(
                lambda x: self._line_mean_col(x, y0, y1),
                lambda x: self._paper_col(x, y0, y1),
                nxt,
                -1,
                left_limit,
                mean,
            ):
                break
            peak_l = max(peak_l, mean)
            if peak_l - mean > ramp:
                break
            prev_l = mean
            left = nxt
        right_limit = min(w, x1 + limit)
        right = x1
        origin_r = self._line_mean_col(x1 - 1, y0, y1)
        prev_r = peak_r = origin_r
        while True:
            nxt = self._paper_or_near_bed_bridge(
                right,
                1,
                right_limit,
                lambda x: self._line_mean_col(x, y0, y1),
                lambda x: self._paper_col(x, y0, y1),
                lambda x: float(self.shadow[y0:y1, x].mean()),
            )
            if nxt is None:
                break
            mean = self._line_mean_col(nxt, y0, y1)
            if self._leaving_fringe_into_fill(origin_r, mean, sheet_bright):
                break
            if abs(mean - prev_r) > step and not self._jump_sustains(
                lambda x: self._line_mean_col(x, y0, y1),
                lambda x: self._paper_col(x, y0, y1),
                nxt,
                1,
                right_limit,
                mean,
            ):
                break
            peak_r = max(peak_r, mean)
            if peak_r - mean > ramp:
                break
            prev_r = mean
            right = nxt + 1
        top_limit = max(0, y0 - limit)
        top = y0
        origin_t = self._line_mean_row(y0, x0, x1)
        prev_t = peak_t = origin_t
        while True:
            nxt = self._paper_or_near_bed_bridge(
                top - 1,
                -1,
                top_limit,
                lambda y: self._line_mean_row(y, left, right),
                lambda y: self._paper_row(y, x0, x1),
                lambda y: float(self.shadow[y, x0:x1].mean()),
            )
            if nxt is None:
                break
            mean = self._line_mean_row(nxt, left, right)
            if self._leaving_fringe_into_fill(origin_t, mean, sheet_bright):
                break
            if abs(mean - prev_t) > step and not self._jump_sustains(
                lambda y: self._line_mean_row(y, left, right),
                lambda y: self._paper_row(y, x0, x1),
                nxt,
                -1,
                top_limit,
                mean,
            ):
                break
            peak_t = max(peak_t, mean)
            if peak_t - mean > ramp:
                break
            prev_t = mean
            top = nxt
        bottom_limit = min(h, y1 + limit)
        bottom = y1
        origin_b = self._line_mean_row(y1 - 1, x0, x1)
        prev_b = peak_b = origin_b
        while True:
            nxt = self._paper_or_near_bed_bridge(
                bottom,
                1,
                bottom_limit,
                lambda y: self._line_mean_row(y, left, right),
                lambda y: self._paper_row(y, left, right),
                lambda y: float(self.shadow[y, left:right].mean()),
            )
            if nxt is None:
                break
            mean = self._line_mean_row(nxt, left, right)
            if self._leaving_fringe_into_fill(origin_b, mean, sheet_bright):
                break
            if abs(mean - prev_b) > step and not self._jump_sustains(
                lambda y: self._line_mean_row(y, left, right),
                lambda y: self._paper_row(y, left, right),
                nxt,
                1,
                bottom_limit,
                mean,
            ):
                break
            peak_b = max(peak_b, mean)
            if peak_b - mean > ramp:
                break
            prev_b = mean
            bottom = nxt + 1
        return CropRect(left, top, max(1, right - left), max(1, bottom - top))

    def tighten_box(self, box: CropRect, w: int, h: int) -> CropRect:
        if not self.tighten:
            return box
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.w, box.y + box.h
        limit = max(6, int(self.max_tighten_frac * max(w, h)))

        left, right, top, bottom = x0, x1, y0, y1
        for _ in range(limit):
            if left + 8 >= right or not self._bed_fringe_col(
                left, top, bottom, inward=1, far=right
            ):
                break
            left += 1
        for _ in range(limit):
            if right - 8 <= left or not self._bed_fringe_col(
                right - 1, top, bottom, inward=-1, far=left
            ):
                break
            right -= 1
        for _ in range(limit):
            if top + 8 >= bottom or not self._bed_fringe_row(
                top, left, right, inward=1, far=bottom
            ):
                break
            top += 1
        for _ in range(limit):
            if bottom - 8 <= top or not self._bed_fringe_row(
                bottom - 1, left, right, inward=-1, far=top
            ):
                break
            bottom -= 1
        return CropRect(left, top, max(1, right - left), max(1, bottom - top))

    def _soft_climbs_to_pale(
        self,
        get_mean,
        start: int,
        stop: int,
        direction: int,
        limit: int,
    ) -> bool:
        """True if inward walk from soft near-bed reaches pale paper (≥bed+bright_keep).

        Rejects edges that fall into sustained dark content (hang-tags, photos)
        so the soft-climb deepen cannot eat those pages.
        """
        start_mean = get_mean(start)
        paper_thr = self.bed + self.bright_keep
        if abs(start_mean - self.bed) > self.soft_tol * 0.55:
            return False
        if start_mean >= paper_thr:
            return False
        dark_thr = self.bed - self.soft_tol
        dark_run = 0
        for i in range(1, limit + 1):
            idx = start + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            mean = get_mean(idx)
            if mean >= paper_thr:
                return mean - start_mean >= 8.0
            if mean < dark_thr:
                dark_run += 1
                if dark_run >= 6:
                    return False
            else:
                dark_run = 0
        return False

    def tighten_soft_pale_climb(self, box: CropRect, w: int, h: int) -> CropRect:
        """Deeper inward shave on soft bed→pale-paper ramps only.

        Normal ``tighten_box`` stops at ``bed + bright_keep``. Long soft fringes
        (receipts on grey beds) still hold ~10px of near-bed grey past that
        budget; this walk continues while still climbing, up to
        ``bright_keep + soft_climb_overshoot``, and stops at the first flat
        paper line.
        """
        if self.soft_climb_tighten_frac <= 0:
            return box
        extra = max(4, int(self.soft_climb_tighten_frac * max(w, h)))
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.w, box.y + box.h
        paper_thr = self.bed + self.bright_keep
        hard_stop = paper_thr + self.soft_climb_overshoot

        def shave(get_mean, start: int, stop: int, direction: int) -> int:
            if not self._soft_climbs_to_pale(get_mean, start, stop, direction, extra):
                return start
            cur = start
            prev = get_mean(start)
            for _ in range(extra):
                nxt = cur + direction
                if direction > 0 and nxt >= stop:
                    break
                if direction < 0 and nxt < stop:
                    break
                mean = get_mean(nxt)
                if mean >= hard_stop:
                    break
                if mean >= paper_thr and mean < prev - 0.25:
                    break
                if mean >= paper_thr and abs(mean - prev) <= 0.25:
                    cur = nxt
                    break
                cur = nxt
                prev = mean
            return cur

        left = shave(lambda x: self._line_mean_col(x, y0, y1), x0, x1, 1)
        right_incl = shave(
            lambda x: self._line_mean_col(x, y0, y1), x1 - 1, x0, -1
        )
        top = shave(
            lambda y: self._line_mean_row(y, left, right_incl + 1), y0, y1, 1
        )
        bottom_incl = shave(
            lambda y: self._line_mean_row(y, left, right_incl + 1), y1 - 1, y0, -1
        )
        return CropRect(
            left,
            top,
            max(1, right_incl - left + 1),
            max(1, bottom_incl - top + 1),
        )

    def _line_mean_col(self, x: int, y0: int, y1: int) -> float:
        return float(self.gray[y0:y1, x].mean())

    def _line_mean_row(self, y: int, x0: int, x1: int) -> float:
        return float(self.gray[y, x0:x1].mean())

    def _line_std_col(self, x: int, y0: int, y1: int) -> float:
        return float(self.gray[y0:y1, x].std())

    def _line_std_row(self, y: int, x0: int, x1: int) -> float:
        return float(self.gray[y, x0:x1].std())

    def _soft_leads_to_sustained_dark(
        self,
        get_mean,
        start: int,
        stop: int,
        direction: int,
        limit: int,
        *,
        min_dark: int = 12,
    ) -> bool:
        """True if inward walk hits sustained darker-than-bed before bright paper.

        Distinguishes long outer soft-bed strips that fall into dark content
        (safe to search deeper) from pale-card interiors that brighten.
        Brief mid-card valleys reset the dark run and do not qualify.
        """
        thr = self.bed + self.bright_keep
        dark_thr = self.bed - self.soft_tol
        dark_run = 0
        for i in range(1, limit + 1):
            idx = start + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            mean = get_mean(idx)
            if mean >= thr:
                return False
            if mean < dark_thr:
                dark_run += 1
                if dark_run >= min_dark:
                    return True
            else:
                dark_run = 0
        return False

    def _void_band_start(self, start_mean: float) -> bool:
        """True when the edge sits in a hang-hole-style void below the bed.

        Soft near-bed (within ``soft_tol * 0.55``) keeps the existing runway.
        Void starts just outside that band and stops at ``soft_tol`` — deeper
        ink is content, not a halo. Pale faces only a few grey below the bed
        (bed−8) stay on the short budget.
        """
        return (
            self.bed - self.soft_tol
            < start_mean
            <= self.bed - self.soft_tol * 0.45
        )

    def _void_leads_to_pale_sheet(
        self,
        get_mean,
        start: int,
        stop: int,
        direction: int,
        limit: int,
        get_bright=None,
    ) -> bool:
        """True if a long below-bed void later holds as pale paper.

        A brief above-bed lip before dark content (bird silhouette) fails the
        sustained-pale check, so that edge keeps the short snap budget.

        A mixed edge whose line mean sits below the bed only because a dark
        minority pulls it down (bright fraction still high) is page content or
        a printed border, not a hang-hole halo.
        """
        if not self._void_band_start(float(get_mean(start))):
            return False
        if get_bright is not None and float(get_bright(start)) >= 0.35:
            return False
        below = 0
        cross_at: int | None = None
        for i in range(1, limit + 1):
            idx = start + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            mean = float(get_mean(idx))
            if cross_at is None:
                if mean < self.bed:
                    if get_bright is not None and float(get_bright(idx)) >= 0.35:
                        return False
                    below += 1
                    continue
                if below < 12:
                    return False
                cross_at = i
                hold = 1 if mean >= self.bed - 2.0 else 0
                continue
            if mean >= self.bed - 2.0:
                hold += 1
                # A short pale hold is enough to open the runway; the picker
                # still demands a longer hold on the collected samples.
                if hold >= 4:
                    return True
            else:
                return False
        return False

    def _snap_search_limit(
        self,
        get_mean,
        start: int,
        stop: int,
        direction: int,
        max_in: int,
        max_dim: int,
        get_bright=None,
    ) -> int:
        """Deeper snap budget for outer bright fill or soft-bed→dark strips."""
        soft_cap = max(max_in, int(self.max_soft_snap_frac * max_dim))
        void_cap = max(soft_cap, int(self.max_void_snap_frac * max_dim))
        start_mean = get_mean(start)
        thr = self.bed + self.bright_keep
        if start_mean >= thr:
            # Deskew/border bright fill — need room to reach fill→bed→shadow→card.
            return soft_cap
        if self._void_leads_to_pale_sheet(
            get_mean, start, stop, direction, void_cap, get_bright=get_bright
        ):
            return void_cap
        # Soft near-bed only when clearly close to bed *and* the strip falls into
        # sustained dark content (not pale paper that later brightens).
        if abs(start_mean - self.bed) <= self.soft_tol * 0.55 and self._soft_leads_to_sustained_dark(
            get_mean, start, stop, direction, soft_cap
        ):
            return soft_cap
        return max_in

    def _thin_valley_then_paper(
        self,
        get_mean,
        pos: int,
        direction: int,
        stop: int,
        *,
        peek: int = 14,
        dark_max: int = 8,
    ) -> bool:
        """True if a short dark valley then bright paper lies just inward.

        Distinguishes a bright outer fringe above a registration line (keep
        walking so trough-ascent can land on real paper) from bright fringe
        that opens into a sustained dark photo/stripe (abort — vote/electric).
        """
        thr = self.bed + self.bright_keep
        dark_thr = self.bed - self.soft_tol
        saw_dark = 0
        for i in range(1, peek + 1):
            idx = pos + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            mean = get_mean(idx)
            if mean < dark_thr:
                saw_dark += 1
                if saw_dark > dark_max:
                    return False
            elif saw_dark > 0 and mean >= thr:
                return True
        return False

    def _bright_lip_over_sheet(
        self,
        get_mean,
        pos: int,
        direction: int,
        stop: int,
    ) -> bool:
        """True when a bright run peaks, then falls back to near the bed.

        A glare lip outside a near-bed sheet gets brighter for a few lines
        and then drops. Pale paper stays bright. A dark photo falls well
        below the bed, so that edge still aborts.
        """
        peak = float(get_mean(pos))
        floor = peak
        for i in range(1, 16):
            idx = pos + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            mean = float(get_mean(idx))
            peak = max(peak, mean)
            floor = min(floor, mean)
        # A real lip drops hard. A mild bright wobble on paper does not.
        if peak - floor < 18.0:
            return False
        return floor >= self.bed - self.bright_keep

    def _sheet_line_after_shadow(self, samples: list[tuple]) -> int | None:
        """Line where a short shadow climb reaches the bed, if there is one.

        The steepest step is often the first jump out of the trough. The
        sheet is the later line that has climbed back to the bed.
        """
        if len(samples) < 4:
            return None
        means = [float(sample[1]) for sample in samples]
        floor_i = min(range(len(means)), key=lambda i: means[i])
        if floor_i < 2 or len(samples) - 1 - floor_i > 16:
            return None
        # Shallow dips are paper texture. A real shadow sits well below the bed.
        if means[floor_i] > self.bed - 20.0:
            return None
        if means[0] - means[floor_i] < 20.0:
            return None
        chosen = None
        for i in range(floor_i + 1, len(samples)):
            if means[i] >= self.bed - 2.0:
                chosen = i
                break
        # A one-step recovery is the existing steepest-step case. This is the
        # climb that pauses and only reaches the bed several lines later.
        if chosen is None or abs(int(samples[chosen][0]) - int(samples[floor_i][0])) < 8:
            return None
        return int(samples[chosen][0])

    def _inner_edge_of_bright_lip(self, samples: list[tuple]) -> int | None:
        """Line where a glare lip falls back to the sheet.

        The steepest step is often the outer rise onto the lip. The sheet
        is the later line that has fallen back to near the bed.
        """
        if len(samples) < 4:
            return None
        means = [float(sample[1]) for sample in samples]
        peak_i = max(range(len(means)), key=lambda i: means[i])
        if peak_i < 2 or len(samples) - 1 - peak_i < 2:
            return None
        peak = means[peak_i]
        if peak < self.bed + self.bright_keep:
            return None
        # Only a thin lip. A wide bright margin that darkens much later is paper.
        inner = means[peak_i : peak_i + 15]
        if len(inner) < 3:
            return None
        floor = min(inner)
        if peak - floor < 18.0 or floor < self.bed - self.bright_keep:
            return None
        # The margin inset steps off this peak. Walking down to the sheet
        # would cut the page on top of that inset.
        return int(samples[peak_i][0])

    def _quiet_sheet_after_bright_margin(self, samples: list[tuple]) -> int | None:
        """Line where a bright margin falls onto a quiet near-bed sheet.

        A later busy step on that sheet is an inner border. A walk that
        afterwards falls well below the bed is a dark sheet, and that edge
        stays with the trough picker.
        """
        if len(samples) < 20:
            return None
        means = [float(sample[1]) for sample in samples]
        stds = [float(sample[2]) if len(sample) > 2 else 99.0 for sample in samples]
        if means[0] < self.bed + self.bright_keep:
            return None
        cross = None
        for i in range(1, min(13, len(means))):
            if means[i] <= self.bed + 2.0 and stds[i] < 15.0:
                cross = i
                break
        if cross is None:
            return None
        run_means = means[cross : cross + 12]
        run_stds = stds[cross : cross + 12]
        if len(run_means) < 12:
            return None
        if max(run_means) - min(run_means) > 16.0 or max(run_stds) >= 15.0:
            return None
        if min(run_means) < self.bed - 20.0:
            return None
        if min(means[cross:]) < self.bed - self.soft_tol - 10.0:
            return None
        return int(samples[cross][0])

    def _local_edge_on_quiet_sheet(self, samples: list[tuple]) -> int | None:
        """Edge to keep when the walk already starts on a quiet sheet.

        A pale receipt is paper at the tightened line. The pink stripe further
        in is a larger step, and it is not the edge. Only a clear step in the
        first few lines still counts. A bed or bright-fill start is not this
        case, and neither is a mixed line (those still use the full walk).
        """
        if len(samples) < 2:
            return None
        start_mean = float(samples[0][1])
        start_std = float(samples[0][2]) if len(samples[0]) > 2 else 99.0
        if start_mean >= self.bed - self.bright_keep:
            return None
        # Same quiet-line gate as the bright-margin sheet check. A mixed
        # bed/paper column is busier than this and still has a ramp to find.
        if start_std >= 15.0:
            return None
        # A step as large as the bright-keep band changes the whole line. That
        # is still the edge, even when it sits further in. A smaller step is a
        # stripe or texture on a sheet we have already reached.
        largest = 0.0
        for i in range(1, len(samples)):
            largest = max(
                largest, abs(float(samples[i][1]) - float(samples[i - 1][1]))
            )
        if largest >= self.bright_keep:
            return None
        lip = self._dark_lip_trough(samples)
        if lip is not None:
            return lip
        floor = 2.0 * self.min_snap_grad
        held = self._held_level_after_step(samples, start_mean, floor)
        if held is not None:
            return held
        best_i = 0
        best_g = floor
        found = False
        for i in range(1, min(8, len(samples))):
            step = abs(float(samples[i][1]) - float(samples[i - 1][1]))
            if step >= best_g:
                best_g = step
                best_i = i
                found = True
        if not found:
            return int(samples[0][0])
        return int(samples[best_i][0])

    def _dark_lip_trough(self, samples: list[tuple]) -> int | None:
        """Trough of a dark lip that falls and comes back to the same grey.

        The lip is the page edge. A climb that settles brighter than the
        start has walked out of a shadow onto the sheet, and that landing
        is the edge instead.
        """
        means = [float(sample[1]) for sample in samples]
        floor_i = min(range(len(means)), key=lambda i: means[i])
        if floor_i < 2 or floor_i > len(means) - 6:
            return None
        start = means[0]
        floor = means[floor_i]
        drop = start - floor
        if drop < 8.0:
            return None
        settled_i = floor_i
        while (
            settled_i + 1 < len(means)
            and means[settled_i + 1] > means[settled_i] + 0.5
        ):
            settled_i += 1
        plateau = means[settled_i + 1 : settled_i + 7]
        if len(plateau) < 4 or max(plateau) - min(plateau) > 6.0:
            return None
        settled = sum(plateau) / len(plateau)
        if settled < floor + 0.5 * drop or settled > start + 4.0:
            return None
        return int(samples[floor_i][0])

    def _held_level_after_step(
        self,
        samples: list[tuple],
        start_mean: float,
        floor: float,
    ) -> int | None:
        """First step whose new grey is still there several lines later.

        A shallow shadow climb lands on the sheet and stays. A pink stripe
        is a one-line dip, or it sits at the end of the walk with no landing
        to confirm.
        """
        hold = 6
        for i in range(1, len(samples) - hold):
            landing = float(samples[i][1])
            step = landing - float(samples[i - 1][1])
            if abs(step) < floor:
                continue
            shift = landing - start_mean
            if abs(shift) < floor:
                continue
            tail = [float(samples[i + k][1]) for k in range(1, hold + 1)]
            if all(abs(mean - start_mean) >= floor and (mean - start_mean) * shift > 0 for mean in tail):
                return int(samples[i][0])
        return None

    def _bright_paper_abort(
        self,
        prev: float,
        cur: float,
        *,
        get_mean=None,
        pos: int | None = None,
        direction: int | None = None,
        stop: int | None = None,
        from_void: bool = False,
    ) -> bool:
        """Stop when consecutive lines look like pale paper (flat/rising bright).

        Descending bright→bed (outer deskew fill) must keep walking so the
        trough/ramp under the fill remains visible to the snap picker.

        Also stop on a flat *above-bed* pale plateau (cream card face just under
        ``bed + bright_keep``). Soft-bed runway otherwise walks that face into
        a later dark content stripe and trough-ascent snaps to the stripe's
        far edge — under-cropping the header margin. Soft bed *at* bed grey
        must still walk (outer fringe → content), so the pale-face floor is
        strictly above ``bed``.

        Bright ``bed+bright_keep`` rising abort is skipped when a thin dark
        valley then paper is immediately inward (registration-line fringe),
        or when the bright run is a glare lip that falls back to the bed.

        A plateau only a few grey above the bed is still the bed when a
        brighter sheet holds further in. The corner estimate can sit that
        far under the real bed. The abort stays when the brighter run is
        the next lines, which is the sheet edge itself.

        A walk that started in a hang-hole void may enter the pale sheet; the
        pale-plateau abort waits until the void picker has enough pale hold.
        """
        thr = self.bed + self.bright_keep
        # Tiny epsilon absorbs float noise; any real descent continues.
        bright_run = prev >= thr and cur >= thr and cur >= prev - 0.25
        pale_face = self.bed + 3.0
        pale_plateau = (
            prev >= pale_face and cur >= pale_face and cur >= prev - 0.25
        )
        if not bright_run and not pale_plateau:
            return False
        if (
            get_mean is not None
            and pos is not None
            and direction is not None
            and stop is not None
            and (
                self._thin_valley_then_paper(get_mean, pos, direction, stop)
                or self._bright_lip_over_sheet(get_mean, pos, direction, stop)
            )
            ):
            return False
        if (
            not bright_run
            and pale_plateau
            and cur < self.bed + 8.0
            and self._brighter_sheet_ahead(get_mean, pos, direction, stop, cur)
        ):
            return False
        if from_void and pale_plateau and not bright_run:
            return False
        return True

    def _brighter_sheet_ahead(
        self,
        get_mean,
        pos: int | None,
        direction: int | None,
        stop: int | None,
        cur: float,
    ) -> bool:
        """True when a brighter run holds well inside this near-bed plateau.

        The first line of that run has to sit at least 15 lines in. A rise
        on the next few lines is the sheet edge, and the walk should stop.
        """
        if get_mean is None or pos is None or direction is None or stop is None:
            return False
        hold = 0
        for i in range(1, 90):
            idx = pos + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            if get_mean(idx) >= cur + 8.0:
                hold += 1
                if hold >= 4:
                    return i >= 15
            else:
                hold = 0
        return False

    def _void_then_pale_edge(self, samples: list[tuple]) -> int | None:
        """Line where a long below-bed void climbs onto sustained pale paper.

        Hang-hole halos are flatter than a shadow trough, so the strongest-step
        picker leaves the edge on the void. Require a long below-bed run and a
        pale hold so a brief lip before dark content does not win. Snap at the
        climb peak on that pale sheet, not the first bed crossing (the ramp
        onto a hang-tag card often runs another ~10 px past bed grey).
        """
        if len(samples) < 20:
            return None
        if not self._void_band_start(float(samples[0][1])):
            return None
        # Sample tuple may carry bright fraction as a 4th field.
        if len(samples[0]) > 3 and float(samples[0][3]) >= 0.35:
            return None
        below = 0
        cross_i: int | None = None
        hold_end: int | None = None
        for i in range(1, len(samples)):
            mean = float(samples[i][1])
            bright = float(samples[i][3]) if len(samples[i]) > 3 else 0.0
            if cross_i is None:
                if mean < self.bed:
                    if bright >= 0.35:
                        return None
                    below += 1
                    continue
                if below < 12:
                    return None
                cross_i = i
                hold = 1 if mean >= self.bed - 2.0 else 0
                continue
            if mean >= self.bed - 2.0:
                hold += 1
                if hold >= 8:
                    hold_end = i
                    break
            else:
                return None
        if cross_i is None or hold_end is None:
            return None
        peak_i = cross_i
        peak_mean = float(samples[cross_i][1])
        for i in range(cross_i + 1, len(samples)):
            mean = float(samples[i][1])
            if mean < self.bed - 2.0:
                return None
            if mean > peak_mean:
                peak_mean = mean
                peak_i = i
                continue
            if peak_mean - mean >= 2.0 and peak_mean >= self.bed + 3.0:
                break
        return int(samples[peak_i][0])

    def _snap_low(
        self,
        get_mean,
        get_std,
        start: int,
        stop: int,
        max_in: int,
        max_dim: int,
        get_bright=None,
    ) -> int:
        """Move a low edge (left/top) inward to the bed/content boundary.

        If the walk shows a dark trough then a climb (shadow valley outside a
        card), snap on that climb. Otherwise use the strongest |Δ| (soft
        bed→dark content).
        """
        samples: list[tuple] = []
        prev = get_mean(start)
        bright0 = float(get_bright(start)) if get_bright is not None else 0.0
        samples.append((start, prev, get_std(start), bright0))
        void_cap = max(
            max(max_in, int(self.max_soft_snap_frac * max_dim)),
            int(self.max_void_snap_frac * max_dim),
        )
        from_void = self._void_leads_to_pale_sheet(
            get_mean, start, stop, 1, void_cap, get_bright=get_bright
        )
        limit = self._snap_search_limit(
            get_mean, start, stop, 1, max_in, max_dim, get_bright=get_bright
        )
        for i, idx in enumerate(range(start + 1, stop)):
            if i >= limit:
                break
            cur = get_mean(idx)
            if self._bright_paper_abort(
                prev,
                cur,
                get_mean=get_mean,
                pos=idx,
                direction=1,
                stop=stop,
                from_void=from_void,
            ):
                break
            bright = float(get_bright(idx)) if get_bright is not None else 0.0
            samples.append((idx, cur, get_std(idx), bright))
            prev = cur
        return self._pick_snap_from_samples(samples, start)

    def _monotonic_dark_descent(self, samples: list[tuple[int, float]]) -> bool:
        """True when the walk only falls, with no climb along the way.

        A shadow valley climbs after its trough. A tilted card face does not.
        A sheet that rises and then dips at the end is not this face either:
        the darkest line being last is not the same as a walk that only falls.
        """
        means = [sample[1] for sample in samples]
        if len(means) < 2:
            return False
        for i in range(1, len(means)):
            if means[i] - means[i - 1] >= self.min_snap_grad:
                return False
        return True

    def _pick_snap_from_samples(
        self,
        samples: list[tuple[int, float]],
        start_idx: int,
    ) -> int:
        """Pick snap index (inclusive sample idx along the inward walk)."""
        if len(samples) < 2:
            return start_idx
        void_pale = self._void_then_pale_edge(samples)
        if void_pale is not None:
            return void_pale
        sheet = self._sheet_line_after_shadow(samples)
        if sheet is not None:
            return sheet
        lip = self._inner_edge_of_bright_lip(samples)
        if lip is not None:
            return lip
        quiet = self._quiet_sheet_after_bright_margin(samples)
        if quiet is not None:
            return quiet
        # Strongest absolute step (fallback).
        best_abs_i = 0
        best_abs_g = -1.0
        for i in range(1, len(samples)):
            g = abs(samples[i][1] - samples[i - 1][1])
            if g > best_abs_g:
                best_abs_g = g
                best_abs_i = i
        abs_idx = samples[best_abs_i][0]

        # Valley: walk finds a deeper trough then a climb — prefer that climb
        # (shadow→card) over the bed→shadow descent.
        edge_mean = samples[0][1]
        means = [sample[1] for sample in samples]
        floor_i = int(min(range(len(means)), key=lambda i: means[i]))
        floor_mean = means[floor_i]
        near_bed = self.bed - self.soft_tol
        deep_trough = (
            floor_i >= 2
            and floor_mean < near_bed - 8.0
            and edge_mean - floor_mean >= 20.0
        )
        if deep_trough:
            ascent_end = floor_i
            saw_ascent = False
            for i in range(floor_i + 1, len(samples)):
                rise = samples[i][1] - samples[i - 1][1]
                if rise >= self.min_snap_grad:
                    saw_ascent = True
                    ascent_end = i
                elif saw_ascent and rise < self.min_snap_grad * 0.35:
                    break
            if saw_ascent:
                landed = samples[ascent_end][1]
                recovery = landed - floor_mean
                drop = edge_mean - floor_mean
                # Real shadow valleys recover a chunk of the drop. Soft
                # bed→shadow ramps can drop 100+ grey while dark cards only
                # climb trough→card (~15–40), so cap the fraction-of-drop
                # demand — a 2-grey photo wiggle still fails the floor of 10
                # and usually fails sustained-ascent / deep_trough.
                min_recovery = max(10.0, min(0.20 * drop, 20.0))
                # Shallow dark lip on a dark face: trough and landing both sit
                # well below the bed, so a few grey of climb is the whole rise
                # onto the face. Only relax when the trough sits near the outer
                # ramp (right after the strongest step) — a deep texture dip
                # inside the page keeps the 10-grey floor.
                if (
                    landed <= near_bed
                    and floor_mean <= near_bed - 8.0
                    and floor_i <= 20
                    and best_abs_i < floor_i
                ):
                    min_recovery = min(min_recovery, 3.0)
                if (
                    landed <= edge_mean + 12.0
                    and recovery >= min_recovery
                    and not self._climb_lands_on_flat_face(samples, floor_i, ascent_end)
                ):
                    return samples[ascent_end][0]

        if best_abs_g >= self.min_snap_grad:
            # A tilted dark face keeps falling, so the biggest step sits inside
            # the card. Take the outer knee. A later climb is a shadow valley;
            # that edge stays on the strongest step (or the trough ascent above).
            start_mean = samples[0][1]
            if (
                start_mean < self.bed - self.soft_tol
                and self._monotonic_dark_descent(samples)
            ):
                floor = max(self.min_snap_grad, 0.75 * best_abs_g)
                for i in range(1, len(samples)):
                    step = abs(samples[i][1] - samples[i - 1][1])
                    if step >= floor:
                        return samples[i][0]
            local = self._local_edge_on_quiet_sheet(samples)
            if local is not None:
                return local
            return abs_idx
        return start_idx

    def gradient_snap(self, box: CropRect, w: int, h: int) -> CropRect:
        """Snap each edge inward to the bed→paper intensity ramp (not a fixed soft_tol)."""
        if not self.snap:
            return box
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.w, box.y + box.h
        max_dim = max(w, h)
        max_in = max(8, int(self.max_snap_frac * max_dim))

        left = self._snap_low(
            lambda x: self._line_mean_col(x, y0, y1),
            lambda x: self._line_std_col(x, y0, y1),
            x0,
            x1 - 8,
            max_in,
            max_dim,
            get_bright=lambda x: self._bright_frac_col(x, y0, y1),
        )
        right_incl = self._snap_high_inclusive(
            lambda x: self._line_mean_col(x, y0, y1),
            lambda x: self._line_std_col(x, y0, y1),
            x1,
            x0 + 8,
            max_in,
            max_dim,
            get_bright=lambda x: self._bright_frac_col(x, y0, y1),
        )
        right = right_incl + 1
        top = self._snap_low(
            lambda y: self._line_mean_row(y, x0, x1),
            lambda y: self._line_std_row(y, x0, x1),
            y0,
            y1 - 8,
            max_in,
            max_dim,
            get_bright=lambda y: self._bright_frac_row(y, x0, x1),
        )
        bottom_incl = self._snap_high_inclusive(
            lambda y: self._line_mean_row(y, x0, x1),
            lambda y: self._line_std_row(y, x0, x1),
            y1,
            y0 + 8,
            max_in,
            max_dim,
            get_bright=lambda y: self._bright_frac_row(y, x0, x1),
        )
        bottom = bottom_incl + 1
        if right <= left + 8:
            right = x1
        if bottom <= top + 8:
            bottom = y1
        return CropRect(left, top, max(1, right - left), max(1, bottom - top))

    def _climb_lands_on_flat_face(
        self,
        samples: list[tuple],
        floor_i: int,
        ascent_end: int,
    ) -> bool:
        """True when a dark-start climb recovers onto a uniform face.

        A tilted sheet mixes bed and card along the line, so the trough is
        busy and the face inside it is flat. Real shadow→content climbs get
        busier. Walks that still start on the bed are not this case.
        """
        if samples[0][1] >= self.bed - self.soft_tol:
            return False
        floor = samples[floor_i]
        landed = samples[ascent_end]
        if len(floor) < 3 or len(landed) < 3:
            return False
        floor_std = float(floor[2])
        if floor_std < 15.0:
            return False
        return float(landed[2]) < floor_std * 0.6

    def _snap_high_inclusive(
        self,
        get_mean,
        get_std,
        start_excl: int,
        stop: int,
        max_in: int,
        max_dim: int,
        get_bright=None,
    ) -> int:
        """Like _snap_high but returns the inclusive edge sample index."""
        samples: list[tuple] = []
        prev = get_mean(start_excl - 1)
        bright0 = (
            float(get_bright(start_excl - 1)) if get_bright is not None else 0.0
        )
        samples.append((start_excl - 1, prev, get_std(start_excl - 1), bright0))
        void_cap = max(
            max(max_in, int(self.max_soft_snap_frac * max_dim)),
            int(self.max_void_snap_frac * max_dim),
        )
        from_void = self._void_leads_to_pale_sheet(
            get_mean,
            start_excl - 1,
            stop,
            -1,
            void_cap,
            get_bright=get_bright,
        )
        limit = self._snap_search_limit(
            get_mean,
            start_excl - 1,
            stop,
            -1,
            max_in,
            max_dim,
            get_bright=get_bright,
        )
        for i, idx in enumerate(range(start_excl - 2, stop - 1, -1)):
            if i >= limit:
                break
            cur = get_mean(idx)
            if self._bright_paper_abort(
                prev,
                cur,
                get_mean=get_mean,
                pos=idx,
                direction=-1,
                stop=stop,
                from_void=from_void,
            ):
                break
            bright = float(get_bright(idx)) if get_bright is not None else 0.0
            samples.append((idx, cur, get_std(idx), bright))
            prev = cur
        return self._pick_snap_from_samples(samples, start_excl - 1)

    def _shadow_frac_col(self, x: int, y0: int, y1: int) -> float:
        return float(self.shadow[y0:y1, x].mean())

    def _shadow_frac_row(self, y: int, x0: int, x1: int) -> float:
        return float(self.shadow[y, x0:x1].mean())

    def _bright_frac_col(self, x: int, y0: int, y1: int) -> float:
        return float(self.bright[y0:y1, x].mean())

    def _bright_frac_row(self, y: int, x0: int, x1: int) -> float:
        return float(self.bright[y, x0:x1].mean())

    def _shadow_recovery_ahead(
        self,
        *,
        direction: int,
        start: int,
        stop: int,
        limit: int,
        get_mean,
        get_bright,
    ) -> bool:
        """True if inward walk reaches near-bed grey or bright pale paper.

        A mere rise from a dark trough is not enough — photo cards often climb
        into mid-grey content without ever approaching bed/paper luminance.
        """
        near_bed = self.bed - self.soft_tol
        for i in range(1, limit + 1):
            idx = start + direction * i
            if direction > 0 and idx >= stop:
                break
            if direction < 0 and idx < stop:
                break
            mean = get_mean(idx)
            if mean >= near_bed:
                return True
            if get_bright(idx) >= 0.35 and mean >= self.bed - 2.0 * self.soft_tol:
                return True
        return False

    def _peel_shadow_edge(
        self,
        *,
        start: int,
        stop: int,
        direction: int,
        limit: int,
        get_mean,
        get_shadow,
        get_bright,
        get_std,
    ) -> int:
        """Peel one edge through a shadow valley toward paper; return new edge idx.

        `start` is the inclusive edge sample; `direction` points inward.
        For a high exclusive edge, pass start=right-1 and apply result+1 outside.
        A busy start line is a printed border, not a smooth drop-shadow.
        """
        thr = self.shadow_peel_frac
        near_bed = self.bed - self.soft_tol
        if get_shadow(start) < thr or get_std(start) > _BUSY_EDGE_STD:
            return start
        if not self._shadow_recovery_ahead(
            direction=direction,
            start=start,
            stop=stop,
            limit=limit,
            get_mean=get_mean,
            get_bright=get_bright,
        ):
            return start

        pos = start
        seen_ascent = False
        for _ in range(limit):
            nxt = pos + direction
            if direction > 0 and nxt >= stop:
                break
            if direction < 0 and nxt < stop:
                break
            if get_bright(pos) >= 0.35 or get_mean(pos) >= near_bed:
                break
            if get_shadow(pos) < thr:
                break
            rise = get_mean(nxt) - get_mean(pos)
            if rise > 2.0:
                seen_ascent = True
                pos = nxt
                continue
            if not seen_ascent:
                # Still in the outer trough — keep walking toward paper.
                pos = nxt
                continue
            # Ascent stalled below near-bed: dark content, not scanner bed.
            break
        return pos

    def peel_shadow(self, box: CropRect, w: int, h: int) -> CropRect:
        """Shave dark scanner drop-shadow when paper/near-bed recovers inward.

        Dark photographic content has no near-bed/bright recovery, so it is left
        alone. Depth is capped by image scale.
        """
        if not self.shadow_peel:
            return box
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.w, box.y + box.h
        limit = max(6, int(self.max_shadow_peel_frac * max(w, h)))

        left = self._peel_shadow_edge(
            start=x0,
            stop=x1 - 8,
            direction=1,
            limit=limit,
            get_mean=lambda x: self._line_mean_col(x, y0, y1),
            get_shadow=lambda x: self._shadow_frac_col(x, y0, y1),
            get_bright=lambda x: self._bright_frac_col(x, y0, y1),
            get_std=lambda x: self._line_std_col(x, y0, y1),
        )
        right_incl = self._peel_shadow_edge(
            start=x1 - 1,
            stop=left + 8,
            direction=-1,
            limit=limit,
            get_mean=lambda x: self._line_mean_col(x, y0, y1),
            get_shadow=lambda x: self._shadow_frac_col(x, y0, y1),
            get_bright=lambda x: self._bright_frac_col(x, y0, y1),
            get_std=lambda x: self._line_std_col(x, y0, y1),
        )
        right = right_incl + 1
        top = self._peel_shadow_edge(
            start=y0,
            stop=y1 - 8,
            direction=1,
            limit=limit,
            get_mean=lambda y: self._line_mean_row(y, left, right),
            get_shadow=lambda y: self._shadow_frac_row(y, left, right),
            get_bright=lambda y: self._bright_frac_row(y, left, right),
            get_std=lambda y: self._line_std_row(y, left, right),
        )
        bottom_incl = self._peel_shadow_edge(
            start=y1 - 1,
            stop=top + 8,
            direction=-1,
            limit=limit,
            get_mean=lambda y: self._line_mean_row(y, left, right),
            get_shadow=lambda y: self._shadow_frac_row(y, left, right),
            get_bright=lambda y: self._bright_frac_row(y, left, right),
            get_std=lambda y: self._line_std_row(y, left, right),
        )
        bottom = bottom_incl + 1
        if right <= left + 8:
            left, right = x0, x1
        if bottom <= top + 8:
            top, bottom = y0, y1
        return CropRect(left, top, max(1, right - left), max(1, bottom - top))

    def _upper_level(self, line: np.ndarray) -> float:
        return float(np.percentile(line, 90))

    def _on_swallowed_sheet(self, level: float, sheet: float) -> bool:
        """True when a line is the pale sheet, and that sheet sits on the bed estimate.

        A corner estimate pulled bright by deskew fill lands inside the paper.
        The sheet then lives in the same narrow band as the bed, so the coarse
        box keeps the ink and leaves the blank margin outside. A line brighter
        than the sheet is fill or a halo, not that margin.
        """
        near_bed = self.bed < level <= self.bed + self.bright_keep
        near_sheet = (sheet - self.bright_keep) <= level <= (sheet + self.bright_keep)
        return near_bed and near_sheet

    def _swallowed_sheet_run(
        self,
        start: int,
        direction: int,
        bound: int,
        get_line,
        sheet: float,
        min_len: int,
    ) -> int | None:
        """Last line of a long pale run that ends by falling off the sheet."""
        last = None
        steps = 0
        idx = start
        while True:
            if direction > 0 and idx >= bound:
                return None
            if direction < 0 and idx < bound:
                return None
            line = get_line(idx)
            if line.size < 8:
                return None
            level = self._upper_level(line)
            if self._on_swallowed_sheet(level, sheet):
                last = idx
                idx += direction
                steps += 1
                continue
            fell = level <= self.bed or level < sheet - self.bright_keep
            if last is not None and fell and steps >= min_len:
                return last
            return None

    def recover_swallowed_sheet(self, box: CropRect, w: int, h: int) -> CropRect:
        """Grow into a blank margin the corner bed estimate treated as bed."""
        sheet = self._sheet_bright(box)
        min_len = max(8, int(_SWALLOWED_SHEET_MIN_FRAC * max(w, h)))
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.w, box.y + box.h

        top = self._swallowed_sheet_run(
            y0 - 1, -1, 0, lambda y: self.gray[y, x0:x1], sheet, min_len
        )
        if top is not None:
            y0 = top
        bottom = self._swallowed_sheet_run(
            y1, 1, h, lambda y: self.gray[y, x0:x1], sheet, min_len
        )
        if bottom is not None:
            y1 = bottom + 1
        left = self._swallowed_sheet_run(
            x0 - 1, -1, 0, lambda x: self.gray[y0:y1, x], sheet, min_len
        )
        if left is not None:
            x0 = left
        right = self._swallowed_sheet_run(
            x1, 1, w, lambda x: self.gray[y0:y1, x], sheet, min_len
        )
        if right is not None:
            x1 = right + 1
        if x1 <= x0 + 8 or y1 <= y0 + 8:
            return box
        return CropRect(x0, y0, x1 - x0, y1 - y0)

    def refine(self, coarse: CropRect, w: int, h: int) -> CropRect:
        expanded = self.expand(coarse, w, h)
        tightened = self.tighten_box(expanded, w, h)
        tightened = self.tighten_soft_pale_climb(tightened, w, h)
        snapped = self.gradient_snap(tightened, w, h)
        peeled = self.peel_shadow(snapped, w, h)
        return self.recover_swallowed_sheet(peeled, w, h)


class OpenCvCrop:
    def __init__(self, margin_px: int | None = None, min_area_frac: float = 0.05):
        self.margin_px = (
            _env_int("FINAL_EDGE_MARGIN_PX", 3) if margin_px is None else int(margin_px)
        )
        self.min_area_frac = min_area_frac
        self.max_area_frac = _env_float("OPENCV_CROP_MAX_AREA_FRAC", 0.97)
        self.refine = os.environ.get("OPENCV_CROP_REFINE", "true").lower() not in {
            "0",
            "false",
            "no",
        }

    def _coarse_bbox(self, path: Path, width: int, height: int, bed: float) -> CropRect:
        collapse = _env_float("OPENCV_CROP_FUZZ_COLLAPSE_FRAC", 0.10)
        # Adjacent peek uses `collapse`. A bed-blob box may only drop onto the
        # real sheet several fuzz steps later (burger ~61% at fuzz 38); require
        # a much larger drop before walking past a non-collapsing neighbor so
        # mild late shrinks (aeon/watch ~10–16%) do not re-cut settled pages.
        lookahead = _env_float("OPENCV_CROP_FUZZ_LOOKAHEAD_COLLAPSE_FRAC", 0.40)
        keep = 1.0 - collapse
        keep_jump = 1.0 - lookahead
        fuzzes = _fuzz_ladder()
        gray = to_gray(read_bgr(path))
        # Lazily cache usable boxes so normal pages still stop after the first
        # non-collapse, probing only the last ladder step for a large jump.
        cache: dict[int, tuple[int, int, int, int] | None] = {}

        def usable_at(i: int) -> tuple[int, int, int, int] | None:
            if i in cache:
                return cache[i]
            bbox = foreground_bbox(gray, fuzzes[i], bed)
            if bbox is None:
                cache[i] = None
                return None
            cw, ch, cx, cy = bbox
            if not _bbox_usable(cw, ch, width, height, max_area_frac=self.max_area_frac):
                cache[i] = None
                return None
            cache[i] = (cw, ch, cx, cy)
            return cache[i]

        def end_has_large_jump(start: int, pending_area: float) -> bool:
            """True if the last usable fuzz beyond `start` collapses enough.

            Probe from the ladder end so a no-jump page costs one extra
            foreground box (high fuzz), not a full rescan of every step.
            """
            for j in range(len(fuzzes) - 1, start - 1, -1):
                box = usable_at(j)
                if box is None:
                    continue
                uw, uh, _, _ = box
                return uw * uh < keep_jump * pending_area
            return False

        pending: tuple[int, int, int, int] | None = None
        for i in range(len(fuzzes)):
            box = usable_at(i)
            if box is None:
                continue
            cw, ch, cx, cy = box
            if pending is None or collapse <= 0:
                pending = box
                if collapse <= 0:
                    return CropRect(cx, cy, cw, ch)
                continue
            pw, ph, px, py = pending
            area = cw * ch
            pending_area = pw * ph
            if area < keep * pending_area:
                # A colored sheet can fall out of a higher fuzz without the
                # dropped band being scanner bed. Keep the wider box, but keep
                # walking so a later real bed collapse can still win.
                if not _shed_is_same_sheet(gray, pending, box, bed):
                    pending = box
                continue
            if lookahead > 0 and end_has_large_jump(i + 1, pending_area):
                continue
            adopted = self._adopt_bed_strip(
                gray, pending, fuzzes, i, width, height, bed
            )
            if adopted is not None:
                aw, ah, ax, ay = adopted
                return CropRect(ax, ay, aw, ah)
            return CropRect(px, py, pw, ph)
        if pending is not None:
            pw, ph, px, py = pending
            return CropRect(px, py, pw, ph)
        raise CropFailedError(
            f"no usable paper bbox (bed={bed:.1f}, fuzzes={fuzzes})"
        )

    def _first_later_strip(
        self,
        gray: np.ndarray,
        fuzzes: list[float],
        start: int,
        width: int,
        height: int,
        bed: float,
        accept,
    ) -> tuple[int, int, int, int] | None:
        """First later fuzz box ``accept`` keeps, or None."""
        for fuzz in fuzzes[start:]:
            bbox = foreground_bbox(gray, fuzz, bed)
            if bbox is None:
                continue
            cw, ch, _cx, _cy = bbox
            if not _bbox_usable(cw, ch, width, height, max_area_frac=self.max_area_frac):
                continue
            adopted = accept(bbox)
            if adopted is not None:
                return adopted
        return None

    def _adopt_bed_strip(
        self,
        gray: np.ndarray,
        pending: tuple[int, int, int, int],
        fuzzes: list[float],
        start: int,
        width: int,
        height: int,
        bed: float,
    ) -> tuple[int, int, int, int] | None:
        """Replace ``pending`` when a later fuzz only sheds a flat bed band."""
        if start >= len(fuzzes):
            return None
        near_tol = _env_float("OPENCV_CROP_TIGHTEN_BRIGHT_KEEP", 12.0)
        soft_px = _env_float("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", 0.045) * max(width, height)
        if _box_touches_bed(gray, pending, bed, near_tol):
            return self._first_later_strip(
                gray,
                fuzzes,
                start,
                width,
                height,
                bed,
                lambda bbox: _uniform_bed_strip(
                    gray, pending, bbox, bed, soft_px=soft_px, near_tol=near_tol
                ),
            )
        return self._first_later_strip(
            gray,
            fuzzes,
            start,
            width,
            height,
            bed,
            lambda bbox: _off_estimate_bed_strip(
                gray, pending, bbox, bed, soft_px=soft_px
            ),
        )

    def detect_crop(self, image_path: Path) -> tuple[CropRect, int, int]:
        """Return (rect, image_w, image_h) in preprocess coordinates without writing."""
        path = Path(image_path)
        width, height = image_size(path)
        bed = _robust_bed_gray(path)
        coarse = self._coarse_bbox(path, width, height, bed)
        if self.refine:
            gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if gray is not None:
                coarse = _EdgeRefiner(gray.astype(np.float32), bed).refine(
                    coarse, width, height
                )
        rect = _apply_margin(coarse.x, coarse.y, coarse.w, coarse.h, self.margin_px, width, height)
        return rect, width, height

    def crop_page(self, page: PageWork) -> PageWork:
        if os.environ.get("AUTO_CROP", "true").lower() in {"0", "false", "no"}:
            return page
        rect, w, h = self.detect_crop(page.path)
        img = cv2.imread(str(page.path), cv2.IMREAD_COLOR)
        if img is None:
            raise CropFailedError(f"cannot read {page.path}")
        cropped = img[rect.y : rect.y + rect.h, rect.x : rect.x + rect.w]
        if not cv2.imwrite(str(page.path), cropped):
            raise CropFailedError(f"failed to write {page.path}")
        page.preprocess_w = w
        page.preprocess_h = h
        page.crop = rect
        return page
