"""Unit tests for OpenCvCrop.detect_crop (stdlib unittest)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.crop.opencv_crop import OpenCvCrop, _bbox_usable


class OpenCvCropDetectTests(unittest.TestCase):
    def test_detects_dark_rect_on_light_bed(self) -> None:
        img = np.full((400, 300, 3), 200, dtype=np.uint8)
        img[80:320, 40:260] = 40
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, w, h = OpenCvCrop(margin_px=2).detect_crop(path)
            self.assertEqual((w, h), (300, 400))
            self.assertTrue(30 <= rect.x <= 50)
            self.assertTrue(70 <= rect.y <= 90)
            self.assertGreater(rect.w, 150)
            self.assertGreater(rect.h, 150)

    def test_detects_light_paper_on_grey_bed(self) -> None:
        """Light sheet on grey bed (paper is not darker-than-bed)."""
        img = np.full((500, 400, 3), 200, dtype=np.uint8)
        img[60:440, 50:350] = 245
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, w, h = OpenCvCrop(margin_px=2).detect_crop(path)
            self.assertEqual((w, h), (400, 500))
            self.assertLess(rect.x, 60)
            self.assertLess(rect.y, 70)
            self.assertGreater(rect.w, 250)
            self.assertGreater(rect.h, 330)

    def test_refine_recovers_pale_margin(self) -> None:
        """A pale (near-bed) left band must not be clipped: refine grows into it."""
        bed = 205
        # Keep an 80px+ pure-bed border so the corner bed estimate stays clean.
        img = np.full((500, 400, 3), bed, dtype=np.uint8)
        img[100:400, 160:340] = 40  # strong content the coarse fuzz locks onto
        img[100:400, 100:160] = 226  # faint margin: brighter than bed, below fuzz tol
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            os.environ["OPENCV_CROP_MAX_EXPAND_FRAC"] = "0.3"
            try:
                refined, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
                os.environ["OPENCV_CROP_REFINE"] = "false"
                raw, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
            finally:
                os.environ.pop("OPENCV_CROP_REFINE", None)
                os.environ.pop("OPENCV_CROP_MAX_EXPAND_FRAC", None)
            # Refined left edge reaches the pale margin; raw box clips it.
            self.assertLess(refined.x, 130)
            self.assertGreater(raw.x, refined.x + 30)

    def test_expand_reaches_wide_pale_margin_at_default_cap(self) -> None:
        """Unprinted pale margin ~9% of the long side must still be recovered.

        Coarse fuzz locks onto dark ink; expand used to stop at 6% of the long
        side and leave a 90px+ white strip on a card. Default cap must cover
        that class without a per-case override.
        """
        bed = 205
        h, w = 600, 400
        img = np.full((h, w, 3), bed, dtype=np.uint8)
        img[80:520, 160:340] = 40
        img[80:520, 100:160] = 226  # 60px pale = 0.10 of h
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        self.assertLess(rect.x, 115, f"pale margin still clipped: {rect}")

    def test_expand_stops_on_soft_pale_ramp_toward_bed(self) -> None:
        """Bright expand must not walk a soft pale ramp that fades into bed."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 120:300] = 250.0  # solid pale paper
        # Soft ramp 80..120 fading from paper toward bed (outer false margin).
        for x in range(80, 120):
            t = (x - 80) / 40.0
            gray[40:260, x] = bed * (1 - t) + 250.0 * t
        os.environ["OPENCV_CROP_EXPAND_RAMP_DROP"] = "2.5"
        os.environ["OPENCV_CROP_MAX_EXPAND_FRAC"] = "0.2"
        try:
            refiner = _EdgeRefiner(gray, bed)
            coarse = CropRect(120, 40, 180, 220)
            expanded = refiner.expand(coarse, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_EXPAND_RAMP_DROP", None)
            os.environ.pop("OPENCV_CROP_MAX_EXPAND_FRAC", None)
        # May grow a little into the bright end of the ramp, not all the way to 80.
        self.assertGreaterEqual(expanded.x, 100)
        self.assertLess(expanded.x, 120)

    def test_expand_continues_past_mid_plateau_dip_at_default_ramp(self) -> None:
        """A ~2.5-grey dip mid pale plateau must not abort expand (aeon-again top)."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((400, 200), bed, dtype=np.float32)
        # Tall pale margin; coarse starts low; a one-row dip sits mid-walk.
        gray[40:360, 40:160] = 244.0
        gray[80:82, 40:160] = 241.4  # ~2.6 below peak — old ramp 2.5 aborted here
        os.environ["OPENCV_CROP_MAX_EXPAND_FRAC"] = "0.4"
        try:
            refiner = _EdgeRefiner(gray, bed)
            coarse = CropRect(40, 200, 120, 160)
            expanded = refiner.expand(coarse, 200, 400)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_EXPAND_FRAC", None)
        self.assertLessEqual(expanded.y, 50)

    def test_expand_stops_climbing_from_bed_fringe_into_fill(self) -> None:
        """A near-bed edge must not grow into brighter deskew fill."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 209.0
        gray = np.full((400, 300), 230.0, dtype=np.float32)
        gray[40:200, 40:260] = 40.0
        # Bottom fringe sits just above bed, then brighter fill below the card.
        for y, mean in ((200, 218.0), (201, 223.0), (202, 226.0), (203, 227.0)):
            gray[y, 40:260] = mean
        refiner = _EdgeRefiner(gray, bed)
        coarse = CropRect(40, 40, 220, 161)  # bottom exclusive edge at 201
        expanded = refiner.expand(coarse, 300, 400)
        self.assertEqual(expanded.y + expanded.h, 201)

    def test_expand_stops_on_bed_glare_when_dark_card_has_foil(self) -> None:
        """p90 foil on a dark card must not bless bed-side glare as sheet.

        The sheet body is dark; a bright minority (foil/specular) pulls p90
        near bed glare. Expand still stops when the near-bed edge steps out
        into that glare.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((400, 300), bed, dtype=np.float32)
        gray[80:320, 40:260] = 70.0  # dark card body
        # Small foil minority in the inset — enough to lift p90 near the bed,
        # not the median (median stays deep dark, below bed - 2*soft_tol).
        gray[160:200, 120:160] = 235.0
        # Near-bed top fringe, then brighter glare above the card.
        gray[78:80, 40:260] = 223.0
        gray[74:78, 40:260] = 238.0
        refiner = _EdgeRefiner(gray, bed)
        coarse = CropRect(40, 78, 220, 242)
        self.assertLess(refiner._sheet_bright(coarse), bed)
        # Mid-tone sheet with a bright sky must keep p90 (not drop to median).
        photo = gray.copy()
        photo[80:320, 40:260] = 170.0
        photo[140:240, 80:220] = 248.0
        photo_ref = _EdgeRefiner(photo, bed)
        self.assertGreaterEqual(photo_ref._sheet_bright(coarse), bed)
        expanded = refiner.expand(coarse, 300, 400)
        self.assertEqual(expanded.y, 78)

    def test_expand_stops_on_big_single_step_glare_spike(self) -> None:
        """A big one-row/col brightness jump (bed-side glare) is not a gradual
        pale margin — expand must not walk into it, unlike the soft ramp case."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 120:300] = 40.0  # solid dark content
        # Sharp glare spike right next to content: one big step, then it
        # reverts to plain bed within a few lines (too narrow to sustain).
        gray[40:260, 116:120] = 250.0
        os.environ["OPENCV_CROP_MAX_EXPAND_FRAC"] = "0.2"
        try:
            refiner = _EdgeRefiner(gray, bed)
            coarse = CropRect(120, 40, 180, 220)
            expanded = refiner.expand(coarse, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_EXPAND_FRAC", None)
        # Must not walk into the glare spike — left edge stays at content.
        self.assertEqual(expanded.x, 120)

    def test_expand_bridges_a_short_near_bed_gap_to_paper(self) -> None:
        """A few columns just above the bed must not hide a bright paper edge.

        Coarse fuzz can sit inside a pale sheet. The columns between it and
        the bright edge are above the bed but not bright enough to count as
        paper on their own. Expand crosses that short gap. A run at or below
        the bed is the scanner bed and stays put.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((400, 300), bed, dtype=np.float32)
        gray[40:360, 84:250] = bed + 10.0
        gray[40:360, 92:100] = bed + 6.0  # above the bed, not bright enough
        refiner = _EdgeRefiner(gray, bed)
        coarse = CropRect(100, 40, 150, 320)
        expanded = refiner.expand(coarse, 300, 400)
        self.assertEqual(expanded.x, 84)
        # The lip is dimmer than bright_keep, so tighten used to shave it.
        kept = refiner.tighten_box(expanded, 300, 400)
        self.assertEqual(kept.x, 84)

        # A bright halo outside a dark page is not that lip. Tighten shaves it.
        halo = np.full((400, 300), bed, dtype=np.float32)
        halo[40:360, 100:250] = 40.0
        halo[40:360, 96:100] = bed + 10.0
        halo_refiner = _EdgeRefiner(halo, bed)
        loose = CropRect(96, 40, 154, 320)
        shaved = halo_refiner.tighten_box(loose, 300, 400)
        self.assertGreaterEqual(shaved.x, 100)

        dark = np.full((400, 300), bed, dtype=np.float32)
        dark[40:360, 84:250] = bed + 10.0
        dark[40:360, 92:100] = bed - 10.0
        dark_refiner = _EdgeRefiner(dark, bed)
        stayed = dark_refiner.expand(coarse, 300, 400)
        self.assertEqual(stayed.x, 100)

        plateau = np.full((400, 300), bed, dtype=np.float32)
        plateau[40:360, 60:250] = 245.0
        plateau[40:360, 100:250] = bed + 10.0
        plateau[40:360, 92:100] = bed + 6.0
        wide = _EdgeRefiner(plateau, bed).expand(coarse, 300, 400)
        self.assertEqual(wide.x, 100)

    def test_expand_bridges_a_soft_below_bed_crumple_to_paper(self) -> None:
        """A shallow crumple below the bed must not hide a long pale margin.

        Coarse can sit inside crumpled sheet a few grey under the bed. The
        real edge is brighter paper further out. Expand crosses that soft
        depression onto the margin. A deep dark band is still the bed.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((400, 300), bed, dtype=np.float32)
        gray[40:360, 70:250] = bed + 10.0
        gray[40:360, 100:124] = bed - 4.0  # soft crumple, not deep bed
        os.environ["OPENCV_CROP_MAX_EXPAND_FRAC"] = "0.25"
        try:
            refiner = _EdgeRefiner(gray, bed)
            coarse = CropRect(124, 40, 126, 320)
            expanded = refiner.expand(coarse, 300, 400)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_EXPAND_FRAC", None)
        self.assertEqual(expanded.x, 70)

        deep = np.full((400, 300), bed, dtype=np.float32)
        deep[40:360, 70:250] = bed + 10.0
        deep[40:360, 100:124] = bed - 12.0
        stayed = _EdgeRefiner(deep, bed).expand(coarse, 300, 400)
        self.assertEqual(stayed.x, 124)

    def test_refine_does_not_grow_into_bed(self) -> None:
        """The bright-gate must stop expansion at the bed (no runaway growth)."""
        bed = 210
        img = np.full((500, 400, 3), bed, dtype=np.uint8)
        img[100:400, 140:300] = 255  # bright paper, pure bed on all sides
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
            self.assertGreaterEqual(rect.x, 135)
            self.assertLessEqual(rect.x, 150)

    def test_tighten_strips_soft_bed_fringe_not_pale_paper(self) -> None:
        """Soft bed columns are shaved; brighter-than-bed pale paper is kept."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 210.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 80:250] = 40.0  # strong content
        gray[40:260, 250:290] = 245.0  # pale paper
        gray[40:260, 290:330] = bed  # soft bed fringe inside a loose box
        os.environ["OPENCV_CROP_MAX_TIGHTEN_FRAC"] = "0.15"
        os.environ["OPENCV_CROP_GRAD_SNAP"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 250, 220)  # includes fringe to x=330
            tight = refiner.tighten_box(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_TIGHTEN_FRAC", None)
            os.environ.pop("OPENCV_CROP_GRAD_SNAP", None)
        self.assertLess(tight.x + tight.w, loose.x + loose.w)
        self.assertGreaterEqual(tight.x + tight.w, 288)  # kept pale paper (~290)

    def test_pick_skips_climb_onto_flat_face(self) -> None:
        """A dark-start climb onto a uniform face is not the sheet edge.

        The trough is a tilted edge (busy line). The later climb is the flat
        card. Snap stays on the descent, not the end of that climb.
        """
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 209.0)
        samples = [
            (496, 178.0, 19.0),
            (497, 168.0, 25.0),
            (498, 154.0, 31.0),
            (499, 137.0, 37.0),
            (500, 119.0, 41.0),
            (501, 101.0, 42.0),
            (502, 85.0, 40.0),
            (503, 71.0, 36.0),
            (504, 60.0, 33.0),
            (505, 53.0, 31.0),
            (506, 51.0, 30.0),
            (507, 56.0, 32.0),
            (508, 65.0, 35.0),
            (509, 76.0, 36.0),
            (510, 87.0, 33.0),
            (511, 98.0, 23.0),
            (512, 107.0, 14.0),
            (513, 110.0, 10.0),
        ]
        chosen = refiner._pick_snap_from_samples(samples, 496)
        self.assertEqual(chosen, 500)

    def test_pick_keeps_climb_onto_varied_content(self) -> None:
        """A shadow valley that opens onto a varied face still snaps there."""
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 229.0)
        samples = [
            (2904, 193.0, 18.0),
            (2903, 184.0, 24.0),
            (2902, 172.0, 30.0),
            (2901, 156.0, 36.0),
            (2900, 137.0, 40.0),
            (2899, 121.0, 38.0),
            (2898, 107.0, 33.0),
            (2897, 94.0, 28.0),
            (2896, 84.0, 26.0),
            (2895, 80.0, 25.0),
            (2894, 78.0, 24.0),
            (2893, 80.0, 23.0),
            (2892, 89.0, 33.0),
            (2891, 106.0, 54.0),
            (2890, 116.0, 63.0),
            (2889, 122.0, 65.0),
        ]
        self.assertEqual(refiner._pick_snap_from_samples(samples, 2904), 2889)

    def test_gradient_snap_dark_edge_keeps_outer_knee(self) -> None:
        """A walk that already starts dark must not jump to a later inner step.

        Tilted dark cards: the column mean keeps falling across the face, and
        the single biggest step sits inside the card. The outer knee is the edge.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 209.0
        gray = np.full((400, 400), bed, dtype=np.float32)
        # Monotonic descent. Early step is ~80% of the later inner step.
        for x, mean in (
            (260, 167.0),
            (261, 158.0),
            (262, 146.0),
            (263, 133.5),
            (280, 110.0),
            (281, 94.0),
        ):
            gray[:, x] = mean
        for x in range(264, 280):
            gray[:, x] = 120.0
        for x in range(282, 400):
            gray[:, x] = 90.0
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.08"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(260, 40, 120, 300)
            snapped = refiner.gradient_snap(loose, 400, 400)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertLessEqual(snapped.x, 264)
        self.assertGreaterEqual(snapped.x, 261)

    def test_gradient_snap_pulls_in_soft_ramp(self) -> None:
        """Bed→paper intensity ramp snaps the edge inward past a soft fringe."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 80:260] = 80.0  # dark content
        # Steep soft ramp from content to bed on the right (260..290).
        for x in range(260, 290):
            t = (x - 260) / 30.0
            gray[40:260, x] = 80.0 * (1 - t) + bed * t
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.2"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 250, 220)  # right at 330 into bed
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertLess(snapped.x + snapped.w, loose.x + loose.w)
        self.assertLessEqual(snapped.x + snapped.w, 280)

    def test_gradient_snap_prefers_ascent_out_of_shadow_valley(self) -> None:
        """Bed→dark trough→card: snap to trough→card ascent, not bed→trough."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 80:280] = 120.0  # dark card
        gray[40:260, 280:300] = 70.0  # shadow trough
        # Outside 300..330 climbs back toward bed (already bed in array).
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.15"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 250, 220)  # right at 330 into bed
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        right = snapped.x + snapped.w
        self.assertLessEqual(right, 285)
        self.assertGreaterEqual(right, 275)

    def test_gradient_snap_soft_shadow_on_dark_card(self) -> None:
        """Wide soft bed→trough→dark-card ascent must beat bed→trough |Δ|.

        Mirrors goya-style edges: deep soft drop from bed, then only a modest
        climb into a dark card (recovery << 20% of the bed drop).
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 230.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 80:280] = 90.0  # dark card
        gray[40:260, 280:295] = 65.0  # trough floor
        # Soft climb bed←trough on 295..330 (outside the card).
        for x in range(295, 330):
            t = (x - 295) / 35.0
            gray[40:260, x] = 65.0 * (1 - t) + bed * t
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.15"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 250, 220)  # right at 330 into bed
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        right = snapped.x + snapped.w
        self.assertLessEqual(right, 285)
        self.assertGreaterEqual(right, 275)

    def test_gradient_snap_shallow_dark_lip_on_dark_card(self) -> None:
        """A few-grey climb from a dark lip onto a dark face is the card edge.

        Bed→card walks find the steepest step on the outer ramp. A shallow
        trough just inside that ramp is the lip; the face is only ~5 grey
        brighter, so the old 10-grey recovery floor missed it.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 80:270] = 90.0  # dark face
        gray[40:260, 270:276] = 85.0  # shallow dark lip
        # Short outer ramp so the lip sits near the start of the walk.
        for x in range(276, 290):
            t = (x - 276) / 14.0
            gray[40:260, x] = 85.0 * (1 - t) + bed * t
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.15"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 210, 220)  # right at 290 into bed
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        right = snapped.x + snapped.w
        self.assertLessEqual(right, 272)
        self.assertGreaterEqual(right, 265)

    def test_gradient_snap_walks_descending_bright_fill(self) -> None:
        """Outer bright deskew fill must not abort before the shadow→card ascent."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 225.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 70:200] = 250.0  # pale card starts right after trough
        gray[40:260, 50:70] = 90.0  # shadow trough
        # Gentle descending bright fill 0..50 (deskew/border).
        for x in range(0, 50):
            t = x / 50.0
            gray[40:260, x] = 255.0 * (1 - t) + bed * t
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.02"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.2"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(0, 40, 280, 220)
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertGreaterEqual(snapped.x, 65)
        self.assertLessEqual(snapped.x, 85)

    def test_gradient_snap_keeps_the_peak_of_a_bright_glare_lip(self) -> None:
        """A glare lip that falls back to the bed snaps at the peak, not the sheet.

        The final margin insets from that peak. Walking on down to the sheet
        would cut the page. A fall into a dark photo is not a lip.
        """
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 210.0)
        lip = [
            (3581, 228.0, 2.0),
            (3576, 234.0, 2.0),
            (3572, 240.0, 2.0),
            (3569, 237.0, 2.0),
            (3560, 216.0, 2.0),
        ]
        self.assertEqual(refiner._pick_snap_from_samples(lip, 3581), 3572)
        into_photo = [
            (300, 230.0, 2.0),
            (290, 245.0, 2.0),
            (280, 180.0, 2.0),
            (270, 90.0, 2.0),
        ]
        self.assertIsNone(refiner._inner_edge_of_bright_lip(into_photo))

    def test_gradient_snap_stops_on_a_quiet_sheet_after_a_bright_margin(self) -> None:
        """A bright margin that falls onto a quiet sheet does not walk to an inner border.

        A later drop well below the bed is a dark sheet, so that walk is not
        this case.
        """
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 226.0)
        quiet = [
            (223, 238.0, 9.0),
            (224, 237.0, 11.0),
            (225, 236.0, 13.0),
            (226, 234.0, 14.0),
            (227, 233.0, 14.0),
            (228, 227.0, 12.0),
        ]
        for x, mean in enumerate(
            (222.0, 219.0, 217.0, 216.0, 215.0, 215.0, 214.0, 214.0, 214.0, 214.0, 214.0, 214.0),
            start=229,
        ):
            quiet.append((x, mean, 8.0))
        quiet.extend([(273, 195.0, 26.0), (277, 205.0, 26.0)])
        self.assertEqual(refiner._pick_snap_from_samples(quiet, 223), 228)
        dark_sheet = list(quiet)
        dark_sheet.append((300, 80.0, 10.0))
        self.assertIsNone(refiner._quiet_sheet_after_bright_margin(dark_sheet))

    def test_gradient_snap_keeps_a_quiet_sheet_off_an_inner_stripe(self) -> None:
        """A pale sheet that is already paper does not snap to an inner stripe.

        The tightened edge sits below the bed. A few-grey knee in the first
        lines can still be the edge. A larger step dozens of lines in is the
        stripe, and a bed-grey start still walks to that step.
        """
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 224.0)
        sheet = [(227, 193.5, 4.1)]
        for x, mean in enumerate(
            (191.5, 191.7, 193.0, 196.2, 201.5, 203.1, 201.9),
            start=228,
        ):
            sheet.append((x, mean, 6.0))
        for x in range(235, 263):
            sheet.append((x, 200.0, 8.0))
        sheet.extend([(263, 198.8, 22.0), (264, 189.4, 36.2)])
        self.assertEqual(refiner._pick_snap_from_samples(sheet, 227), 232)

        wobble = [(909, 208.2, 10.2)]
        mean = 208.2
        for step, delta in enumerate((-0.4, -2.4, -2.6, -1.5, -0.8), start=1):
            mean += delta
            wobble.append((909 - step, mean, 8.0))
        x = wobble[-1][0]
        for _ in range(20):
            x -= 1
            wobble.append((x, mean, 8.0))
        wobble.append((x - 1, mean + 7.1, 12.0))
        self.assertEqual(refiner._pick_snap_from_samples(wobble, 909), 909)

        from_bed = [(0, 224.0, 2.0)]
        for i in range(1, 15):
            from_bed.append((i, 223.0, 2.0))
        from_bed.append((15, 180.0, 8.0))
        self.assertEqual(refiner._pick_snap_from_samples(from_bed, 0), 15)

        lasting = [(100, 190.0, 4.0), (101, 194.0, 4.0)]
        for i in range(2, 14):
            lasting.append((100 + i, 188.0, 4.0))
        lasting.append((114, 160.0, 6.0))
        self.assertEqual(refiner._pick_snap_from_samples(lasting, 100), 114)

        climb = [(1717, 218.6, 4.9)]
        mean = 218.6
        for y, delta in enumerate(
            (-1.2, -1.4, -1.6, -1.6, -1.9, -1.9, -1.8, -1.4, -0.7, 0.0, 1.1, 2.3, 3.0, 3.8),
            start=1,
        ):
            mean += delta
            climb.append((1717 - y, mean, 6.0))
        for y, landed in enumerate((224.1, 225.9, 227.6, 227.3, 226.5, 227.1, 225.6), start=15):
            climb.append((1717 - y, landed, 8.0))
        self.assertEqual(refiner._pick_snap_from_samples(climb, 1717), 1702)

    def test_gradient_snap_takes_the_trough_of_a_dark_lip(self) -> None:
        """A quiet sheet that dips and returns to the same grey keeps the dip.

        The dip is the page edge. The first small step of the fall is still
        the fade, and the grey after the dip is the sheet we already had.
        """
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 228.0)
        means = (
            196.3, 192.1, 188.3, 185.6, 183.3, 180.5, 178.1, 176.5,
            178.0, 184.4, 191.1, 192.6, 192.7, 193.1, 192.7, 192.2,
            192.4, 192.3,
        )
        samples = [(228 + i, mean, 8.0) for i, mean in enumerate(means)]
        self.assertEqual(refiner._pick_snap_from_samples(samples, 228), 235)

    def test_gradient_snap_finishes_a_shadow_climb_to_the_sheet(self) -> None:
        """A shadow climb that pauses still ends where the sheet reaches the bed."""
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 206.0)
        samples = [
            (0, 250.0, 8.0),
            (20, 206.0, 3.0),
            (40, 206.0, 3.0),
            (52, 179.0, 12.0),
            (54, 187.0, 20.0),
            (56, 190.0, 28.0),
            (62, 206.0, 24.0),
            (64, 210.0, 21.0),
        ]
        self.assertEqual(refiner._pick_snap_from_samples(samples, 0), 62)

    def test_gradient_snap_soft_bed_runway_reaches_distant_ramp(self) -> None:
        """Long soft near-bed strip beyond max_snap_frac still finds content ramp."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 40:200] = 100.0  # dark content
        # Soft ramp into content on the bottom of a tall soft-bed band.
        for y in range(200, 220):
            t = (y - 200) / 20.0
            gray[y, 40:200] = bed * (1 - t) + 100.0 * t
        # Bottom edge sits in soft bed far below the ramp (y=280).
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.02"  # ~8px on max_dim=400
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.25"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(40, 40, 160, 240)  # bottom at 280
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        bottom = snapped.y + snapped.h
        self.assertLessEqual(bottom, 225)
        self.assertGreaterEqual(bottom, 200)

    def test_gradient_snap_skips_soft_runway_on_pale_card_brightening(self) -> None:
        """Pale soft card that brightens inward must not get the soft-bed runway."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed - 8.0, dtype=np.float32)  # soft pale
        gray[40:260, 150:300] = 245.0  # brighter paper further in
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.03"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.25"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(60, 40, 200, 220)  # left in soft pale
            snapped = refiner.gradient_snap(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        # Without runway, left stays near start (must not jump to bright at 150).
        self.assertLess(snapped.x, 90)

    def test_gradient_snap_climbs_hang_hole_void_onto_pale_sheet(self) -> None:
        """A long dark-below-bed halo above pale paper must snap to the sheet.

        Hang-tag cards: the coarse box keeps a scanner shadow / hang-hole void
        ~15–25 grey below the bed for many rows, then the pale card. Short snap
        budget and pale-plateau abort used to freeze on the void.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 238.0
        h, w = 800, 500
        gray = np.full((h, w), bed, dtype=np.float32)
        # Void length stays inside MAX_VOID_SNAP_FRAC (0.08 of h ≈ 64 px).
        gray[40:100, 80:420] = bed - 20.0  # hang-hole void
        gray[100:700, 80:420] = bed + 8.0  # pale card
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.014"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.045"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 340, 660)
            snapped = refiner.gradient_snap(loose, w, h)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertGreaterEqual(snapped.y, 90)
        self.assertLessEqual(snapped.y, 115)

    def test_gradient_snap_void_runway_skips_brief_pale_before_dark(self) -> None:
        """A short above-bed lip before dark content is not a pale sheet.

        Bird-style silhouettes: the edge sits a little below the bed, rises
        briefly, then falls into dark content. Void runway must not walk in.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 233.0
        h, w = 400, 400
        gray = np.full((h, w), bed, dtype=np.float32)
        gray[40:300, 40:50] = bed - 6.0  # slight below-bed start
        gray[40:300, 50:80] = bed + 5.0  # brief pale lip
        gray[40:300, 80:300] = 100.0  # dark content
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.014"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.045"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(40, 40, 260, 260)
            snapped = refiner.gradient_snap(loose, w, h)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertLess(snapped.x, 55)

    def test_gradient_snap_void_runway_skips_bright_majority_dark_mean(self) -> None:
        """A bright sheet with a dark minority must not look like a hang-hole void.

        Pho/shrimp-style left margins: line mean sits below the bed because a
        dark strip pulls it down, but most pixels stay bright. Void runway must
        not walk past that border into the page body.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 224.0
        h, w = 400, 500
        gray = np.full((h, w), bed, dtype=np.float32)
        # Mostly bright (bed+10) with a dark strip that pulls the column mean down.
        gray[40:360, 40:120] = bed + 10.0
        gray[40:360, 40:55] = bed - 40.0
        gray[40:360, 120:400] = bed + 16.0
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.014"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.045"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(40, 40, 360, 320)
            snapped = refiner.gradient_snap(loose, w, h)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertLess(snapped.x, 70)

    def test_gradient_snap_stops_on_pale_margin_before_dark_stripe(self) -> None:
        """Pale near-bed header above a dark content band must not snap inward.

        Vote-style pages: cream top margin sits just under bed+bright_keep, then
        a dark header/photo stripe. Soft-bed runway + trough ascent would
        otherwise jump the top to the far side of that stripe (~100px under-crop).
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 231.0
        gray = np.full((800, 600), bed, dtype=np.float32)
        # Pale card face just under bed+bright_keep (≈243): short enough that
        # soft runway (max_soft_snap_frac) can reach the dark stripe.
        gray[80:200, 50:550] = bed + 10.0
        gray[200:230, 50:550] = 100.0  # dark content stripe
        gray[230:700, 50:550] = bed + 8.0
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.02"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.25"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            # Top already on pale margin (as after tighten on vote p3).
            loose = CropRect(50, 120, 500, 550)
            snapped = refiner.gradient_snap(loose, 600, 800)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        # Must not jump down to the dark stripe (~200) or its far edge (~230).
        self.assertLess(snapped.y, 140)

    def test_gradient_snap_walks_thin_valley_then_paper(self) -> None:
        """Bright fringe above a thin dark registration line must reach paper.

        Pal-order-style tops: edge sits at bed+bright_keep on a short bright
        fringe, then a few dark rows, then real pale paper. Without a valley
        peek the bright-keep rising abort freezes on the fringe.
        """
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 217.0
        gray = np.full((500, 400), bed, dtype=np.float32)
        gray[40:48, 40:360] = bed + 14.0  # bright fringe (above bright_keep)
        gray[48:56, 40:360] = 140.0  # thin dark valley
        gray[56:400, 40:360] = 248.0  # real paper
        os.environ["OPENCV_CROP_MAX_SNAP_FRAC"] = "0.02"
        os.environ["OPENCV_CROP_MAX_SOFT_SNAP_FRAC"] = "0.25"
        os.environ["OPENCV_CROP_TIGHTEN"] = "false"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(40, 42, 320, 300)  # top on bright fringe
            snapped = refiner.gradient_snap(loose, 400, 500)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_MAX_SOFT_SNAP_FRAC", None)
            os.environ.pop("OPENCV_CROP_TIGHTEN", None)
        self.assertGreaterEqual(snapped.y, 52)
        self.assertLessEqual(snapped.y, 60)

    def test_soft_climb_tighten_recovers_long_soft_fringe(self) -> None:
        """Soft near-bed→pale climb past normal tighten budget is shaved inward."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 223.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        # Soft ramp on the right into pale paper at x=260.
        for x in range(260, 300):
            t = (x - 260) / 40.0
            gray[40:260, x] = 246.0 * (1 - t) + bed * t
        gray[40:260, 80:260] = 246.0
        os.environ["OPENCV_CROP_MAX_TIGHTEN_FRAC"] = "0.004"  # ~1–2 px on max_dim=400
        os.environ["OPENCV_CROP_SOFT_CLIMB_TIGHTEN_FRAC"] = "0.08"
        os.environ["OPENCV_CROP_SOFT_CLIMB_OVERSHOOT"] = "10"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 220, 220)  # right at 300 into soft bed
            tight = refiner.tighten_box(loose, 400, 300)
            deep = refiner.tighten_soft_pale_climb(tight, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_TIGHTEN_FRAC", None)
            os.environ.pop("OPENCV_CROP_SOFT_CLIMB_TIGHTEN_FRAC", None)
            os.environ.pop("OPENCV_CROP_SOFT_CLIMB_OVERSHOOT", None)
        self.assertLess(deep.x + deep.w, tight.x + tight.w)
        self.assertLessEqual(deep.x + deep.w, 275)

    def test_soft_climb_tighten_skips_dark_content_edge(self) -> None:
        """Hang-tag / dark-content edges must not get the soft-climb deepen."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 100:280] = 80.0  # dark content
        os.environ["OPENCV_CROP_SOFT_CLIMB_TIGHTEN_FRAC"] = "0.08"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(90, 40, 200, 220)
            deep = refiner.tighten_soft_pale_climb(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_SOFT_CLIMB_TIGHTEN_FRAC", None)
        self.assertEqual(deep.x, loose.x)
        self.assertEqual(deep.w, loose.w)

    def test_refine_peels_scanner_shadow_not_dark_photo(self) -> None:
        """Dark drop-shadow outside bright paper is peeled; dark photo is not."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 220.0
        # Bright paper with a dark scanner-shadow band on the right.
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:260, 80:300] = 245.0
        gray[40:260, 300:315] = 80.0  # shadow valley outside paper
        os.environ["OPENCV_CROP_MAX_SHADOW_PEEL_FRAC"] = "0.08"
        try:
            refiner = _EdgeRefiner(gray, bed)
            loose = CropRect(80, 40, 235, 220)  # right at 315 into shadow
            peeled = refiner.peel_shadow(loose, 400, 300)
        finally:
            os.environ.pop("OPENCV_CROP_MAX_SHADOW_PEEL_FRAC", None)
        self.assertLessEqual(peeled.x + peeled.w, 302)

        # Dark photographic card: no near-bed/bright recovery inward → no peel.
        photo = np.full((300, 400), bed, dtype=np.float32)
        photo[40:260, 80:320] = 90.0
        refiner = _EdgeRefiner(photo, bed)
        dark = CropRect(80, 40, 240, 220)
        self.assertEqual(refiner.peel_shadow(dark, 400, 300), dark)

    def test_robust_bed_uses_ring_when_corners_near_fill(self) -> None:
        """Lower-median corner ~247 (mixed deskew fill) must still use ring P10."""
        from yokaze.lib.crop.opencv_crop import _robust_bed_gray

        h, w = 400, 500
        img = np.full((h, w, 3), 210, dtype=np.uint8)
        # Three corners pure fill, one mixed → lower-median in mid-240s.
        img[0:80, 0:80] = 255
        img[0:80, w - 80 : w] = 255
        img[h - 80 : h, 0:80] = 255
        img[h - 80 : h, w - 80 : w] = 247
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            bed = _robust_bed_gray(path)
        self.assertLess(bed, 230.0, f"still using fill-poisoned corner bed: {bed}")

        """White deskew fill inflates bed grey so a slightly darker real bed
        looks like paper at fuzz 12 and vanishes at 14 — take the collapsed
        box (the small pale sheet), not the first usable near-frame blob.
        """
        h, w = 600, 500
        fill, near_bed, dark_bed, paper, ink = 255, 220, 197, 215, 40
        img = np.full((h, w, 3), fill, dtype=np.uint8)
        img[20 : h - 20, 20 : w - 20] = near_bed
        img[40 : h - 40, 40 : w - 40] = dark_bed
        img[100:420, 80:280] = paper
        img[140:380, 110:250] = ink
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        area = rect.w * rect.h / (w * h)
        self.assertLess(area, 0.45, f"still grabbed the bed blob: {rect} area={area:.3f}")
        self.assertGreater(rect.x, 50)
        self.assertLess(rect.x + rect.w, 320)
        self.assertGreater(rect.y, 70)
        self.assertLess(rect.y + rect.h, 450)

    def test_coarse_skips_mild_fuzz_collapse_from_speck(self) -> None:
        """A single darker speck above the sheet makes fuzz 12 ~13% taller
        than fuzz 14. Collapse 0.10 must still skip the speck box; 0.25
        would have kept it (the hole after the 50%+ bird case).
        """
        h, w = 600, 500
        bed, ink, speck = 232, 40, 201  # |speck-bed|=31: between fuzz 12 and 14
        img = np.full((h, w, 3), bed, dtype=np.uint8)
        img[80:520, 50:450] = ink
        img[20, 250] = speck
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        self.assertGreater(rect.y, 50, f"kept speck-extended box: {rect}")
        self.assertLess(rect.y, 100)
        self.assertGreater(rect.x, 40)
        self.assertLess(rect.x + rect.w, 460)

    def test_coarse_lookahead_skips_multi_step_bed_blob(self) -> None:
        """Speckled ADF bed extends the low-fuzz AABB far past a narrow pale
        receipt. Adjacent 12→14 barely shrinks; the real sheet only appears
        around fuzz 38 (~60% area drop). Look-ahead must walk to that collapse
        without a mild 10% late shrink re-cutting other pages.
        """
        h, w = 500, 800
        bed, paper, ink, speck = 235, 228, 40, 200
        img = np.full((h, w, 3), bed, dtype=np.uint8)
        # Narrow receipt (left): mostly near-bed paper + dark ink.
        img[80:420, 120:320] = paper
        img[100:400, 140:300] = ink
        # Sparse darker bed speckles to the right — unioned into fuzz-12 AABB.
        for x in range(400, 760, 12):
            for y in range(90, 410, 18):
                img[y, x] = speck
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        self.assertLess(rect.w, 280, f"still unioned bed speckles: {rect}")
        self.assertGreater(rect.x, 90)
        self.assertLess(rect.x + rect.w, 360)

    def test_coarse_drops_flat_bed_band_the_area_ratio_misses(self) -> None:
        """A flat bed band above the page moves one edge by more than the snap
        walk can cross, but less than 10% of the box area. The later fuzz that
        lands on the page must win. A brighter band is the sheet and stays.
        """
        # Wide page: soft-snap budget is 4.5% of the long side, under 10% of height.
        h, w = 1100, 1600
        bed, speck, paper = 220, 170, 40
        img = np.full((h, w, 3), bed, dtype=np.uint8)
        img[120:1040, 200:1400] = paper
        img[30, 600:608] = speck
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        self.assertGreater(rect.y, 90, f"kept the bed band: {rect}")
        self.assertLess(rect.y, 160, f"cut into the page: {rect}")

        bright = np.full((h, w, 3), bed, dtype=np.uint8)
        bright[110:1040, 200:1400] = paper
        bright[20:110, 200:1400] = 255
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), bright)
            kept, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        self.assertLess(kept.y, 100, f"dropped a bright sheet band: {kept}")

    def test_coarse_keeps_a_colored_sheet_high_fuzz_would_drop(self) -> None:
        """A colored sheet stays foreground until a high fuzz, which then keeps
        only a few darker specks in one column. That column is the same sheet,
        not a bed blob falling away, so the wide box stays.
        """
        h, w = 800, 600
        bed, sheet, speck = 220, 170, 80
        img = np.full((h, w, 3), bed, dtype=np.uint8)
        img[80:720, 60:540] = sheet
        img[100:700:40, 70:130:20] = speck
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect, _, _ = OpenCvCrop(margin_px=2).detect_crop(path)
        self.assertGreater(rect.w, 400, f"kept only the speck column: {rect}")
        self.assertLess(rect.x, 90)
        self.assertGreater(rect.x + rect.w, 500)

    def test_coarse_keeps_a_sheet_that_fills_most_of_the_frame(self) -> None:
        """A sheet near 95% of the frame stays, including a paler edge.

        A higher fuzz drops that edge, but the drop is under 10% of the area,
        so the wide box is the one to keep. A box that spans the frame, or
        that is larger than the area cap, is still rejected.
        """
        h, w = 600, 800
        bed, sheet, pale = 210, 160, 177
        img = np.full((h, w, 3), bed, dtype=np.uint8)
        img[8:592, 8:792] = sheet
        img[8:592, 752:792] = pale
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect = OpenCvCrop(margin_px=0)._coarse_bbox(path, w, h, float(bed))
        self.assertLess(rect.x, 20, f"cut the left edge: {rect}")
        self.assertGreater(rect.x + rect.w, 780, f"dropped the pale edge: {rect}")
        self.assertFalse(
            _bbox_usable(793, 500, 800, 600, max_area_frac=0.97),
            "a box within 8 px of the frame width should stay rejected",
        )
        self.assertFalse(
            _bbox_usable(790, 590, 800, 600, max_area_frac=0.97),
            "a box over the area cap should stay rejected",
        )

    def test_coarse_drops_flat_bed_off_the_corner_estimate(self) -> None:
        """A flat band about 12 grey under a fill-biased corner bed is still bed.

        The outer line is a ridge, so it does not sit on the corner grey, and
        the band misses the near-bed fraction. The sheet beyond it is far from
        that estimate, so the later fuzz wins. The same band around a sheet
        that is equally far from the estimate stays.
        """
        h, w = 1000, 1400
        corner_bed = 232.0
        real_bed, ridge, paper = 220, 190, 40
        img = np.full((h, w, 3), real_bed, dtype=np.uint8)
        img[40, 200:1100] = ridge
        img[200:900, 200:1100] = paper
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), img)
            rect = OpenCvCrop(margin_px=0)._coarse_bbox(path, w, h, corner_bed)
        self.assertGreater(rect.y, 150, f"kept the bed band: {rect}")
        self.assertLess(rect.y, 230, f"cut into the page: {rect}")

        same = np.full((h, w, 3), real_bed, dtype=np.uint8)
        same[40, 200:1100] = ridge
        same[200:900:40, 200:1100:40] = paper
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.tiff"
            cv2.imwrite(str(path), same)
            kept = OpenCvCrop(margin_px=0)._coarse_bbox(path, w, h, corner_bed)
        self.assertLess(kept.y, 80, f"dropped a band as far from the bed as the sheet: {kept}")

    def test_bright_abort_walks_a_near_bed_plateau_toward_a_later_sheet(self) -> None:
        """A flat band a few grey above the bed keeps walking when a brighter sheet holds later.

        Nothing brighter ahead is the sheet, and so is a rise on the next lines.
        Those still stop.
        """
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        refiner = _EdgeRefiner(np.zeros((4, 4), dtype=np.float32), 200.0)

        def later(idx: int) -> float:
            return 218.0 if idx >= 50 else 206.0

        def flat(idx: int) -> float:
            return 206.0

        def soon(idx: int) -> float:
            return 218.0 if idx >= 12 else 206.0

        self.assertFalse(
            refiner._bright_paper_abort(
                206.0, 206.0, get_mean=later, pos=10, direction=1, stop=200
            )
        )
        self.assertTrue(
            refiner._bright_paper_abort(
                206.0, 206.0, get_mean=flat, pos=10, direction=1, stop=200
            )
        )
        self.assertTrue(
            refiner._bright_paper_abort(
                206.0, 206.0, get_mean=soon, pos=10, direction=1, stop=200
            )
        )

    def test_peel_leaves_a_busy_printed_border(self) -> None:
        """A dark border that changes along the line is print, not a drop-shadow."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        bed = 210.0
        gray = np.full((300, 400), bed, dtype=np.float32)
        gray[40:200, 40:360] = 245.0
        border = np.full(320, 190.0, dtype=np.float32)
        border[:200] = 50.0
        gray[200:245, 40:360] = border
        refiner = _EdgeRefiner(gray, bed)
        loose = CropRect(40, 40, 320, 205)
        peeled = refiner.peel_shadow(loose, 400, 300)
        self.assertEqual(peeled.y + peeled.h, loose.y + loose.h)

    def test_recovers_a_pale_header_the_corner_bed_swallowed(self) -> None:
        """Blank paper within a few grey of a bright corner estimate is still the sheet."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        surround, paper, ink = 220.0, 248.0, 40.0
        gray = np.full((600, 400), surround, dtype=np.float32)
        gray[80:520, 80:320] = paper
        gray[220:480, 110:290:5] = ink
        refiner = _EdgeRefiner(gray, 240.0)
        # Body of the receipt, missing the blank header above y=200.
        box = CropRect(80, 200, 240, 320)
        recovered = refiner.recover_swallowed_sheet(box, 400, 600)
        self.assertLessEqual(recovered.y, 90)
        self.assertGreaterEqual(recovered.y, 70)
        self.assertEqual(recovered.x, box.x)

    def test_dark_sheet_does_not_grow_into_a_brighter_bed(self) -> None:
        """A bright fringe beside a dark page is the bed, not a swallowed sheet."""
        from yokaze.lib.util.types import CropRect
        from yokaze.lib.crop.opencv_crop import _EdgeRefiner

        gray = np.full((400, 500), 210.0, dtype=np.float32)
        gray[40:260, 80:360] = 180.0
        gray[40:260, 40:80] = 242.0
        refiner = _EdgeRefiner(gray, 235.0)
        box = CropRect(80, 40, 280, 220)
        self.assertEqual(refiner.recover_swallowed_sheet(box, 500, 400), box)


if __name__ == "__main__":
    unittest.main()
